"""Check threshold inversion and the independent screening gates."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stock_thresholds", ROOT / "scripts/analyze_stock_thresholds.py")
thresholds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thresholds)


def fixture(consensus=False):
    # The gates need three positive annual observations, not a real issuer's
    # archived research data. One loss below deliberately crosses the 10% gate.
    snapshot = {
        "symbol": "SYNTHETIC_GATE_TEST", "synthetic": True, "asset_type": "stock",
        "as_of": "2012-12-31", "price": 120.0, "price_date": "2012-12-31",
        "currency": "USD", "eps0": 12.0, "eps_period_end": "2011-12-31",
        "eps_available_at": "2012-02-01", "eps_basis": "GAAP_diluted",
        "share_basis": "synthetic_constant_shares",
        "history": [{"period_end": f"{year}-12-31", "available_at": "2012-02-01", "eps": eps}
                    for year, eps in zip((2009, 2010, 2011), (10.0, 11.0, 12.0))],
        "consensus": [], "source": {"provider": "synthetic unit-test fixture"},
    }
    if consensus:
        snapshot["consensus"] = [{"target_date": "2013-12-31", "available_at": "2012-12-30",
                                  "eps_mean": 50.0, "eps_std": 5.0, "analysts": 10,
                                  "eps_basis": snapshot["eps_basis"], "share_basis": snapshot["share_basis"]}]
    return snapshot


class StockThresholdTests(unittest.TestCase):
    def test_inverse_recovers_unconditional_probability(self):
        model = thresholds.build_baseline(fixture())
        for probability in (0.30, 0.70, 0.90):
            with self.subTest(probability=probability):
                boundary = thresholds.minimum_threshold(model, probability)
                evaluation = thresholds.threshold_diagnostics(model, boundary["threshold"])
                self.assertAlmostEqual(evaluation["p_below_threshold"], probability, places=12)
                lower = thresholds.threshold_diagnostics(model, boundary["threshold"] * 0.99)
                self.assertLess(lower["p_below_threshold"], probability)

    def test_no_finite_cutoff_can_exceed_positive_state_mass(self):
        model = thresholds.build_baseline(fixture())
        result = thresholds.minimum_threshold(model, 1 - model["loss_probability"])
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["status"], "unattainable_at_finite_threshold")

    def test_numeric_pass_cannot_bypass_missing_consensus(self):
        absent = thresholds.build_baseline(fixture())
        present = thresholds.build_baseline(fixture(consensus=True))
        original = deepcopy(absent)
        for model, expected in ((absent, False), (present, True)):
            result = thresholds.threshold_diagnostics(model, 1e6)
            self.assertTrue(result["probability_and_loss_conditions_pass"])
            self.assertEqual(result["consensus_gate_passes"], expected)
            self.assertEqual(result["formal_buy_eligible"], expected)
        self.assertEqual(absent, original)
        self.assertFalse(absent["config"]["allow_history_only_candidates"])

    def test_large_cutoff_cannot_bypass_loss_limit(self):
        snapshot = fixture(consensus=True)
        snapshot["history"][0]["eps"] = -1.0
        model = thresholds.build_baseline(snapshot)
        result = thresholds.threshold_diagnostics(model, 1e6)
        self.assertTrue(result["probability_condition_passes"])
        self.assertTrue(result["consensus_gate_passes"])
        self.assertFalse(result["loss_condition_passes"])
        self.assertFalse(result["probability_and_loss_conditions_pass"])
        self.assertFalse(result["formal_buy_eligible"])


if __name__ == "__main__":
    unittest.main()
