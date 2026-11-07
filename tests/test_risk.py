"""Risk admission precedes valuation; fixtures are inline and synthetic."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stocking.cli import adjust, file_hash, screen
from stocking.data import DataError
from stocking.export import create_exports
from stocking.model import build_baseline, evaluate, apply_events, ModelError
from stocking.risk import DEFAULT_RISK_POLICY, normalize_risk_policy, policy_from_export


ROOT = Path(__file__).resolve().parents[1]


def snapshot(symbol="SAFE", years=None, earnings=None, eps0=5.0, consensus=False):
    years = list(range(2015, 2026)) if years is None else list(years)
    earnings = [3 + i * .2 for i in range(len(years))] if earnings is None else earnings
    result = {"symbol": symbol, "as_of": "2026-11-06", "price": 10.0, "price_date": "2026-11-05",
              "eps0": eps0, "eps_period_end": "2025-12-31", "eps_available_at": "2026-02-15",
              "currency": "USD", "eps_basis": "GAAP_diluted", "share_basis": "current_shares",
              "synthetic": True, "source": "inline synthetic fixture", "consensus": [],
              "history": [{"period_end": f"{year}-12-31", "available_at": f"{year + 1}-02-15", "eps": eps}
                          for year, eps in zip(years, earnings)]}
    if consensus:
        result["consensus"] = [{"target_date": "2027-12-31", "available_at": "2026-11-01", "eps_mean": 6,
                                "analysts": 10, "eps_std": .6, "eps_basis": result["eps_basis"],
                                "share_basis": result["share_basis"]}]
    return result


def event(loss_delta=.15):
    return {"event_id": "risk-change", "published_at": "2026-11-05", "source_url": "https://example.org/synthetic",
            "summary": "Synthetic risk increase", "rationale": "Inline test only", "already_in_consensus": False,
            "confidence": 1, "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1,
            "loss_probability_delta": loss_delta}


@contextmanager
def temporary_workspace():
    temp = tempfile.TemporaryDirectory(dir=ROOT)
    path = Path(temp.name).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError("Test directory escaped workspace")
    try:
        yield path
    finally:
        temp.cleanup()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_screen(root, snapshots, policy=None, limit=None):
    source = root / "snapshots.json"
    source.write_text(json.dumps(snapshots), encoding="utf-8")
    policy_path = None
    if policy is not None:
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
    args = SimpleNamespace(input=str(source), universe=None, config=None, risk_policy=str(policy_path) if policy_path else None,
                           exclude_loss_probability=limit, mode="research", retention_days=7, raw_input=None,
                           archive_root=str(root / "archive"), output=str(root / "screen"))
    with redirect_stdout(io.StringIO()):
        screen(args)
    return read(root / "screen/baseline.json")


def run_adjust(root, event_map=None, limit=None):
    baseline = root / "screen/baseline.json"
    events = root / "events.json"
    events.write_text(json.dumps({"baseline_sha256": file_hash(baseline), "events_by_symbol": event_map or {}}), encoding="utf-8")
    args = SimpleNamespace(baseline=str(baseline), events=str(events), exclude_loss_probability=limit,
                           mode="research", retention_days=7, raw_input=None,
                           archive_root=str(root / "archive"), output=str(root / "adjust"))
    with redirect_stdout(io.StringIO()):
        adjust(args)
    return read(root / "adjust/adjusted.json")


class RiskTests(unittest.TestCase):
    def test_strict_threshold_boundary_and_below(self):
        at_limit = snapshot(years=range(2020, 2026), earnings=[-1, 3, 3, 3, 3, 3])
        below = snapshot(years=range(2019, 2026), earnings=[-1, 3, 3, 3, 3, 3, 3])
        blocked = build_baseline(at_limit, risk_policy=DEFAULT_RISK_POLICY)
        passed = build_baseline(below, risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(blocked["loss_probability"], .1)
        self.assertEqual(blocked["status"], "risk_excluded")
        self.assertIn("loss_probability_at_or_above_limit", blocked["risk_screen"]["exclusion_reasons"])
        self.assertEqual(passed["status"], "ready")

    def test_recent_loss_excludes_even_when_shrunken_risk_is_low(self):
        s = snapshot(earnings=[5] * 10 + [-1])
        model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
        self.assertLess(model["loss_probability"], .1)
        self.assertEqual(model["status"], "risk_excluded")
        self.assertIn("nonpositive_recent_annual_eps", model["risk_screen"]["exclusion_reasons"])
        self.assertNotIn("terminal_log_eps_mean", model)
        self.assertIsNone(evaluate(model)["p_below_6"])

    def test_zero_recent_eps_also_excludes(self):
        model = build_baseline(snapshot(earnings=[5] * 10 + [0]), risk_policy=DEFAULT_RISK_POLICY)
        self.assertIn("nonpositive_recent_annual_eps", model["risk_screen"]["exclusion_reasons"])

    def test_current_and_valid_forward_losses_are_blocked(self):
        for eps in (0, -1):
            with self.subTest(eps=eps):
                model = build_baseline(snapshot(eps0=eps), risk_policy=DEFAULT_RISK_POLICY)
                self.assertIn("nonpositive_current_eps", model["risk_screen"]["exclusion_reasons"])
                self.assertIsNotNone(evaluate(model)["p_loss"])
        s = snapshot(consensus=True)
        s["consensus"][0]["eps_mean"] = -2
        model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(model["status"], "risk_excluded")
        self.assertIn("nonpositive_consensus_eps", model["risk_screen"]["exclusion_reasons"])

    def test_future_or_stale_negative_consensus_not_used(self):
        for publication in ("2026-11-20", "2025-11-01"):
            with self.subTest(publication=publication):
                s = snapshot(consensus=True)
                s["consensus"][0].update(available_at=publication, eps_mean=-2)
                model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
                self.assertEqual(model["status"], "ready")

    def test_consensus_risk_uses_latest_available_vintage_independent_of_order(self):
        cases = ((-2, 6, "2026-11-01", "ready"),
                 (6, -2, "2026-11-01", "risk_excluded"),
                 (-2, 6, "2026-11-20", "risk_excluded"),
                 (6, -2, "2026-11-20", "ready"))
        for old_eps, new_eps, new_date, expected in cases:
            for reverse in (False, True):
                with self.subTest(old_eps=old_eps, new_eps=new_eps, new_date=new_date, reverse=reverse):
                    s = snapshot(consensus=True)
                    old = dict(s["consensus"][0], available_at="2026-10-01", eps_mean=old_eps)
                    new = dict(s["consensus"][0], available_at=new_date, eps_mean=new_eps)
                    s["consensus"] = [new, old] if reverse else [old, new]
                    model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
                    self.assertEqual(model["status"], expected)
                    if expected == "ready":
                        selected = new if new_date <= s["as_of"] else old
                        self.assertEqual(model["consensus_anchors"][0]["available_at"], selected["available_at"])
                        control = deepcopy(s)
                        control["consensus"] = [selected]
                        self.assertEqual(evaluate(model)["p_below_6"],
                                         evaluate(build_baseline(control, risk_policy=DEFAULT_RISK_POLICY))["p_below_6"])
                    else:
                        self.assertIn("nonpositive_consensus_eps", model["risk_screen"]["exclusion_reasons"])
                        self.assertNotIn("terminal_log_eps_mean", model)
                    if old_eps < 0 and new_date <= s["as_of"]:
                        # The old mathematical API and explicit v1 preserve the
                        # archived early-abstention behavior for exact replay.
                        self.assertEqual(build_baseline(s)["reason"], "nonpositive_consensus_eps")
                        legacy = {"version": 1, "max_loss_probability": .25, "recent_positive_years": 0}
                        self.assertEqual(build_baseline(s, risk_policy=legacy)["reason"], "nonpositive_consensus_eps")

    def test_missing_short_or_gapped_history_is_unassessed(self):
        for years, reason in (([], "insufficient_recent_annual_history"),
                              ([2024, 2025], "insufficient_recent_annual_history"),
                              ([2021, 2024, 2025], "nonconsecutive_recent_annual_history")):
            with self.subTest(years=years):
                s = snapshot(years=years)
                model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
                self.assertEqual(model["status"], "risk_not_assessed")
                self.assertIn(reason, model["risk_screen"]["missing_reasons"])
                self.assertIsNone(evaluate(model)["p_below_6"])
                row = {"symbol": s["symbol"], "as_of": s["as_of"], "input": s, "model": model, "prediction": evaluate(model)}
                product = create_exports([row], {}, risk_policy=DEFAULT_RISK_POLICY)
                self.assertEqual(product["stocks"][0]["risk_assessment"], "not_assessed")
                self.assertEqual(product["stocks"][0]["export_status"], "data_unavailable")
                self.assertFalse(product["excluded_stocks"])

    def test_old_contiguous_history_is_not_recent(self):
        model = build_baseline(snapshot(years=[2018, 2019, 2020]), risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(model["status"], "risk_not_assessed")
        self.assertIn("stale_recent_annual_history", model["risk_screen"]["missing_reasons"])

    def test_remote_loss_not_mislabeled_as_recent_across_gap(self):
        s = snapshot(years=[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2020, 2024, 2025],
                     earnings=[5, 5, 5, 5, 5, 5, 5, -1, 5, 5])
        model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
        self.assertLess(model["loss_probability"], .1)
        self.assertEqual(model["status"], "risk_not_assessed")
        self.assertFalse(model["risk_screen"]["known_recent_nonpositive_periods"])

    def test_future_annual_publication_does_not_fill_recent_gap(self):
        s = snapshot(years=[2023, 2024, 2025])
        s["history"][-1]["available_at"] = "2026-11-20"
        model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(model["risk_screen"]["annual_observations"], 2)
        self.assertEqual(model["status"], "risk_not_assessed")

    def test_duplicate_versions_choose_latest_available_before_risk(self):
        s = snapshot()
        old = deepcopy(s["history"][-1])
        old.update(available_at="2026-01-15", eps=-1)
        s["history"].append(old)
        model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(model["status"], "ready")
        self.assertEqual(model["risk_screen"]["nonpositive_annual_observations"], 0)

    def test_passed_model_keeps_original_math(self):
        s = snapshot(consensus=True)
        old, new = evaluate(build_baseline(s)), evaluate(build_baseline(s, risk_policy=DEFAULT_RISK_POLICY))
        self.assertEqual(old, new)

    def test_strict_export_masks_legacy_or_externally_changed_valuations(self):
        for strict_model in (False, True):
            with self.subTest(strict_model=strict_model):
                s = snapshot(consensus=True)
                model = build_baseline(s, risk_policy=DEFAULT_RISK_POLICY if strict_model else None)
                prediction = evaluate(model)
                if strict_model:
                    prediction["p_loss"] = .2  # An external result already crossed the policy line.
                row = {"symbol": s["symbol"], "as_of": s["as_of"], "input": s, "model": model,
                       "prediction": prediction, "baseline_prediction": deepcopy(prediction)}
                original = deepcopy(row)
                product = create_exports([row], {}, risk_policy=DEFAULT_RISK_POLICY)
                output = (product["stocks"] + product["excluded_stocks"])[0]
                self.assertIsNone(output["prediction"]["p_below_6"])
                self.assertIsNone(output["prediction"]["positive_conditional_eps_mean"])
                self.assertEqual(output["prediction"]["p_loss"], prediction["p_loss"])
                self.assertEqual(output["prediction"]["candidate"], "abstain")
                self.assertEqual(output["baseline_prediction"], original["baseline_prediction"])
                self.assertNotIn("annual", output["model"])
                self.assertEqual(row, original)

    def test_events_recheck_risk_before_refreshing_valuation(self):
        model = build_baseline(snapshot(consensus=True), risk_policy=DEFAULT_RISK_POLICY)
        self.assertEqual(evaluate(model)["candidate"], "buy_review")
        with patch("stocking.model._refresh", side_effect=AssertionError("excluded event model must not refresh valuation")):
            changed = apply_events(model, [event()])
        self.assertEqual(changed["status"], "risk_excluded")
        self.assertIsNone(evaluate(changed)["p_below_6"])
        self.assertGreater(evaluate(changed)["p_loss"], .1)
        self.assertNotIn("annual", changed)

    def test_risk_excluded_cannot_be_rescued_by_negative_risk_event(self):
        model = build_baseline(snapshot(earnings=[5] * 10 + [-1], consensus=True), risk_policy=DEFAULT_RISK_POLICY)
        with self.assertRaisesRegex(ModelError, "preliminary screened candidates"):
            apply_events(model, [event(-.1)])

    def test_invalid_and_inconsistent_policy_rejected(self):
        for change in ({"version": 3}, {"version": True}, {"version": 2.0}, {"recent_positive_years": 0},
                       {"max_loss_probability": 0}, {"max_loss_probability": True}, {"max_loss_probability": float("nan")},
                       {"unknown": 1}):
            with self.subTest(change=change), self.assertRaises(DataError):
                normalize_risk_policy(change)
        self.assertEqual(policy_from_export({"max_loss_probability": .25}),
                         {"version": 1, "max_loss_probability": .25, "recent_positive_years": 0})


class StrictWorkflowTests(unittest.TestCase):
    def test_screen_excluded_or_unassessed_never_calls_valuation(self):
        with temporary_workspace() as root:
            bad = snapshot("LOSS", earnings=[5] * 10 + [-1], consensus=True)
            short = snapshot("SHORT", years=[2024, 2025])
            with patch("stocking.cli.evaluate", side_effect=AssertionError("valuation must be skipped")), \
                 patch("stocking.model._refresh", side_effect=AssertionError("positive model must be skipped")):
                baseline = run_screen(root, [bad, short])
            self.assertEqual(baseline["export_policy"]["risk_policy"], DEFAULT_RISK_POLICY)
            self.assertFalse(read(root / "screen/agent_queue.json")["candidates"])
            self.assertEqual(read(root / "screen/all_stocks.json")["stocks"][0]["export_status"], "data_unavailable")
            self.assertEqual(read(root / "screen/excluded_stocks.json")["excluded_stocks"][0]["symbol"], "LOSS")

    def test_all_unassessed_still_writes_completed_audit(self):
        with temporary_workspace() as root:
            baseline = run_screen(root, [snapshot(years=[])])
            self.assertEqual(baseline["results"][0]["model"]["status"], "risk_not_assessed")
            lifecycle = read(root / "screen/run_lifecycle.json")
            self.assertNotEqual(lifecycle.get("status"), "pending")

    def test_adjust_inherits_full_policy_and_blocks_crossing_event(self):
        with temporary_workspace() as root:
            run_screen(root, [snapshot(consensus=True)])
            adjusted = run_adjust(root, {"SAFE": [event()]})
            self.assertEqual(adjusted["export_policy"]["risk_policy"], DEFAULT_RISK_POLICY)
            row = adjusted["results"][0]
            self.assertIsNone(row["prediction"]["p_below_6"])
            self.assertEqual(row["model"]["risk_screen"]["status"], "excluded")
            self.assertEqual(read(root / "adjust/excluded_stocks.json")["excluded_stocks"][0]["symbol"], "SAFE")

    def test_adjust_cannot_change_frozen_strict_policy_or_rescue_rejected_rows(self):
        with temporary_workspace() as root:
            run_screen(root, [snapshot(earnings=[5] * 10 + [-1], consensus=True)])
            with self.assertRaisesRegex(DataError, "new screen"):
                run_adjust(root, limit=.25)
            with self.assertRaisesRegex(DataError, "preliminary candidates"):
                run_adjust(root, {"SAFE": [event(-.1)]})

    def test_old_baseline_without_nested_policy_remains_legacy(self):
        with temporary_workspace() as root:
            s = snapshot(earnings=[5] * 10 + [-1])
            legacy = {"version": 1, "max_loss_probability": .25, "recent_positive_years": 0}
            baseline = run_screen(root, [s], policy=legacy)
            self.assertEqual(baseline["results"][0]["model"]["status"], "ready")
            baseline["export_policy"].pop("risk_policy")
            (root / "screen/baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
            adjusted = run_adjust(root)
            self.assertEqual(adjusted["export_policy"]["risk_policy"], legacy)
            self.assertEqual(adjusted["results"][0]["prediction"], baseline["results"][0]["prediction"])
            self.assertEqual(read(root / "adjust/all_stocks.json")["summary"]["available_stock_count"], 1)


if __name__ == "__main__":
    unittest.main()
