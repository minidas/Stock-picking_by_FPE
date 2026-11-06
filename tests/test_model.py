import copy
from datetime import date
import math
import unittest

import numpy as np

from stocking.model import ModelError, apply_events, build_baseline, evaluate


def snapshot(price=15):
    return {
        "symbol": "TEST", "as_of": "2020-06-30", "price": price,
        "price_date": "2020-06-30", "eps0": 5.0, "eps_period_end": "2019-12-31",
        "eps_available_at": "2020-02-15", "currency": "USD", "eps_basis": "GAAP_diluted",
        "share_basis": "2020-06-30", "history": [
            {"period_end": f"{year}-12-31", "available_at": f"{year+1}-02-15", "eps": eps}
            for year, eps in zip(range(2013, 2020), [3, 3.4, 3.5, 3.8, 4, 4.5, 5])
        ], "consensus": [
            {"target_date": "2021-12-31", "available_at": "2020-06-29", "eps_mean": 6.2,
             "eps_std": 0.9, "analysts": 15, "eps_basis": "GAAP_diluted", "share_basis": "2020-06-30"}
        ],
    }


def event(**changes):
    result = {"event_id": "earnings-2020", "published_at": "2020-06-20",
              "source_url": "https://example.com/filing", "summary": "Illustrative event",
              "rationale": "Illustrative incremental revenue change", "already_in_consensus": False,
              "confidence": 0.5, "annual_log_growth_delta": [0.02] * 6,
              "variance_multiplier": 1.2, "loss_probability_delta": 0.01}
    result.update(changes)
    return result


