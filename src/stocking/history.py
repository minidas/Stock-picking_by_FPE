"""Extend annual EPS under an explicit, unverified as-reported assumption.

Equal overlapping EPS permits a research join; it does not verify corporate
actions, accounting comparability or share classes. No split ratio is inferred.
The current annual history has priority, and the separate TTM anchor is never
changed. Callers retain raw filings and explicitly approve issuer identities.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
from urllib.parse import urlparse

from .model import _history
from .ttm import FULL_YEAR_DAYS, TTMError, validate_annual_history

EPS_TAGS = {"EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"}
JOIN_ASSUMPTION = "overlap_consistent_unverified_assumption"


class HistoryError(ValueError):
    """An extension request has an invalid or ambiguous contract."""


def _date(value, name):
    if not isinstance(value, str):
        raise HistoryError(f"{name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoryError(f"{name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise HistoryError(f"{name} must be YYYY-MM-DD")
    return parsed


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HistoryError(f"{name} must be a finite number")
    return float(value)


def _context(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise HistoryError("context must be an object")
    # Other fields, such as dimension_status, are explanatory metadata.
    return {key: value.get(key) or "" for key in ("segments", "coreg")}


def _url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _same(a, b):
    # Rounding is not a license to reconcile materially different observations.
    return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)


def _mapping(mapping, snapshot, asof):
    if not isinstance(mapping, dict):
        raise HistoryError("share_basis_mapping_must_be_an_object")
    factor = _number(mapping.get("eps_multiplier"), "mapping.eps_multiplier")
    if factor <= 0:
        raise HistoryError("share_basis_mapping_multiplier_must_be_positive")
    if mapping.get("target_share_basis") != snapshot.get("share_basis"):
        raise HistoryError("share_basis_mapping_target_mismatch")
    if not _url(mapping.get("source_url")):
        raise HistoryError("share_basis_mapping_missing_evidence_url")
    published = _date(mapping.get("available_at"), "mapping.available_at")
    if published > asof:
        raise HistoryError("future_share_basis_mapping_evidence")
    tolerance = _number(mapping.get("reviewed_overlap_tolerance", 0.0), "mapping.reviewed_overlap_tolerance")
    if tolerance < 0:
        raise HistoryError("reviewed_overlap_tolerance_must_be_nonnegative")
    explanation = mapping.get("reviewed_overlap_tolerance_rationale")
    if tolerance and (not isinstance(explanation, str) or not explanation.strip()):
        raise HistoryError("reviewed_overlap_tolerance_requires_a_rounding_bound_rationale")
    return factor, published, tolerance, explanation


def _prepare_filing(filing, snapshot, cutoff, first_year, last_period, bridge):
    """Normalize one whole filing or leave it pending; never select a conflict."""
    if filing.get("identity_join_allowed") is not True:
        raise HistoryError("issuer_identity_not_explicitly_approved")
    if not snapshot.get("cik") or filing.get("cik") != snapshot["cik"]:
        raise HistoryError("issuer_cik_mismatch")
    if filing.get("eps_basis") != snapshot.get("eps_basis"):
        raise HistoryError("accounting_basis_mismatch")
    context = _context(filing.get("context"))
    if context != _context(snapshot.get("context")):
        raise HistoryError("share_class_or_coregistrant_context_mismatch")
    if filing.get("conflicts"):
        raise HistoryError("source_filing_has_unresolved_conflicts")
    if not _url(filing.get("source_url")):
        raise HistoryError("source_filing_missing_evidence_url")
    publication = _date(filing.get("available_at"), "filing.available_at")
    if publication > cutoff:
        raise HistoryError("future_filing_or_restatement")
    mapped = filing.get("already_mapped")
    if mapped is not None and bridge is not None:
        raise HistoryError("duplicate_share_basis_mapping")
    factor, mapping_publication, mapping_kind = 1.0, publication, "same_basis_unverified_assumption"
    tolerance, tolerance_rationale = 0.0, None
    mapping_evidence = None
    if mapped is not None:
        factor, mapping_publication, tolerance, tolerance_rationale = _mapping(mapped, snapshot, cutoff)
        mapping_kind, mapping_evidence = "already_mapped_by_caller_no_second_multiplication", deepcopy(mapped)
        multiplier_to_apply = 1.0
    elif bridge is not None:
        factor, mapping_publication, tolerance, tolerance_rationale = _mapping(bridge, snapshot, cutoff)
        mapping_kind, mapping_evidence = "explicit_evidenced_eps_multiplier", deepcopy(bridge)
        multiplier_to_apply = factor
    else:
        multiplier_to_apply = 1.0
        if filing.get("share_basis") != snapshot.get("share_basis"):
            raise HistoryError("share_basis_mismatch_without_reviewed_mapping")
    rows = filing.get("history")
    if not isinstance(rows, list) or not rows:
        raise HistoryError("source_filing_has_no_annual_history")
    selected, raw_observations, out_of_window = {}, [], []
    for raw in rows:
        if not isinstance(raw, dict):
            raise HistoryError("annual_history_row_must_be_an_object")
        if raw.get("measure", raw.get("eps_measure", "annual")) != "annual":
            raise HistoryError("source_history_contains_nonannual_measure")
        if "measure" in raw and "eps_measure" in raw and raw["measure"] != raw["eps_measure"]:
            raise HistoryError("source_history_has_conflicting_measure_labels")
        tag = raw.get("tag", filing.get("tag"))
        if tag not in EPS_TAGS:
            raise HistoryError("source_history_not_diluted_eps")
        raw_unit = raw.get("unit_as_reported", raw.get("unit", filing.get("unit")))
        unit = str(raw_unit or "").lower().replace(" ", "")
        if unit not in {"usd", "usd/shares", "usd/share", "usd_per_share"}:
            raise HistoryError("source_history_eps_unit_not_supported")
        if "qtrs" in raw and str(raw["qtrs"]) != "4":
            raise HistoryError("source_history_duration_not_annual")
        row_context = raw.get("context")
        if row_context is None:
            row_context = {key: raw.get(key, context[key]) for key in ("segments", "coreg")}
        if _context(row_context) != context:
            raise HistoryError("source_row_context_differs_from_selected_filing_context")
        if raw.get("eps_basis", filing["eps_basis"]) != snapshot.get("eps_basis"):
            raise HistoryError("source_row_accounting_basis_mismatch")
        if raw.get("currency", "USD") != "USD":
            raise HistoryError("source_row_currency_mismatch")
        if "source_url" in raw and not _url(raw["source_url"]):
            raise HistoryError("source_row_evidence_url_invalid")
        period = _date(raw.get("period_end"), "history.period_end")
        row_publication = _date(raw.get("available_at", filing["available_at"]), "history.available_at")
        if period > row_publication:
            raise HistoryError("source_period_follows_its_publication")
        financial_publication = max(publication, row_publication)
        if financial_publication > cutoff:
            raise HistoryError("future_history_observation_or_restatement")
        value = _number(raw.get("eps"), "history.eps")
        if "period_start" in raw:
            start = _date(raw["period_start"], "history.period_start")
            if (period - start).days + 1 not in FULL_YEAR_DAYS:
                raise HistoryError("source_history_period_not_a_full_year")
        elif raw.get("measure") == "annual" or raw.get("eps_measure") == "annual":
            raise HistoryError("explicit_annual_history_missing_period_start")
        transformed = value * multiplier_to_apply
        if not math.isfinite(transformed):
            raise HistoryError("mapped_eps_not_finite")
        previous = selected.get(period)
        if previous is not None:
            if not _same(previous["eps"], transformed):
                raise HistoryError("conflicting_same_period_eps_within_filing")
            continue
        item = deepcopy(raw)
        item.update(eps=transformed, period_end=period.isoformat(),
                    available_at=max(financial_publication, mapping_publication).isoformat(),
                    eps_basis=snapshot["eps_basis"], share_basis=snapshot["share_basis"],
                    currency="USD", share_basis_verified=False,
                    source_url=raw.get("source_url", filing["source_url"]), accession=filing["accession"])
        item["history_extension_provenance"] = {
            "method": JOIN_ASSUMPTION, "source_accession": filing["accession"],
            "source_financial_available_at": financial_publication.isoformat(),
            "source_eps_before_module_mapping": value, "source_unit_as_reported": raw_unit,
            "legacy_usd_unit_interpreted_as_per_share": unit == "usd",
            "annual_duration_from_upstream_history_contract_only": "period_start" not in raw and "qtrs" not in raw,
            "context": context, "mapping_kind": mapping_kind,
            "declared_eps_multiplier": factor, "multiplier_applied_by_this_module": multiplier_to_apply,
            "reviewed_overlap_tolerance": tolerance,
            "reviewed_overlap_tolerance_rationale": tolerance_rationale,
            "mapping_evidence": mapping_evidence,
            "filing_native_share_basis": filing.get("native_share_basis", filing.get("share_basis")),
            "share_basis_verified": False,
        }
        selected[period] = item
        raw_observations.append(deepcopy(raw))
        if period.year < first_year or period > last_period:
            out_of_window.append(period.isoformat())
    return selected, out_of_window, publication, tolerance, tolerance_rationale


def extend_annual_history(snapshot, candidate_filings, *, as_of=None, max_years=11, reviewed_bridges=None):
    """Return ``(history, audit)`` without changing snapshot, filings or TTM.

    Filings require exact CIK, identity_join_allowed=True, canonical eps_basis,
    share_basis, context, accession, available_at, source_url and annual history.
    The annual history contract is supplied upstream; explicit period starts
    or qtrs are validated when present. Legacy EPS ``USD`` units are retained
    and interpreted as per-share only for the two supported diluted EPS tags.

    Each accepted filing must overlap the currently connected annual chain,
    with no conflicting overlap. Chains are retried as earlier years connect.
    Evidence-based multipliers are explicit; equal overlaps never establish
    verified share bases. The window uses the latest *annual* history year,
    not the current TTM period, and includes at most eleven calendar years.
    """
    if not isinstance(snapshot, dict) or not isinstance(candidate_filings, list):
        raise HistoryError("snapshot must be an object and candidate_filings a list")
    if isinstance(max_years, bool) or not isinstance(max_years, int) or not 1 <= max_years <= 11:
        raise HistoryError("max_years must be an integer from 1 to 11")
    cutoff = _date(as_of or snapshot.get("as_of"), "as_of")
    if snapshot.get("as_of") and cutoff.isoformat() != snapshot["as_of"]:
        raise HistoryError("as_of must match the snapshot decision date")
    for field in ("eps_basis", "share_basis"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise HistoryError(f"snapshot requires {field}")
    try:
        validate_annual_history(snapshot, cutoff)
    except TTMError as exc:
        raise HistoryError(str(exc)) from exc
    current, future_current = {}, []
    for row in snapshot.get("history", []):
        publication = _date(row.get("available_at"), "current_history.available_at")
        if publication > cutoff:
            future_current.append(deepcopy(row))
            continue
        period = _date(row.get("period_end"), "current_history.period_end")
        if period > publication:
            raise HistoryError("Current history period follows publication")
        value = _number(row.get("eps"), "current_history.eps")
        for field in ("eps_basis", "share_basis"):
            if row.get(field, snapshot[field]) != snapshot[field]:
                raise HistoryError(f"Current history {field} mismatch")
        previous = current.get(period)
        if previous is None or publication > _date(previous["available_at"], "available_at"):
            current[period] = deepcopy(row)
        elif publication.isoformat() == previous["available_at"] and not _same(value, previous["eps"]):
            raise HistoryError("Conflicting current history at the same period and publication")
    bridge_by_accession = {}
    for bridge in reviewed_bridges or []:
        accession = bridge.get("source_accession")
        if not accession or accession in bridge_by_accession:
            raise HistoryError("Reviewed bridges require unique source_accession")
        bridge_by_accession[accession] = bridge
    records, seen_accessions = [], set()
    for filing in candidate_filings:
        if not isinstance(filing, dict) or not filing.get("accession"):
            raise HistoryError("Each candidate filing requires an accession")
        if filing["accession"] in seen_accessions:
            raise HistoryError("Duplicate candidate accession; consolidate raw duplicates upstream")
        seen_accessions.add(filing["accession"])
        records.append({"source_accession": filing["accession"], "source_filing": deepcopy(filing),
                        "status": "pending", "reasons": [], "added_periods": []})
    if not current:
        for record in records:
            record["reasons"] = ["no_current_annual_history_to_anchor_overlap_or_window"]
        return [], {"method": JOIN_ASSUMPTION, "as_of": cutoff.isoformat(), "max_years": max_years,
                    "share_basis_verified": False, "history_count": 0, "history_growth_observations": 0,
                    "filing_audit": records, "pending_filings": [{"source_accession": r["source_accession"], "reasons": r["reasons"]} for r in records]}
    last_period = max(current)
    first_year = last_period.year - max_years + 1
    old_current = [deepcopy(value) for period, value in current.items() if period.year < first_year]
    current = {period: value for period, value in current.items() if period.year >= first_year}
    original_periods = set(current)
    prepared = []
    for record in records:
        filing = record["source_filing"]
        try:
            values, out_of_window, publication, tolerance, rationale = _prepare_filing(
                filing, snapshot, cutoff, first_year, last_period, bridge_by_accession.get(filing["accession"]))
        except (HistoryError, TypeError) as exc:
            record["reasons"] = [str(exc)]
            continue
        record["outside_window_periods"] = out_of_window
        record["reviewed_overlap_tolerance"] = tolerance
        record["reviewed_overlap_tolerance_rationale"] = rationale
        prepared.append((publication, record, values))
    # Date-only availability cannot resolve different values reported by two
    # filings on the same date. Do not let input order choose an EPS vintage.
    same_day_groups = {}
    for _, record, values in prepared:
        for period, value in values.items():
            vintage = value["history_extension_provenance"]["source_financial_available_at"]
            same_day_groups.setdefault((period, vintage), []).append((record, value["eps"]))
    ambiguous_accessions = set()
    for (period, vintage), observations in same_day_groups.items():
        if len(observations) < 2 or all(_same(observations[0][1], value) for _, value in observations[1:]):
            continue
        conflict = {"period_end": period.isoformat(), "source_financial_available_at": vintage,
                    "candidates": sorted([{"source_accession": record["source_accession"], "eps": value}
                                          for record, value in observations], key=lambda row: row["source_accession"])}
        for record, _ in observations:
            ambiguous_accessions.add(record["source_accession"])
            record["reasons"] = ["same_day_cross_filing_eps_conflict"]
            record.setdefault("same_day_conflicts", []).append(deepcopy(conflict))
    prepared = [item for item in prepared if item[1]["source_accession"] not in ambiguous_accessions]
    # Prefer recent publication vintages; exact-equivalent date ties use a
    # stable accession order only after differing same-day values are rejected.
    prepared.sort(key=lambda value: (value[0], value[1]["source_accession"]), reverse=True)
    pending = prepared
    while pending:
        next_pending, progress = [], False
        for publication, record, values in pending:
            overlap = sorted(set(values) & set(current))
            tolerance = record["reviewed_overlap_tolerance"]
            comparisons = [{"period_end": period.isoformat(), "current_eps": current[period]["eps"],
                            "candidate_eps": values[period]["eps"],
                            "absolute_difference": abs(current[period]["eps"] - values[period]["eps"]),
                            "reviewed_overlap_tolerance": tolerance,
                            "requires_reviewed_rounding_tolerance": not _same(current[period]["eps"], values[period]["eps"])}
                           for period in overlap]
            conflicts = [{"period_end": period.isoformat(), "current_eps": current[period]["eps"],
                          "candidate_eps": values[period]["eps"]} for period in overlap
                         if not _same(current[period]["eps"], values[period]["eps"])
                         and abs(current[period]["eps"] - values[period]["eps"]) > tolerance + 1e-12]
            record["overlap_periods"] = [period.isoformat() for period in overlap]
            record["overlap_comparisons"] = comparisons
            if conflicts:
                record["reasons"] = ["overlapping_annual_eps_conflict_no_ratio_inferred"]
                record["overlap_conflicts"] = conflicts
                continue
            if not overlap:
                record["reasons"] = ["no_common_annual_period"]
                next_pending.append((publication, record, values))
                continue
            additions = {period: value for period, value in values.items()
                         if first_year <= period.year and period <= last_period and period not in current}
            trial = dict(current) | additions
            try:
                validate_annual_history({"history": [trial[p] for p in sorted(trial)]}, cutoff)
            except TTMError as exc:
                record["reasons"] = ["annual_period_overlap_or_invalid_duration: " + str(exc)]
                continue
            current.update(additions)
            # If a newer filing connected later through another report, update
            # provenance for equal-valued added years, never protected base years.
            for period in overlap:
                if period in original_periods:
                    continue
                previous_date = current[period]["history_extension_provenance"]["source_financial_available_at"]
                new_date = values[period]["history_extension_provenance"]["source_financial_available_at"]
                if new_date > previous_date:
                    current[period] = values[period]
            record.update(status="accepted_unverified", reasons=[],
                          added_periods=[period.isoformat() for period in sorted(additions)])
            progress = progress or bool(additions)
        if not progress:
            break
        pending = next_pending
    kept_periods = [period for period in sorted(current) if first_year <= period.year and period <= last_period][-max_years:]
    result = [deepcopy(current[period]) for period in kept_periods]
    probe = deepcopy(snapshot)
    probe["history"] = result
    history, growth = _history(probe, cutoff, set())
    kept_years = {period.year for period in kept_periods}
    audit = {
        "method": JOIN_ASSUMPTION, "share_basis_verified": False,
        "as_of": cutoff.isoformat(), "max_years": max_years,
        "latest_annual_period_end": last_period.isoformat(), "first_calendar_year_in_window": first_year,
        "protected_current_periods": [period.isoformat() for period in sorted(original_periods)],
        "history_count": len(result), "history_growth_observations": len(growth),
        "historical_nonpositive_observations": sum(eps <= 0 for _, eps in history),
        "coverage_start": kept_periods[0].isoformat() if kept_periods else None,
        "coverage_end": kept_periods[-1].isoformat() if kept_periods else None,
        "missing_calendar_years_in_window": [year for year in range(first_year, last_period.year + 1) if year not in kept_years],
        "added_periods": [period.isoformat() for period in kept_periods if period not in original_periods],
        "discarded_current_future_vintages": future_current,
        "discarded_current_outside_window": old_current,
        "filing_audit": records,
        "pending_filings": [{"source_accession": record["source_accession"], "reasons": record["reasons"]}
                            for record in records if record["status"] == "pending"],
        "limitations": [
            "Matching overlapping annual EPS is an unverified research assumption, not a corporate-action or accounting audit.",
            "No split multiplier is inferred from an EPS ratio, and TTM/price inputs are never changed.",
            "Missing years remain missing; long gaps and nonpositive EPS do not manufacture growth observations.",
            "The time window is based on the latest annual history year, not the latest TTM period.",
        ],
    }
    return result, audit
