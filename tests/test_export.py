from contextlib import contextmanager
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from stocking.data import DataError
from stocking.export import create_exports, write_exports
from stocking.model import build_baseline, evaluate


@contextmanager
def workspace_temp():
    root = Path(__file__).resolve().parents[1]
    directory = tempfile.TemporaryDirectory(dir=root)
    if not Path(directory.name).resolve().is_relative_to(root):
        raise RuntimeError("Temporary path outside workspace")
    try:
        yield Path(directory.name)
    finally:
        directory.cleanup()


def record(symbol="EXAMPLE", eps=5.0, price=50.0, consensus=False):
    snapshot = {
        "symbol": symbol, "as_of": "2026-11-06", "price": price, "price_date": "2026-11-05",
        "eps0": eps, "eps_period_end": "2025-12-31", "eps_available_at": "2026-02-15",
        "currency": "USD", "eps_basis": "GAAP diluted annual", "share_basis": "as_of shares",
        "synthetic": True, "source": {"provider": "synthetic test"},
        "history": [{"period_end": f"{y}-12-31", "available_at": f"{y+1}-02-15", "eps": e}
                    for y, e in zip(range(2021, 2026), [3, 3.5, 4, 4.5, eps])],
        "consensus": [],
    }
    if consensus:
        snapshot["consensus"] = [{"target_date": "2027-12-31", "available_at": "2026-11-01",
                                  "eps_mean": 6.0, "analysts": 10, "eps_std": .6,
                                  "eps_basis": snapshot["eps_basis"], "share_basis": snapshot["share_basis"]}]
    model = build_baseline(snapshot)
    return {"symbol": symbol, "as_of": snapshot["as_of"], "synthetic": True,
            "input": snapshot, "model": model, "prediction": evaluate(model)}


def coverage(missing=None):
    return {"as_of": "2026-11-06", "missing_symbols": missing or [], "complete": not missing}


