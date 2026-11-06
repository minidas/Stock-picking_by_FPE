import copy
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from stocking.backtest import probability_metrics, score_panel, target_six_years
from stocking.cli import adjust, file_hash, screen, score_history, write_json
from stocking.data import DataError
from stocking.model import ModelError, build_baseline, evaluate


def snapshot(as_of="2010-06-30", symbol="TEST", price=15):
    year = date.fromisoformat(as_of).year
    return {"symbol": symbol, "as_of": as_of, "price": price, "price_date": as_of,
            "eps0": 5.0, "eps_period_end": f"{year-1}-12-31", "eps_available_at": f"{year}-03-01",
            "currency": "USD", "eps_basis": "GAAP_diluted_annual", "share_basis": "fixed historical share basis",
            "history": [], "consensus": [], "source": "synthetic test", "synthetic": True}


def outcome(snap, eps6=10.0, available_at=None):
    target = target_six_years(snap["as_of"])
    return {"symbol": snap["symbol"], "as_of": snap["as_of"], "target_date": target,
            "available_at": available_at or target, "eps6": eps6, "source": "synthetic test",
            **{key: snap[key] for key in ("eps_basis", "share_basis", "currency")}}


def memberships(*symbols):
    return [{"symbol": symbol, "effective_from": "2000-01-01", "effective_to": None,
             "available_at": "2000-01-01", "source": "synthetic test"} for symbol in symbols]


