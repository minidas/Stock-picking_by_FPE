"""Inline issuer fixtures test evidence-gated forecast dates, never real TTM edits."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest

from stocking.fiscal_calendar import (FiscalCalendarError, derive_fiscal_end, map_forecast_target,
                                     prior_actual_period_for_forecast, review_calendar)

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = "2026-11-07T13:00:00+00:00"


def issuer_fixture(directory, *, symbol="GPU", kind="last_weekday_in_month", weekday=6):
    source = Path(directory) / (symbol + ".html")
    source.write_text("Reviewed fiscal calendar policy fixture: " + kind, encoding="utf-8")
    rule = {"kind": kind, "month": 1, "weekday": weekday}
    # Explicitly known ends exercise a 53-week year without calling production
    # date derivation to create the expected observations.
    ends = ["2023-01-29", "2024-01-28", "2025-01-26", "2026-01-25"] if weekday == 6 else [
        "2023-01-28", "2024-02-03", "2025-02-01", "2026-01-31"]
    history = [{"period_start": (date.fromisoformat(previous) + timedelta(days=1)).isoformat(), "period_end": end,
                "eps": 2, "available_at": "2026-02-27", "measure": "annual",
                "source_url": "https://www.sec.gov/Archives/edgar/data/1/annual.htm", "accession": "ANNUAL"}
               for previous, end in zip(ends, ends[1:])]
    snapshot = {"symbol": symbol, "cik": "0000000001", "eps_basis": "GAAP_diluted", "share_basis": "current_shares", "history": history}
    config = {"symbol": symbol, "cik": "0000000001", "eps_basis": "GAAP_diluted", "share_basis": "current_shares",
              "review_status": "reviewed", "reviewed_at": "2026-11-07T12:00:00+00:00", "rule": rule,
              "valid_from": "2023-01-01", "valid_through": "2028-12-31", "evidence": [{
                  "source_url": "https://www.sec.gov/Archives/edgar/data/1/annual.htm", "source_file": str(source),
                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "locator": "Note 1, Fiscal Year",
                  "financial_available_at": "2026-02-27", "retrieved_at": "2026-11-07T11:00:00+00:00"}]}
    return snapshot, config


class FiscalCalendarTests(unittest.TestCase):
    def review(self, config, snapshot):
        return review_calendar(config, snapshot, information_cutoff=CUTOFF, evidence_root=ROOT)

    def test_last_sunday_supports_53_week_year_and_preserves_raw_label(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            original = deepcopy((snapshot, config))
            reviewed = self.review(config, snapshot)
            current = map_forecast_target("2027-01-31", reviewed)
            following = map_forecast_target("2028-01-31", reviewed)
            self.assertEqual(current["target_date"], "2027-01-31")
            self.assertFalse(current["mapping_applied"])
            self.assertEqual((date(2027, 1, 31) - date(2026, 1, 25)).days, 371)
            self.assertEqual(following["as_reported_target_date"], "2028-01-31")
            self.assertEqual(following["target_date"], "2028-01-30")
            self.assertEqual(prior_actual_period_for_forecast(current, reviewed), "2026-01-25")
            self.assertEqual((snapshot, config), original)

    def test_independent_issuer_nearest_saturday_crosses_month_end(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory, symbol="RETAIL_TWO", kind="weekday_nearest_month_end", weekday=5)
            reviewed = self.review(config, snapshot)
            mapping = map_forecast_target("2024-01-31", reviewed)
            self.assertEqual(mapping["target_date"], "2024-02-03")
            self.assertEqual(map_forecast_target("2024-02-03", reviewed)["target_date"], "2024-02-03")
            self.assertFalse(map_forecast_target("2024-02-03", reviewed)["mapping_applied"])
            self.assertEqual(map_forecast_target("2028-01-31", reviewed)["target_date"], "2028-01-29")

    def test_nearest_weekday_can_cross_calendar_year(self):
        rule = {"kind": "weekday_nearest_month_end", "month": 12, "weekday": 5}
        self.assertEqual(derive_fiscal_end(rule, 2020), date(2021, 1, 2))

    def test_missing_unreviewed_and_expired_rules_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            for change in ({"evidence": []}, {"review_status": "pending"}, {"valid_through": "2026-11-06"}):
                with self.subTest(change=change), self.assertRaises(FiscalCalendarError):
                    self.review(config | change, snapshot)
            with self.assertRaises(FiscalCalendarError):
                map_forecast_target("2027-01-31", config)

    def test_wrong_identity_accounting_share_basis_or_source_hash_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            cases = [config | {"cik": "2"}, config | {"symbol": "DIFFERENT"},
                     config | {"eps_basis": "adjusted"}, config | {"share_basis": "other_shares"}]
            for field, value in (("sha256", "0" * 64), ("source_url", "https://www.sec.gov/Archives/edgar/data/2/annual.htm")):
                bad = deepcopy(config)
                bad["evidence"][0][field] = value
                cases.append(bad)
            for bad in cases:
                with self.subTest(bad=bad), self.assertRaises(FiscalCalendarError):
                    self.review(bad, snapshot)

    def test_future_financial_retrieval_and_review_dates_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            for field, value in (("financial_available_at", "2026-11-08"), ("retrieved_at", "2026-11-07T14:00:00+00:00")):
                bad = deepcopy(config)
                bad["evidence"][0][field] = value
                with self.subTest(field=field), self.assertRaises(FiscalCalendarError):
                    self.review(bad, snapshot)
            with self.assertRaises(FiscalCalendarError):
                self.review(config | {"reviewed_at": "2026-11-07T14:00:00+00:00"}, snapshot)

    def test_history_must_match_rule_and_have_three_complete_years(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            bad = deepcopy(snapshot)
            bad["history"][1]["period_end"] = "2025-01-27"
            for history in (bad["history"], snapshot["history"][:2], snapshot["history"][:1] + snapshot["history"][2:]):
                with self.subTest(history=history), self.assertRaises(FiscalCalendarError):
                    self.review(config, snapshot | {"history": history})

    def test_wrong_month_nearby_dates_and_out_of_scope_forecasts_never_map(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            reviewed = self.review(config, snapshot)
            for raw in ("2028-02-29", "2028-01-29", "2029-01-31"):
                with self.subTest(raw=raw), self.assertRaises(FiscalCalendarError):
                    map_forecast_target(raw, reviewed)
            exact = map_forecast_target("2028-01-30", reviewed)
            self.assertFalse(exact["mapping_applied"])
            for purpose in ("ttm", "annual_actual", "quarter_actual"):
                with self.subTest(purpose=purpose), self.assertRaises(FiscalCalendarError):
                    map_forecast_target("2028-01-31", reviewed, purpose=purpose)

    def test_source_file_must_remain_inside_declared_evidence_root(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            snapshot, config = issuer_fixture(directory)
            inside = Path(directory) / "nested"
            inside.mkdir()
            with self.assertRaisesRegex(FiscalCalendarError, "outside"):
                review_calendar(config, snapshot, information_cutoff=CUTOFF, evidence_root=inside)


if __name__ == "__main__":
    unittest.main()
