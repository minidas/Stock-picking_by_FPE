"""Six-year earnings scenario model; probabilities are uncalibrated, not confidence levels.

No network or stochastic simulation is used. All forecasts are conditional on the
supplied, dated snapshot. Loss-making outcomes have a separate probability mass.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
from urllib.parse import urlparse

import numpy as np
from scipy.stats import norm


class ModelError(ValueError):
    """An input violates the model's temporal, unit, or numerical contract."""


DEFAULT_CONFIG = {
    "long_run_log_growth": 0.04,
    "history_shrinkage_years": 6.0,
    "min_annual_log_sigma": 0.20,
    "max_annual_log_sigma": 0.80,
    "persistent_log_sigma": 0.08,
    "year_correlation": 0.35,
    "consensus_weight_floor": 0.70,
    "consensus_weight_cap": 0.95,
    "consensus_prior_analysts": 2.0,
    "growth_fade_years": 2.0,
    "loss_prior_probability": 0.06,
    "loss_prior_strength": 10.0,
    "buy_probability": 0.70,
    "sell_probability": 0.80,
    "buy_fpe_threshold": 6.0,
    "sell_fpe_threshold": 6.5,
    "max_buy_loss_probability": 0.10,
    "sell_loss_probability": 0.25,
    "max_price_age_days": 7,
    "max_eps_age_days": 550,
    "max_consensus_age_days": 180,
    "event_window_days": 180,
    "allow_history_only_candidates": False,
}


def _date(value, name):
    try:
        # Date-only snapshots deliberately cannot distinguish intraday disclosures.
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{name} must be an ISO date YYYY-MM-DD") from exc


def _number(value, name, positive=False):
    if isinstance(value, bool):
        raise ModelError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ModelError(f"{name} must be finite" + (" and positive" if positive else ""))
    return result


def _year_after(day, n):
    try:
        return day.replace(year=day.year + n)
    except ValueError:  # Leap-day six-year target is February 28.
        return day.replace(year=day.year + n, day=28)


def _config(overrides):
    result = dict(DEFAULT_CONFIG)
    if overrides:
        unknown = set(overrides) - set(result)
        if unknown:
            raise ModelError(f"Unknown configuration keys: {sorted(unknown)}")
        result.update(overrides)
    for key, value in result.items():
        if key == "allow_history_only_candidates":
            if not isinstance(value, bool):
                raise ModelError(f"{key} must be boolean")
            continue
        result[key] = _number(value, key)
    for key in ("history_shrinkage_years", "min_annual_log_sigma", "max_annual_log_sigma",
                "persistent_log_sigma", "consensus_prior_analysts", "growth_fade_years",
                "loss_prior_strength", "max_price_age_days", "max_eps_age_days",
                "max_consensus_age_days", "event_window_days",
                "buy_fpe_threshold", "sell_fpe_threshold"):
        if result[key] <= 0:
            raise ModelError(f"{key} must be positive")
    for key in ("year_correlation", "consensus_weight_floor", "consensus_weight_cap",
                "loss_prior_probability", "buy_probability", "sell_probability",
                "max_buy_loss_probability", "sell_loss_probability"):
        if not 0 <= result[key] < 1:
            raise ModelError(f"{key} must lie in [0, 1)")
    if result["consensus_weight_floor"] > result["consensus_weight_cap"]:
        raise ModelError("Consensus weight floor exceeds cap")
    if result["min_annual_log_sigma"] > result["max_annual_log_sigma"]:
        raise ModelError("Annual volatility floor exceeds cap")
    if result["buy_fpe_threshold"] >= result["sell_fpe_threshold"]:
        raise ModelError("buy_fpe_threshold must be strictly below sell_fpe_threshold")
    if not -0.25 <= result["long_run_log_growth"] <= 0.25:
        raise ModelError("long_run_log_growth must lie in [-0.25, .25]")
    return result


def _check_basis(row, snapshot):
    for key in ("currency", "eps_basis", "share_basis"):
        if key in row and row[key] != snapshot[key]:
            raise ModelError(f"Incompatible {key}: {row[key]!r} vs {snapshot[key]!r}")


