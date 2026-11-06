import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_2012_sample.py"
spec = importlib.util.spec_from_file_location("sample2012", SCRIPT)
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)


class HistoricalAcquisitionTests(unittest.TestCase):
    def test_future_or_stale_earnings_are_not_current_anchor(self):
        rows = [{"period_end": "2012-12-31", "available_at": "2013-02-01", "eps": 8},
                {"period_end": "2009-12-31", "available_at": "2010-02-01", "eps": 3}]
        self.assertIsNone(sample.fresh_anchor({"history": rows}))
        rows.append({"period_end": "2011-12-31", "available_at": "2012-02-01", "eps": 5})
        self.assertEqual(sample.fresh_anchor({"history": rows})["eps"], 5)
        self.assertIsNone(sample.fresh_anchor({"period_end": "2012-09-30", "history": rows}))

    def test_every_member_survives_missing_inputs(self):
        members = [{"symbol": "A", "cik": "0000000001", "name": "A Co"},
                   {"symbol": "B", "cik": "0000000002", "name": "B Co"}]
        rows = sample.assemble(members, [], [], [], [])
        self.assertEqual([r["symbol"] for r in rows], ["A", "B"])
        self.assertTrue(all(r["prediction"]["candidate"] == "abstain" for r in rows))
        self.assertTrue(all(r["input"]["price"] is None for r in rows))

    def test_unreviewed_eps_does_not_create_probability_or_audited_loss(self):
        member = {"symbol": "A", "cik": "0000000001", "name": "A Co"}
        price = {"symbol": "A", "price": 10, "date": "2012-12-31", "source": "source",
                 "price_kind": "fund_valuation_divided_by_reported_shares"}
        company = {"cik": "0000000001", "history": [
            {"period_end": "2011-12-31", "available_at": "2012-02-01", "eps": -2}]}
        row = sample.assemble([member], [price], [company], [], [])[0]
        self.assertEqual(row["input"]["unreviewed_annual_eps"], -2)
        self.assertNotIn("eps0", row["input"])
        self.assertNotIn("p_below_6", row["prediction"])
        self.assertIn("historical_issuer_and_price_eps_share_basis_require_review", row["download_status"]["blockers"])

    def test_duplicate_mapping_is_rejected(self):
        with self.assertRaises(ValueError):
            sample.unique_by([{"symbol": "A"}, {"symbol": "A"}], "symbol")

    def test_incomplete_download_is_not_a_claim_that_eps_does_not_exist(self):
        self.assertEqual(sample.financial_status({}, {}, False), "eps_download_incomplete")
        self.assertEqual(sample.financial_status({}, {}, True),
                         "no_supported_unambiguous_annual_eps_in_completed_query")
        member = {"2012_filing_identity_confirmation": {"identity_join_allowed": False}}
        self.assertEqual(sample.financial_status(member, {}, True),
                         "no_confirmed_2012_filing_identity_in_downloaded_batch")
        facts = [{"tag": "EarningsPerShareDiluted", "segments": "ClassA"}]
        self.assertEqual(sample.financial_status({}, {}, True, facts),
                         "eps_requires_share_class_or_coregistrant_selection")
        facts[0]["segments"] = ""
        self.assertEqual(sample.financial_status({}, {}, True, facts),
                         "eps_requires_nonannual_filing_or_context_review")


if __name__ == "__main__":
    unittest.main()
