import importlib.util
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

spec = importlib.util.spec_from_file_location("validation", Path(__file__).resolve().parents[1] / "scripts/validate_history.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class ValidationTests(unittest.TestCase):
    def test_matured_labels_include_reporting_delay(self):
        predictions = pd.DataFrame({"year": [2000, 2001, 2002], "y_below_6": [1, 0, 1]})
        self.assertEqual(v.matured_rows(predictions, 2007).year.tolist(), [2000])
        self.assertTrue(v.matured_rows(predictions, 2006).empty)

    def test_signal_cannot_capture_previous_month_return(self):
        monthly = pd.DataFrame({
            "date": pd.to_datetime(["2001-01-31", "2001-02-28", "2001-03-31"]),
            "year": [2001] * 3, "month": [1, 2, 3], "nominal_total_return": [100, 110, 121]})
        forecasts = pd.DataFrame({"year": [2000], "p_below_6": [.8], "p_above_6_5": [.1]})
        metrics, equity = v.strategy(monthly, forecasts, cost_bps=0, start_year=2001)
        self.assertEqual(equity.strategy_wealth.iloc[0], 1)
        self.assertAlmostEqual(equity.strategy_wealth.iloc[-1], 1.21)
        self.assertEqual(metrics["entries"], 1)

    def test_gap_holds_position_and_one_way_cost(self):
        monthly = pd.DataFrame({
            "date": pd.to_datetime(["2001-01-31", "2001-02-28", "2002-01-31", "2003-01-31"]),
            "year": [2001, 2001, 2002, 2003], "month": [1, 2, 1, 1],
            "nominal_total_return": [100] * 4})
        forecasts = pd.DataFrame({"year": [2000, 2001, 2002], "p_below_6": [.8, .4, .1], "p_above_6_5": [.1, .4, .8]})
        metrics, equity = v.strategy(monthly, forecasts, cost_bps=10, start_year=2001)
        self.assertEqual(equity.position_after_trade.tolist(), [1, 1, 1, 0])
        self.assertAlmostEqual(equity.strategy_wealth.iloc[-1], .999**2)
        self.assertEqual(metrics["position_changes"], 2)

    def test_block_ci_deterministic(self):
        self.assertEqual(v.moving_block_ci(np.zeros(24)), [0, 0])
        self.assertEqual(v.moving_block_ci(np.arange(24)), v.moving_block_ci(np.arange(24)))


if __name__ == "__main__":
    unittest.main()
