"""Point-in-time panel replay and probability scoring, not a return backtest.

Outcomes must have the same security/share/EPS basis as their original snapshot.
Missing, immature and out-of-universe cases remain visible in the denominator.
"""
from datetime import date
import math

import numpy as np

from .data import universe_at, coverage_report, DataError
from .model import build_baseline, evaluate, ModelError


def target_six_years(value):
    day = date.fromisoformat(value)
    try:
        return day.replace(year=day.year + 6).isoformat()
    except ValueError:
        return day.replace(year=day.year + 6, day=28).isoformat()


def probability_metrics(probabilities, labels):
    p = np.array(probabilities, dtype=float)
    y = np.array(labels, dtype=float)
    if not len(p):
        return None
    clipped = np.clip(p, 1e-12, 1 - 1e-12)
    bins = []
    for i in range(10):
        keep = (p >= i / 10) & ((p < (i + 1) / 10) if i < 9 else (p <= 1))
        if keep.any():
            bins.append({"lower": i / 10, "upper": (i + 1) / 10, "n": int(keep.sum()),
                         "mean_prediction": float(p[keep].mean()), "observed_rate": float(y[keep].mean())})
    return {"n": len(p), "brier": float(np.mean((p-y)**2)),
            "log_loss": float(-np.mean(y*np.log(clipped)+(1-y)*np.log1p(-clipped))),
            "observed_rate": float(y.mean()), "mean_prediction": float(p.mean()), "reliability": bins}


def score_panel(snapshots, outcomes, memberships, evaluation_date):
    date.fromisoformat(evaluation_date)
    if not isinstance(outcomes, list):
        raise DataError("outcomes must be a list")
    coverage = [coverage_report(snapshots, memberships, day)
                for day in sorted({s["as_of"] for s in snapshots if s["as_of"] <= evaluation_date})]
    outcome_map = {}
    for outcome in outcomes:
        key = (outcome["symbol"], outcome["as_of"])
        if key in outcome_map:
            raise DataError(f"duplicate outcome {key}; select one declared label vintage")
        outcome_map[key] = outcome
    records, excluded, keys = [], [], set()
    for snapshot in sorted(snapshots, key=lambda x: (x["as_of"], x["symbol"])):
        key = (snapshot["symbol"], snapshot["as_of"])
        if key in keys:
            raise DataError(f"duplicate snapshot {key}")
        keys.add(key)
        def exclude(reason):
            excluded.append({"symbol": key[0], "as_of": key[1], "reason": reason})
        if snapshot["as_of"] > evaluation_date:
            exclude("snapshot_after_evaluation_date")
            continue
        if snapshot["symbol"] not in universe_at(memberships, snapshot["as_of"]):
            exclude("not_in_dated_universe")
            continue
        outcome = outcome_map.get(key)
        if outcome is None:
            exclude("missing_outcome_including_possible_delisting")
            continue
        target = target_six_years(snapshot["as_of"])
        if outcome["target_date"] != target:
            raise DataError(f"{key}: target_date must be exactly {target}; interpolate fiscal EPS explicitly upstream")
        known = date.fromisoformat(outcome["available_at"]).isoformat()
        if known < target:
            raise DataError(f"{key}: realized label cannot precede target date")
        if known > evaluation_date:
            exclude("six_year_label_not_available")
            continue
        for field in ("eps_basis", "share_basis", "currency"):
            if outcome[field] != snapshot[field]:
                raise DataError(f"{key}: outcome {field} does not match forecast")
        if not outcome.get("source"):
            raise DataError(f"{key}: outcome source required")
        if isinstance(outcome["eps6"], bool) or not isinstance(outcome["eps6"], (int, float)):
            raise DataError(f"{key}: eps6 must be a JSON number, not bool or text")
        eps = float(outcome["eps6"])
        if not math.isfinite(eps):
            raise DataError(f"{key}: nonfinite EPS outcome")
        try:
            pred = evaluate(build_baseline(snapshot))
        except (ModelError, ValueError, KeyError) as exc:
            exclude("model_error: " + str(exc))
            continue
        if pred.get("p_below_6") is None:
            exclude("model_abstained")
            continue
        ratio = snapshot["price"] / eps if eps > 0 else None
        row = {"symbol": key[0], "as_of": key[1], "label_available_at": known,
               "target_date": target, "eps6": eps, "realized_fpe": ratio,
               "p_buy": pred["p_below_6"], "y_buy": int(eps > 0 and ratio < 6),
               "p_sell": pred["p_above_6_5_positive"], "y_sell": int(eps > 0 and ratio > 6.5),
               "p_loss": pred["p_loss"], "y_loss": int(eps <= 0),
               "forecast_mode": pred.get("forecast_mode", "see_model"),
               "synthetic": snapshot.get("synthetic", False)}
        # Strict inequality: date-only labels from the same date may arrive after decision time.
        matured = [r for r in records if r["label_available_at"] < row["as_of"]]
        row["benchmark_training_n"] = len(matured)
        for event in ("buy", "sell", "loss"):
            row["baseline_" + event] = (sum(r["y_" + event] for r in matured) + 1) / (len(matured) + 2)
        records.append(row)
    metrics = {}
    for event in ("buy", "sell", "loss"):
        metrics[event] = probability_metrics([r["p_"+event] for r in records], [r["y_"+event] for r in records])
        comparable = [r for r in records if r["benchmark_training_n"] >= 30]
        metrics[event + "_comparison_model"] = probability_metrics([r["p_"+event] for r in comparable], [r["y_"+event] for r in comparable])
        metrics[event + "_comparison_causal_base_rate"] = probability_metrics([r["baseline_"+event] for r in comparable], [r["y_"+event] for r in comparable])
    return {"input_snapshots": len(snapshots), "scored": len(records), "excluded": excluded,
            "universe_coverage_by_date": coverage,
            "missing_member_snapshots": sum(len(c["missing_symbols"]) for c in coverage),
            "expected_member_snapshots_at_supplied_dates": sum(c["expected_count"] for c in coverage),
            "metrics": metrics, "records": records, "evaluation_date": evaluation_date,
            "distinct_decision_dates": len({r["as_of"] for r in records}),
            "limitations": ["Probability scoring only: does not test trading returns or AI value added.",
                            "Overlapping six-year horizons and same-date stocks are dependent; sample count is not independent N.",
                            "Availability fields enforce declared chronology, not authenticity of vendor point-in-time history.",
                            "Exclude counts must be investigated, especially delistings; never interpret missing outcomes as successes.",
                            "Current model parameters are fixed heuristics; no historical fitting or calibration is claimed."]}
