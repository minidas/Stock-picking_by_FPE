from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_reconciliation_runs import compare, index


class ReconciliationComparisonTests(unittest.TestCase):
    def fixture(self):
        actual = {"eps0": -1.5, "eps_period_start": "2025-10-01", "eps_period_end": "2026-09-30",
                  "ttm_provenance": {"method": "four_quarter_diluted_eps_sum"}, "history": []}
        stock = {"export_status": "data_unavailable", "risk_assessment": "not_assessed",
                 "prediction": {"p_below_6": None, "p_loss": None}}
        before = {"stocks": {"OTHER": stock}, "actuals": {}, "admission": {}, "files": [],
                  "as_of": "2026-11-07", "policy": {"risk": "strict"}, "information_cutoff": "2026-11-07T01:00:00Z"}
        after = deepcopy(before)
        after["actuals"] = {"OTHER": actual}
        after["stocks"]["OTHER"].update(export_status="excluded", risk_assessment="assessed", exclusion_reasons=["nonpositive_current_eps"])
        return before, after

    def test_negative_recovery_is_logged_as_excluded_without_fabricated_probability(self):
        before, after = self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out"
            summary = compare(before, after, path)
            self.assertEqual(summary["security_count"], 1)
            row = json.loads((path / "change_log.jsonl").read_text())
            self.assertEqual(row["after"]["eps0"], -1.5)
            self.assertEqual(row["after"]["export_status"], "excluded")
            self.assertIsNone(row["after"]["p_below_6"])
            with self.assertRaises(ValueError):
                compare(before, after, path)

    def test_mismatching_universes_and_duplicate_symbols_fail_before_writing(self):
        before, after = self.fixture()
        after["stocks"]["EXTRA"] = deepcopy(after["stocks"]["OTHER"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out"
            with self.assertRaises(ValueError):
                compare(before, after, path)
            self.assertFalse(path.exists())
        with self.assertRaises(ValueError):
            index([{"symbol": "X"}, {"symbol": "X"}])

    def test_forecast_mapping_logs_nested_resolved_date_and_preserves_exact_label(self):
        before, after = self.fixture()
        after["admission"] = {"OTHER": {"admitted_annual_count": 2, "group": "consensus_research_join", "observations": [
            {"admitted": True, "as_reported_target_date": "2028-01-31", "fiscal_calendar_mapping": {"target_date": "2028-01-30"}},
            {"admitted": True, "as_reported_target_date": "2027-01-31"}]}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out"
            compare(before, after, path)
            row = json.loads((path / "change_log.jsonl").read_text())
            self.assertEqual([r["after"] for r in row["new_forecast_period_evidence"]], ["2028-01-30", "2027-01-31"])

    def test_threshold_change_is_logged_when_fixed_legacy_probability_is_unchanged(self):
        before, _ = self.fixture()
        before["stocks"]["OTHER"].update(export_status="available", risk_assessment="assessed",
            prediction={"buy_fpe_threshold": 6, "sell_fpe_threshold": 6.5,
                        "p_below_buy_threshold": .40, "p_above_sell_threshold_positive": .50,
                        "p_between_thresholds": .05, "p_risk_or_above_sell_threshold": .55,
                        "p_below_6": .40, "p_loss": .05, "candidate": "watch"})
        after = deepcopy(before)
        after["stocks"]["OTHER"]["prediction"].update(buy_fpe_threshold=6.5,
            p_below_buy_threshold=.45, p_between_thresholds=0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out"
            summary = compare(before, after, path)
            row = json.loads((path / "change_log.jsonl").read_text())
            self.assertEqual(summary["changed_security_count"], 1)
            self.assertEqual(set(row["changed_fields"]),
                             {"buy_fpe_threshold", "p_below_buy_threshold", "p_between_thresholds"})
            self.assertEqual(row["before"]["p_below_6"], row["after"]["p_below_6"])
            self.assertEqual(row["after"]["p_below_buy_threshold"], .45)


if __name__ == "__main__":
    unittest.main()
