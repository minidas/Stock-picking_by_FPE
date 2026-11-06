"""Regression checks for the archived 2012 IVV holdings reconstruction."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prices2012", ROOT / "scripts/download_2012_prices.py")
prices2012 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prices2012)


@unittest.skipUnless(prices2012.PDF.exists(), "Archived historical PDF is not installed")
class HistoricalPriceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.text = prices2012.parse_holdings(prices2012.PDF)

    def test_complete_stock_table_reconciles_to_report(self):
        self.assertEqual(len(self.rows), 500)
        self.assertEqual(sum(row["fund_value_usd"] for row in self.rows), 34_986_901_401)
        self.assertEqual(len({row["company_name"] for row in self.rows}), 500)

    def test_ticker_mapping_preserves_historical_share_classes(self):
        rows = copy.deepcopy(self.rows)
        status = prices2012.match_tickers(rows, ROOT / "data/universe_2012_members.json")
        self.assertEqual(status, {"matched": 500, "unmatched_prices": [], "missing_members": []})
        by_symbol = {row["symbol"]: row for row in rows}
        checks = {"GOOG": "Google Inc. Class A", "BRK.B": "Berkshire Hathaway Inc. Class B",
                  "BF.B": "Brown-Forman Corp. Class B NVS", "FII": "Federated Investors Inc. Class B",
                  "TWC": "Time Warner Cable Inc.", "TWX": "Time Warner Inc.",
                  "WPI": "Watson Pharmaceuticals Inc."}
        for symbol, company in checks.items():
            self.assertEqual(by_symbol[symbol]["company_name"], company)
        self.assertNotIn("ABBV", by_symbol)

    def test_report_numeric_spacing_and_digits_in_company_name(self):
        by_name = {row["company_name"]: row for row in self.rows}
        phillips = by_name["Phillips 66"]
        self.assertEqual((phillips["fund_shares"], phillips["fund_value_usd"]), (1_718_498, 91_252_244))
        nasdaq = by_name["NASDAQ OMX Group Inc. (The)"]
        self.assertEqual(nasdaq["fund_value_usd"], 8_043_716)
        for row in self.rows:
            # Published integer fund values need not use banker/nearest rounding.
            # All rows reconcile to a cent valuation within one printed dollar.
            self.assertLessEqual(abs(row["price_rounded_cents"] * row["fund_shares"] - row["fund_value_usd"]), 1.001)
            self.assertEqual(row["price_kind"], "fund_valuation_divided_by_reported_shares")

    def test_duplicate_ticker_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaisesRegex(ValueError, "Multiple fund holdings"):
            prices2012.match_tickers(rows, ROOT / "data/universe_2012_members.json")

    def test_one_dollar_extraction_error_breaks_reconciliation(self):
        pages = [SimpleNamespace(extract_text=lambda: "")] * 58
        for chunk in self.text.split("\n\nPAGE "):
            chunk = chunk.replace("13,055,669", "13,055,668", 1)
            pages.append(SimpleNamespace(extract_text=lambda text=chunk: text))
        with patch.object(prices2012, "PdfReader", return_value=SimpleNamespace(pages=pages)):
            with self.assertRaisesRegex(ValueError, "differ from report total"):
                prices2012.parse_holdings(prices2012.PDF)


if __name__ == "__main__":
    unittest.main()