def _history(snapshot, asof, flags):
    by_period = {}
    for row in snapshot.get("history", []):
        available = _date(row.get("available_at"), "history.available_at")
        period = _date(row.get("period_end"), "history.period_end")
        if available > asof:
            flags.add("future_history_excluded")
            continue
        if period > available or period > asof:
            raise ModelError("Historical EPS period cannot follow its availability date")
        _check_basis(row, snapshot)
        eps = _number(row.get("eps"), "history.eps")
        previous = by_period.get(period)
        if previous is None or available > previous[0]:
            by_period[period] = (available, eps)
        elif available == previous[0] and eps != previous[1]:
            raise ModelError("Conflicting historical EPS records with identical dates")
    rows = [(p, x[1]) for p, x in sorted(by_period.items())][-11:]
    rates = []
    for (d0, e0), (d1, e1) in zip(rows, rows[1:]):
        dt = (d1 - d0).days / 365.2425
        if 0.75 <= dt <= 1.25 and e0 > 0 and e1 > 0:
            rates.append(float(np.clip(math.log(e1 / e0) / dt, -1, 1)))
    return rows, np.array(rates, dtype=float)


def _cumulative_variance(covariance, durations):
    weighted = covariance * np.outer(durations, durations)
    return np.array([weighted[:i, :i].sum() for i in range(1, 7)])


def _refresh(model):
    """Refresh transparent yearly summaries after an event changes parameters."""
    mean = np.array(model["annual_log_growth_mean"])
    cov = np.array(model["annual_log_growth_covariance"])
    duration = np.array(model["interval_years"])
    cumvar = _cumulative_variance(cov, duration)
    cummean = np.cumsum(mean * duration)
    model["terminal_log_eps_mean"] = float(math.log(model["eps0"]) + cummean[-1])
    model["terminal_log_eps_variance"] = float(cumvar[-1])
    model["annual"] = []
    for i in range(6):
        v = float(cov[i, i])
        model["annual"].append({
            "year": i + 1,
            "target_date": model["annual_target_dates"][i],
            "interval_years": float(duration[i]),
            "annualized_log_growth_mean": float(mean[i]),
            "annualized_log_growth_variance": v,
            "annualized_growth_mean": math.expm1(float(mean[i]) + v / 2),
            "annualized_growth_variance": math.expm1(v) * math.exp(2 * float(mean[i]) + v),
            "positive_conditional_eps_mean": model["eps0"] * math.exp(float(cummean[i] + cumvar[i] / 2)),
        })
    return model


