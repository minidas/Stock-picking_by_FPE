import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("history_download", ROOT / "scripts/download_sec_history.py")
history_download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_download)


def submission(**overrides):
    return dict({"adsh": "0000001234-12-000001", "cik": "1234", "name": "Example",
                 "form": "10-K", "period": "20111231", "filed": "20120215",
                 "accepted": "2012-02-15 12:00:00"}, **overrides)


def number(**overrides):
    return dict({"adsh": "0000001234-12-000001", "tag": "EarningsPerShareDiluted",
                 "version": "us-gaap/2011", "ddate": "20111231", "qtrs": "4",
                 "uom": "USD/shares", "coreg": "", "segments": "", "value": "2.0"}, **overrides)


def table(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def make_zip(path, submissions=None, numbers=None, extras=None):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sub.txt", table(submissions or [submission()]))
        archive.writestr("num.txt", table(numbers or [number()]))
        for name, value in (extras or {}).items():
            archive.writestr(name, value)
    return path


class Response(io.BytesIO):
    def __init__(self, content, claimed_size=None):
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content) if claimed_size is None else claimed_size),
                        "Content-Type": "application/zip"}
        self.status = 200


class HistoryDownloadTests(unittest.TestCase):
    def temp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_dated_filings_units_and_no_cross_vintage_join(self):
        directory = self.temp()
        later = "0000001234-12-000002"
        future = "0000001234-13-000001"
        path = make_zip(directory / "local.zip",
                        [submission(), submission(adsh=later, form="10-K/A", filed="20120415"),
                         submission(adsh=future, filed="20130215", period="20121231")],
                        [number(ddate="20101231", value="1"), number(),
                         number(adsh=later, value="2.1", uom="USD"),
                         number(adsh=future, ddate="20121231", value="99")])
        result = history_download.extract_archive(path, member_ciks={"0000001234"})
        self.assertEqual(len(result["filings"]), 2)
        original, amendment = result["filings"]
        self.assertEqual([r["eps"] for r in original["history"]], [1, 2])
        self.assertEqual(amendment["history"][0]["unit_as_reported"], "USD")
        self.assertNotEqual(original["share_basis"], amendment["share_basis"])
        self.assertEqual(original["source_archive_sha256"], history_download.sha256_file(path))
        self.assertFalse(original["share_basis_verified"])
        self.assertEqual(history_download.extract_archive(path, member_ciks={"0000009999"})["filings"], [])

    def test_rejects_dimensions_nonannual_custom_taxonomy_and_nonfinite(self):
        directory = self.temp()
        rows = [number(), number(value="3", coreg="Subsidiary"), number(value="4", segments="ClassA"),
                number(value="5", qtrs="1"), number(value="6", version="0000001234-12-000001"),
                number(value="NaN"), number(value="Infinity"), number(value="7", uom="shares"),
                number(ddate="20121231", value="8")]
        result = history_download.extract_archive(make_zip(directory / "facts.zip", numbers=rows))
        self.assertEqual([r["eps"] for r in result["filings"][0]["history"]], [2])
        self.assertEqual(result["rejected_fact_counts"]["dimensional_or_coregistered"], 2)
        self.assertEqual(result["rejected_fact_counts"]["invalid_value_or_period"], 2)

    def test_conflict_is_preserved_and_no_value_is_selected(self):
        path = make_zip(self.temp() / "conflict.zip", numbers=[number(), number(value="7")])
        candidate = history_download.extract_archive(path)["filings"][0]
        self.assertEqual(candidate["history"], [])
        self.assertEqual(len(candidate["conflicts"][0]["facts"]), 2)
        self.assertFalse(candidate["has_current_report_eps"])

    def test_basic_and_diluted_fallback_and_legacy_schema(self):
        row = number(tag="EarningsPerShareBasicAndDiluted")
        del row["segments"]
        path = make_zip(self.temp() / "legacy.zip", numbers=[row])
        actual = history_download.extract_archive(path)["filings"][0]["history"][0]
        self.assertEqual(actual["eps"], 2)
        self.assertFalse(actual["context"]["segments_field_present"])
        self.assertEqual(actual["context"]["dimension_status"], "legacy_no_segments_field_coreg_empty")

    def test_official_requires_real_contact_but_offline_does_not(self):
        with self.assertRaisesRegex(history_download.HistoryDownloadError, "SEC_USER_AGENT"):
            history_download.source_request("2011q1", user_agent=None)
        url, headers, origin = history_download.source_request("2011q1", user_agent="Existing app real-contact@example.org")
        self.assertEqual(url, "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2011q1.zip")
        self.assertTrue(origin.startswith("official"))
        self.assertIn("@", headers["User-Agent"])
        _, mirror_headers, mirror_origin = history_download.source_request("2011q1", "https://mirror.example.org/{period}.zip")
        self.assertIn("unverified", mirror_origin)
        self.assertNotIn("@", mirror_headers["User-Agent"])

    def test_download_size_limits_and_successful_file_never_overwritten(self):
        directory = self.temp()
        raw = make_zip(directory / "fixture.zip").read_bytes()
        destination = directory / "download.zip"
        def opener(request, timeout):
            return Response(raw)
        first = history_download.download_archive("https://example.org/quarter.zip", destination, {}, opener=opener)
        self.assertEqual(first["status"], "downloaded")
        with patch.object(history_download, "urlopen", side_effect=AssertionError("network forbidden")):
            second = history_download.download_archive("https://different.example/quarter.zip", destination, {})
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertEqual(second["status"], "reused_existing")
        for claimed in (len(raw), 0):
            too_large = directory / f"limited-{claimed}.zip"
            with self.assertRaisesRegex(history_download.HistoryDownloadError, "byte limit"):
                history_download.download_archive("https://example.org/quarter.zip", too_large, {}, max_bytes=10,
                                                   opener=lambda request, timeout: Response(raw, claimed_size=claimed))
            self.assertFalse(too_large.exists())
        self.assertEqual(list(directory.glob("*.part")), [])

    def test_zip_path_and_expansion_checks(self):
        directory = self.temp()
        unsafe = make_zip(directory / "unsafe.zip", extras={"../outside.txt": "never extract"})
        with self.assertRaisesRegex(history_download.HistoryDownloadError, "Unsafe"):
            history_download.inspect_zip(unsafe)
        normal = make_zip(directory / "normal.zip")
        with self.assertRaisesRegex(history_download.HistoryDownloadError, "expanded"):
            history_download.inspect_zip(normal, max_expanded=1)
        bomb = make_zip(directory / "expanded.zip", extras={"padding.txt": "0" * 1000000})
        with self.assertRaisesRegex(history_download.HistoryDownloadError, "expansion ratio"):
            history_download.inspect_zip(bomb)

    def test_no_raw_archives_preserves_existing_output(self):
        directory = self.temp()
        saved = directory / "candidate_filings.json"
        saved.write_text('{"previous":"keep"}', encoding="utf-8")
        with self.assertRaisesRegex(history_download.HistoryDownloadError, "left unchanged"):
            history_download.normalize_archives([], directory, as_of="2012-12-31")
        self.assertEqual(json.loads(saved.read_text()), {"previous": "keep"})

    def test_local_only_cli_uses_no_network_and_deduplicates_archives(self):
        directory = self.temp()
        archive = make_zip(directory / "2010q1.zip")
        members = directory / "members.json"
        members.write_text(json.dumps({"members": [{"cik": "0000001234"}]}), encoding="utf-8")
        out = directory / "output"
        with patch.object(history_download, "source_request", side_effect=AssertionError("network forbidden")):
            status = history_download.main(["--normalize-only", "--local-archive", str(archive),
                                            "--zip-dir", str(directory), "--output", str(out), "--members", str(members)])
        self.assertEqual(status, 0)
        result = json.loads((out / "candidate_filings.json").read_text(encoding="utf-8"))
        self.assertEqual(result["summary"]["archives"], 1)
        self.assertEqual(result["summary"]["filing_candidates"], 1)

    def test_quarter_selection_respects_cutoff_and_explicit_limits(self):
        self.assertEqual(history_download.selected_quarters(2009, 2012, "2012-02-01", ["2011q4", "2012q1", "2012q2"]),
                         ["2011q4", "2012q1"])
        with self.assertRaises(history_download.HistoryDownloadError):
            history_download.selected_quarters(2011, 2012, "2012-12-31", ["2010q1"])


if __name__ == "__main__":
    unittest.main()