class ModelTests(unittest.TestCase):
    def test_probability_partition_and_price_monotonicity(self):
        low = evaluate(build_baseline(snapshot(15)))
        high = evaluate(build_baseline(snapshot(100)))
        self.assertAlmostEqual(sum(low[k] for k in ("p_below_6", "p_above_6_5_positive", "p_between_6_6_5", "p_loss")), 1)
        self.assertGreater(low["p_below_6"], high["p_below_6"])
        self.assertLess(low["p_above_6_5_positive"], high["p_above_6_5_positive"])

    def test_default_thresholds_preserve_fixed_probabilities(self):
        model = build_baseline(snapshot())
        result = evaluate(model)
        pairs = [("p_below_6", "p_below_buy_threshold"),
                 ("p_above_6_5_positive", "p_above_sell_threshold_positive"),
                 ("p_between_6_6_5", "p_between_thresholds"),
                 ("p_risk_or_above_6_5", "p_risk_or_above_sell_threshold")]
        for fixed, configurable in pairs:
            self.assertEqual(result[fixed], result[configurable])
        self.assertEqual(result["buy_fpe_threshold"], 6.0)
        self.assertEqual(result["sell_fpe_threshold"], 6.5)
        # Old saved baseline files lack these config fields but retain semantics.
        del model["config"]["buy_fpe_threshold"]
        del model["config"]["sell_fpe_threshold"]
        self.assertEqual(evaluate(model), result)

    def test_custom_thresholds_do_not_relabel_fixed_events(self):
        standard = evaluate(build_baseline(snapshot(40)))
        relaxed = evaluate(build_baseline(snapshot(40), {"buy_fpe_threshold": 20, "sell_fpe_threshold": 25}))
        for key in ("p_below_6", "p_above_6_5_positive", "p_between_6_6_5", "p_risk_or_above_6_5"):
            self.assertEqual(standard[key], relaxed[key])
        self.assertGreater(relaxed["p_below_buy_threshold"], standard["p_below_buy_threshold"])
        self.assertLess(relaxed["p_above_sell_threshold_positive"], standard["p_above_sell_threshold_positive"])
        self.assertAlmostEqual(sum(relaxed[key] for key in
                                   ("p_below_buy_threshold", "p_above_sell_threshold_positive", "p_between_thresholds", "p_loss")), 1)
        self.assertEqual(relaxed["candidate"], "buy_review")
        self.assertNotEqual(standard["candidate"], "buy_review")
        expensive = evaluate(build_baseline(snapshot(40), {"buy_fpe_threshold": 1, "sell_fpe_threshold": 1.5}))
        self.assertEqual(expensive["candidate"], "sell_review")

    def test_custom_threshold_at_conditional_median(self):
        model = build_baseline(snapshot(40))
        median_ratio = model["price"] / math.exp(model["terminal_log_eps_mean"])
        custom = evaluate(build_baseline(snapshot(40),
                                        {"buy_fpe_threshold": median_ratio,
                                         "sell_fpe_threshold": median_ratio * 2}))
        self.assertAlmostEqual(custom["p_below_buy_threshold"], (1 - custom["p_loss"]) / 2)
        custom = evaluate(build_baseline(snapshot(40),
                                        {"buy_fpe_threshold": median_ratio / 2,
                                         "sell_fpe_threshold": median_ratio}))
        self.assertAlmostEqual(custom["p_above_sell_threshold_positive"], (1 - custom["p_loss"]) / 2)

    def test_invalid_valuation_thresholds_rejected(self):
        cases = [{"buy_fpe_threshold": 0}, {"buy_fpe_threshold": -1},
                 {"sell_fpe_threshold": 0}, {"buy_fpe_threshold": float("nan")},
                 {"sell_fpe_threshold": float("inf")}, {"buy_fpe_threshold": True},
                 {"buy_fpe_threshold": 6.5}, {"sell_fpe_threshold": 6},
                 {"buy_fpe_threshold": 10, "sell_fpe_threshold": 9}]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ModelError):
                build_baseline(snapshot(), config)

    def test_abstention_keeps_custom_threshold_metadata(self):
        s = snapshot()
        s["eps0"] = -1
        result = evaluate(build_baseline(s, {"buy_fpe_threshold": 9, "sell_fpe_threshold": 12}))
        self.assertEqual(result["candidate"], "abstain")
        self.assertEqual(result["buy_fpe_threshold"], 9)
        self.assertEqual(result["sell_fpe_threshold"], 12)
        for key in ("p_below_buy_threshold", "p_above_sell_threshold_positive", "p_between_thresholds", "p_risk_or_above_sell_threshold"):
            self.assertIsNone(result[key])

    def test_six_year_date_and_anchor_stub(self):
        model = build_baseline(snapshot())
        self.assertEqual(model["target_date"], "2026-06-30")
        self.assertGreater(model["interval_years"][0], 1.4)
        self.assertGreater(sum(model["interval_years"]), 6.4)

    def test_future_facts_do_not_change_prediction(self):
        original = snapshot()
        modified = copy.deepcopy(original)
        modified["history"].append({"period_end": "2020-12-31", "available_at": "2021-02-15", "eps": 5000})
        future = copy.deepcopy(modified["consensus"][0])
        future.update(available_at="2020-07-01", eps_mean=9000)
        modified["consensus"].append(future)
        m1, m2 = build_baseline(original), build_baseline(modified)
        self.assertEqual(m1["terminal_log_eps_mean"], m2["terminal_log_eps_mean"])
        self.assertEqual(m1["terminal_log_eps_variance"], m2["terminal_log_eps_variance"])

    def test_unavailable_base_eps_rejected(self):
        s = snapshot()
        s["eps_available_at"] = "2020-07-01"
        with self.assertRaises(ModelError):
            build_baseline(s)

    def test_losses_are_not_cheap_and_mixed_basis_rejected(self):
        s = snapshot()
        s["eps0"] = -1
        r = evaluate(build_baseline(s))
        self.assertEqual(r["candidate"], "abstain")
        self.assertIsNone(r["p_below_6"])
        s = snapshot()
        s["consensus"][0]["eps_basis"] = "adjusted"
        with self.assertRaises(ModelError):
            build_baseline(s)

    def test_dispersion_is_not_divided_by_analyst_count(self):
        s = snapshot()
        s["consensus"][0]["eps_std"] = 5
        m1 = build_baseline(s)
        s["consensus"][0]["analysts"] = 200
        m2 = build_baseline(s)
        self.assertEqual(m1["terminal_log_eps_variance"], m2["terminal_log_eps_variance"])

    def test_covariance_positive_and_terminal_formula(self):
        m = build_baseline(snapshot())
        cov, dt = np.array(m["annual_log_growth_covariance"]), np.array(m["interval_years"])
        self.assertTrue(np.all(np.linalg.eigvalsh(cov) > 0))
        self.assertAlmostEqual(float(dt @ cov @ dt), m["terminal_log_eps_variance"])
        self.assertGreater(float(dt @ cov @ dt), float(np.sum(dt * dt * np.diag(cov))))

    def test_history_only_inhibits_ai_screen_by_default(self):
        s = snapshot()
        s["consensus"] = []
        model = build_baseline(s)
        self.assertEqual(evaluate(model)["candidate"], "none")
        with self.assertRaises(ModelError):
            apply_events(model, [event()])
        model = build_baseline(s, {"allow_history_only_candidates": True})
        self.assertEqual(evaluate(model)["candidate"], "buy_review")

    def test_variance_only_adjustment_preserves_expected_eps(self):
        m = build_baseline(snapshot())
        adjusted = apply_events(m, [event(annual_log_growth_delta=[0] * 6, loss_probability_delta=0)])
        self.assertAlmostEqual(evaluate(m)["positive_conditional_eps_mean"], evaluate(adjusted)["positive_conditional_eps_mean"])
        self.assertGreater(adjusted["terminal_log_eps_variance"], m["terminal_log_eps_variance"])
        self.assertEqual(m["events"], [])

    def test_positive_event_changes_mean_by_documented_drift(self):
        m = build_baseline(snapshot())
        a = apply_events(m, [event()])
        ratio = evaluate(a)["positive_conditional_eps_mean"] / evaluate(m)["positive_conditional_eps_mean"]
        six_future_years = (date.fromisoformat(m["target_date"]) - date.fromisoformat(m["as_of"])).days / 365.2425
        self.assertAlmostEqual(ratio, math.exp(.01 * six_future_years))

    def test_events_future_duplicate_already_priced_and_excess_rejected(self):
        m = build_baseline(snapshot())
        bad_sets = [[event(published_at="2020-07-01")], [event(), event()],
                    [event(already_in_consensus=True)], [event(annual_log_growth_delta=[1] * 6)]]
        for events in bad_sets:
            with self.subTest(events=events), self.assertRaises(ModelError):
                apply_events(m, events)

    def test_stale_prices_and_nan_rejected(self):
        for change in ({"price_date": "2020-01-01"}, {"price": float("nan")}, {"price": -1}):
            s = snapshot()
            s.update(change)
            with self.subTest(change=change), self.assertRaises(ModelError):
                build_baseline(s)


if __name__ == "__main__":
    unittest.main()
