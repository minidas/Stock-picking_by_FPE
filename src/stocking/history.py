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
LATEST_ASSUMPTION = "latest_available_financial_vintage_unverified_comparability"


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


def _prepare_filing(filing, snapshot, cutoff, first_year, last_period, bridge, *, financial_vintage=False):
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
        access_publication = max(publication, row_publication)
        financial_publication = access_publication
        if financial_vintage:
            explicit_row_date = any(raw.get(key) for key in ("financial_filed_date", "source_financial_available_at")) or raw.get("history_extension_provenance", {}).get("source_financial_available_at")
            explicit_filing_date = any(filing.get(key) for key in ("financial_filed_date", "source_financial_available_at"))
            if explicit_row_date:
                financial_publication = _financial_available(raw)
                if explicit_filing_date:
                    financial_publication = max(financial_publication, _financial_available(filing))
            elif explicit_filing_date:
                financial_publication = _financial_available(filing)
            elif "retriev" in str(filing.get("availability_rule", "") + raw.get("availability_rule", "")).lower():
                raise HistoryError("financial_publication_date_required_for_latest_selection")
        if financial_publication > access_publication:
            raise HistoryError("financial_publication_after_history_availability")
        if period > financial_publication:
            raise HistoryError("source_period_follows_financial_publication")
        if access_publication > cutoff or financial_publication > cutoff:
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
            if previous.get("period_start") != raw.get("period_start"):
                raise HistoryError("conflicting_annual_period_within_filing")
            if not _same(previous["eps"], transformed):
                raise HistoryError("conflicting_same_period_eps_within_filing")
            continue
        item = deepcopy(raw)
        item.update(eps=transformed, period_end=period.isoformat(),
                    available_at=max(access_publication, mapping_publication).isoformat(),
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


def _extend_strict_annual_history(snapshot, candidate_filings, *, as_of=None, max_years=11, reviewed_bridges=None):
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


def _financial_available(row):
    provenance = row.get("history_extension_provenance", {})
    if provenance.get("source_financial_available_at"):
        return _date(provenance["source_financial_available_at"], "source_financial_available_at")
    if row.get("source_financial_available_at"):
        return _date(row["source_financial_available_at"], "source_financial_available_at")
    if row.get("financial_filed_date"):
        from datetime import timedelta
        return _date(row["financial_filed_date"], "financial_filed_date") + timedelta(days=1)
    if "retriev" in str(row.get("availability_rule", "")).lower():
        raise HistoryError("financial_publication_date_required_for_latest_selection")
    return _date(row.get("available_at"), "history.available_at")


def _observation_summary(row):
    return {"period_start": row.get("period_start"), "period_end": row["period_end"], "eps": row["eps"],
            "available_at": row["available_at"], "source_financial_available_at": _financial_available(row).isoformat(),
            "source_accession": row.get("accession", "current_snapshot"), "source_url": row.get("source_url"),
            "history_extension_provenance": deepcopy(row.get("history_extension_provenance"))}


def _entity_boundary(start, evidence, cutoff):
    if start is None:
        if evidence is not None:
            raise HistoryError("entity_basis_evidence_without_boundary")
        return None
    boundary = _date(start, "entity_basis_start")
    if not isinstance(evidence, dict) or not _url(evidence.get("source_url")):
        raise HistoryError("entity_basis_boundary_requires_evidence_url")
    if _date(evidence.get("available_at"), "entity_basis_evidence.available_at") > cutoff:
        raise HistoryError("future_entity_basis_evidence")
    if not isinstance(evidence.get("rationale"), str) or not evidence["rationale"].strip():
        raise HistoryError("entity_basis_boundary_requires_rationale")
    if boundary > cutoff:
        raise HistoryError("entity_basis_boundary_after_decision_date")
    return boundary


def _extend_latest_annual_history(snapshot, candidate_filings, *, as_of=None, max_years=11,
                                  reviewed_bridges=None, entity_basis_start=None, entity_basis_evidence=None):
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
    boundary = _entity_boundary(entity_basis_start, entity_basis_evidence, cutoff)
    observations, current_periods, future_current, entity_excluded, share_excluded = {}, set(), [], [], []

    def add_observation(row, *, filing=None):
        item = deepcopy(row)
        period = _date(item.get("period_end"), "history.period_end")
        available = _date(item.get("available_at"), "history.available_at")
        financial = _financial_available(item)
        if financial > available:
            raise HistoryError("financial_publication_after_history_availability")
        if available > cutoff or financial > cutoff:
            future_current.append(item)
            return False
        if period > financial:
            raise HistoryError("Current history period follows publication")
        item["eps"] = _number(item.get("eps"), "history.eps")
        for field in ("eps_basis", "share_basis", "currency"):
            expected = snapshot.get(field, "USD" if field == "currency" else None)
            if item.get(field, expected) != expected:
                raise HistoryError(f"Current history {field} mismatch")
        try:
            validate_annual_history({"history": [item]}, cutoff)
        except TTMError as exc:
            raise HistoryError(str(exc)) from exc
        if boundary is not None:
            if not item.get("period_start"):
                raise HistoryError("entity_basis_boundary_requires_actual_annual_period_start")
            if _date(item["period_start"], "history.period_start") < boundary:
                entity_excluded.append({**_observation_summary(item), "reason": "annual_period_before_entity_basis_start",
                                        "entity_basis_start": boundary.isoformat()})
                return False
        guard = (filing or {}).get("history_extension_guard")
        if guard is not None:
            allowed = guard.get("allowed_period_ends") if isinstance(guard, dict) else None
            if not isinstance(allowed, list) or not isinstance(guard.get("evidence"), list) or not guard["evidence"]:
                raise HistoryError("invalid_history_extension_share_basis_guard")
            if period.isoformat() not in allowed:
                share_excluded.append({**_observation_summary(item), "reason": "share_basis_review_required",
                                       "evidence": deepcopy(guard["evidence"])})
                return False
        observations.setdefault(period, []).append(item)
        return True

    raw_current = snapshot.get("history", [])
    if not isinstance(raw_current, list):
        raise HistoryError("history must be a list")
    for row in raw_current:
        if not isinstance(row, dict):
            raise HistoryError("current history row must be an object")
        if add_observation(row):
            current_periods.add(_date(row["period_end"], "history.period_end"))
    if not current_periods:
        raise HistoryError("no_current_annual_history_to_anchor_window")
    last_period = max(current_periods)
    first_year = last_period.year - max_years + 1
    outside_current = [deepcopy(r) for p, rows in observations.items() if p.year < first_year for r in rows]
    observations = {p: rows for p, rows in observations.items() if p.year >= first_year}
    bridges = {}
    for bridge in reviewed_bridges or []:
        accession = bridge.get("source_accession")
        if not accession or accession in bridges:
            raise HistoryError("Reviewed bridges require unique source_accession")
        bridges[accession] = bridge
    records, seen = [], set()
    for filing in candidate_filings:
        if not isinstance(filing, dict) or not filing.get("accession"):
            raise HistoryError("Each candidate filing requires an accession")
        accession = filing["accession"]
        if accession in seen:
            raise HistoryError("Duplicate candidate accession; consolidate raw duplicates upstream")
        seen.add(accession)
        record = {"source_accession": accession, "source_filing": deepcopy(filing), "status": "pending",
                  "reasons": [], "added_periods": [], "selected_periods": []}
        records.append(record)
        try:
            if snapshot.get("entity_basis") and filing.get("entity_basis", snapshot["entity_basis"]) != snapshot["entity_basis"]:
                raise HistoryError("economic_entity_basis_mismatch")
            values, outside, _, _, _ = _prepare_filing(filing, snapshot, cutoff, first_year, last_period, bridges.get(accession), financial_vintage=True)
            record["outside_window_periods"] = outside
            before_share, before_entity = len(share_excluded), len(entity_excluded)
            accepted = []
            for period, item in values.items():
                if first_year <= period.year and period <= last_period:
                    item["history_extension_provenance"]["method"] = LATEST_ASSUMPTION
                    if add_observation(item, filing=filing):
                        accepted.append(period.isoformat())
            record["eligible_periods"] = accepted
            if accepted:
                record.update(status="accepted_unverified", reasons=[])
            elif len(share_excluded) > before_share:
                record["reasons"] = ["share_basis_review_required"]
            elif len(entity_excluded) > before_entity:
                record["reasons"] = ["annual_period_before_entity_basis_start"]
            else:
                record["reasons"] = ["no_period_inside_annual_window"]
        except (HistoryError, TypeError) as exc:
            record["reasons"] = [str(exc)]
            if str(exc) in {"source_filing_has_unresolved_conflicts", "conflicting_same_period_eps_within_filing",
                            "conflicting_annual_period_within_filing"}:
                # Keep valid conflicting rows visible: latest ambiguity must
                # leave a gap, never silently fall back to a superseded filing.
                for raw in filing.get("history", []):
                    isolated = deepcopy(filing)
                    isolated.pop("conflicts", None)
                    isolated["history"] = [raw]
                    try:
                        values, _, _, _, _ = _prepare_filing(isolated, snapshot, cutoff, first_year, last_period, bridges.get(accession), financial_vintage=True)
                        for period, item in values.items():
                            if first_year <= period.year and period <= last_period:
                                item["history_extension_provenance"]["method"] = LATEST_ASSUMPTION
                                add_observation(item, filing=filing)
                    except (HistoryError, TypeError):
                        continue
    result, selections, revisions, pending_periods = [], [], [], []
    for period, rows in sorted(observations.items()):
        if not first_year <= period.year or period > last_period:
            continue
        newest_date = max(_financial_available(row) for row in rows)
        newest = [row for row in rows if _financial_available(row) == newest_date]
        starts = {row["period_start"] for row in rows if row.get("period_start")}
        # Equal end dates do not prove equal fiscal periods when starts differ.
        reason = "annual_period_definition_conflict" if len(starts) > 1 else None
        if any(not _same(row["eps"], newest[0]["eps"]) for row in newest[1:]):
            reason = "same_day_latest_annual_eps_conflict"
        all_rows = sorted((_observation_summary(r) for r in rows),
                          key=lambda r: (r["source_financial_available_at"], r["source_accession"], r["eps"]))
        if reason:
            pending_periods.append({"period_end": period.isoformat(), "reason": reason,
                                    "latest_financial_available_at": newest_date.isoformat(), "observations": all_rows})
            selections.append({"period_end": period.isoformat(), "status": "pending", "reason": reason,
                               "selected": None, "all_observations": all_rows})
            for record in records:
                if record["source_accession"] in {r.get("accession") for r in newest}:
                    record["status"] = "pending"
                    record["reasons"] = sorted(set(record["reasons"] + [reason]))
            continue  # Never fall back to an older value when latest is ambiguous.
        selected = max(newest, key=lambda r: (str(r.get("accession", "current_snapshot")), r["available_at"]))
        summary = _observation_summary(selected)
        result.append(deepcopy(selected))
        selections.append({"period_end": period.isoformat(), "status": "selected", "selected": summary,
                           "all_observations": all_rows})
        for old in rows:
            if old is selected:
                continue
            before = _observation_summary(old)
            revisions.append({"period_end": period.isoformat(), "before": before, "after": deepcopy(summary),
                              "reason": "latest_available_financial_publication" if _financial_available(old) < newest_date else "equivalent_same_day_stable_provenance",
                              "value_changed": not _same(old["eps"], selected["eps"])})
        for record in records:
            if record["source_accession"] == selected.get("accession"):
                record["selected_periods"].append(period.isoformat())
                if period not in current_periods:
                    record["added_periods"].append(period.isoformat())
    # Cross-period validation still applies; latest does not authorize annual
    # overlap, short periods, missing-year interpolation, or synthetic growth.
    try:
        validate_annual_history({"history": result}, cutoff)
    except TTMError as exc:
        raise HistoryError("selected_annual_period_overlap_or_invalid_duration: " + str(exc)) from exc
    result = result[-max_years:]
    kept_periods = {row["period_end"] for row in result}
    for selection in selections:
        selection["kept_in_history"] = selection["period_end"] in kept_periods
        if selection["status"] == "selected" and not selection["kept_in_history"]:
            selection["status"] = "excluded_by_observation_cap"
    probe = deepcopy(snapshot)
    probe["history"] = result
    history, growth = _history(probe, cutoff, set())
    kept_years = {d.year for d, _ in history}
    audit = {"method": LATEST_ASSUMPTION, "conflict_policy": "latest_available", "share_basis_verified": False,
             "as_of": cutoff.isoformat(), "max_years": max_years, "latest_annual_period_end": last_period.isoformat(),
             "first_calendar_year_in_window": first_year, "protected_current_periods": [],
             "history_count": len(result), "history_growth_observations": len(growth),
             "historical_nonpositive_observations": sum(e <= 0 for _, e in history),
             "coverage_start": history[0][0].isoformat() if history else None,
             "coverage_end": history[-1][0].isoformat() if history else None,
             "missing_calendar_years_in_window": [y for y in range(first_year, last_period.year + 1) if y not in kept_years],
             "added_periods": [d.isoformat() for d, _ in history if d not in current_periods],
             "discarded_current_future_vintages": future_current, "discarded_current_outside_window": outside_current,
             "revision_audit": revisions, "period_selection_audit": selections, "pending_periods": pending_periods,
             "entity_basis_start": entity_basis_start, "entity_basis_evidence": deepcopy(entity_basis_evidence),
             "entity_boundary_exclusions": entity_excluded, "share_basis_exclusions": share_excluded,
             "filing_audit": records,
             "pending_filings": [{"source_accession": r["source_accession"], "reasons": r["reasons"]}
                                 for r in records if r["status"] == "pending"],
             "limitations": ["Latest available selects financial publication vintages, not retrieval or mapping dates.",
                             "Mixed financial vintages can retain accounting-method differences; latest does not certify comparability.",
                             "Issuer, accounting, annual-period, unit and share/context contracts still apply; no split ratio is inferred.",
                             "Missing or ambiguous annual periods remain absent; negative EPS is retained and TTM/price are unchanged."]}
    return result, audit


def extend_annual_history(snapshot, candidate_filings, *, as_of=None, max_years=11, reviewed_bridges=None,
                          conflict_policy="latest_available", entity_basis_start=None, entity_basis_evidence=None):
    """Choose annual financial vintages; retain strict overlap for old research.

    Latest selection is limited to explicitly compatible issuer/share/accounting
    contexts. It never establishes split comparability from EPS ratios. An
    evidenced economic-entity boundary can exclude obsolete standalone years.
    """
    if conflict_policy == "strict_overlap":
        if entity_basis_start is not None or entity_basis_evidence is not None:
            raise HistoryError("entity boundary requires latest_available; strict_overlap is unchanged replay")
        return _extend_strict_annual_history(snapshot, candidate_filings, as_of=as_of, max_years=max_years,
                                             reviewed_bridges=reviewed_bridges)
    if conflict_policy != "latest_available":
        raise HistoryError("conflict_policy must be latest_available or strict_overlap")
    return _extend_latest_annual_history(snapshot, candidate_filings, as_of=as_of, max_years=max_years,
                                         reviewed_bridges=reviewed_bridges, entity_basis_start=entity_basis_start,
                                         entity_basis_evidence=entity_basis_evidence)
