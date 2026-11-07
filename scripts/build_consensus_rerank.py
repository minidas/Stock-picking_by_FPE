"""Assemble a new intraday consensus research run; never run or change a model.

The user requested immediate use of newly retrieved observations. This policy
uses their actual UTC observation day after checking a timestamp cutoff, while
preserving the earlier next-day staging policy in every admitted observation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from normalize_current_consensus import parse_collected, verify_sources, year_ago_comparison, utc_time, raw_number

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
RETRY = "network_retry_2026-11-07"
REVIEWED = "normalized_latest_available_reviewed"
RUN = "consensus_rerank_v5"
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import validate_snapshot
from stocking.fiscal_calendar import (FiscalCalendarError, map_forecast_target, prior_actual_period_for_forecast,
                                     review_calendar)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_file(path, expected_hash):
    path = (ROOT / path).resolve()
    if not path.is_relative_to(ROOT) or not expected_hash or sha(path) != expected_hash:
        raise ValueError("Evidence file missing, changed or outside workspace: " + str(path))
    return {"path": str(path), "sha256": expected_hash}


def apply_reviewed_price(snapshot, review, *, information_cutoff):
    """Use the audited regular-session quote, retaining the frozen numerator."""
    cutoff = utc_time(information_cutoff)
    if (review.get("symbol") != snapshot["symbol"] or not review.get("price_observation_valid")
            or review.get("currency") != snapshot["currency"] or review.get("validation_errors")):
        raise ValueError("Price review identity/currency/validity failure")
    if review.get("old_price") != snapshot["price"] or review.get("old_price_date") != snapshot["price_date"]:
        raise ValueError("Price attribution baseline differs from frozen actual snapshot")
    price = raw_number(review.get("new_price"))
    delay = raw_number(review.get("exchange_data_delayed_by_minutes"))
    if price is None or price <= 0 or delay is None or delay < 0:
        raise ValueError("Reviewed price or delay is invalid")
    retrieved = utc_time(review["retrieved_at"])
    market_time = utc_time(review["regular_market_timestamp"])
    completed = utc_time(review["session_completed_plus15min_at"])
    quote_available = market_time + timedelta(minutes=delay)
    if retrieved > cutoff or max(quote_available, completed) > min(cutoff, retrieved):
        raise ValueError("Quote observation/session/delay lies after information cutoff or retrieval")
    if date.fromisoformat(review["new_price_date"]) > cutoff.date():
        raise ValueError("Future price date")
    source = review["new_source"]
    evidence = [verify_file(source["raw_file"], source["sha256"]),
                verify_file(source["collected_file"], source["collected_sha256"])]
    raw = read(evidence[0]["path"])["quoteSummary"]["result"][0]["price"]
    if (raw.get("symbol") != snapshot["symbol"].replace(".", "-") or raw.get("currency") != snapshot["currency"]
            or raw_number(raw.get("regularMarketPrice")) != price
            or raw_number(raw.get("regularMarketTime")) != market_time.timestamp()):
        raise ValueError("Price review does not match archived regular-session fields")
    updated = deepcopy(snapshot)
    updated["prior_run_price_comparison"] = {"price": snapshot["price"], "price_date": snapshot["price_date"],
                                            "price_kind": snapshot.get("price_kind"), "source": deepcopy(review.get("old_source"))}
    updated.update(price=price, price_date=review["new_price_date"], price_kind=review["new_price_kind"],
                   price_selection_status="reviewed_vendor_completed_regular_session_quote",
                   price_observation_review=deepcopy(review))
    updated.setdefault("source", {})["price"] = deepcopy(source)
    return updated, evidence


def load_ttm_updates(path, base_path, actuals, *, information_cutoff):
    if path is None:
        return {}, [], [], set()
    payload = read(path)
    if payload.get("base_snapshots_sha256") != sha(base_path):
        raise ValueError("TTM update base does not match frozen reviewed actual snapshots")
    validation = payload.get("validation", {})
    if (validation.get("status") != "verified_research_inputs" or not validation.get("rationale")
            or not isinstance(validation.get("evidence_files"), list) or not validation["evidence_files"]):
        raise ValueError("TTM updates require explicit validation and archived evidence")
    files = [{"path": str(Path(path).resolve()), "sha256": sha(path)}]
    files.extend(verify_file(row["path"], row["sha256"]) for row in validation["evidence_files"])
    originals = {row["symbol"]: row for row in actuals}
    cutoff = utc_time(information_cutoff)
    updates, audit = {}, []
    for supplied in payload.get("snapshots", []):
        symbol = supplied.get("symbol")
        if symbol not in originals or symbol in updates:
            raise ValueError("TTM update requires a unique existing reviewed security")
        old = originals[symbol]
        for field in ("as_of", "cik", "currency", "eps_basis", "share_basis", "price", "price_date"):
            if supplied.get(field) != old.get(field):
                raise ValueError("TTM update changes identity/basis or price: " + symbol + " " + field)
        if supplied.get("consensus"):
            raise ValueError("TTM updates must not inject consensus")
        observed = supplied.get("ttm_refresh_source_observations")
        if not isinstance(observed, dict) or not observed:
            raise ValueError("TTM update requires actual source observation timestamps")
        for record in observed.values():
            if (record.get("symbol") != symbol or record.get("cik") != supplied["cik"]
                    or utc_time(record["retrieved_at"]) > cutoff or record.get("status") != 200):
                raise ValueError("TTM source identity/status or observation cutoff invalid")
            files.append(verify_file(record["file"], record["sha256"]))
        verified = validate_snapshot(supplied)
        if verified.get("eps_measure") != "ttm" or verified["eps_period_end"] <= old["eps_period_end"]:
            raise ValueError("TTM update must advance the validated actual period")
        updates[symbol] = verified
        audit.append({"symbol": symbol, "before": deepcopy(old), "after": deepcopy(verified), "validation": deepcopy(validation),
                      "annual_history_updated": old.get("history") != verified.get("history"),
                      "source_observations_verified_by_information_cutoff": information_cutoff})
    unavailable = payload.get("unavailable_symbols", [])
    if (not isinstance(unavailable, list) or any(not isinstance(symbol, str) for symbol in unavailable)
            or len(set(unavailable)) != len(unavailable) or set(unavailable) - set(originals) or set(unavailable) & set(updates)):
        raise ValueError("Unavailable TTM updates must name unique, unreplaced existing securities")
    return updates, files, audit, set(unavailable)


def latest_actual_guard(snapshot, security, information_cutoff):
    """A newly returned actual quarter is a freshness signal, never a TTM sum."""
    cutoff = utc_time(information_cutoff)
    observed_at = security.get("source_audit", {}).get("response_retrieved_at")
    if observed_at and utc_time(observed_at) > cutoff:
        return {"module": "earningsHistoryGaap", "latest_returned_actual_period_end": None,
                "selected_ttm_period_end": snapshot["eps_period_end"], "period_gap_days": None,
                "blocks_old_ttm": False, "source_observation_after_cutoff_excluded": True,
                "quarterly_eps_summed_or_substituted": False}
    module = security.get("raw_modules", {}).get("earningsHistoryGaap") or {}
    periods = []
    for row in module.get("history", []):
        epoch = raw_number(row.get("quarter"))
        eps = raw_number(row.get("epsActual"))
        if epoch is None or eps is None or row.get("currency") != "USD":
            continue
        period = datetime.fromtimestamp(epoch, timezone.utc).date()
        if period <= cutoff.date():
            periods.append(period)
    latest = max(periods).isoformat() if periods else None
    gap = (date.fromisoformat(latest) - date.fromisoformat(snapshot["eps_period_end"])).days if latest else None
    return {"module": "earningsHistoryGaap", "latest_returned_actual_period_end": latest,
            "selected_ttm_period_end": snapshot["eps_period_end"], "period_gap_days": gap,
            "blocks_old_ttm": gap is not None and gap > 45, "stale_gap_threshold_days": 45,
            "quarterly_eps_summed_or_substituted": False,
            "limitation": "Actual-quarter dates are freshness evidence; share-weighted quarterly EPS are not substituted for direct TTM."}


def calendar_fiscal_evidence(snapshot, as_of):
    rows = sorted((r for r in snapshot.get("history", []) if r.get("available_at", "9999-12-31") <= as_of),
                  key=lambda r: r["period_end"])[-3:]
    evidence = {"method": "three_consecutive_complete_sec_years_with_fixed_calendar_fiscal_end",
                "supported": False, "future_fiscal_end_fully_verified": False,
                "annual_sources": [{key: r.get(key) for key in ("period_start", "period_end", "available_at", "accession", "source_url")}
                                   for r in rows],
                "limitation": "Uses the vendor's returned date only when it agrees with the issuer's recent SEC calendar-year pattern; does not invent 52/53-week future fiscal dates."}
    if len(rows) != 3:
        return evidence | {"reason": "three_recent_complete_annual_periods_unavailable"}
    try:
        ends = [date.fromisoformat(r["period_end"]) for r in rows]
        starts = [date.fromisoformat(r["period_start"]) for r in rows]
    except (KeyError, TypeError, ValueError):
        return evidence | {"reason": "annual_period_dates_invalid"}
    if (len({(end.month, end.day) for end in ends}) != 1
            or [end.year for end in ends] != list(range(ends[-1].year - 2, ends[-1].year + 1))
            or any((end - start).days + 1 not in (365, 366) for start, end in zip(starts, ends))
            or any(starts[i] != ends[i - 1] + timedelta(days=1) for i in (1, 2))):
        return evidence | {"reason": "variable_week_based_or_unverified_fiscal_calendar"}
    if any(urlsplit(r.get("source_url", "")).hostname not in {"www.sec.gov", "sec.gov"} or not r.get("accession") for r in rows):
        return evidence | {"reason": "official_sec_annual_date_sources_unavailable"}
    return evidence | {"supported": True, "calendar_month_day": ends[-1].strftime("%m-%d"),
                       "latest_annual_period_end": ends[-1].isoformat(),
                       "basis": "research_calendar_date_join_supported_by_archived_sec_annual_periods"}


def fiscal_year_ago_comparison(observation, snapshot, mapping, fiscal, as_of):
    prior_end = prior_actual_period_for_forecast(mapping, fiscal)
    evidence = {"status": "not_compared", "method": "reviewed_calendar_exact_prior_actual_fiscal_period",
                "candidate_actual_period_end": prior_end, "full_basis_verified": False, "eps_absolute_tolerance": 1e-8}
    prior = raw_number(observation.get("year_ago_eps_as_reported"))
    actuals = [row for row in snapshot["history"] if row["period_end"] == prior_end and row["available_at"] <= as_of]
    if prior is None or len(actuals) != 1 or raw_number(actuals[0].get("eps")) is None:
        return evidence | {"reason": "no_unique_same_fiscal_period_actual_and_prior_eps"}
    actual = actuals[0]
    difference = prior - actual["eps"]
    return evidence | {"status": "matching_supporting_evidence" if abs(difference) <= 1e-8 else "mismatch_requires_review",
        "actual_eps": actual["eps"], "vendor_year_ago_eps": prior, "eps_difference": difference,
        "actual_period_start": actual.get("period_start"), "actual_period_end": prior_end,
        "actual_available_at": actual["available_at"], "actual_source_url": actual.get("source_url"),
        "actual_accession": actual.get("accession"), "actual_eps_basis": actual.get("eps_basis"),
        "actual_share_basis": actual.get("share_basis")}


def check_actual_observation_cutoff(value, cutoff):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"retrieved_at", "retrieval_finished_at", "information_cutoff"} and item is not None:
                if utc_time(item) > cutoff:
                    raise ValueError("Actual input observation follows information cutoff")
            elif isinstance(item, (dict, list)):
                check_actual_observation_cutoff(item, cutoff)
    elif isinstance(value, list):
        for item in value:
            check_actual_observation_cutoff(item, cutoff)


def admit_snapshot(snapshot, security, *, as_of, information_cutoff, fiscal_calendar=None, evidence_root=ROOT):
    cutoff = utc_time(information_cutoff)
    if cutoff.date().isoformat() != as_of or snapshot.get("as_of") != as_of:
        raise ValueError("New intraday study and input must share the cutoff UTC date")
    check_actual_observation_cutoff(snapshot, cutoff)
    if snapshot.get("consensus"):
        raise ValueError("Expected the frozen history-only reviewed input")
    result = deepcopy(snapshot)
    fiscal = calendar_fiscal_evidence(snapshot, as_of)
    if fiscal_calendar is not None:
        try:
            fiscal = review_calendar(fiscal_calendar, snapshot, information_cutoff=information_cutoff, evidence_root=evidence_root)
        except FiscalCalendarError as exc:
            fiscal = {"supported": False, "reason": str(exc), "method": "explicit_fiscal_calendar_review_failed"}
    shared = []
    if security.get("symbol") != snapshot["symbol"] or security.get("cik") != snapshot.get("cik"):
        shared.append("security_identity_mismatch")
    if not security.get("source_audit", {}).get("verified_matching_response"):
        shared.append("archived_response_unverified")
    if security.get("module_used") != "earningsTrendGaap":
        shared.append("explicit_gaap_module_required")
    observations = security.get("observations", [])
    mappings = {}
    if fiscal_calendar is not None and fiscal["supported"]:
        for index, observation in enumerate(observations):
            try:
                mappings[index] = map_forecast_target(observation.get("as_reported_target_date"), fiscal)
            except FiscalCalendarError as exc:
                mappings[index] = {"error": str(exc)}
    current = [r for r in observations if r.get("period") == "0y"]
    current_indices = [i for i, row in enumerate(observations) if row.get("period") == "0y"]
    if fiscal_calendar is not None:
        current_mapping = mappings.get(current_indices[0], {}) if len(current_indices) == 1 else {}
        comparison = (fiscal_year_ago_comparison(current[0], snapshot, current_mapping, fiscal, as_of)
                      if current_mapping.get("target_date") else {"status": "not_compared", "reason": "fiscal_mapping_not_validated"})
    else:
        comparison = year_ago_comparison(current[0], snapshot, as_of) if len(current) == 1 else {"status": "not_compared"}
    if comparison.get("status") != "matching_supporting_evidence":
        shared.append("current_year_prior_eps_basis_support_missing_or_mismatched")
    if not fiscal["supported"]:
        shared.append("fiscal_date_requires_independent_review")
    try:
        years = {r["period"]: (mappings[i]["calendar_anchor_year"] if fiscal_calendar is not None
                              else date.fromisoformat(r["as_reported_target_date"]).year)
                 for i, r in enumerate(observations)}
        if len(observations) != 2 or set(years) != {"0y", "+1y"} or years["+1y"] != years["0y"] + 1:
            shared.append("annual_forecast_period_labels_not_consistent")
    except (KeyError, TypeError, ValueError):
        shared.append("annual_forecast_period_labels_not_consistent")
    rows, admitted = [], []
    for index, observation in enumerate(observations):
        reasons = list(shared)
        if not observation.get("raw_complete"):
            reasons.append("raw_annual_observation_incomplete")
        try:
            retrieved = utc_time(observation["retrieved_at"])
            if retrieved > cutoff:
                reasons.append("retrieved_after_information_cutoff")
            available_at = retrieved.date().isoformat()
        except (KeyError, TypeError, ValueError):
            available_at = None
            reasons.append("actual_observation_timestamp_unavailable")
        try:
            target = date.fromisoformat(mappings[index]["target_date"] if fiscal_calendar is not None else observation["as_reported_target_date"])
            if target <= date.fromisoformat(as_of):
                reasons.append("reported_annual_target_already_ended")
            if fiscal_calendar is None and target.strftime("%m-%d") != fiscal.get("calendar_month_day"):
                reasons.append("reported_target_disagrees_with_sec_fiscal_calendar")
        except (KeyError, TypeError, ValueError):
            reasons.append("reported_target_date_invalid")
        if observation.get("currency") != snapshot.get("currency"):
            reasons.append("forecast_currency_mismatch")
        evidence = {"period": observation.get("period"), "as_reported_target_date": observation.get("as_reported_target_date"),
                    "eps_mean": observation.get("eps_mean"), "analysts": observation.get("analysts"),
                    "original_staged_available_at": observation.get("available_at"),
                    "new_observed_available_at": available_at, "retrieved_at": observation.get("retrieved_at"),
                    "admitted": not reasons, "reasons": sorted(set(reasons))}
        if fiscal_calendar is not None:
            evidence["fiscal_calendar_mapping"] = deepcopy(mappings.get(index, {"error": fiscal.get("reason")}))
        rows.append(evidence)
        if reasons:
            continue
        admitted.append({"target_date": target.isoformat(), "as_reported_target_date": observation["as_reported_target_date"],
                         "available_at": available_at, "original_staged_available_at": observation["available_at"],
                         "retrieved_at": observation["retrieved_at"], "information_cutoff": information_cutoff,
                         "availability_policy": "observed_by_decision_timestamp",
                         "eps_mean": observation["eps_mean"], "eps_std": None, "analysts": observation["analysts"],
                         "currency": observation["currency"], "eps_basis": snapshot["eps_basis"], "share_basis": snapshot["share_basis"],
                         "source": "Yahoo earningsTrendGaap earningsEstimate", "source_module": "earningsTrendGaap",
                         "source_period": observation["period"], "source_evidence": deepcopy(security["source_audit"]),
                         "collected_archive": deepcopy(security.get("collected_archive")),
                         "vendor_default_methodology_metadata": security.get("vendor_default_methodology_metadata"),
                         "basis_join_status": "research_supported_not_fully_verified", "full_economic_basis_verified": False,
                         "year_ago_eps_support": deepcopy(comparison), "fiscal_date_support": deepcopy(fiscal),
                         "raw_annual_observations": deepcopy(observation.get("raw_rows", []))})
        if fiscal_calendar is not None:
            admitted[-1]["fiscal_calendar_mapping"] = deepcopy(mappings[index])
    result["consensus"] = admitted
    group = "consensus_research_join" if admitted else "history_only_pending_consensus_review"
    result["consensus_rerank_group"] = group
    result["information_cutoff"] = information_cutoff
    result["consensus_join_full_economic_basis_verified"] = False
    audit = {"symbol": snapshot["symbol"], "group": group, "admitted_annual_count": len(admitted),
             "fiscal_date_evidence": fiscal, "year_ago_eps_support": comparison, "observations": rows,
             "negative_or_zero_admitted_eps_count": sum(r["eps_mean"] <= 0 for r in admitted),
             "price_unchanged": True, "price": snapshot.get("price"), "price_date": snapshot.get("price_date"),
             "ttm_and_annual_history_unchanged": True, "full_economic_basis_verified": False}
    return result, audit


def build(study=STUDY, *, information_cutoff=None, as_of="2026-11-07", output_dir=None, price_review=None, ttm_updates=None,
          actuals=None, consensus_observations=None, collected_dir=None, fiscal_calendars=None):
    study = Path(study).resolve()
    cutoff = utc_time(information_cutoff) if information_cutoff else datetime.now(timezone.utc)
    if cutoff > datetime.now(timezone.utc) or cutoff.date().isoformat() != as_of:
        raise ValueError("Information cutoff must be observed already and on the new study UTC date")
    information_cutoff = cutoff.isoformat()
    destination = Path(output_dir).resolve() if output_dir else study / RUN / "input"
    if (destination == study or not destination.is_relative_to(study) or destination == study / "input"
            or any(part in {".git", ".agents", ".codex"} for part in destination.relative_to(study).parts)):
        raise ValueError("New inputs require a dedicated study subdirectory, never the frozen universe input")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Refusing to overwrite existing consensus rerank inputs")
    actuals_overridden = actuals is not None
    actual_path = Path(actuals).resolve() if actuals else study / REVIEWED / "snapshots.json"
    staged_path = Path(consensus_observations).resolve() if consensus_observations else study / RETRY / "normalized_consensus_observations.json"
    collected_root = Path(collected_dir).resolve() if collected_dir else None
    if collected_root is not None and not collected_root.is_relative_to(study):
        raise ValueError("Collected archive directory must remain inside the study")
    universe_path = study / "input/universe.json"
    actuals, staged, universe = read(actual_path)["snapshots"], read(staged_path), read(universe_path)
    if ((not actuals_overridden and staged.get("actual_snapshots_sha256") != sha(actual_path))
            or staged.get("universe_sha256") != sha(universe_path)):
        raise ValueError("Staged source cohort hashes differ from the reviewed inputs")
    members = {r["symbol"]: r for r in universe["members"]}
    if (len({r["symbol"] for r in actuals}) != len(actuals)
            or any(r["symbol"] not in members or r.get("cik") != members[r["symbol"]].get("cik") for r in actuals)):
        raise ValueError("Actual inputs must have unique identities in the supplied security universe")
    securities = {r["symbol"]: r for r in staged["securities"]}
    if len(securities) != len(staged["securities"]) or set(securities) != set(members):
        raise ValueError("Staging must contain exactly the frozen full security universe")
    manifest = [{"path": str(p), "sha256": sha(p)} for p in (actual_path, staged_path, universe_path, Path(__file__).resolve(),
                                                            ROOT / "scripts/normalize_current_consensus.py", ROOT / "src/stocking/fiscal_calendar.py")]
    calendars = {}
    if fiscal_calendars is not None:
        calendar_path = Path(fiscal_calendars).resolve()
        payload = read(calendar_path)
        if payload.get("schema_version") != 1 or not isinstance(payload.get("calendars"), dict):
            raise ValueError("Fiscal calendar file requires schema_version 1 and calendars mapping")
        calendars = payload["calendars"]
        if set(calendars) - set(members):
            raise ValueError("Fiscal calendar configuration includes unknown securities")
        manifest.append({"path": str(calendar_path), "sha256": sha(calendar_path)})
    price_path = Path(price_review) if price_review else study / RUN / "price_review.json"
    price_payload = read(price_path)
    prices = price_payload["prices"]
    if {row["symbol"] for row in actuals} - set(prices):
        raise ValueError("Price review must cover every selected actual security")
    manifest.append({"path": str(price_path.resolve()), "sha256": sha(price_path)})
    ttm_replacements, ttm_evidence, ttm_audit, unavailable_updates = load_ttm_updates(ttm_updates, actual_path, actuals,
                                                                                 information_cutoff=information_cutoff)
    manifest.extend(ttm_evidence)
    snapshots, audits = [], []
    for actual in actuals:
        original = actual
        actual = ttm_replacements.get(actual["symbol"], actual)
        security = securities[actual["symbol"]]
        archive = security["collected_archive"]
        path = (collected_root / (actual["symbol"] + "_collected.json") if collected_root else ROOT / archive["file"]).resolve()
        if not path.is_relative_to(study) or sha(path) != archive["sha256"]:
            raise ValueError("Collected consensus archive changed: " + actual["symbol"])
        collected = read(path)
        sources = verify_sources(collected)
        if not sources["verified_matching_response"]:
            raise ValueError("Original consensus response failed verification: " + actual["symbol"])
        parsed = parse_collected(collected, members[actual["symbol"]], as_of=as_of, snapshot=actual, source_audit=sources)
        raw_fields = lambda observations: [{key: value for key, value in row.items() if key not in {"year_ago_eps_comparison", "model_admission_reasons"}}
                                           for row in observations]
        if raw_fields(parsed["observations"]) != raw_fields(security["observations"]):
            raise ValueError("Staged observations no longer match archived response: " + actual["symbol"])
        parsed["collected_archive"] = deepcopy(archive)
        manifest.append({"path": str(path), "sha256": sha(path)})
        for source in sources["sources"]:
            if source.get("sha256_verified"):
                manifest.append({"path": str((ROOT / source["file"]).resolve()), "sha256": source["sha256"]})
        updated, audit = admit_snapshot(actual, parsed, as_of=as_of, information_cutoff=information_cutoff,
                                        fiscal_calendar=calendars.get(actual["symbol"]))
        for source in audit["fiscal_date_evidence"].get("evidence", []):
            manifest.append({"path": source["source_file"], "sha256": source["sha256"]})
        freshness = latest_actual_guard(actual, parsed, information_cutoff)
        audit["actual_period_freshness"] = freshness
        if freshness["blocks_old_ttm"] or actual["symbol"] in unavailable_updates:
            reason = "newer_actual_period_without_valid_current_ttm" if freshness["blocks_old_ttm"] else "new_actual_ttm_refresh_unavailable"
            audit.update(group="data_unavailable_current_ttm_required", admitted_annual_count=0,
                         negative_or_zero_admitted_eps_count=0, excluded_from_model_input=True, reason=reason)
            for observation in audit["observations"]:
                observation["admitted"] = False
                observation["reasons"] = sorted(set(observation["reasons"] + [reason]))
            audits.append(audit)
            continue
        updated, quote_files = apply_reviewed_price(updated, prices[actual["symbol"]], information_cutoff=information_cutoff)
        updated = validate_snapshot(updated)
        manifest.extend(quote_files)
        audit.update(price_unchanged=False, previous_price=original["price"], previous_price_date=original["price_date"],
                     price=updated["price"], price_date=updated["price_date"], ttm_updated=actual["symbol"] in ttm_replacements,
                     annual_history_updated=actual.get("history") != original.get("history"),
                     ttm_and_annual_history_unchanged=actual["symbol"] not in ttm_replacements)
        snapshots.append(updated)
        audits.append(audit)
    policy = {"version": 1, "as_of": as_of, "information_cutoff": information_cutoff,
              "availability_policy": "observed_by_decision_timestamp",
              "authorization": "User explicitly requested reranking now using the newly retrieved consensus.",
              "scope": "A new observed-information research run, not a historical backtest and not backfill of a frozen run.",
              "prior_staging_policy_preserved": "next_utc_day stored per row as original_staged_available_at",
              "price_policy": "reviewed_completed_regular_session_vendor_quote_with_prior_snapshot_price_comparison",
              "date_admission": "Preserve fixed calendar dates; optional reviewed issuer fiscal rules may map forecast month-end labels after three complete actual years and dated source evidence validate. Never relabel TTM actual periods.",
              "explicit_fiscal_calendar_symbols": sorted(calendars),
              "actuals_sha256": sha(actual_path), "original_staged_actuals_sha256": staged.get("actual_snapshots_sha256"),
              "explicit_actual_input_override": actuals_overridden,
              "basis_admission": "Explicit GAAP module plus exact same-period prior-year EPS match is research join support, not full economic verification.",
              "ranking_separation": "Only consensus_research_join securities belong in the institutional-consensus ranking; history-only records stay separate.",
              "risk_policy_changed": False, "negative_forecasts": "Retained for the existing strict risk screen; never removed to rescue a security.",
              "actual_freshness": "Any returned GAAP actual quarter more than 45 days beyond the selected TTM endpoint requires a validated update; failed refreshes are omitted for CLI full-universe missing placeholders.",
              "model_or_old_outputs_changed": False, "uses_test_data": False}
    summary = {"as_of": as_of, "information_cutoff": information_cutoff, "universe_security_count": len(members),
               "reviewed_actual_snapshot_count": len(actuals), "assembled_model_input_count": len(snapshots),
               "missing_actual_snapshot_count": len(members) - len(snapshots),
               "consensus_research_join_security_count": sum(a["admitted_annual_count"] > 0 for a in audits),
               "history_only_security_count": sum(a["group"] == "history_only_pending_consensus_review" for a in audits),
               "admitted_annual_observation_count": sum(a["admitted_annual_count"] for a in audits),
               "updated_ttm_security_count": len(ttm_replacements), "updated_ttm_symbols": sorted(ttm_replacements),
               "new_price_date_counts": dict(Counter(row["price_date"] for row in snapshots)),
               "nonpositive_admitted_forecast_security_count": sum(a["negative_or_zero_admitted_eps_count"] > 0 for a in audits),
               "calendar_fiscal_date_supported_count": sum(a["fiscal_date_evidence"]["supported"] for a in audits),
               "reviewed_fiscal_calendar_admitted_observation_count": sum(r["admitted"] and "fiscal_calendar_mapping" in r for a in audits for r in a["observations"]),
               "changed_forecast_target_date_count": sum(r["admitted"] and r.get("fiscal_calendar_mapping", {}).get("mapping_applied", False) for a in audits for r in a["observations"]),
               "excluded_observation_reason_counts": dict(Counter(reason for a in audits for r in a["observations"] for reason in r["reasons"])),
               "history_only_symbols": [a["symbol"] for a in audits if a["group"] == "history_only_pending_consensus_review"],
               "newly_unavailable_ttm_symbols": [a["symbol"] for a in audits if a.get("excluded_from_model_input")],
               "screen_or_adjust_executed": False, "old_outputs_modified": False}
    destination.mkdir(parents=True, exist_ok=True)
    values = {"snapshots.json": {"snapshots": snapshots}, "admission_audit.json": {"policy": policy, "summary": summary, "securities": audits},
              "policy.json": policy, "summary.json": summary, "ttm_update_audit.json": {"replacements": ttm_audit},
              "source_manifest.json": {"files": list({r["path"]: r for r in manifest}.values())}}
    for name, value in values.items():
        with (destination / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=STUDY)
    parser.add_argument("--information-cutoff", help="Actual observed UTC timestamp; defaults to invocation time")
    parser.add_argument("--as-of", default="2026-11-07")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--price-review", type=Path, help="Defaults to consensus_rerank_v5/price_review.json")
    parser.add_argument("--ttm-updates", type=Path, help="Separately validated newer actual snapshots with evidence hashes")
    parser.add_argument("--actuals", type=Path, help="Explicit newly reviewed actual snapshots; original staging hash remains in audit")
    parser.add_argument("--consensus-observations", type=Path, help="Full-universe staged consensus observation archive")
    parser.add_argument("--collected-dir", type=Path, help="Directory of original SYMBOL_collected.json files")
    parser.add_argument("--fiscal-calendars", type=Path, help="Evidence-backed reviewed issuer fiscal calendar configurations")
    args = parser.parse_args()
    print(json.dumps(build(args.study, information_cutoff=args.information_cutoff, as_of=args.as_of, output_dir=args.output_dir,
                           price_review=args.price_review, ttm_updates=args.ttm_updates, actuals=args.actuals,
                           consensus_observations=args.consensus_observations, collected_dir=args.collected_dir,
                           fiscal_calendars=args.fiscal_calendars),
                     ensure_ascii=False, indent=2))