class ExportTests(unittest.TestCase):
    def test_all_nonexcluded_stocks_not_only_buy_candidates(self):
        observe = record("OBSERVE")
        expensive = record("EXPENSIVE", price=500, consensus=True)
        self.assertEqual(observe["prediction"]["candidate"], "none")
        self.assertEqual(expensive["prediction"]["candidate"], "sell_review")
        result = create_exports([observe, expensive], coverage())
        self.assertEqual({r["symbol"] for r in result["stocks"]}, {"OBSERVE", "EXPENSIVE"})
        self.assertEqual(result["summary"]["excluded_stock_count"], 0)
        self.assertEqual(result["summary"]["available_stock_count"], 2)

    def test_current_losses_and_probability_boundary_excluded(self):
        negative, zero, high, below = record("NEG", eps=-1), record("ZERO", eps=0), record("HIGH"), record("BELOW")
        high["prediction"]["p_loss"] = .25
        below["prediction"]["p_loss"] = .2499
        result = create_exports([negative, zero, high, below], coverage())
        self.assertEqual([r["symbol"] for r in result["stocks"]], ["BELOW"])
        excluded = {r["symbol"]: r["exclusion_reasons"] for r in result["excluded_stocks"]}
        self.assertIn("nonpositive_current_eps", excluded["NEG"])
        self.assertIn("nonpositive_current_eps", excluded["ZERO"])
        self.assertIn("loss_probability_at_or_above_limit", excluded["HIGH"])
        relaxed = create_exports([high], coverage(), max_loss_probability=.3)
        self.assertEqual(len(relaxed["stocks"]), 1)

    def test_forecast_exclusion_uses_dated_model_not_future_raw_rows(self):
        future = record("FUTURE", consensus=True)
        forecast = copy.deepcopy(future["input"]["consensus"][0])
        forecast.update(available_at="2026-11-20", eps_mean=-5)
        future["input"]["consensus"].append(forecast)
        future["model"] = build_baseline(future["input"])
        future["prediction"] = evaluate(future["model"])
        loss = record("LOSS", consensus=True)
        loss["input"]["consensus"][0]["eps_mean"] = -1
        loss["model"] = build_baseline(loss["input"])
        loss["prediction"] = evaluate(loss["model"])
        result = create_exports([future, loss], coverage())
        self.assertEqual([r["symbol"] for r in result["stocks"]], ["FUTURE"])
        self.assertEqual(result["excluded_stocks"][0]["exclusion_reasons"], ["nonpositive_consensus_eps"])

    def test_high_risk_flag_and_post_event_risk_use_final_record(self):
        flagged, adjusted = record("FLAGGED"), record("ADJUSTED")
        flagged["input"]["risk_status"] = "high"
        adjusted["baseline_prediction"] = copy.deepcopy(adjusted["prediction"])
        adjusted["prediction"]["p_loss"] = .3
        result = create_exports([flagged, adjusted], coverage())
        self.assertFalse(result["stocks"])
        self.assertEqual(result["summary"]["excluded_stock_count"], 2)
        self.assertEqual(result["summary"]["exclusion_reason_counts"]["explicit_high_risk"], 1)

    def test_missing_and_model_error_are_visible_without_fabricated_risk(self):
        failed = record("FAILED")
        failed.pop("model")
        failed["error"] = "Price is stale"
        failed["prediction"] = {"candidate": "abstain"}
        result = create_exports([failed], coverage(["MISSING"]))
        self.assertEqual(result["summary"]["exported_stock_count"], 2)
        self.assertEqual(result["summary"]["data_unavailable_stock_count"], 2)
        self.assertEqual(result["summary"]["missing_input_stock_count"], 1)
        self.assertEqual(result["summary"]["risk_not_assessed_count"], 2)
        for row in result["stocks"]:
            self.assertEqual(row["risk_assessment"], "not_assessed")
            self.assertIsNone(row["prediction"].get("p_loss"))
        self.assertFalse(result["excluded_stocks"])

    def test_input_preserved_and_legacy_model_supported(self):
        full = record("FULL", consensus=True)
        original = copy.deepcopy(full)
        result = create_exports([full], coverage())
        self.assertEqual(full, original)
        self.assertEqual(result["stocks"][0]["input"], original["input"])
        self.assertEqual(result["stocks"][0]["model"], original["model"])
        legacy = copy.deepcopy(full)
        legacy.pop("input")
        result = create_exports([legacy], coverage())
        self.assertEqual(result["stocks"][0]["export_status"], "available")
        self.assertFalse(result["stocks"][0]["input_available"])

    def test_duplicate_mixed_dates_and_identity_mismatch_rejected(self):
        first = record("FIRST")
        second = record("SECOND")
        with self.assertRaises(DataError):
            create_exports([first, first], coverage())
        second["as_of"] = "2026-11-07"
        with self.assertRaises(DataError):
            create_exports([first, second], coverage())
        mismatched = record()
        mismatched["input"]["symbol"] = "OTHER"
        with self.assertRaises(DataError):
            create_exports([mismatched], coverage())
        with self.assertRaises(DataError):
            create_exports([first], coverage(["FIRST"]))

    def test_invalid_risk_thresholds_rejected(self):
        for threshold in (True, 0, -1, 1.1, float("nan"), float("inf"), ".25"):
            with self.subTest(threshold=threshold), self.assertRaises(DataError):
                create_exports([], coverage(), max_loss_probability=threshold)

    def test_written_files_have_full_rows_annual_moments_and_risk_separation(self):
        good, loss = record("GOOD", consensus=True), record("LOSS", eps=-1)
        with workspace_temp() as folder:
            summary = write_exports(folder, [good, loss], coverage(["MISSING"]))
            self.assertEqual(set(summary["output_files"]), {"all_stocks.json", "all_stocks.csv", "excluded_stocks.json", "excluded_stocks.csv"})
            main = json.loads((folder / "all_stocks.json").read_text(encoding="utf-8"))
            self.assertEqual({r["symbol"] for r in main["stocks"]}, {"GOOD", "MISSING"})
            excluded = json.loads((folder / "excluded_stocks.json").read_text(encoding="utf-8"))
            self.assertEqual(excluded["excluded_stocks"][0]["symbol"], "LOSS")
            with (folder / "all_stocks.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = {r["symbol"]: r for r in csv.DictReader(stream)}
            self.assertEqual(json.loads(rows["GOOD"]["history_json"]), good["input"]["history"])
            self.assertEqual(json.loads(rows["GOOD"]["consensus_json"]), good["input"]["consensus"])
            self.assertEqual(json.loads(rows["GOOD"]["covariance_json"]), good["model"]["annual_log_growth_covariance"])
            self.assertAlmostEqual(float(rows["GOOD"]["year_6_annualized_growth_variance"]), good["model"]["annual"][5]["annualized_growth_variance"])
            self.assertEqual(rows["MISSING"]["p_loss"], "")
            self.assertEqual(rows["MISSING"]["export_status"], "data_unavailable")

    def test_nonfinite_data_preflight_prevents_partial_files(self):
        bad = record()
        bad["input"]["price"] = float("nan")
        with workspace_temp() as folder:
            with self.assertRaises(DataError):
                write_exports(folder, [bad], coverage())
            self.assertFalse(list(folder.iterdir()))


if __name__ == "__main__":
    unittest.main()