class ScoringTests(unittest.TestCase):
    def test_boundary_and_proper_score(self):
        result = probability_metrics([0, 1, .25, .75], [0, 1, 0, 1])
        self.assertAlmostEqual(result["brier"], .03125)
        self.assertEqual(sum(x["n"] for x in result["reliability"]), 4)

    def test_calendar_horizon(self):
        self.assertEqual(target_six_years("2020-02-29"), "2026-02-28")
        self.assertEqual(target_six_years("2026-11-06"), "2032-11-06")

    def test_causal_baseline_waits_for_label_and_excludes_same_day(self):
        early = snapshot("2010-06-30")
        on_publication = snapshot("2016-07-15")
        after_publication = snapshot("2016-07-16")
        snaps = [after_publication, early, on_publication]  # Input order must not matter.
        labels = [outcome(early, available_at="2016-07-15"), outcome(on_publication), outcome(after_publication)]
        rows = score_panel(snaps, labels, memberships("TEST"), "2026-11-06")["records"]
        self.assertEqual([r["benchmark_training_n"] for r in rows], [0, 0, 1])
        self.assertEqual(rows[0]["baseline_buy"], 0.5)
        self.assertEqual(rows[1]["baseline_buy"], 0.5)
        self.assertAlmostEqual(rows[2]["baseline_buy"], 2 / 3)
        # A later label is not eligible to influence any earlier decision.
        labels[-1]["eps6"] = -1000
        changed = score_panel(snaps, labels, memberships("TEST"), "2026-11-06")["records"]
        self.assertEqual([r["baseline_buy"] for r in rows], [r["baseline_buy"] for r in changed])

    def test_negative_zero_and_ratio_boundaries_have_correct_labels(self):
        snaps = [snapshot(symbol=symbol, price=price) for symbol, price in
                 (("EXACT6", 60), ("EXACT65", 65), ("NEGATIVE", 60), ("ZERO", 60))]
        labels = [outcome(snap, eps) for snap, eps in zip(snaps, [10, 10, -2, 0])]
        result = score_panel(snaps, labels, memberships(*(s["symbol"] for s in snaps)), "2026-11-06")
        self.assertEqual(result["scored"], 4)
        rows = {r["symbol"]: r for r in result["records"]}
        for name in ("EXACT6", "EXACT65"):
            self.assertEqual((rows[name]["y_buy"], rows[name]["y_sell"], rows[name]["y_loss"]), (0, 0, 0))
        for name in ("NEGATIVE", "ZERO"):
            self.assertEqual((rows[name]["y_buy"], rows[name]["y_sell"], rows[name]["y_loss"]), (0, 0, 1))
            self.assertIsNone(rows[name]["realized_fpe"])

    def test_missing_immature_and_outside_universe_visible(self):
        missing = snapshot(symbol="MISSING")
        immature = snapshot("2025-06-30", "IMMATURE")
        outside = snapshot(symbol="OUTSIDE")
        result = score_panel([missing, immature, outside], [outcome(immature), outcome(outside)],
                             memberships("MISSING", "IMMATURE"), "2026-11-06")
        self.assertEqual(result["input_snapshots"], 3)
        self.assertEqual(result["scored"], 0)
        reasons = {r["symbol"]: r["reason"] for r in result["excluded"]}
        self.assertEqual(reasons["MISSING"], "missing_outcome_including_possible_delisting")
        self.assertEqual(reasons["IMMATURE"], "six_year_label_not_available")
        self.assertEqual(reasons["OUTSIDE"], "not_in_dated_universe")

    def test_future_consensus_cannot_change_panel_prediction(self):
        original = snapshot()
        future = copy.deepcopy(original)
        future["consensus"] = [{"target_date": "2011-12-31", "available_at": "2010-07-01",
                                "eps_mean": 5000, "analysts": 30, "eps_basis": original["eps_basis"],
                                "share_basis": original["share_basis"]}]
        args = ([outcome(original)], memberships("TEST"), "2026-11-06")
        before = score_panel([original], *args)["records"][0]
        after = score_panel([future], *args)["records"][0]
        for key in ("p_buy", "p_sell", "p_loss"):
            self.assertEqual(before[key], after[key])

    def test_outcome_bases_and_future_period_are_not_silently_mixed(self):
        snap = snapshot()
        for field, value in (("eps_basis", "adjusted"), ("share_basis", "latest split basis"),
                             ("currency", "EUR"), ("target_date", "2016-12-31"),
                             ("available_at", "2016-01-01")):
            label = outcome(snap)
            label[field] = value
            with self.subTest(field=field), self.assertRaises(DataError):
                score_panel([snap], [label], memberships("TEST"), "2026-11-06")

    def test_duplicate_panel_records_are_rejected(self):
        snap = snapshot()
        label = outcome(snap)
        for snaps, labels in (([snap, snap], [label]), ([snap], [label, label])):
            with self.assertRaises(DataError):
                score_panel(snaps, labels, memberships("TEST"), "2026-11-06")

    def test_negative_current_eps_abstention_is_visible(self):
        snap = snapshot()
        snap["eps0"] = -5
        result = score_panel([snap], [outcome(snap)], memberships("TEST"), "2026-11-06")
        self.assertEqual(result["scored"], 0)
        self.assertEqual(result["excluded"][0]["reason"], "model_abstained")

    def test_missing_member_snapshots_are_counted_by_decision_date(self):
        first = snapshot("2010-06-30", "PROVIDED")
        second = snapshot("2011-06-30", "PROVIDED")
        universe = memberships("PROVIDED", "ABSENT")
        result = score_panel([first, second], [outcome(first), outcome(second)], universe, "2026-11-06")
        self.assertEqual(result["input_snapshots"], 2)
        self.assertEqual(result["expected_member_snapshots_at_supplied_dates"], 4)
        self.assertEqual(result["missing_member_snapshots"], 2)
        self.assertEqual(len(result["universe_coverage_by_date"]), 2)
        for coverage in result["universe_coverage_by_date"]:
            self.assertEqual(coverage["missing_symbols"], ["ABSENT"])
            self.assertFalse(coverage["complete"])

    def test_bool_and_text_outcomes_rejected(self):
        snap = snapshot()
        for eps in (True, False, "10.0"):
            with self.subTest(eps=eps), self.assertRaisesRegex(DataError, "JSON number"):
                score_panel([snap], [outcome(snap, eps)], memberships("TEST"), "2026-11-06")


class AdjustmentReplayTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.directory = Path(self.tmp.name).resolve()
        if not self.directory.is_relative_to(root):
            raise RuntimeError("Temporary path outside workspace")
        self.addCleanup(self.tmp.cleanup)
        snap = snapshot()
        snap["consensus"] = [{"target_date": "2011-12-31", "available_at": "2010-06-01",
                              "eps_mean": 6.0, "analysts": 20, "eps_basis": snap["eps_basis"],
                              "share_basis": snap["share_basis"]}]
        model = build_baseline(snap)
        self.assertEqual(evaluate(model)["candidate"], "buy_review")
        self.baseline = self.directory / "baseline.json"
        write_json(self.baseline, {"results": [{"symbol": "TEST", "as_of": snap["as_of"],
                                                "model": model, "prediction": evaluate(model)}]})
        self.events = self.directory / "events.json"
        self.args = SimpleNamespace(baseline=str(self.baseline), events=str(self.events),
                                    output=str(self.directory / "adjusted"))

    def test_stale_baseline_hash_is_rejected_before_output(self):
        write_json(self.events, {"baseline_sha256": "stale", "events_by_symbol": {"TEST": []}})
        with self.assertRaisesRegex(DataError, "baseline_sha256"):
            adjust(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_future_evidence_is_rejected_even_with_correct_hash(self):
        future_event = {"event_id": "future", "published_at": "2010-07-01",
                        "source_url": "https://example.com/filing", "summary": "Future information",
                        "rationale": "Must not enter current snapshot", "already_in_consensus": False,
                        "confidence": 1, "annual_log_growth_delta": [0.01] * 6,
                        "variance_multiplier": 1, "loss_probability_delta": 0}
        write_json(self.events, {"baseline_sha256": file_hash(self.baseline),
                                "events_by_symbol": {"TEST": [future_event]}})
        with self.assertRaisesRegex(ModelError, "future-dated"):
            adjust(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_unselected_symbol_rejected_even_with_noop_events(self):
        write_json(self.events, {"baseline_sha256": file_hash(self.baseline), "events_by_symbol": {"OTHER": []}})
        with self.assertRaisesRegex(DataError, "preliminary candidates"):
            adjust(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_duplicate_event_symbol_keys_are_rejected(self):
        self.events.write_text('{"baseline_sha256": ' + json.dumps(file_hash(self.baseline))
                               + ', "events_by_symbol": {"TEST": [], "TEST": []}}', encoding="utf-8")
        with self.assertRaisesRegex(DataError, "Duplicate JSON key"):
            adjust(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_duplicate_baseline_keys_are_rejected(self):
        self.baseline.write_text('{"results": [], "results": []}', encoding="utf-8")
        write_json(self.events, {"baseline_sha256": file_hash(self.baseline), "events_by_symbol": {}})
        with self.assertRaisesRegex(DataError, "Duplicate JSON key"):
            adjust(self.args)

    def test_screen_rejects_stock_outside_dated_universe(self):
        input_path = self.directory / "snapshots.json"
        member_path = self.directory / "memberships.json"
        write_json(input_path, [snapshot(symbol="OUTSIDE")])
        write_json(member_path, memberships("INSIDE"))
        args = SimpleNamespace(input=str(input_path), universe=str(member_path), config=None,
                               output=str(self.directory / "screen"))
        with self.assertRaisesRegex(DataError, "outside supplied dated universe"):
            screen(args)
        self.assertFalse(Path(args.output).exists())

    def test_duplicate_config_keys_are_rejected(self):
        input_path = self.directory / "snapshots.json"
        config_path = self.directory / "config.json"
        write_json(input_path, [snapshot()])
        config_path.write_text('{"buy_probability": 0.7, "buy_probability": 0.2}', encoding="utf-8")
        args = SimpleNamespace(input=str(input_path), universe=None, config=str(config_path),
                               output=str(self.directory / "screen"))
        with self.assertRaisesRegex(DataError, "Duplicate JSON key"):
            screen(args)

    def test_duplicate_outcome_json_keys_are_rejected(self):
        outcomes_path = self.directory / "outcomes.json"
        outcomes_path.write_text('{"outcomes": [], "outcomes": []}', encoding="utf-8")
        args = SimpleNamespace(outcomes=str(outcomes_path), input="not-needed", universe="not-needed",
                               evaluation_date="2026-11-06", output=str(self.directory / "scores"))
        with self.assertRaisesRegex(DataError, "Duplicate JSON key"):
            score_history(args)


if __name__ == "__main__":
    unittest.main()
