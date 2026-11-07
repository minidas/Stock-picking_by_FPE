"""End-to-end stock export/retention contracts, with isolated workspace archives."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from stocking.cli import adjust, file_hash, screen
from stocking.data import DataError, load_json
from stocking.model import ModelError
from stocking.retention import RUN_MANIFEST, archive_run, finalize_run


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def workspace_temp():
    directory = tempfile.TemporaryDirectory(dir=ROOT)
    path = Path(directory.name).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError("Test cleanup target outside workspace")
    try:
        yield path
    finally:
        directory.cleanup()


class WorkflowTests(unittest.TestCase):
    def fixture(self, root, snapshots=None, mode="research", limit=.25, missing=False):
        source = root / "snapshots.json"
        if snapshots is None:
            snapshots = load_json(ROOT / "examples/snapshots.json")
            if isinstance(snapshots, dict):
                snapshots = snapshots["snapshots"]
        source.write_text(json.dumps(snapshots), encoding="utf-8")
        universe = root / "memberships.json"
        names = [s["symbol"] for s in snapshots] + (["MISSING"] if missing else [])
        universe.write_text(json.dumps([{"symbol": name, "effective_from": "2000-01-01",
            "effective_to": None, "available_at": "2000-01-01", "source": "synthetic fixture"}
            for name in names]), encoding="utf-8")
        raw = root / "provider.raw.json"
        raw.write_bytes(b'{"untouched_provider_payload":true}')
        # These tests describe the pre-risk-first workflow's historical export
        # semantics. New defaults and early exclusion have dedicated tests.
        policy = root / "legacy-risk-policy.json"
        policy.write_text(json.dumps({"version": 1, "max_loss_probability": limit, "recent_positive_years": 0}), encoding="utf-8")
        return SimpleNamespace(input=str(source), universe=str(universe), config=None,
            output=str(root / "result"), archive_root=str(root / "archive"), mode=mode,
            raw_input=[str(raw)], retention_days=7, exclude_loss_probability=limit, risk_policy=str(policy))

    def execute_screen(self, args):
        with redirect_stdout(io.StringIO()):
            screen(args)
        out = Path(args.output)
        return load_json(out / "all_stocks.json"), load_json(out / "excluded_stocks.json"), load_json(out / "baseline.json")

    def adjust_args(self, root, screen_args, event_map, limit=None):
        baseline = Path(screen_args.output) / "baseline.json"
        events = root / "events.json"
        events.write_text(json.dumps({"baseline_sha256": file_hash(baseline), "events_by_symbol": event_map}), encoding="utf-8")
        return SimpleNamespace(baseline=str(baseline), events=str(events),
            output=str(root / "adjusted"), mode="research", archive_root=screen_args.archive_root,
            raw_input=None, retention_days=7, exclude_loss_probability=limit)

    def raw_path(self, run):
        manifest = load_json(run / RUN_MANIFEST)
        return run / next(f["path"] for f in manifest["files"] if f["category"] == "raw")

    def test_research_keeps_raw_and_exports_observation_stocks(self):
        with workspace_temp() as root:
            args = self.fixture(root)
            main, excluded, baseline = self.execute_screen(args)
            included = {r["symbol"]: r for r in main["stocks"]}
            self.assertIn("DEMO_HISTORY_ONLY", included)
            self.assertEqual(included["DEMO_HISTORY_ONLY"]["prediction"]["candidate"], "none")
            self.assertEqual(included["DEMO_HISTORY_ONLY"]["input"]["history"],
                             next(s for s in load_json(args.input) if s["symbol"] == "DEMO_HISTORY_ONLY")["history"])
            self.assertEqual([r["symbol"] for r in excluded["excluded_stocks"]], ["DEMO_LOSS"])
            run = Path(baseline["run_archive"])
            manifest = load_json(run / RUN_MANIFEST)
            self.assertEqual((manifest["mode"], manifest["status"]), ("research", "complete"))
            self.assertEqual(manifest["explicit_raw_file_count"], 1)
            provider = next(r for r in manifest["files"] if r["label"].startswith("provider_"))
            self.assertEqual((run / provider["path"]).read_bytes(), Path(args.raw_input[0]).read_bytes())
            self.assertEqual(provider["sha256"], hashlib.sha256(Path(args.raw_input[0]).read_bytes()).hexdigest())
            self.assertTrue(manifest["metadata"]["model_configurations"])
            for row in baseline["results"]:
                self.assertEqual(row["model"]["config"]["buy_fpe_threshold"], 6.5)
                self.assertEqual(row["prediction"]["buy_fpe_threshold"], 6.5)
            self.assertIn("model.py", manifest["metadata"]["code_sha256"])
            self.assertTrue(any(r["category"] == "working" for r in manifest["files"]))
            self.assertTrue(any(r["category"] == "audit" for r in manifest["files"]))

    def test_excluded_high_risk_is_not_in_ai_queue(self):
        with workspace_temp() as root:
            snapshots = load_json(ROOT / "examples/snapshots.json")["snapshots"]
            high = deepcopy(next(s for s in snapshots if s["symbol"] == "DEMO_EXPENSIVE"))
            high["symbol"] = "HIGH_RISK"
            high["risk_status"] = "high"
            args = self.fixture(root, snapshots + [high])
            main, excluded, _ = self.execute_screen(args)
            self.assertNotIn("HIGH_RISK", {r["symbol"] for r in main["stocks"]})
            risk = next(r for r in excluded["excluded_stocks"] if r["symbol"] == "HIGH_RISK")
            self.assertEqual(risk["prediction"]["candidate"], "sell_review")
            self.assertIn("explicit_high_risk", risk["exclusion_reasons"])
            queue = load_json(Path(args.output) / "agent_queue.json")
            self.assertNotIn("HIGH_RISK", {r["symbol"] for r in queue["candidates"]})

    def test_missing_member_is_visible_and_not_fabricated(self):
        with workspace_temp() as root:
            args = self.fixture(root, missing=True)
            main, _, _ = self.execute_screen(args)
            missing = next(r for r in main["stocks"] if r["symbol"] == "MISSING")
            self.assertEqual(missing["export_status"], "data_unavailable")
            self.assertEqual(missing["risk_assessment"], "not_assessed")
            self.assertIsNone(missing["prediction"]["p_below_6"])
            self.assertEqual(main["summary"]["missing_input_stock_count"], 1)

    def test_adjust_inherits_risk_policy_and_reexports_all_stocks(self):
        with workspace_temp() as root:
            args = self.fixture(root, limit=.1)
            self.execute_screen(args)
            event = deepcopy(load_json(ROOT / "examples/events.json")["events_by_symbol"]["DEMO_VALUE"][0])
            event.update(annual_log_growth_delta=[0] * 6, variance_multiplier=1,
                         confidence=1, loss_probability_delta=.15)
            adjusted_args = self.adjust_args(root, args, {"DEMO_VALUE": [event]})
            with redirect_stdout(io.StringIO()):
                adjust(adjusted_args)
            main = load_json(Path(adjusted_args.output) / "all_stocks.json")
            excluded = load_json(Path(adjusted_args.output) / "excluded_stocks.json")
            self.assertEqual(main["policy"]["max_loss_probability"], .1)
            self.assertIn("DEMO_HISTORY_ONLY", {r["symbol"] for r in main["stocks"]})
            changed = next(r for r in excluded["excluded_stocks"] if r["symbol"] == "DEMO_VALUE")
            self.assertIn("loss_probability_at_or_above_limit", changed["exclusion_reasons"])
            self.assertIn("baseline_prediction", changed)
            self.assertEqual(changed["event_review"], "reviewed")

    def test_relaxing_export_policy_cannot_expand_original_ai_queue(self):
        with workspace_temp() as root:
            # All modeled examples have p_loss > .03, so the initial queue is empty.
            args = self.fixture(root, limit=.03)
            self.execute_screen(args)
            attempt = self.adjust_args(root, args, {"DEMO_VALUE": []}, limit=.9)
            with self.assertRaisesRegex(DataError, "preliminary candidates"):
                adjust(attempt)

    def test_index_input_rejected_before_archiving(self):
        with workspace_temp() as root:
            snapshots = load_json(ROOT / "examples/snapshots.json")["snapshots"]
            snapshots[0]["asset_type"] = "index"
            args = self.fixture(root, snapshots)
            with self.assertRaisesRegex(DataError, "individual stocks only"):
                screen(args)
            self.assertFalse(Path(args.archive_root).exists())

    def test_invalid_global_config_rejected_before_archiving(self):
        with workspace_temp() as root:
            args = self.fixture(root)
            config = root / "config.json"
            config.write_text('{"unknown_parameter":100}', encoding="utf-8")
            args.config = str(config)
            with self.assertRaises((DataError, ModelError)):
                screen(args)
            self.assertFalse(Path(args.archive_root).exists())

    def test_live_run_cleans_only_owned_old_live_copies(self):
        with workspace_temp() as root:
            args = self.fixture(root, mode="live")
            old_time = datetime.now(timezone.utc) - timedelta(days=30)
            old = archive_run(args.archive_root, {"input": args.input}, mode="live", now=old_time)
            finalize_run(old, now=old_time)
            research = archive_run(args.archive_root, {"input": args.input}, mode="research", now=old_time)
            finalize_run(research, now=old_time)
            old_raw = self.raw_path(old)
            unknown = old / "raw" / "unregistered.json"
            unknown.write_text("must remain", encoding="utf-8")
            _, _, baseline = self.execute_screen(args)
            self.assertFalse(old_raw.exists())
            self.assertTrue(self.raw_path(research).exists())
            self.assertTrue(self.raw_path(Path(baseline["run_archive"])).exists())
            self.assertTrue(unknown.exists())
            self.assertTrue(Path(args.input).exists())
            lifecycle = load_json(Path(args.output) / "run_lifecycle.json")
            self.assertEqual(len(lifecycle["cleanup"]["deleted_files"]), 1)

    def test_all_model_failures_leave_pending_run_and_preserve_previous_good_raw(self):
        with workspace_temp() as root:
            snapshots = load_json(ROOT / "examples/snapshots.json")["snapshots"]
            snapshots = [s for s in snapshots if s["eps0"] > 0]
            for snap in snapshots:
                snap["price_date"] = "2026-10-01"  # Valid input chronology, invalid model price age.
            args = self.fixture(root, snapshots, mode="live")
            old_time = datetime.now(timezone.utc) - timedelta(days=30)
            old = archive_run(args.archive_root, {"input": args.input}, mode="live", now=old_time)
            finalize_run(old, now=old_time)
            with self.assertRaises(DataError), redirect_stdout(io.StringIO()):
                screen(args)
            self.assertTrue(self.raw_path(old).exists())
            pending = [load_json(p) for p in Path(args.archive_root).glob(f"*/{RUN_MANIFEST}") if p.parent != old]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
