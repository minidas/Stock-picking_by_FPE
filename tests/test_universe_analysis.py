from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("universe_analysis", ROOT / "scripts/analyze_2012_universe.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def member(symbol="TEST", cik="0000000001", allowed=True):
    return {"symbol": symbol, "cik": cik, "name": f"{symbol} Company",
            "2012_filing_identity_confirmation": {"identity_join_allowed": allowed}}


def price(symbol="TEST"):
    return {"symbol": symbol, "date": "2012-12-31", "price": 1.0,
            "source": "https://example.com/price", "price_kind": "synthetic_test"}


def company(eps=(2.0, 2.5, 3.0), symbol="TEST", cik="0000000001"):
    return {"symbol": symbol, "cik": cik, "accession": "TEST-2012", "period_end": "2011-12-31",
            "source_url": "https://example.com/filing", "share_basis_verified": False,
            "history": [{"period_end": f"{year}-12-31", "available_at": "2012-02-01", "eps": value}
                        for year, value in zip((2009, 2010, 2011), eps)]}


class UniverseResearchTests(unittest.TestCase):
    def test_manual_snapshot_retains_its_own_price_and_history(self):
        # Deliberately differ from the bulk price/history; no historical issuer
        # dataset is needed to check that the supplied manual snapshot wins.
        manual = {
            "symbol": "MANUAL_TEST", "synthetic": True, "asset_type": "stock",
            "as_of": "2012-12-31", "price": 75.0, "price_date": "2012-12-31",
            "currency": "USD", "eps0": 5.0, "eps_period_end": "2011-12-31",
            "eps_available_at": "2012-02-01", "eps_basis": "GAAP_diluted",
            "share_basis": "synthetic_constant_shares",
            "history": [{"period_end": f"{year}-12-31", "available_at": "2012-02-01", "eps": eps}
                        for year, eps in zip((2009, 2010, 2011), (3.0, 4.0, 5.0))],
            "consensus": [], "source": {"provider": "synthetic unit-test fixture"},
        }
        row = analysis.assemble([member("MANUAL_TEST")], [price("MANUAL_TEST")],
                                [company(symbol="MANUAL_TEST")], [manual])[0]
        self.assertEqual(row["input"], manual)
        self.assertNotEqual(row["input"]["price"], row["price_evidence"]["price"])
        self.assertEqual(row["input_status"], "manually_checked_snapshot")

    def test_missing_consensus_still_gets_probabilities_without_ai(self):
        row = analysis.assemble([member()], [price()], [company()], [])[0]
        self.assertEqual(row["test_status"], "evaluated")
        self.assertIsNotNone(row["prediction"]["p_below_6"])
        self.assertFalse(row["risk_excluded"])
        self.assertFalse(row["ai_queue_eligible"])
        self.assertEqual(row["prediction"]["candidate"], "none")
        self.assertFalse(row["input"]["share_basis_verified"])

    def test_nonpositive_eps_is_retained_unless_loss_prior_is_high(self):
        low = analysis.assemble([member()], [price()], [company((2, 2.5, -1))], [])[0]
        high = analysis.assemble([member()], [price()], [company((-2, -2.5, -1))], [])[0]
        self.assertEqual(low["test_status"], "model_abstained")
        self.assertLess(low["loss_probability"], 0.25)
        self.assertIsNone(low["prediction"]["p_below_6"])
        self.assertFalse(low["risk_excluded"])
        self.assertTrue(high["risk_excluded"])
        self.assertGreaterEqual(high["loss_probability"], 0.25)
        self.assertEqual(high["computation_status"], "not_tested_high_loss")
        self.assertIsNone(high["model"])
        self.assertEqual(high["threshold_scenarios"], [])

    def test_high_loss_positive_eps_is_excluded_before_threshold_testing(self):
        source = company((-1, -2.5, 3.0))
        source["history"].insert(0, {"period_end": "2008-12-31", "available_at": "2012-02-01", "eps": -0.5})
        row = analysis.assemble([member()], [price()], [source], [])[0]
        self.assertGreater(row["input"]["eps0"], 0)
        self.assertTrue(row["risk_excluded"])
        self.assertEqual(row["computation_status"], "not_tested_high_loss")
        self.assertIsNone(row["prediction"])
        self.assertEqual(row["threshold_scenarios"], [])

    def test_missing_inputs_and_disallowed_identity_keep_roster_rows(self):
        rows = analysis.assemble([member(), member("DENIED", "0000000002", False)],
                                 [price("DENIED")], [company(symbol="DENIED", cik="0000000002")], [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["test_status"] == "data_unavailable" for row in rows))
        self.assertTrue(all(row["loss_probability"] is None for row in rows))
        self.assertIn("issuer_identity_join_disallowed", rows[1]["blockers"])

    def test_supplemental_cannot_bypass_identity_or_override_bulk(self):
        good = analysis.assemble([member()], [price()], [], [], supplemental=[company()])[0]
        denied = analysis.assemble([member(allowed=False)], [price()], [], [], supplemental=[company()])[0]
        self.assertEqual(good["test_status"], "evaluated")
        self.assertEqual(denied["test_status"], "data_unavailable")
        with self.assertRaises(ValueError):
            analysis.assemble([member()], [price()], [company()], [], supplemental=[company()])

    def test_adjustment_changes_only_copied_coherent_eps(self):
        original = company()
        saved = deepcopy(original)
        adjustment = {"symbol": "TEST", "expected_accession": "TEST-2012", "effective_date": "2012-08-01",
                      "eps_multiplier": 0.5, "source_url": "https://example.com/split", "rationale": "Test 2-for-1 split"}
        row = analysis.assemble([member()], [price()], [original], [], adjustments=[adjustment])[0]
        self.assertEqual(original, saved)
        self.assertEqual(row["input"]["eps0"], 1.5)
        self.assertEqual([h["eps"] for h in row["input"]["history"]], [1.0, 1.25, 1.5])
        self.assertEqual(row["annual_eps_evidence_before_adjustment"], original)
        self.assertFalse(row["input"]["share_basis_verified"])
        for change in ({"effective_date": "2013-01-01"}, {"expected_accession": "OTHER"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                analysis.apply_adjustment(original, adjustment | change)

    def test_probability_and_original_buy_loss_gate_are_separate_counts(self):
        # One historical loss out of three puts the default prior above the
        # original 10% buy gate but below this research run's 25% exclusion.
        rows = analysis.assemble([member()], [price()], [company((-1, 2.5, 3))], [])
        summary = analysis.summarize(rows)
        self.assertEqual(summary["retained_main_table_count"], 1)
        self.assertEqual(summary["threshold_counts"][-1]["probability_at_least_70_count"], 1)
        self.assertEqual(summary["threshold_counts"][-1]["probability_at_least_70_and_loss_at_most_10_count"], 0)


if __name__ == "__main__":
    unittest.main()
