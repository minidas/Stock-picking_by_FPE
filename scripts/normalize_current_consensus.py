"""Audit archived Yahoo GAAP annual consensus without changing model inputs.

Only earningsTrendGaap annual 0y/+1y estimates are normalized. Requested GAAP
and a historical EPS match are supporting evidence, not a completed review of
dilution, fiscal dates, share class or corporate actions. No model is run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
RETRY_NAME = "network_retry_2026-11-07"
MODULE = "earningsTrendGaap"
PERIODS = ("0y", "+1y")
OUTPUT_NAMES = ("normalized_consensus_observations.json", "coverage_summary.json")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def raw_number(value):
    value = value.get("raw") if isinstance(value, dict) else value
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def utc_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("retrieval_timestamp_requires_timezone")
    return parsed.astimezone(timezone.utc)


def observed_available(retrieved_at):
    return (utc_time(retrieved_at).date() + timedelta(days=1)).isoformat()


def year_ago_comparison(observation, snapshot, as_of):
    evidence = {"status": "not_compared", "full_basis_verified": False,
                "method": "exact_period_end_one_calendar_year_before_as_reported_target",
                "eps_absolute_tolerance": 1e-8,
                "limitations": "Returned target date is not yet an independently verified fiscal end; matching EPS alone does not verify the forecast basis."}
    if observation["period"] != "0y":
        return evidence | {"reason": "next_year_yearAgoEps_may_itself_be_a_forecast"}
    if snapshot is None:
        return evidence | {"reason": "reviewed_actual_snapshot_unavailable"}
    if observation["year_ago_eps_as_reported"] is None:
        return evidence | {"reason": "yearAgoEps_unavailable"}
    try:
        target = date.fromisoformat(observation["as_reported_target_date"])
        prior_end = target.replace(year=target.year - 1).isoformat()
    except (TypeError, ValueError):
        return evidence | {"reason": "exact_prior_calendar_date_unavailable"}
    evidence["candidate_actual_period_end"] = prior_end
    rows = [r for r in snapshot.get("history", []) if r.get("period_end") == prior_end
            and r.get("available_at", "9999-12-31") <= as_of]
    if len(rows) != 1 or raw_number(rows[0].get("eps")) is None:
        return evidence | {"reason": "no_unique_same_period_available_actual"}
    actual = rows[0]
    difference = observation["year_ago_eps_as_reported"] - actual["eps"]
    return evidence | {"status": "matching_supporting_evidence" if abs(difference) <= 1e-8 else "mismatch_requires_review",
                       "actual_eps": actual["eps"], "vendor_year_ago_eps": observation["year_ago_eps_as_reported"],
                       "eps_difference": difference, "actual_period_start": actual.get("period_start"),
                       "actual_period_end": actual["period_end"], "actual_available_at": actual.get("available_at"),
                       "actual_source_url": actual.get("source_url"), "actual_accession": actual.get("accession"),
                       "actual_eps_basis": actual.get("eps_basis"), "actual_share_basis": actual.get("share_basis")}


def verify_sources(record, root=ROOT):
    audits, matched = [], []
    for source in record.get("sources", []):
        audit = deepcopy(source)
        audit.update(sha256_verified=False, response_matches_collected=False)
        try:
            path = (root / source["file"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("source_path_outside_workspace")
            raw = path.read_bytes()
            audit["computed_sha256"] = digest(raw)
            if audit["computed_sha256"] != source.get("sha256"):
                raise ValueError("source_hash_mismatch")
            audit["sha256_verified"] = True
            response = json.loads(raw)
            audit["response_matches_collected"] = response == record.get("response")
            utc_time(source["retrieved_at"])
            if source.get("status") == 200 and audit["response_matches_collected"]:
                matched.append(source["retrieved_at"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            audit["verification_error"] = str(exc)
        audits.append(audit)
    return {"verified_matching_response": bool(matched), "sources": audits,
            "response_retrieved_at": max(matched, key=utc_time) if matched else None}


def parse_collected(record, member, *, as_of, snapshot=None, source_audit=None):
    """Return two explicit annual slots, including missing/incomplete observations."""
    date.fromisoformat(as_of)
    symbol = member["symbol"]
    reasons = []
    if record is None:
        record = {}
        reasons.append("not_collected")
    elif record.get("symbol") != symbol:
        reasons.append("collected_security_identity_mismatch")
    response = record.get("response") or {}
    quote = response.get("quoteSummary", {}) if isinstance(response, dict) else {}
    quote = quote if isinstance(quote, dict) else {}
    results = quote.get("result")
    if quote.get("error"):
        reasons.append("quote_summary_error")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        result = {}
        reasons.append("no_unique_quote_summary_result")
    else:
        result = results[0]
    price = result.get("price") or {}
    if not isinstance(price, dict) or price.get("symbol") != symbol.replace(".", "-"):
        reasons.append("response_security_identity_unverified")
    module = result.get(MODULE)
    if not isinstance(module, dict):
        module = {}
        reasons.append("gaap_module_missing")
    trend = module.get("trend")
    if not isinstance(trend, list):
        trend = []
        reasons.append("gaap_trend_missing")
    source_audit = source_audit or {"verified_matching_response": False, "sources": [], "response_retrieved_at": None}
    retrieved_at = source_audit.get("response_retrieved_at") or record.get("retrieval_finished_at")
    try:
        available_at = observed_available(retrieved_at)
    except (TypeError, ValueError):
        available_at = None
        reasons.append("retrieval_timestamp_missing_or_invalid")
    observations = []
    for period in PERIODS:
        raw_rows = [r for r in trend if isinstance(r, dict) and r.get("period") == period]
        row = raw_rows[0] if len(raw_rows) == 1 else {}
        estimate = row.get("earningsEstimate") or {}
        estimate = estimate if isinstance(estimate, dict) else {}
        target, mean = row.get("endDate"), raw_number(estimate.get("avg"))
        count = raw_number(estimate.get("numberOfAnalysts"))
        analysts = int(count) if count is not None and count > 0 and float(count).is_integer() else None
        currency = estimate.get("earningsCurrency")
        incomplete = list(reasons)
        if len(raw_rows) != 1:
            incomplete.append("annual_period_missing" if not raw_rows else "duplicate_annual_period_ambiguous")
        try:
            parsed_target = date.fromisoformat(target)
            target_is_forward = parsed_target > date.fromisoformat(as_of)
        except (TypeError, ValueError):
            target_is_forward = None
            incomplete.append("reported_target_date_missing_or_invalid")
        if mean is None:
            incomplete.append("eps_mean_missing_or_invalid")
        if analysts is None:
            incomplete.append("analyst_count_missing_or_invalid")
        if currency != "USD":
            incomplete.append("earnings_currency_not_confirmed_usd")
        admission_reasons = ["forecast_dilution_and_accounting_basis_pending_review", "share_class_and_corporate_action_basis_pending_review",
                             "exact_forecast_fiscal_period_pending_review"]
        if not source_audit["verified_matching_response"]:
            admission_reasons.append("archived_response_not_verified")
        if snapshot is None:
            admission_reasons.append("normalized_actual_eps_and_ttm_input_unavailable")
        if available_at is None:
            admission_reasons.append("availability_unknown")
        elif available_at > as_of:
            admission_reasons.append("observation_not_available_at_model_as_of")
        if target_is_forward is False:
            admission_reasons.append("reported_target_not_forward_of_model_as_of")
        if incomplete:
            admission_reasons.append("raw_observation_incomplete")
        observation = {"period": period, "module": MODULE, "as_reported_target_date": target,
                       "as_reported_target_after_model_as_of": target_is_forward,
                       "target_date_verified": False, "eps_mean": mean, "analysts": analysts, "currency": currency,
                       "eps_std": None, "eps_std_status": "not_supplied_not_inferred_from_range",
                       "eps_low_as_reported": raw_number(estimate.get("low")), "eps_high_as_reported": raw_number(estimate.get("high")),
                       "year_ago_eps_as_reported": raw_number(estimate.get("yearAgoEps")),
                       "retrieved_at": retrieved_at, "available_at": available_at, "model_as_of": as_of,
                       "requested_eps_basis": "GAAP", "eps_basis": None, "share_basis": None,
                       "raw_complete": not incomplete, "raw_incomplete_reasons": sorted(set(incomplete)),
                       "model_admitted": False, "model_admission_reasons": admission_reasons,
                       "raw_rows": deepcopy(raw_rows)}
        observation["year_ago_eps_comparison"] = year_ago_comparison(observation, snapshot, as_of)
        observations.append(observation)
    return {"symbol": symbol, "cik": member.get("cik"), "name": member.get("name"),
            "normalized_actual_snapshot_present": snapshot is not None,
            "collection_present": bool(record), "response_present": bool(response),
            "collection_error_type": record.get("error_type"), "rate_limited": record.get("rate_limited"),
            "retrieval_started_at": record.get("retrieval_started_at"), "retrieval_finished_at": record.get("retrieval_finished_at"),
            "modules_requested": record.get("modules_requested", []), "module_used": MODULE,
            "vendor_default_methodology_metadata": module.get("defaultMethodology"),
            "default_methodology_interpretation": "ticker_vendor_default_metadata_not_the_methodology_of_the_explicit_gaap_module",
            "top_level_trend_growth_used": False,
            "raw_modules": {key: deepcopy(result.get(key)) for key in (MODULE, "earningsTrend", "earningsHistoryGaap", "price")},
            "source_audit": source_audit, "observations": observations,
            "raw_complete_annual_count": sum(r["raw_complete"] for r in observations), "model_admitted_annual_count": 0}


def build(study=STUDY, *, input_dir=None, output_dir=None, as_of="2026-11-07", snapshots_path=None):
    study = Path(study).resolve()
    retry = study / RETRY_NAME
    input_dir = Path(input_dir).resolve() if input_dir else retry / "host_network/yahoo"
    output_dir = Path(output_dir).resolve() if output_dir else retry
    if not input_dir.is_relative_to(retry) or not output_dir.is_relative_to(retry):
        raise ValueError("Consensus archives and outputs must remain inside the dedicated network retry directory")
    if any((output_dir / name).exists() for name in OUTPUT_NAMES):
        raise ValueError("Refusing to overwrite existing normalized consensus outputs")
    universe_path = study / "input/universe.json"
    universe_raw = universe_path.read_bytes()
    members = json.loads(universe_raw)["members"]
    if len({r["symbol"] for r in members}) != len(members):
        raise ValueError("Duplicate universe security")
    snapshots_path = Path(snapshots_path) if snapshots_path else study / "normalized_latest_available_reviewed/snapshots.json"
    snapshots_raw = snapshots_path.read_bytes() if snapshots_path.exists() else None
    snapshots = json.loads(snapshots_raw).get("snapshots", []) if snapshots_raw else []
    actuals = {s["symbol"]: s for s in snapshots}
    securities = []
    for member in members:
        path = input_dir / (member["symbol"] + "_collected.json")
        raw = path.read_bytes() if path.exists() else None
        record = None
        parse_error = None
        if raw is not None:
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("collected_record_not_an_object")
            except ValueError as exc:
                record = {"symbol": member["symbol"], "error_type": "invalid_collected_json"}
                parse_error = str(exc)
        source_audit = verify_sources(record or {})
        security = parse_collected(record, member, as_of=as_of, snapshot=actuals.get(member["symbol"]), source_audit=source_audit)
        security["collected_archive"] = {"file": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                                           "sha256": digest(raw) if raw is not None else None, "parse_error": parse_error}
        securities.append(security)
    observations = [row for security in securities for row in security["observations"]]
    metadata = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "model_as_of": as_of,
                "availability_rule": "next_utc_calendar_day_after_archived_response_retrieval",
                "module_used": MODULE, "universe_security_count": len(members),
                "universe_sha256": digest(universe_raw), "actual_snapshots_sha256": digest(snapshots_raw) if snapshots_raw else None,
                "normalizer_sha256": digest(Path(__file__).read_bytes()), "model_inputs_modified": False,
                "limitations": ["This is an observation archive, not model-admitted consensus or a new investment ranking.",
                                "Requested GAAP does not fully verify dilution, share class, corporate actions or exact fiscal dates.",
                                "defaultMethodology is preserved as vendor-default metadata; the explicit GAAP module supplies EPS.",
                                "Top-level trend growth is ignored; EPS mean and analyst count come only from earningsEstimate.",
                                "Historical yearAgoEps matching is supporting evidence only; next-year yearAgoEps can itself be a forecast.",
                                "Estimate standard deviation is unavailable; no dispersion is invented from ranges.",
                                "A November 7 retrieval is available November 8, never backfilled into the November 7 model."]}
    summary = {**metadata, "collected_security_count": sum(s["collection_present"] for s in securities),
               "response_present_security_count": sum(s["response_present"] for s in securities),
               "verified_response_security_count": sum(s["source_audit"]["verified_matching_response"] for s in securities),
               "raw_complete_annual_observation_count": sum(r["raw_complete"] for r in observations),
               "raw_complete_two_year_security_count": sum(s["raw_complete_annual_count"] == 2 for s in securities),
               "raw_complete_one_or_more_year_security_count": sum(s["raw_complete_annual_count"] > 0 for s in securities),
               "model_admitted_annual_observation_count": 0, "model_admitted_security_count": 0,
               "raw_incomplete_reason_counts": dict(Counter(reason for r in observations for reason in r["raw_incomplete_reasons"])),
               "availability_date_counts": dict(Counter(r["available_at"] or "unknown" for r in observations)),
               "current_year_actual_comparison_counts": dict(Counter(s["observations"][0]["year_ago_eps_comparison"]["status"] for s in securities)),
               "missing_or_incomplete_securities": [{"symbol": s["symbol"], "complete_annual_count": s["raw_complete_annual_count"],
                   "period_reasons": {r["period"]: r["raw_incomplete_reasons"] for r in s["observations"] if not r["raw_complete"]}}
                   for s in securities if s["raw_complete_annual_count"] < 2]}
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in zip(OUTPUT_NAMES, ({**metadata, "securities": securities}, summary)):
        with (output_dir / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=STUDY)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--as-of", default="2026-11-07")
    args = parser.parse_args()
    summary = build(args.study, input_dir=args.input_dir, output_dir=args.output_dir, as_of=args.as_of, snapshots_path=args.snapshots)
    print(json.dumps({key: summary[key] for key in ("universe_security_count", "collected_security_count",
        "verified_response_security_count", "raw_complete_annual_observation_count", "raw_complete_two_year_security_count",
        "model_admitted_security_count", "availability_date_counts")}, ensure_ascii=False, indent=2))
