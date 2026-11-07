"""Synthetic evidence contracts and actual-period regressions, no data/ inputs."""
from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from stocking.reviewed_evidence import EvidenceError, validate_reviewed_quarters, merge_reviewed_quarters, ensure_observed_by_cutoff
import build_current_snapshots as current


class ReviewedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        raw = b"Synthetic issuer official table: GAAP diluted quarter EPS 1.76."
        (self.root / "release.html").write_bytes(raw)
        self.row = {"symbol": "TEST", "cik": "0000000001", "share_basis": "post-split",
            "eps_basis": "GAAP_diluted", "currency": "USD", "unit": "USD/shares",
            "earnings_scope": "total", "review_status": "reviewed",
            "observation_kind": "reported_quarterly_diluted_eps",
            "period_start": "2025-10-27", "period_end": "2026-01-25", "eps": 1.76,
            "financial_available_at": "2026-02-25", "retrieved_at": "2026-11-07T01:00:00+00:00",
            "reviewed_at": "2026-11-07T02:00:00+00:00", "source_kind": "issuer_release",
            "source_url": "https://example.com/official-results", "source_file": "release.html",
            "source_sha256": hashlib.sha256(raw).hexdigest(), "evidence_locator": "quarter GAAP table",
            "basis_evidence": "Current share units match comparison annual EPS", "rationale": "Explicitly published single quarter"}

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, rows=None):
        return validate_reviewed_quarters(rows if rows is not None else [self.row], symbol="TEST", cik="0000000001",
            share_basis="post-split", as_of="2026-11-07", information_cutoff="2026-11-07T03:00:00+00:00", base_dir=self.root)

    def test_explicit_quarter_keeps_financial_vintage_and_not_verified_flag(self):
        result = self.validate()[0]
        self.assertEqual(result["filed"], "2026-02-25")
        self.assertEqual(result["val"], 1.76)
        self.assertFalse(result["reviewed_quarter_evidence"]["share_basis_verified"])

    def test_wrong_identity_scope_measure_and_estimated_q4_rejected(self):
        for key, value in [("symbol", "OTHER"), ("cik", "0000000002"), ("share_basis", "pre-split"),
                           ("earnings_scope", "continuing"), ("eps_basis", "nonGAAP"),
                           ("observation_kind", "annual_minus_ytd"), ("review_status", "pending"), ("eps", float("nan"))]:
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.validate([{**self.row, key: value}])

    def test_future_or_timezone_missing_evidence_rejected(self):
        for key, value in [("retrieved_at", "2026-11-07T04:00:00+00:00"),
                           ("reviewed_at", "2026-11-08T02:00:00+00:00"),
                           ("financial_available_at", "2026-11-08"), ("reviewed_at", "2026-11-07T02:00:00")]:
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.validate([{**self.row, key: value}])

    def test_hash_missing_file_and_sec_identity_rejected(self):
        for changed in [{"source_sha256": "0" * 64}, {"source_file": "../outside.html"},
                        {"source_kind": "sec_filing", "source_url": "https://www.sec.gov/Archives/edgar/data/2/filing.htm"},
                        {"evidence_locator": ""}]:
            with self.subTest(changed=changed), self.assertRaises(EvidenceError):
                self.validate([{**self.row, **changed}])

    def test_same_day_conflict_rejected_but_new_financial_vintage_retained(self):
        other = {**self.row, "eps": 1.8}
        with self.assertRaises(EvidenceError):
            self.validate([self.row, other])
        other["financial_available_at"] = "2026-03-01"
        self.assertEqual(len(self.validate([self.row, other])), 2)

    def test_annual_or_overlapping_period_not_a_quarter(self):
        with self.assertRaises(EvidenceError):
            self.validate([{**self.row, "period_start": "2025-01-26"}])

    def test_supplement_same_day_conflict_with_sec_cannot_hide_behind_vendor_ttm(self):
        reviewed = self.validate()
        sec = {**reviewed[0], "val": 2.1}
        with self.assertRaises(EvidenceError):
            merge_reviewed_quarters([sec], reviewed)
        sec["filed"] = "2026-02-24"  # An older disclosure can be superseded.
        self.assertEqual(len(merge_reviewed_quarters([sec], reviewed)), 2)

    def test_quote_observed_or_eligible_later_same_utc_day_is_rejected(self):
        cutoff = "2026-11-07T02:00:00Z"
        for value in [{"source": {"retrieved_at": "2026-11-07T02:00:01Z"}},
                      {"eligible_at": "2026-11-07T02:01:00Z"}]:
            with self.subTest(value=value), self.assertRaises(EvidenceError):
                ensure_observed_by_cutoff(value, cutoff)
        ensure_observed_by_cutoff({"source": {"retrieved_at": cutoff}}, cutoff)


