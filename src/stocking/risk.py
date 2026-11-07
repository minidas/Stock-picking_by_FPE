"""Versioned admission rules, separate from the uncalibrated EPS model.

These rules decide whether valuation may be calculated. They do not change the
loss prior or turn a short earnings history into a calibrated risk estimate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math

from .data import DataError


DEFAULT_RISK_POLICY = {"version": 2, "max_loss_probability": .10, "recent_positive_years": 3}
LEGACY_RISK_POLICY = {"version": 1, "max_loss_probability": .25, "recent_positive_years": 0}


def normalize_risk_policy(policy=None, *, legacy=False, max_loss_probability=None):
    result = deepcopy(LEGACY_RISK_POLICY if legacy else DEFAULT_RISK_POLICY)
    if policy is not None:
        if not isinstance(policy, dict) or set(policy) - set(result):
            raise DataError("risk_policy must contain only version, max_loss_probability, recent_positive_years")
        result.update(policy)
    if max_loss_probability is not None:
        result["max_loss_probability"] = max_loss_probability
    version, years = result["version"], result["recent_positive_years"]
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
        raise DataError("risk_policy.version must be 1 or 2")
    if isinstance(years, bool) or not isinstance(years, int) or years != (3 if version == 2 else 0):
        raise DataError("risk_policy requires 3 recent positive years in version 2, or 0 in legacy version 1")
    limit = result["max_loss_probability"]
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or not 0 < limit <= 1:
        raise DataError("max_loss_probability must be a finite number in (0, 1]")
    return result


def policy_from_export(export_policy):
    """An archived policy without a versioned child retains its old semantics."""
    export_policy = export_policy or {}
    if "risk_policy" in export_policy:
        return normalize_risk_policy(export_policy["risk_policy"])
    return normalize_risk_policy(legacy=True, max_loss_probability=export_policy.get("max_loss_probability", .25))


def historical_loss_probability(history, config):
    losses = sum(eps <= 0 for _, eps in history)
    return (losses + config["loss_prior_probability"] * config["loss_prior_strength"]) / (len(history) + config["loss_prior_strength"])


def assess_history_risk(history, *, eps0, asof, config, policy, explicit_high=False,
                        p_loss=None, nonpositive_consensus=False):
    """Use only the model's validated, dated and deduplicated annual history."""
    policy = normalize_risk_policy(policy)
    history = list(history)
    prior = historical_loss_probability(history, config)
    probability = prior if p_loss is None else p_loss
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise DataError("risk p_loss must be a finite probability")
    count = policy["recent_positive_years"]
    recent = history[-count:] if count else []
    reasons, missing = [], []
    if eps0 <= 0:
        reasons.append("nonpositive_current_eps")
    if nonpositive_consensus:
        reasons.append("nonpositive_consensus_eps")
    if explicit_high:
        reasons.append("explicit_high_risk")
    if probability >= policy["max_loss_probability"]:
        reasons.append("loss_probability_at_or_above_limit")
    continuous = len(recent) == count and all(.75 <= (b[0] - a[0]).days / 365.2425 <= 1.25
                                              for a, b in zip(recent, recent[1:]))
    latest_age = (asof - history[-1][0]).days if history else None
    known_recent_losses = []
    if count:
        # Do not call a distant loss "recent" just because missing observations
        # made it one of the last three rows. A dated loss inside the potential
        # three-period window still excludes when another year is missing.
        if latest_age is not None and latest_age <= config["max_eps_age_days"]:
            known_recent_losses = [d.isoformat() for d, eps in recent if eps <= 0
                                   and (history[-1][0] - d).days / 365.2425 <= (count - 1) * 1.25]
        if known_recent_losses:
            reasons.append("nonpositive_recent_annual_eps")
        if len(recent) < count:
            missing.append("insufficient_recent_annual_history")
        elif not continuous:
            missing.append("nonconsecutive_recent_annual_history")
        if latest_age is not None and latest_age > config["max_eps_age_days"]:
            missing.append("stale_recent_annual_history")
    status = "excluded" if reasons else ("not_assessed" if missing else "passed")
    return {
        "policy": policy, "status": status, "exclusion_reasons": reasons, "missing_reasons": missing,
        "p_loss": float(probability), "historical_loss_prior": float(prior),
        "annual_observations": len(history), "nonpositive_annual_observations": sum(e <= 0 for _, e in history),
        "observed_nonpositive_fraction": sum(e <= 0 for _, e in history) / len(history) if history else None,
        "annual_history": [{"period_end": d.isoformat(), "eps": e} for d, e in history],
        "recent_annual_history": [{"period_end": d.isoformat(), "eps": e} for d, e in recent],
        "known_recent_nonpositive_periods": known_recent_losses,
        "recent_years_contiguous": continuous, "latest_annual_age_days": latest_age,
        "explicit_high_risk": bool(explicit_high), "nonpositive_consensus": bool(nonpositive_consensus),
        "probability_status": "uncalibrated_terminal_loss_prior",
    }


def recheck_model_risk(model, policy=None):
    """Reapply a frozen policy after an event changes terminal loss risk."""
    previous = model.get("risk_screen")
    if previous is None:
        return None  # Legacy numerical baselines were not screened this way.
    selected = normalize_risk_policy(policy or previous["policy"])
    history = [(date.fromisoformat(r["period_end"]), r["eps"]) for r in previous["annual_history"]]
    return assess_history_risk(history, eps0=model["eps0"], asof=date.fromisoformat(model["as_of"]),
                               config=model["config"], policy=selected,
                               explicit_high=previous.get("explicit_high_risk", False),
                               p_loss=model.get("loss_probability", previous["p_loss"]),
                               nonpositive_consensus=previous.get("nonpositive_consensus", False))
