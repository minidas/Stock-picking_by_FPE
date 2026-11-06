from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stocking.data import DataError, load_json
from stocking.retention import (ROOT_MARKER, RUN_MANIFEST, archive_run,
                                cleanup_runs, finalize_run)


@contextmanager
def workspace_temp():
    root = Path(__file__).resolve().parents[1]
    directory = tempfile.TemporaryDirectory(dir=root)
    if not Path(directory.name).resolve().is_relative_to(root):
        raise RuntimeError("Temporary cleanup target outside workspace")
    try:
        yield Path(directory.name)
    finally:
        directory.cleanup()


class RetentionTests(unittest.TestCase):
    now = datetime(2026, 11, 6, 6, tzinfo=timezone.utc)

    def fixture(self, workspace):
        source = workspace / "source.json"
        source.write_text('{"input":"preserve original"}', encoding="utf-8")
        config = workspace / "config.json"
        config.write_text('{"buy_fpe_threshold":6}', encoding="utf-8")
        summary = workspace / "summary.csv"
        summary.write_text("symbol,p_buy\nA,.5\n", encoding="utf-8")
        return workspace / "archive", source, config, summary

    def make_run(self, archive, source, *, days=20, mode="live", complete=True, extra=None):
        created = self.now - timedelta(days=days)
        run = archive_run(archive, {"snapshots": source, **(extra or {})}, mode=mode, now=created)
        if complete:
            finalize_run(run, now=created + timedelta(minutes=1))
        return run

    def raw_file(self, run):
        manifest = load_json(run / RUN_MANIFEST)
        return run / next(row["path"] for row in manifest["files"] if row["category"] == "raw")

    def test_research_and_pending_runs_never_expire(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            research = self.make_run(archive, source, mode="research", days=100)
            pending = self.make_run(archive, source, complete=False, days=100)
            report = cleanup_runs(archive, retention_days=0, dry_run=False, now=self.now)
            self.assertEqual(report["deleted_files"], [])
            self.assertTrue(self.raw_file(research).exists())
            self.assertTrue(self.raw_file(pending).exists())

    def test_live_cleanup_preserves_original_config_audit_and_latest(self):
        with workspace_temp() as work:
            archive, source, config, summary = self.fixture(work)
            old = archive_run(archive, {"snapshots": source, "config": config}, mode="live", now=self.now-timedelta(days=20))
            finalize_run(old, {"summary": summary}, metadata={"source_sha256": "audit"}, now=self.now-timedelta(days=20))
            latest = self.make_run(archive, source, days=10)
            unknown = old / "raw" / "unregistered.json"
            unknown.write_text("keep me", encoding="utf-8")
            copied = self.raw_file(old)
            report = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertEqual(len(report["deleted_files"]), 1)
            self.assertFalse(copied.exists())
            self.assertTrue(source.exists())
            self.assertTrue(self.raw_file(latest).exists())
            self.assertTrue(unknown.exists())
            manifest = load_json(old / RUN_MANIFEST)
            self.assertTrue(manifest["cleanup_history"])
            self.assertEqual(manifest["metadata"], {"source_sha256": "audit"})
            for record in manifest["files"]:
                if record["category"] != "raw":
                    self.assertTrue((old / record["path"]).exists())
            again = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertEqual(again["deleted_files"], [])

    def test_dry_run_does_not_mutate_files_or_manifest(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            old = self.make_run(archive, source)
            self.make_run(archive, source, days=1)
            before = (old / RUN_MANIFEST).read_bytes()
            report = cleanup_runs(archive, now=self.now)
            self.assertEqual(len(report["eligible_files"]), 1)
            self.assertEqual(report["deleted_files"], [])
            self.assertEqual((old / RUN_MANIFEST).read_bytes(), before)
            self.assertTrue(self.raw_file(old).exists())

    def test_changed_raw_copy_is_preserved(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            old = self.make_run(archive, source)
            self.make_run(archive, source, days=1)
            raw = self.raw_file(old)
            raw.write_text("manual edits", encoding="utf-8")
            report = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertTrue(raw.exists())
            self.assertEqual(report["deleted_files"], [])
            self.assertTrue(any("edited" in s["reason"] for s in report["skipped"]))

    def test_tampered_path_cannot_delete_outside_owned_raw_directory(self):
        for malicious in ("../source.json", "/outside.json", "raw/../../source.json", "raw\\escape.json", "C:/outside.json"):
            with self.subTest(path=malicious), workspace_temp() as work:
                archive, source, _, _ = self.fixture(work)
                old = self.make_run(archive, source)
                self.make_run(archive, source, days=1)
                manifest = load_json(old / RUN_MANIFEST)
                manifest["files"][0]["path"] = malicious
                (old / RUN_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
                report = cleanup_runs(archive, dry_run=False, now=self.now)
                self.assertEqual(report["deleted_files"], [])
                self.assertTrue(source.exists())

    def test_reparse_parent_blocks_archive_and_cleanup(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            old = self.make_run(archive, source)
            self.make_run(archive, source, days=1)
            from stocking import retention
            original = retention._linked
            def linked(path):
                return True if path == old / "raw" else original(path)
            with patch("stocking.retention._linked", side_effect=linked):
                report = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertEqual(report["deleted_files"], [])
            self.assertTrue(self.raw_file(old).exists())
            with patch("stocking.retention._linked", return_value=True), self.assertRaises(DataError):
                archive_run(archive, {"snapshots": source}, now=self.now)

    def test_unknown_directory_without_registry_is_not_cleaned(self):
        with workspace_temp() as work:
            source = work / "something.json"
            source.write_text("keep", encoding="utf-8")
            with self.assertRaises((DataError, OSError)):
                cleanup_runs(work, dry_run=False, now=self.now)
            self.assertTrue(source.exists())

    def test_registry_traversal_rejected(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            self.make_run(archive, source)
            registry = load_json(archive / ROOT_MARKER)
            registry["runs"].append("../outside")
            (archive / ROOT_MARKER).write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaises(DataError):
                cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertTrue(source.exists())

    def test_retention_uses_completion_time_not_mtime(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            created = self.now-timedelta(days=20)
            old = archive_run(archive, {"snapshots": source}, mode="live", now=created)
            finalize_run(old, now=self.now-timedelta(days=2))
            self.make_run(archive, source, days=1)
            report = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertEqual(report["deleted_files"], [])

    def test_finalize_is_immutable_and_explicit_provider_files_are_copied(self):
        with workspace_temp() as work:
            archive, source, config, summary = self.fixture(work)
            run = archive_run(archive, {"snapshots": source}, raw_paths=[config], now=self.now)
            before = load_json(run / RUN_MANIFEST)
            self.assertEqual(len(before["files"]), 2)
            self.assertEqual(before["mode"], "research")
            self.assertEqual(before["explicit_raw_file_count"], 1)
            self.assertEqual(before["raw_source_scope"], "explicit_provider_files")
            finalize_run(run, [summary], now=self.now)
            with self.assertRaises(DataError):
                finalize_run(run, [summary], now=self.now)
            with self.assertRaises(DataError):
                archive_run(archive, {"input": work}, now=self.now)

    def test_working_documents_expire_but_compact_audit_survives(self):
        with workspace_temp() as work:
            archive, source, _, summary = self.fixture(work)
            old = archive_run(archive, {"snapshots": source}, mode="live", now=self.now-timedelta(days=20))
            manifest = finalize_run(old, {"summary": summary}, working_files={"baseline": source},
                                    now=self.now-timedelta(days=20))
            self.assertEqual(manifest["raw_source_scope"], "normalized_input_files_only")
            working = old / next(r["path"] for r in manifest["files"] if r["category"] == "working")
            audit = old / next(r["path"] for r in manifest["files"] if r["category"] == "audit")
            self.make_run(archive, source, days=1)
            report = cleanup_runs(archive, dry_run=False, now=self.now)
            self.assertEqual(len(report["deleted_files"]), 2)
            self.assertFalse(working.exists())
            self.assertTrue(audit.exists())
            self.assertTrue(source.exists())

    def test_invalid_time_days_labels_and_metadata_fail(self):
        with workspace_temp() as work:
            archive, source, _, _ = self.fixture(work)
            with self.assertRaises(DataError):
                archive_run(archive, {"../evil": source}, now=self.now)
            with self.assertRaises(DataError):
                archive_run(archive, {"input": source}, now=datetime(2026, 1, 1))
            run = archive_run(archive, {"input": source}, now=self.now)
            with self.assertRaises(DataError):
                finalize_run(run, now=self.now-timedelta(days=1))
            with self.assertRaises(DataError):
                finalize_run(run, metadata=["not an object"], now=self.now)
            for days in (-1, True, float("inf")):
                with self.assertRaises(DataError):
                    cleanup_runs(archive, retention_days=days, now=self.now)


if __name__ == "__main__":
    unittest.main()