class ActualPeriodTests(unittest.TestCase):
    def fixture(self):
        periods = [("2025-07-28", "2025-10-26", 1.3, "2025-11-19"),
                   ("2025-10-27", "2026-01-25", 1.76, "2026-02-25"),
                   ("2026-01-26", "2026-04-26", 2.39, "2026-05-20"),
                   ("2026-04-27", "2026-07-26", 2.46, "2026-08-26")]
        rows = [{"start": a, "end": b, "val": eps, "filed": filed,
                 "accn": str(i), "tag": "EarningsPerShareDiluted", "form": "10-Q",
                 "duration_days": (date.fromisoformat(b)-date.fromisoformat(a)).days+1}
                for i,(a,b,eps,filed) in enumerate(periods)]
        history = [{"period_start": "2025-01-27", "period_end": "2026-01-25", "eps": 4.9,
                    "financial_filed_date": "2026-02-25", "accession": "annual", "source_url": "https://example.com/annual"}]
        yahoo = {"annual": [{"provider_asOfDate": "2026-01-31", "eps": 4.9}],
                 "quarterly": [{"provider_asOfDate": end, "eps": r["val"]} for end,r in zip(
                     ["2025-10-31", "2026-01-31", "2026-04-30", "2026-07-31"],rows)],
                 "ttm": [{"provider_asOfDate": "2026-07-31", "eps": 7.91, "raw": {"asOfDate": "2026-07-31"}}]}
        return rows,history,yahoo

    def anchor(self, rows, history, yahoo):
        return current.choose_anchor(rows,history,current.match_annual_basis(yahoo,history),yahoo,
            {"symbol":"TEST","cik":"0000000001"},"post-split","2026-11-07","2026-11-07",{"url":"https://example.com/ttm"})

    def test_month_end_observation_uses_actual_four_quarters_not_relabelled_direct_ttm(self):
        rows,history,yahoo=self.fixture(); before=deepcopy(yahoo)
        a=self.anchor(rows,history,yahoo)
        self.assertEqual((a["method"],a["period_end"],a["eps"]),("four_quarter_diluted_eps_sum","2026-07-26",7.91))
        self.assertEqual(yahoo,before)
        self.assertTrue(all(not x["direct_ttm_relabelled"] for x in a["actual_period_reconciliation"]))

    def test_missing_q4_is_not_derived_from_annual_minus_ytd(self):
        rows,history,yahoo=self.fixture()
        with self.assertRaises(current.Unavailable):self.anchor([rows[0]]+rows[2:],history,yahoo)

    def test_newer_actual_or_non_month_end_label_still_blocks_stale_ttm(self):
        for end in ["2026-10-31","2026-07-30"]:
            rows,history,yahoo=self.fixture(); yahoo["quarterly"][-1]["provider_asOfDate"]=end
            with self.subTest(end=end),self.assertRaises(current.Unavailable):self.anchor(rows,history,yahoo)

    def test_latest_quarter_eps_conflict_or_same_day_conflict_not_rescued(self):
        rows,history,yahoo=self.fixture(); yahoo["quarterly"][-1]["eps"]=99
        with self.assertRaises(current.Unavailable):self.anchor(rows,history,yahoo)

    def test_exact_vendor_ttm_does_not_bypass_known_quarter_conflict(self):
        rows,history,yahoo=self.fixture()
        yahoo["ttm"][-1]["provider_asOfDate"]="2026-07-26"
        for raw,row in zip(yahoo["quarterly"],rows):raw["provider_asOfDate"]=row["end"]
        rows.append({**rows[-1],"val":99})
        rows.append({"start":"2025-04-28","end":"2025-07-27","filed":"2025-08-26","val":1,
                     "duration_days":91,"accn":"prior","form":"10-Q","tag":"EarningsPerShareDiluted"})
        with self.assertRaises(current.Unavailable):self.anchor(rows,history,yahoo)
        rows,history,yahoo=self.fixture(); rows.append({**rows[-1],"val":99})
        with self.assertRaises(current.Unavailable):self.anchor(rows,history,yahoo)

    def test_full_annual_anchor_uses_official_dates_and_eps(self):
        rows,history,yahoo=self.fixture(); r={"start":"2025-09-01","end":"2026-08-30","val":20.76,
            "filed":"2026-10-07","accn":"annual-new","form":"10-K","duration_days":364}
        history.append({"period_start":r["start"],"period_end":r["end"],"eps":r["val"],
            "financial_filed_date":r["filed"],"accession":r["accn"],"source_url":"https://example.com/new-annual"})
        yahoo["ttm"]=[{"provider_asOfDate":"2026-08-31","eps":20.76,"raw":{}}]
        a=self.anchor(rows+[r],history,yahoo)
        self.assertEqual((a["method"],a["period_end"]),("annual_report_latest_ttm","2026-08-30"))
        yahoo["ttm"][0]["eps"]=99
        with self.assertRaises(current.Unavailable):self.anchor(rows+[r],history,yahoo)

    def test_observed_cutoff_rejects_late_data_and_keeps_default_policy(self):
        r={"retrieved_at":"2026-11-07T01:00:00+00:00"}
        self.assertEqual(current.observed_available(r),"2026-11-08")
        self.assertEqual(current.observed_available(r,"2026-11-07T02:00:00+00:00"),"2026-11-07")
        with self.assertRaises(current.Unavailable):current.observed_available(r,"2026-11-07T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
