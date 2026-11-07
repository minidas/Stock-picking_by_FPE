"""Regression checks for current-source joins; all fixtures are inline synthetic."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_current_snapshots as current


def annual(year, eps, accession, filed):
    return {"start": f"{year}-01-01", "end": f"{year}-12-31", "filed": filed,
            "accn": accession, "val": eps, "form": "10-K", "tag": "EarningsPerShareDiluted",
            "duration_days": 366 if year % 4 == 0 else 365}


class CurrentSourceTests(unittest.TestCase):
    def test_connected_history_extends_but_split_conflict_does_not(self):
        member = {"symbol": "TEST", "cik": "0000000001"}
        recent = [annual(2025, 5, "new", "2026-02-01"), annual(2024, 4, "new", "2026-02-01"),
                  annual(2023, 3, "new", "2026-02-01")]
        older = [annual(2023, 3, "old", "2024-02-01"), annual(2022, 2, "old", "2024-02-01")]
        history, _, _ = current.annual_chain(recent + older, member, "test-basis", "2026-11-07", history_policy="strict_overlap")
        self.assertEqual([r["eps"] for r in history], [2, 3, 4, 5])
        older[0]["val"] = 12  # A potential 4:1 split cannot be guessed.
        history, _, _ = current.annual_chain(recent + older, member, "test-basis", "2026-11-07", history_policy="strict_overlap")
        self.assertEqual([r["eps"] for r in history], [3, 4, 5])

    def test_future_sec_record_cannot_define_latest_period(self):
        record = annual(2026, 9, "future", "2027-02-01")
        payload = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [record]}}}}}
        self.assertEqual(current.sec_rows(payload, "EarningsPerShareDiluted", "2026-11-06"), [])

    def test_period_label_cannot_turn_a_quarter_into_annual(self):
        payload = {"timeseries": {"result": [{"meta": {"symbol": ["TEST"]}, "annualDilutedEPS": [
            {"asOfDate": "2025-12-31", "currencyCode": "USD", "periodType": "3M", "reportedValue": {"raw": 2}}
        ]}], "error": None}}
        with self.assertRaises(current.Unavailable):
            current.parse_yahoo(payload, "TEST", "2026-11-06")

    def test_annual_date_mapping_requires_matching_eps_and_unique_period(self):
        yahoo = {"annual": [{"provider_asOfDate": "2026-09-30", "eps": 8.98}]}
        history = [{"period_start": "2025-09-28", "period_end": "2026-09-26", "eps": 8.98, "accession": "test"}]
        result = current.match_annual_basis(yahoo, history)
        self.assertEqual(result[0]["sec_period_end"], "2026-09-26")
        history[0]["eps"] = 8.72
        with self.assertRaises(current.Unavailable):
            current.match_annual_basis(yahoo, history)

    def test_source_observation_is_next_utc_day_not_local_calendar_day(self):
        self.assertEqual(current.observed_available({"retrieved_at": "2026-11-07T00:30:00+08:00"}), "2026-11-07")


if __name__ == "__main__":
    unittest.main()