def build_baseline(snapshot: dict, config: dict | None = None) -> dict:
    """Construct an auditable baseline using only data available on ``as_of``.

    Institution EPS level anchors are shrunk toward historical growth; beyond
    their final target the last growth rate fades toward a long-run prior. EPS
    and price must use the same dated share basis. All EPS observations must use
    one accounting basis. Date-only availability means end-of-day observations;
    a tradable backtest must execute no earlier than the following session.
    """
    cfg = _config(config)
    required = ("symbol", "as_of", "price", "price_date", "eps0", "eps_period_end",
                "eps_available_at", "currency", "eps_basis", "share_basis")
    missing = [k for k in required if k not in snapshot]
    if missing:
        raise ModelError(f"Missing snapshot fields: {missing}")
    if not str(snapshot["symbol"]).strip():
        raise ModelError("symbol must be nonempty")
    for key in ("eps_basis", "share_basis"):
        if not isinstance(snapshot[key], str) or not snapshot[key].strip():
            raise ModelError(f"{key} must be a nonempty, explicit basis label")
    if snapshot["currency"] != "USD":
        raise ModelError("This implementation requires USD EPS and price")
    asof = _date(snapshot["as_of"], "as_of")
    price_date = _date(snapshot["price_date"], "price_date")
    period = _date(snapshot["eps_period_end"], "eps_period_end")
    available = _date(snapshot["eps_available_at"], "eps_available_at")
    if price_date > asof or available > asof or period > available:
        raise ModelError("Price or base EPS is unavailable at the snapshot date")
    if (asof - price_date).days > cfg["max_price_age_days"]:
        raise ModelError("Price is stale")
    if (asof - period).days > cfg["max_eps_age_days"]:
        raise ModelError("Base EPS period is stale")
    price = _number(snapshot["price"], "price", positive=True)
    eps0 = _number(snapshot["eps0"], "eps0")
    flags = set()
    model = {
        "model_version": "1.1.0", "symbol": str(snapshot["symbol"]),
        "as_of": asof.isoformat(), "target_date": _year_after(asof, 6).isoformat(),
        "price": price, "price_date": price_date.isoformat(), "eps0": eps0,
        "eps_period_end": period.isoformat(), "eps_available_at": available.isoformat(),
        "currency": snapshot["currency"], "eps_basis": snapshot["eps_basis"],
        "share_basis": snapshot["share_basis"], "config": cfg,
        "probability_status": "uncalibrated_model_probability",
        "status": "ready", "events": [],
    }
    if eps0 <= 0:
        return dict(model, status="abstain", reason="nonpositive_base_eps", flags=[])
    history, rates = _history(snapshot, asof, flags)
    # Median is insensitive to isolated restructuring/rebound years. Annual
    # growth rates are winsorized, but no observed loss is turned into growth.
    sample_weight = len(rates) / (len(rates) + cfg["history_shrinkage_years"])
    hmean = sample_weight * (float(np.median(rates)) if len(rates) else 0.0)
    hmean += (1 - sample_weight) * cfg["long_run_log_growth"]
    sigma = max(cfg["min_annual_log_sigma"], float(np.std(rates, ddof=1)) if len(rates) >= 3 else 0.0)
    sigma = min(sigma, cfg["max_annual_log_sigma"])
    if len(rates) < 3:
        flags.add("sparse_earnings_history")
    losses = sum(e <= 0 for _, e in history)
    p_loss = (losses + cfg["loss_prior_probability"] * cfg["loss_prior_strength"]) / (len(history) + cfg["loss_prior_strength"])
    if not history:
        flags.add("loss_probability_prior_only")
    targets = [_year_after(asof, i) for i in range(1, 7)]
    times = np.array([(t - period).days / 365.2425 for t in targets])
    durations = np.diff(np.r_[0.0, times])
    anchors = {}
    for row in snapshot.get("consensus", []):
        published = _date(row.get("available_at"), "consensus.available_at")
        target = _date(row.get("target_date"), "consensus.target_date")
        if published > asof:
            flags.add("future_consensus_excluded")
            continue
        if target <= asof:
            flags.add("nonforward_consensus_excluded")
            continue
        if (asof - published).days > cfg["max_consensus_age_days"]:
            flags.add("stale_consensus_excluded")
            continue
        if "eps_basis" not in row or "share_basis" not in row:
            raise ModelError("Consensus must state eps_basis and share_basis explicitly")
        _check_basis(row, snapshot)
        eps = _number(row.get("eps_mean"), "consensus.eps_mean")
        n = _number(row.get("analysts"), "consensus.analysts", positive=True)
        if int(n) != n:
            raise ModelError("consensus.analysts must be an integer")
        if eps <= 0:
            return dict(model, status="abstain", reason="nonpositive_consensus_eps", flags=sorted(flags))
        deviation = row.get("eps_std")
        if deviation is not None:
            deviation = _number(deviation, "consensus.eps_std")
            if deviation < 0:
                raise ModelError("consensus.eps_std must be nonnegative")
        previous = anchors.get(target)
        record = (published, eps, n, deviation)
        if previous is None or published > previous[0]:
            anchors[target] = record
        elif published == previous[0] and record != previous:
            raise ModelError("Conflicting consensus records with identical dates")
    anchor_times, anchor_levels, weights = [0.0], [0.0], []
    for target, (published, eps, n, deviation) in sorted(anchors.items()):
        t = (target - period).days / 365.2425
        # Broker dispersion is not sampling error: NEVER divide it by sqrt(n).
        if deviation is not None:
            sigma = max(sigma, math.sqrt(math.log1p((deviation / eps) ** 2) / t))
        else:
            flags.add("missing_analyst_dispersion")
        weight = float(np.clip(n / (n + cfg["consensus_prior_analysts"]), cfg["consensus_weight_floor"], cfg["consensus_weight_cap"]))
        anchor_times.append(t)
        anchor_levels.append(weight * math.log(eps / eps0) + (1 - weight) * hmean * t)
        weights.append({"target_date": target.isoformat(), "available_at": published.isoformat(), "weight": weight, "analysts": int(n)})
    if not weights:
        flags.add("history_only_no_institution_consensus")
    last_rate = hmean if not weights else (anchor_levels[-1] - anchor_levels[-2]) / (anchor_times[-1] - anchor_times[-2])
    last_rate = float(np.clip(last_rate, -0.50, 0.50))
    expected_log_levels = []
    for t in times:
        if t <= anchor_times[-1]:
            level = float(np.interp(t, anchor_times, anchor_levels))
        else:
            extra = t - anchor_times[-1]
            fade = cfg["growth_fade_years"]
            level = anchor_levels[-1] + cfg["long_run_log_growth"] * extra
            level += (last_rate - cfg["long_run_log_growth"]) * fade * (1 - math.exp(-extra / fade))
        expected_log_levels.append(level)
    # Rate errors integrate over unequal first interval. The persistent term
    # represents a common error in the long-run growth thesis, not six iid draws.
    indices = np.arange(6)
    corr = cfg["year_correlation"] ** np.abs(indices[:, None] - indices[None, :])
    covariance = sigma ** 2 * corr / np.sqrt(np.outer(durations, durations))
    covariance += cfg["persistent_log_sigma"] ** 2
    cumvar = _cumulative_variance(covariance, durations)
    log_means = np.diff(np.r_[0.0, np.array(expected_log_levels) - 0.5 * cumvar]) / durations
    model.update({
        "annual_target_dates": [t.isoformat() for t in targets],
        "interval_years": durations.tolist(),
        "annual_log_growth_mean": log_means.tolist(),
        "annual_log_growth_covariance": covariance.tolist(),
        "loss_probability": float(p_loss), "history_growth_observations": len(rates),
        "historical_log_growth_center": float(hmean), "annual_log_sigma": float(sigma),
        "consensus_anchors": weights, "flags": sorted(flags),
    })
    return _refresh(model)


