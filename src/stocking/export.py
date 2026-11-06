"""Complete per-security exports with explicit risk exclusions and missing rows.

These exports are a data product, not the preliminary AI review queue. A stock
does not need a buy/sell signal or analyst coverage to remain in the main file.
The caller supplies normalized inputs in each result's ``input`` field; legacy
results with only a model remain readable, but cannot recover missing raw data.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import csv
import json
import math
from pathlib import Path

from .data import DataError


PROBABILITY_FIELDS = (
    "p_below_buy_threshold", "p_above_sell_threshold_positive", "p_between_thresholds",
    "p_risk_or_above_sell_threshold", "p_below_6", "p_above_6_5_positive",
    "p_between_6_6_5", "p_risk_or_above_6_5", "p_loss",
)
ANNUAL_FIELDS = (
    "target_date", "interval_years", "annualized_log_growth_mean",
    "annualized_log_growth_variance", "annualized_growth_mean",
    "annualized_growth_variance", "positive_conditional_eps_mean",
)
CSV_FIELDS = [
    "symbol", "company_name", "cik", "as_of", "synthetic", "export_status", "risk_assessment",
    "exclusion_reasons", "candidate", "event_review", "error", "model_reason",
    "price", "price_date", "price_kind", "currency", "eps0", "unreviewed_annual_eps", "eps_measure", "eps_period_start", "eps_period_end", "eps_available_at",
    "eps_basis", "share_basis", "target_date", "model_version", "probability_status",
    "buy_fpe_threshold", "sell_fpe_threshold", *PROBABILITY_FIELDS,
    "positive_conditional_eps_mean", "positive_conditional_eps_median",
    "positive_conditional_eps_p05", "positive_conditional_eps_p95",
    "positive_conditional_ratio_median", "positive_conditional_ratio_p05",
    "positive_conditional_ratio_p95", "history_growth_observations", "annual_log_sigma",
    "consensus_anchor_count", "flags_json", "source_json", "history_json", "consensus_json",
    "covariance_json", "baseline_prediction_json", "events_json", "download_status_json",
    "price_evidence_json", "annual_eps_evidence_json", "older_eps_observations_json", "raw_eps_split_observations_json",
] + [f"year_{year}_{field}" for year in range(1, 7) for field in ANNUAL_FIELDS]


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mapping(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DataError(f"{label} must be an object")
    return value


def _classify(original, limit):
    row = deepcopy(original)
    snapshot = _mapping(row.get("input"), "result.input")
    model = _mapping(row.get("model"), "result.model")
    prediction = _mapping(row.get("prediction"), "result.prediction")
    # Presence in the normalized input takes precedence over legacy model data.
    eps = snapshot.get("eps0", model.get("eps0"))
    p_loss = prediction.get("p_loss")
    reason = prediction.get("reason", model.get("reason"))
    reasons = []
    if (_finite_number(eps) and eps <= 0) or reason == "nonpositive_base_eps":
        reasons.append("nonpositive_current_eps")
    if reason == "nonpositive_consensus_eps":
        reasons.append("nonpositive_consensus_eps")
    loss_known = _finite_number(p_loss) and 0 <= p_loss <= 1
    if loss_known and p_loss >= limit:
        reasons.append("loss_probability_at_or_above_limit")
    if str(snapshot.get("risk_status", "")).strip().lower() == "high":
        reasons.append("explicit_high_risk")
    # Invalid or missing model data are visible, not reclassified as risk-free.
    status = prediction.get("status", model.get("status"))
    available = loss_known and status == "ready" and not row.get("error")
    row["export_status"] = "excluded" if reasons else ("available" if available else "data_unavailable")
    row["risk_assessment"] = "assessed" if reasons or available else "not_assessed"
    row["exclusion_reasons"] = reasons
    row["input_available"] = bool(snapshot)
    return row


def create_exports(results, coverage, *, max_loss_probability=.25):
    """Partition a one-date stock batch without hiding non-candidates or gaps.

    Exclusions: current EPS <= 0, model-abstained nonpositive consensus EPS,
    ``p_loss >= max_loss_probability``, or normalized ``risk_status='high'``.
    Future/stale consensus rows are not inspected directly: their eligibility
    has already been decided by the dated model. Coverage-missing symbols are
    retained as main-table placeholders with no fabricated probabilities.
    """
    if not _finite_number(max_loss_probability) or not 0 < max_loss_probability <= 1:
        raise DataError("max_loss_probability must be a finite number in (0, 1]")
    if not isinstance(results, list):
        raise DataError("results must be a list")
    coverage = deepcopy(_mapping(coverage, "coverage"))
    identities, dates = set(), set()
    stocks, excluded = [], []
    for original in results:
        if not isinstance(original, dict):
            raise DataError("Each result must be an object")
        symbol, as_of = original.get("symbol"), original.get("as_of")
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(as_of, str) or not as_of:
            raise DataError("Each result requires symbol and as_of")
        if symbol in identities:
            raise DataError(f"Duplicate security in one-date export: {symbol}")
        for label in ("input", "model", "prediction"):
            part = _mapping(original.get(label), f"result.{label}")
            if part.get("symbol", symbol) != symbol or part.get("as_of", as_of) != as_of:
                raise DataError(f"Result identity differs from {label}")
        identities.add(symbol)
        dates.add(as_of)
        row = _classify(original, max_loss_probability)
        (excluded if row["exclusion_reasons"] else stocks).append(row)
    if len(dates) > 1:
        raise DataError("A stock export requires one as_of date")
    as_of = next(iter(dates), coverage.get("as_of"))
    if coverage.get("as_of") is not None and coverage["as_of"] != as_of:
        raise DataError("Coverage and result as_of dates differ")
    missing = coverage.get("missing_symbols", [])
    if not isinstance(missing, list) or any(not isinstance(s, str) or not s.strip() for s in missing):
        raise DataError("coverage.missing_symbols must be a list of security identifiers")
    if len(set(missing)) != len(missing) or set(missing) & identities:
        raise DataError("Missing-symbol coverage conflicts with supplied results")
    if missing and not as_of:
        raise DataError("Missing-member placeholders require a coverage as_of date")
    for symbol in missing:
        stocks.append({
            "symbol": symbol, "as_of": as_of, "input_available": False,
            "export_status": "data_unavailable", "risk_assessment": "not_assessed",
            "exclusion_reasons": [], "error": "missing_input_snapshot",
            "event_review": "not_available", "prediction": {
                "status": "data_unavailable", "candidate": "abstain",
                "reason": "missing_input_snapshot", **{key: None for key in PROBABILITY_FIELDS},
            },
        })
    stocks.sort(key=lambda row: row["symbol"])
    excluded.sort(key=lambda row: row["symbol"])
    summary = {
        "input_result_count": len(results), "exported_stock_count": len(stocks),
        "available_stock_count": sum(r["export_status"] == "available" for r in stocks),
        "data_unavailable_stock_count": sum(r["export_status"] == "data_unavailable" for r in stocks),
        "excluded_stock_count": len(excluded), "missing_input_stock_count": len(missing),
        "risk_not_assessed_count": sum(r["risk_assessment"] == "not_assessed" for r in stocks),
        "synthetic_stock_count": sum(r.get("synthetic") is True for r in stocks + excluded),
        "membership_coverage_complete": coverage.get("complete") is True,
        "exclusion_reason_counts": dict(sorted(Counter(reason for row in excluded for reason in row["exclusion_reasons"]).items())),
    }
    result = {
        "schema_version": 1, "scope": "individual_stocks", "as_of": as_of,
        "coverage": coverage, "summary": summary,
        "policy": {"max_loss_probability": max_loss_probability,
                   "exclude_nonpositive_current_eps": True, "exclude_nonpositive_consensus_eps": True,
                   "exclude_explicit_high_risk": True, "include_non_candidates": True,
                   "include_missing_data_placeholders": True,
                   "note": "Missing data are not evidence of low risk. Probabilities remain uncalibrated model outputs."},
        "stocks": stocks, "excluded_stocks": excluded,
    }
    # Preflight the complete product before any file writes; reject NaN and
    # unserializable data instead of producing half-valid result artifacts.
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise DataError("Export contains non-finite or non-JSON values") from exc
    return result


def _json_cell(value):
    return "" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _csv_row(row):
    snapshot = row.get("input") or {}
    model = row.get("model") or {}
    prediction = row.get("prediction") or {}
    flat = {key: row.get(key) for key in ("symbol", "company_name", "cik", "as_of", "synthetic", "export_status", "risk_assessment", "event_review", "error")}
    for key in ("price", "price_date", "price_kind", "currency", "eps0", "unreviewed_annual_eps", "eps_measure", "eps_period_start", "eps_period_end", "eps_available_at", "eps_basis", "share_basis"):
        flat[key] = snapshot.get(key, model.get(key))
    for key in ("target_date", "model_version", "history_growth_observations", "annual_log_sigma"):
        flat[key] = model.get(key, prediction.get(key))
    for key in ("candidate", "probability_status", "buy_fpe_threshold", "sell_fpe_threshold",
                "positive_conditional_eps_mean", "positive_conditional_eps_median",
                "positive_conditional_ratio_median", *PROBABILITY_FIELDS):
        flat[key] = prediction.get(key)
    flat["model_reason"] = prediction.get("reason", model.get("reason"))
    flat["exclusion_reasons"] = ";".join(row.get("exclusion_reasons", []))
    flat["consensus_anchor_count"] = len(model["consensus_anchors"]) if "consensus_anchors" in model else None
    for name in ("eps", "ratio"):
        bounds = prediction.get(f"positive_conditional_{name}_interval_90")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
            flat[f"positive_conditional_{name}_p05"], flat[f"positive_conditional_{name}_p95"] = bounds
    for field, value in {
        "flags_json": prediction.get("flags", model.get("flags")), "source_json": snapshot.get("source", row.get("source")),
        "history_json": snapshot.get("history"), "consensus_json": snapshot.get("consensus"),
        "covariance_json": model.get("annual_log_growth_covariance"),
        "baseline_prediction_json": row.get("baseline_prediction"), "events_json": model.get("events"),
        "download_status_json": row.get("download_status"), "price_evidence_json": row.get("price_evidence"),
        "annual_eps_evidence_json": row.get("annual_eps_evidence"),
        "older_eps_observations_json": row.get("older_filing_eps_observations"),
        "raw_eps_split_observations_json": row.get("raw_eps_and_split_observations"),
    }.items():
        flat[field] = _json_cell(value)
    for annual in prediction.get("annual", model.get("annual", [])):
        if annual.get("year") in range(1, 7):
            for field in ANNUAL_FIELDS:
                flat[f"year_{annual['year']}_{field}"] = annual.get(field)
    # A ticker or source text can otherwise become a spreadsheet formula when
    # the CSV is opened in Excel. Numeric negatives remain numeric and intact.
    return {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            for key, value in flat.items()}


def write_exports(output_dir, results, coverage, max_loss_probability=.25):
    """Write the four stock artifacts and return counts plus relative filenames."""
    product = create_exports(results, coverage, max_loss_probability=max_loss_probability)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    metadata = {key: value for key, value in product.items() if key not in ("stocks", "excluded_stocks")}
    files = []
    for stem, key in (("all_stocks", "stocks"), ("excluded_stocks", "excluded_stocks")):
        json_path, csv_path = root / f"{stem}.json", root / f"{stem}.csv"
        json_path.write_text(json.dumps({**metadata, key: product[key]}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in product[key]:
                writer.writerow(_csv_row(row))
        files.extend([json_path.name, csv_path.name])
    return {**product["summary"], "output_files": files}
