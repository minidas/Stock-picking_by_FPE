"""TTM integration must not turn missing quarterly data into annual forecasts."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("legacy_universe_fixtures", ROOT / "tests/test_universe_analysis.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
analysis = fixtures.analysis


def anchor():
    basis = "GAAP_diluted_as_reported_at_cutoff"
    shares = "ASSUMED_filing_EPS_comparable_to_nominal_shares_2012_12_31"
    periods = [("2011-10-01", "2011-12-31", "2012-02-15", 1.30),
               ("2012-01-01", "2012-03-31", "2012-04-25", 2.63),
               ("2012-04-01", "2012-06-30", "2012-07-25", 1.81),
               ("2012-07-01", "2012-09-30", "2012-10-24", 0.59)]
    return {"symbol": "TEST", "cik": "0000000001", "eps": 6.33,
            "period_start": "2011-10-01", "period_end": "2012-09-30", "available_at": "2012-10-24",
            "eps_basis": basis, "share_basis": shares, "method": "four_quarter_diluted_eps_sum",
            "latest_report_period_end": "2012-09-30",
            "components": [{"period_start": start, "period_end": end, "available_at": available,
                            "eps": eps, "source_url": "https://example.com/quarter",
                            "eps_basis": basis, "share_basis": shares, "reported_single_quarter": True}
                           for start, end, available, eps in periods]}


def assemble(company=None, anchors=None, **kwargs):
    return analysis.assemble([fixtures.member()], [fixtures.price()],
                             [company or fixtures.company()], [], eps_anchor="ttm",
                             ttm_anchors=anchors, **kwargs)[0]


class UniverseTTMTests(unittest.TestCase):
    def test_real_ttm_anchor_keeps_annual_history_independent(self):
        original = fixtures.company()
        before = deepcopy(original)
        row = assemble(original, [anchor()])
        self.assertEqual(row["test_status"], "evaluated")
        self.assertEqual(row["input"]["eps0"], 6.33)
        self.assertEqual(row["input"]["eps_measure"], "ttm")
        self.assertEqual(row["input"]["history"], original["history"])
        self.assertEqual(row["model"]["history_growth_observations"], 2)
        self.assertEqual(original, before)

    def test_missing_ttm_is_data_gap_not_annual_fallback_or_risk_exclusion(self):
        row = assemble()
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertIn("ttm_anchor_unavailable", row["blockers"])
        self.assertIsNone(row["input"]["eps0"])
        self.assertIsNone(row["model"])
        self.assertFalse(row["risk_excluded"])
        self.assertEqual(row["annual_anchor_before_ttm"]["eps0"], 3)

    def test_future_quarter_is_not_an_eligible_ttm(self):
        future = anchor()
        future["available_at"] = "2013-01-15"
        future["components"][-1]["available_at"] = "2013-01-15"
        row = assemble(anchors=[future])
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertTrue(any(x.startswith("invalid_ttm_anchor:") for x in row["blockers"]))
        self.assertIsNone(row["model"])

    def test_negative_ttm_abstains_but_does_not_add_a_loss_year(self):
        negative = anchor()
        negative["components"][-1]["eps"] = -10
        negative["eps"] = sum(q["eps"] for q in negative["components"])
        row = assemble(anchors=[negative])
        self.assertEqual(row["test_status"], "model_abstained")
        self.assertEqual(row["abstention_reason"], "nonpositive_base_eps")
        self.assertFalse(row["risk_excluded"])
        self.assertAlmostEqual(row["loss_probability"], 0.6 / 13)

    def test_annual_split_factor_does_not_double_adjust_ttm(self):
        adjustment = {"symbol": "TEST", "expected_accession": "TEST-2012", "effective_date": "2012-08-01",
                      "eps_multiplier": .5, "source_url": "https://example.com/split", "rationale": "Synthetic split"}
        row = assemble(anchors=[anchor()], adjustments=[adjustment])
        self.assertEqual(row["input"]["eps0"], 6.33)
        self.assertEqual([h["eps"] for h in row["input"]["history"]], [1, 1.25, 1.5])
        self.assertEqual(row["annual_anchor_before_ttm"]["eps0"], 1.5)

    def test_high_loss_prior_still_excludes_before_positive_model(self):
        row = assemble(fixtures.company((-1, -2, -3)), [anchor()])
        self.assertEqual(row["computation_status"], "not_tested_high_loss")
        self.assertTrue(row["risk_excluded"])
        self.assertIsNone(row["model"])
        self.assertEqual(row["threshold_scenarios"], [])

    def test_high_loss_with_missing_ttm_retains_both_reasons(self):
        row = assemble(fixtures.company((-1, -2, -3)))
        self.assertTrue(row["risk_excluded"])
        self.assertIn("ttm_anchor_unavailable", row["blockers"])
        self.assertIsNone(row["model"])
        self.assertIsNone(row["input"]["eps0"])

    def test_valid_ttm_cannot_override_denied_identity(self):
        row = analysis.assemble([fixtures.member(allowed=False)], [fixtures.price()],
                                [fixtures.company()], [], eps_anchor="ttm", ttm_anchors=[anchor()])[0]
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertIn("issuer_identity_join_disallowed", row["blockers"])
        self.assertIsNone(row["model"])

    def test_different_issuer_ttm_is_not_used(self):
        other = anchor()
        other["cik"] = "0000000002"
        row = assemble(anchors=[other])
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertEqual(row["input_status"], "ttm_anchor_invalid")
        self.assertIsNone(row["input"]["eps0"])


if __name__ == "__main__":
    unittest.main()