def evaluate(model: dict) -> dict:
    """Return fixed and configurable ratio-event probabilities and review flags.

    Numeric-suffixed fields retain their original 6/6.5 meanings regardless of
    configuration. Candidate selection uses the explicit threshold-named fields.
    """
    basic = {k: model[k] for k in ("symbol", "as_of", "target_date", "status", "probability_status")}
    basic["flags"] = list(model.get("flags", []))
    cfg = model["config"]
    # Preserve compatibility with baseline JSON archived before custom cutoffs.
    buy_threshold = cfg.get("buy_fpe_threshold", 6.0)
    sell_threshold = cfg.get("sell_fpe_threshold", 6.5)
    basic["buy_fpe_threshold"] = buy_threshold
    basic["sell_fpe_threshold"] = sell_threshold
    if model["status"] != "ready":
        return dict(basic, candidate="abstain", reason=model.get("reason"),
                    p_below_6=None, p_above_6_5_positive=None, p_loss=None,
                    p_between_6_6_5=None, p_risk_or_above_6_5=None,
                    p_below_buy_threshold=None, p_above_sell_threshold_positive=None,
                    p_between_thresholds=None, p_risk_or_above_sell_threshold=None)
    mu = model["terminal_log_eps_mean"]
    variance = model["terminal_log_eps_variance"]
    sd = math.sqrt(variance)
    loss = model["loss_probability"]
    positive = 1 - loss
    low_z = (math.log(model["price"] / 6.5) - mu) / sd
    high_z = (math.log(model["price"] / 6.0) - mu) / sd
    cheap = positive * float(norm.sf(high_z))
    expensive = positive * float(norm.cdf(low_z))
    middle = max(0.0, positive - cheap - expensive)
    configured_cheap = positive * float(norm.sf((math.log(model["price"] / buy_threshold) - mu) / sd))
    configured_expensive = positive * float(norm.cdf((math.log(model["price"] / sell_threshold) - mu) / sd))
    configured_middle = max(0.0, positive - configured_cheap - configured_expensive)
    candidate = "none"
    if model["consensus_anchors"] or cfg["allow_history_only_candidates"]:
        if configured_cheap >= cfg["buy_probability"] and loss <= cfg["max_buy_loss_probability"]:
            candidate = "buy_review"
        elif configured_expensive >= cfg["sell_probability"] or loss >= cfg["sell_loss_probability"]:
            candidate = "sell_review"
    # Loss EPS severity is not modelled; only positive-regime EPS quantiles and
    # mean are meaningful. Do not label these unconditional earnings forecasts.
    basic.update({
        "candidate": candidate, "p_below_6": cheap,
        "p_above_6_5_positive": expensive, "p_loss": loss,
        "p_between_6_6_5": middle, "p_risk_or_above_6_5": expensive + loss,
        "p_below_buy_threshold": configured_cheap,
        "p_above_sell_threshold_positive": configured_expensive,
        "p_between_thresholds": configured_middle,
        "p_risk_or_above_sell_threshold": configured_expensive + loss,
        "positive_conditional_eps_mean": math.exp(mu + variance / 2),
        "positive_conditional_eps_median": math.exp(mu),
        "positive_conditional_eps_interval_90": [math.exp(mu + sd * norm.ppf(q)) for q in (0.05, 0.95)],
        "positive_conditional_ratio_median": model["price"] / math.exp(mu),
        "positive_conditional_ratio_interval_90": [model["price"] / math.exp(mu + sd * norm.ppf(q)) for q in (0.95, 0.05)],
        "annual": deepcopy(model["annual"]),
        "event_count": len(model.get("events", [])),
    })
    return basic


def apply_events(baseline: dict, events: list[dict]) -> dict:
    """Apply a bounded, cited scenario adjustment only to a preliminary candidate.

    Each event requires event_id, published_at, source_url, summary, rationale,
    already_in_consensus, confidence, annual_log_growth_delta (six values),
    variance_multiplier [1,3], and loss_probability_delta [-.1,.2]. The program
    validates structure and limits, not the truth of the evidence or reasoning.
    """
    if not isinstance(events, list):
        raise ModelError("events must be a list")
    if not events:
        return deepcopy(baseline)
    if baseline.get("events"):
        raise ModelError("Apply all events once to the original baseline; stacking is forbidden")
    if evaluate(baseline)["candidate"] not in ("buy_review", "sell_review"):
        raise ModelError("AI events are permitted only for preliminary screened candidates")
    asof = _date(baseline["as_of"], "as_of")
    total_delta = np.zeros(6)
    variance_multiplier, loss_delta = 1.0, 0.0
    seen, audit = set(), []
    for event in events:
        needed = {"event_id", "published_at", "source_url", "summary", "rationale", "already_in_consensus",
                  "confidence", "annual_log_growth_delta", "variance_multiplier", "loss_probability_delta"}
        if needed - set(event):
            raise ModelError(f"Missing event fields: {sorted(needed - set(event))}")
        if not isinstance(event["event_id"], str) or not event["event_id"].strip() or event["event_id"] in seen:
            raise ModelError("Event IDs must be nonempty and unique")
        seen.add(event["event_id"])
        published = _date(event["published_at"], "event.published_at")
        if published > asof or (asof - published).days > baseline["config"]["event_window_days"]:
            raise ModelError("Event is future-dated or outside the recent-event window")
        parsed = urlparse(str(event["source_url"]))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ModelError("Event source_url must be an HTTP(S) evidence URL")
        if any(not isinstance(event[k], str) or not event[k].strip() for k in ("summary", "rationale")):
            raise ModelError("Events need a summary and an incremental-impact rationale")
        if not isinstance(event["already_in_consensus"], bool):
            raise ModelError("already_in_consensus must be boolean")
        confidence = _number(event["confidence"], "event.confidence")
        multiplier = _number(event["variance_multiplier"], "event.variance_multiplier")
        loss = _number(event["loss_probability_delta"], "event.loss_probability_delta")
        delta = event["annual_log_growth_delta"]
        if not isinstance(delta, list) or len(delta) != 6:
            raise ModelError("annual_log_growth_delta must contain six annual adjustments")
        delta = np.array([_number(x, "event.annual_log_growth_delta") for x in delta])
        if not 0 <= confidence <= 1 or not 1 <= multiplier <= 3 or not -0.1 <= loss <= 0.2 or np.any(np.abs(delta) > 0.10):
            raise ModelError("Event exceeds confidence, drift, variance, or loss adjustment limits")
        if event["already_in_consensus"] and (np.any(delta) or multiplier != 1 or loss != 0):
            raise ModelError("Already-priced consensus events must have zero incremental adjustments")
        total_delta += delta * confidence
        variance_multiplier *= multiplier ** confidence
        loss_delta += loss * confidence
        audit.append(deepcopy(event))
    if np.any(np.abs(total_delta) > 0.20) or variance_multiplier > 3 + 1e-12 or not -0.10 <= loss_delta <= 0.25:
        raise ModelError("Combined events exceed aggregate scenario limits")
    result = deepcopy(baseline)
    duration = np.array(result["interval_years"])
    covariance = np.array(result["annual_log_growth_covariance"])
    new_covariance = covariance * variance_multiplier
    # An uncertainty-only change must not inflate expected EPS via Jensen's
    # inequality. Adjust log means to preserve the conditional arithmetic mean.
    change = _cumulative_variance(new_covariance, duration) - _cumulative_variance(covariance, duration)
    # Events affect the six future years, never the already elapsed anchor stub.
    future_edges = [asof] + [_date(t, "annual_target_date") for t in result["annual_target_dates"]]
    future_duration = np.array([(b - a).days / 365.2425 for a, b in zip(future_edges, future_edges[1:])])
    means = np.array(result["annual_log_growth_mean"]) + total_delta * future_duration / duration
    means -= 0.5 * np.diff(np.r_[0, change]) / duration
    result["annual_log_growth_mean"] = means.tolist()
    result["annual_log_growth_covariance"] = new_covariance.tolist()
    result["loss_probability"] = float(np.clip(result["loss_probability"] + loss_delta, 0.01, 0.95))
    result["events"] = audit
    result["flags"] = sorted(set(result["flags"]) | {"ai_adjusted_uncalibrated_scenario"})
    return _refresh(result)
