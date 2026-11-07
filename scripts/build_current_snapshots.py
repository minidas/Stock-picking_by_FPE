"""Normalize current archived EPS into explicit, conservative research snapshots.

No network, no data/ samples, no model configuration changes. SEC annual history
uses latest available financial vintages; strict overlap remains explicit replay.
Provider month-end labels can corroborate actual SEC periods with unique matching
EPS. They never change the date of a direct TTM. Official quarterly supplements
and SEC four-quarter/annual anchors are separately sourced and fully logged.
Passing the input contract is not full corporate-action/accounting verification.
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import DataError, validate_snapshot
from stocking.history import HistoryError, extend_annual_history
from stocking.ttm import FULL_YEAR_DAYS, TTMError, prepare_ttm_snapshot
from stocking.reviewed_evidence import (EvidenceError, utc, validate_reviewed_quarters,
                                       merge_reviewed_quarters, ensure_observed_by_cutoff)

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}
EPS_FORMS = ANNUAL_FORMS | {"10-Q", "10-Q/A", "8-K", "8-K/A", "6-K", "6-K/A"}
VENDOR_EPS_TOLERANCE = 0.005000001


class Unavailable(ValueError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_manifest(path):
    raw = path.read_bytes() if path.exists() else b""
    rows = []
    lines = raw.decode("utf-8").splitlines()
    for index, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
    return rows, raw


def newest(records, key, identity):
    rows = [r for r in records if r.get(key) == identity and r.get("status") == 200 and r.get("file")]
    if not rows:
        raise Unavailable(f"{key}_source_not_downloaded")
    return max(rows, key=lambda r: r["retrieved_at"])


def load_source(study, record):
    path = ROOT / record["file"]
    if not path.resolve().is_relative_to((study / "evidence").resolve()):
        raise Unavailable("source_path_outside_current_evidence")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise Unavailable("source_hash_mismatch")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Unavailable("source_json_decode_failed") from exc


def observed_available(record, information_cutoff=None):
    if information_cutoff is not None:
        timestamp, cutoff = utc(record["retrieved_at"]), utc(information_cutoff)
        if timestamp > cutoff:
            raise Unavailable("source_observation_after_information_cutoff")
        return timestamp.date().isoformat()
    return (datetime.fromisoformat(record["retrieved_at"]).astimezone(timezone.utc).date() + timedelta(days=1)).isoformat()


def filing_url(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.html"


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_yahoo(payload, symbol, cutoff):
    out = {"annual": [], "quarterly": [], "ttm": []}
    block = payload.get("timeseries", {})
    if block.get("error") or not isinstance(block.get("result"), list):
        raise Unavailable("yahoo_timeseries_error")
    fields = {"annual": "annualDilutedEPS", "quarterly": "quarterlyDilutedEPS", "ttm": "trailingDilutedEPS"}
    for result in block["result"]:
        if result.get("meta", {}).get("symbol") != [symbol.replace(".", "-")]:
            raise Unavailable("yahoo_security_identity_mismatch")
        for label, field in fields.items():
            for row in result.get(field, []):
                if row.get("periodType") != {"annual": "12M", "quarterly": "3M", "ttm": "TTM"}[label]:
                    raise Unavailable(f"yahoo_{label}_period_type_mismatch")
                if row.get("currencyCode") != "USD" or not valid_number(row.get("reportedValue", {}).get("raw")):
                    raise Unavailable("yahoo_eps_currency_or_value_invalid")
                day = date.fromisoformat(row["asOfDate"])
                if day.isoformat() <= cutoff:
                    out[label].append({"provider_asOfDate": day.isoformat(), "eps": row["reportedValue"]["raw"],
                                       "periodType": row.get("periodType"), "raw": row})
    for label, rows in out.items():
        dates = {}
        for row in rows:
            key = row["provider_asOfDate"]
            if key in dates and dates[key]["eps"] != row["eps"]:
                raise Unavailable(f"conflicting_yahoo_{label}_period")
            dates[key] = row
        out[label] = sorted(dates.values(), key=lambda r: r["provider_asOfDate"])
    if not out["annual"]:
        raise Unavailable("yahoo_annual_eps_unavailable_for_basis_crosscheck")
    return out


def sec_rows(payload, tag, cutoff):
    facts = payload.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD/shares", [])
    rows = []
    for item in facts:
        if item.get("form") not in EPS_FORMS or not all(item.get(k) for k in ("start", "end", "filed", "accn")):
            continue
        if not valid_number(item.get("val")):
            continue
        start, end, filed = (date.fromisoformat(item[k]) for k in ("start", "end", "filed"))
        if not start <= end <= filed or filed.isoformat() > cutoff:
            continue
        duration = (end - start).days + 1
        if not 70 <= duration <= 380:
            continue
        rows.append({**item, "duration_days": duration, "tag": tag})
    return rows


def history_row(row, member, share_basis):
    result = {"period_start": row["start"], "period_end": row["end"], "eps": row["val"],
            "available_at": (date.fromisoformat(row["filed"]) + timedelta(days=1)).isoformat(),
            "financial_filed_date": row["filed"], "eps_basis": "GAAP_diluted", "share_basis": share_basis,
            "share_basis_verified": False, "currency": "USD", "measure": "annual", "tag": row["tag"],
            "unit": "USD/shares", "accession": row["accn"], "form": row["form"],
            "source_url": row.get("source_url", filing_url(member["cik"], row["accn"]))}
    if row.get("annual_supplement_evidence"):
        result["observation_source"] = "reviewed_annual_filing_supplement_not_companyfacts"
        result["annual_supplement_evidence"] = deepcopy(row["annual_supplement_evidence"])
    return result


def apply_annual_supplements(rows, supplements, member, share_basis, as_of):
    """Append reviewed source-table observations omitted from Companyfacts.

    Only an accession already present in this exact diluted-EPS series is
    eligible. Its financial filing date controls vintage ordering; a review or
    archive date never upgrades the source's financial vintage.
    """
    if not isinstance(supplements, list):
        raise Unavailable("annual_supplements_must_be_a_list")
    result, audit = list(rows), []
    for supplement in supplements:
        if not isinstance(supplement, dict):
            raise Unavailable("annual_supplement_must_be_an_object")
        accession = supplement.get("source_accession")
        existing = [row for row in rows if row["accn"] == accession]
        if not existing:
            raise Unavailable("annual_supplement_requires_existing_source_accession")
        expected = {"cik": member["cik"], "eps_basis": "GAAP_diluted", "share_basis": share_basis,
                    "currency": "USD", "unit": "USD/shares"}
        if any(supplement.get(key) != value for key, value in expected.items()):
            raise Unavailable("annual_supplement_identity_accounting_or_share_basis_mismatch")
        if (supplement.get("form") not in ANNUAL_FORMS
                or {row["form"] for row in existing} != {supplement.get("form")}
                or {row["filed"] for row in existing} != {supplement.get("filed")}
                or supplement.get("tag") not in TAGS
                or {row["tag"] for row in existing} != {supplement.get("tag")}):
            raise Unavailable("annual_supplement_filing_metadata_mismatch")
        try:
            filed = date.fromisoformat(supplement["filed"])
        except (ValueError, TypeError) as exc:
            raise Unavailable("annual_supplement_filed_date_invalid") from exc
        if (filed + timedelta(days=1)).isoformat() > as_of:
            raise Unavailable("annual_supplement_financial_publication_after_cutoff")
        source_url = supplement.get("source_url")
        url = urlsplit(source_url) if isinstance(source_url, str) else None
        expected_path = f"/Archives/edgar/data/{int(member['cik'])}/{accession.replace('-', '')}/"
        if (url is None or url.scheme != "https" or url.hostname not in {"www.sec.gov", "sec.gov"}
                or not url.path.startswith(expected_path) or not url.path[len(expected_path):]):
            raise Unavailable("annual_supplement_source_url_identity_mismatch")
        if any(not isinstance(supplement.get(key), str) or not supplement[key].strip()
               for key in ("source_file", "source_sha256", "evidence_locator", "rationale")):
            raise Unavailable("annual_supplement_requires_archived_source_and_review_evidence")
        source_path = (ROOT / supplement["source_file"]).resolve()
        if not source_path.is_relative_to(ROOT.resolve()) or not source_path.is_file():
            raise Unavailable("annual_supplement_source_file_outside_workspace_or_missing")
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_hash != supplement["source_sha256"]:
            raise Unavailable("annual_supplement_source_hash_mismatch")
        history = supplement.get("history")
        if not isinstance(history, list) or not history:
            raise Unavailable("annual_supplement_requires_annual_observations")
        evidence = {key: deepcopy(value) for key, value in supplement.items() if key != "history"}
        evidence.update(observation_source="reviewed_annual_filing_supplement_not_companyfacts",
                        source_financial_available_at=(filed + timedelta(days=1)).isoformat(),
                        source_file_sha256_verified=True)
        added = []
        for row in history:
            if not isinstance(row, dict) or not valid_number(row.get("eps")):
                raise Unavailable("annual_supplement_eps_invalid")
            try:
                start, end = (date.fromisoformat(row[key]) for key in ("start", "end"))
            except (KeyError, ValueError, TypeError) as exc:
                raise Unavailable("annual_supplement_period_invalid") from exc
            duration = (end - start).days + 1
            if duration not in FULL_YEAR_DAYS or end > filed:
                raise Unavailable("annual_supplement_period_not_complete_available_annual")
            if any(raw["accn"] == accession and raw["end"] == row["end"]
                   and raw["duration_days"] in FULL_YEAR_DAYS for raw in result):
                raise Unavailable("annual_supplement_cannot_replace_existing_annual_period")
            item = {"start": row["start"], "end": row["end"], "val": row["eps"], "filed": supplement["filed"],
                    "accn": accession, "form": supplement["form"], "tag": supplement["tag"], "duration_days": duration,
                    "source_url": source_url, "annual_supplement_evidence": deepcopy(evidence)}
            result.append(item)
            added.append({"period_start": row["start"], "period_end": row["end"], "eps": row["eps"]})
        audit.append({**evidence, "status": "appended_missing_annual_periods", "added_observations": added})
    return result, audit


def annual_chain(rows, member, share_basis, as_of, *, history_policy="latest_available",
                 entity_override=None, split_disclosures=None):
    override = entity_override or {}
    if not isinstance(override, dict) or set(override) - {"cik", "entity_basis_start", "evidence", "reviewed_bridges", "annual_supplements"}:
        raise Unavailable("invalid_entity_or_share_basis_override")
    if override.get("cik", member["cik"]) != member["cik"]:
        raise Unavailable("override_cik_identity_mismatch")
    rows, supplement_audit = apply_annual_supplements(rows, override.get("annual_supplements", []), member, share_basis, as_of)
    grouped = defaultdict(list)
    for row in rows:
        if row["form"] in ANNUAL_FORMS and row["duration_days"] in FULL_YEAR_DAYS:
            grouped[row["accn"]].append(row)
    if not grouped:
        raise Unavailable("no_full_year_sec_diluted_eps_10k_or_20f")
    blocks, invalid_blocks = [], []
    for accession, values in grouped.items():
        periods, conflicts = {}, []
        for row in values:
            previous = periods.get(row["end"])
            if previous and (previous["start"] != row["start"] or abs(previous["val"] - row["val"]) > 1e-8):
                conflicts.append({"period_end": row["end"], "first": previous, "second": row})
            periods[row["end"]] = row
        if conflicts:
            invalid_blocks.append({"accession": accession, "reason": "same_filing_annual_conflict", "conflicts": conflicts,
                                   "filed": max(r["filed"] for r in values), "latest_end": max(r["end"] for r in values),
                                   "records": values})
            continue
        records = sorted(periods.values(), key=lambda r: r["end"])
        blocks.append({"accession": accession, "filed": max(r["filed"] for r in records),
                       "latest_end": max(periods), "records": records})
    if not blocks:
        raise Unavailable("all_sec_annual_filings_conflicted")
    latest_annual_end = max(row["end"] for row in rows if row["form"] in ANNUAL_FORMS and row["duration_days"] in FULL_YEAR_DAYS)
    eligible = [b for b in blocks if b["latest_end"] == latest_annual_end]
    if not eligible:
        raise Unavailable("latest_sec_annual_filing_has_unresolved_conflicts")
    newest_date = max(b["filed"] for b in eligible)
    latest = [b for b in eligible if b["filed"] == newest_date]
    signatures = {tuple((r["start"], r["end"], r["val"]) for r in b["records"]) for b in latest}
    if len(signatures) != 1:
        raise Unavailable("same_day_latest_sec_annual_filing_conflict")
    seed = sorted(latest, key=lambda b: b["accession"])[0]
    snapshot = {"symbol": member["symbol"], "as_of": as_of, "cik": member["cik"],
                "currency": "USD", "eps_basis": "GAAP_diluted", "share_basis": share_basis, "context": {},
                "history": [history_row(r, member, share_basis) for r in seed["records"]]}
    first_year = int(latest_annual_end[:4]) - 10
    candidates = []
    for block in blocks + (invalid_blocks if history_policy == "latest_available" else []):
        if block["accession"] == seed["accession"] or int(block["latest_end"][:4]) < first_year:
            continue
        candidate = {"accession": block["accession"], "cik": member["cik"],
                           "identity_join_allowed": True, "eps_basis": "GAAP_diluted", "share_basis": share_basis,
                           "context": {}, "source_url": filing_url(member["cik"], block["accession"]),
                           "available_at": (date.fromisoformat(block["filed"]) + timedelta(days=1)).isoformat(),
                           "history": [history_row(r, member, share_basis) for r in block["records"]]}
        if block.get("conflicts"):
            candidate["conflicts"] = block["conflicts"]
        candidates.append(candidate)
    bridges = override.get("reviewed_bridges", [])
    if not isinstance(bridges, list):
        raise Unavailable("reviewed_bridges_must_be_a_list")
    for bridge in bridges:
        if not isinstance(bridge, dict):
            raise Unavailable("reviewed_bridge_must_be_an_object")
        if bridge.get("mapping_reason") == "stock_split":
            if not bridge.get("event_id") or not bridge.get("effective_date") or not bridge.get("rationale"):
                raise Unavailable("split_bridge_requires_reviewed_event_identity_date_and_rationale")
            if date.fromisoformat(bridge["effective_date"]).isoformat() > as_of:
                raise Unavailable("future_split_bridge_effective_date")
    split_guard = None
    if history_policy == "latest_available" and split_disclosures:
        # SEC conversion-ratio facts contain duration contexts and duplicate
        # descriptions of the same event. They are a review signal, NEVER an
        # event calendar or a multiplier source. Existing equal-overlap periods
        # remain available; unproven extensions across a conflicting share basis
        # need a separately evidenced, accession-specific mapping.
        strict_history, strict_audit = extend_annual_history(snapshot, candidates, max_years=11,
                                                            conflict_policy="strict_overlap", reviewed_bridges=bridges)
        allowed_periods = sorted(row["period_end"] for row in strict_history)
        connected_accessions = {row["source_accession"] for row in strict_audit["filing_audit"]
                                if row["status"] == "accepted_unverified"} | {seed["accession"]}
        # Quarterly split disclosures may never appear in the annual chain.
        # Filing date establishes when a concern was disclosed, not when the
        # split happened; all such signals need review for older native units.
        dated_disclosures = [r for r in split_disclosures if r.get("filed") and r["filed"] <= as_of]
        bridge_accessions = {b.get("source_accession") for b in bridges}
        guarded = []
        if dated_disclosures:
            # Conflicting and wholly disconnected older reports both lack a
            # proven path to current units. Restrict added historical years;
            # same-year latest revisions are retained.
            latest_disclosure = max(r["filed"] for r in dated_disclosures)
            for candidate in candidates:
                accession = candidate["accession"]
                candidate_allowed = sorted(set(allowed_periods) | {r["period_end"] for r in candidate["history"]
                                                                   if r["period_end"] >= latest_disclosure})
                if (accession not in connected_accessions and accession not in bridge_accessions
                        and any(r["period_end"] not in candidate_allowed for r in candidate["history"])):
                    candidate["history_extension_guard"] = {"allowed_period_ends": candidate_allowed,
                        "reason": "share_basis_review_required", "evidence": dated_disclosures}
                    guarded.append(accession)
        split_guard = {"method": "disclosure_linked_share_basis_review_no_ratio_inference",
                       "strict_compatible_periods": allowed_periods, "guarded_accessions": sorted(guarded),
                       "dated_split_review_signals": dated_disclosures,
                       "all_split_disclosures": split_disclosures,
                       "note": "Fact end/start are not treated as effective event dates. No automatic split multiplier was inferred."}
    history, audit = extend_annual_history(snapshot, candidates, max_years=11, reviewed_bridges=bridges,
                                           conflict_policy=history_policy,
                                           entity_basis_start=override.get("entity_basis_start"),
                                           entity_basis_evidence=override.get("evidence"))
    audit["conflict_policy"] = history_policy
    audit["share_basis_extension_guard"] = split_guard
    audit["seed_accession"] = seed["accession"]
    audit["seed_filed_date"] = seed["filed"]
    audit["rejected_conflicted_filing_blocks"] = invalid_blocks
    audit["annual_supplements"] = supplement_audit
    # Remove duplicated filing contents; source accession and every comparison remain.
    for filing in audit["filing_audit"]:
        filing.pop("source_filing", None)
    return history, audit, seed


def match_annual_basis(yahoo, history):
    requested = yahoo["annual"][-3:]
    mappings = []
    for raw in requested:
        raw_day = date.fromisoformat(raw["provider_asOfDate"])
        nearby = [r for r in history if date.fromisoformat(r["period_end"]).year == raw_day.year
                  and abs((date.fromisoformat(r["period_end"]) - raw_day).days) <= 7]
        matches = [r for r in nearby if abs(r["eps"] - raw["eps"]) <= VENDOR_EPS_TOLERANCE]
        if len(matches) != 1:
            raise Unavailable("latest_three_yahoo_annual_eps_do_not_uniquely_match_sec_chain")
        row = matches[0]
        mappings.append({"provider_asOfDate": raw["provider_asOfDate"], "sec_period_start": row["period_start"],
                         "sec_period_end": row["period_end"], "provider_eps": raw["eps"], "sec_eps": row["eps"],
                         "day_difference": (date.fromisoformat(row["period_end"]) - raw_day).days,
                         "eps_difference": raw["eps"] - row["eps"],
                         "method": "unique_same_calendar_year_within_7_days_and_half_cent_eps_match",
                         "source_accession": row["accession"]})
    return mappings


def primary_quarter_anchor(rows, history, yahoo, member, share_basis, availability, as_of, end):
    """Use four published single-quarter EPS facts, never annual-minus-YTD.

    Every component must match a current vendor quarter within half a cent, or
    belong to a filing with a connected annual EPS basis already corroborated.
    These checks support a research basis join, not a blanket verification flag.
    """
    by_end = defaultdict(list)
    for row in rows:
        if 84 <= row["duration_days"] <= 98:
            by_end[row["end"]].append(row)
    selected = []
    target_end = end
    for _ in range(4):
        candidates = by_end.get(target_end, [])
        if not candidates:
            raise Unavailable("four_quarter_preference_missing_explicit_single_quarter")
        latest_filed = max(row["filed"] for row in candidates)
        latest = [row for row in candidates if row["filed"] == latest_filed]
        signatures = {(row["start"], row["end"], row["val"]) for row in latest}
        if len(signatures) != 1:
            raise Unavailable("four_quarter_preference_conflicting_latest_single_quarter")
        chosen = min(latest, key=lambda row: row["accn"])
        selected.append(chosen)
        target_end = (date.fromisoformat(chosen["start"]) - timedelta(days=1)).isoformat()
    selected.reverse()
    if (date.fromisoformat(end) - date.fromisoformat(selected[0]["start"])).days + 1 not in FULL_YEAR_DAYS:
        raise Unavailable("four_quarter_preference_not_exact_full_year")
    components, checks = [], []
    for row in selected:
        sec_day = date.fromisoformat(row["end"])
        vendor_rows = [raw for raw in yahoo["quarterly"]
                       if date.fromisoformat(raw["provider_asOfDate"]).year == sec_day.year
                       and abs((date.fromisoformat(raw["provider_asOfDate"]) - sec_day).days) <= 7]
        matching = [raw for raw in vendor_rows if abs(raw["eps"] - row["val"]) <= VENDOR_EPS_TOLERANCE]
        if vendor_rows:
            if len(matching) != 1:
                raise Unavailable("four_quarter_preference_current_vendor_quarter_basis_conflict")
            raw = matching[0]
            check = {"method": "unique_current_vendor_quarter_and_sec_eps_match",
                     "provider_asOfDate": raw["provider_asOfDate"], "sec_period_start": row["start"],
                     "sec_period_end": row["end"], "provider_eps": raw["eps"], "sec_eps": row["val"],
                     "eps_difference": raw["eps"] - row["val"],
                     "day_difference": (sec_day - date.fromisoformat(raw["provider_asOfDate"])).days}
        else:
            annual = [h for h in history if h["accession"] == row["accn"]
                      and h["period_start"] <= row["start"] <= row["end"] <= h["period_end"]]
            if len(annual) != 1:
                raise Unavailable("four_quarter_preference_no_current_basis_corroboration_for_component")
            check = {"method": "same_filing_as_connected_annual_history_no_vendor_quarter_available",
                     "sec_period_start": row["start"], "sec_period_end": row["end"],
                     "annual_period_end": annual[0]["period_end"], "annual_eps": annual[0]["eps"],
                     "source_accession": row["accn"]}
        checks.append(check)
        components.append({"period_start": row["start"], "period_end": row["end"],
                           "available_at": availability, "eps": row["val"], "eps_basis": "GAAP_diluted",
                           "share_basis": share_basis, "currency": "USD",
                           "source_url": row.get("source_url", filing_url(member["cik"], row["accn"])),
                           "financial_filed_date": row["filed"], "accession": row["accn"],
                           "observation_source": ("reviewed_official_quarter_supplement" if row.get("reviewed_quarter_evidence") else "sec_companyfacts"),
                           "raw_sec_observation": row, "basis_corroboration": check})
    eps = math.fsum(row["val"] for row in selected)
    original = yahoo["ttm"][-1] if yahoo["ttm"] else None
    same_end = original is not None and original["provider_asOfDate"] == end
    return {"symbol": member["symbol"], "cik": member["cik"], "as_of": as_of,
            "eps": eps, "period_start": selected[0]["start"], "period_end": end,
            "available_at": availability, "eps_basis": "GAAP_diluted", "share_basis": share_basis,
            "currency": "USD", "method": "four_quarter_diluted_eps_sum", "components": components,
            "latest_report_period_end": end, "quarter_basis_corroboration": checks,
            "original_provider_ttm_observation": original,
            "difference_vs_original_provider_ttm_same_period": eps - original["eps"] if same_end else None,
            "source_preference": "Four explicit SEC single-quarter facts take precedence when their periods and current basis are corroborated; no Q4 subtraction.",
            "quarter_sum_is_recomputed_annual_gaap": False,
            "quarter_sum_definition_note": "Quarterly weighted shares and dilution can differ; this sum can differ from a separately recomputed full-year diluted EPS.",
            "availability_rule": "latest_required_source_observation_utc_date_plus_one_day; each financial_filed_date_retained",
            "external_earnings_release_completeness_verified": False}


def actual_period_reconciliation(rows, history, mappings, yahoo):
    """Map only proven actual labels for freshness checks, not direct TTM dates.

Four SEC quarters or a complete SEC annual observation must still construct the
anchor. A nearby date by itself, or a matching EPS from an older vintage, cannot
pass. Forecast periods are handled separately by the fiscal-calendar contract.
"""
    resolved = {m["provider_asOfDate"]: m["sec_period_end"] for m in mappings}
    audit = []
    by_end = defaultdict(list)
    for row in rows:
        if 84 <= row["duration_days"] <= 98:
            by_end[row["end"]].append(row)
    latest = []
    for candidates in by_end.values():
        filed = max(r["filed"] for r in candidates)
        candidates = [r for r in candidates if r["filed"] == filed]
        if len({(r["start"], r["end"], r["val"]) for r in candidates}) == 1:
            latest.append(candidates[0])
    for raw in yahoo["quarterly"]:
        day = date.fromisoformat(raw["provider_asOfDate"])
        if day.day != calendar.monthrange(day.year, day.month)[1]:
            continue
        candidates = [r for r in latest if abs((date.fromisoformat(r["end"]) - day).days) <= 7
                      and abs(raw["eps"] - r["val"]) <= VENDOR_EPS_TOLERANCE]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if raw["provider_asOfDate"] in resolved and resolved[raw["provider_asOfDate"]] != row["end"]:
            raise Unavailable("conflicting_actual_period_label_evidence")
        resolved[raw["provider_asOfDate"]] = row["end"]
        audit.append({"method": "unique_latest_actual_quarter_eps_matches_provider_month_end_label",
                      "provider_period_end": raw["provider_asOfDate"], "official_period_end": row["end"],
                      "provider_eps": raw["eps"], "official_eps": row["val"],
                      "source_accession": row["accn"], "direct_ttm_relabelled": False})
    # A new annual release can precede the provider's annual-series refresh.
    # An identical current TTM value corroborates the official full-year anchor;
    # the selected EPS and dates still come entirely from the official annual.
    for raw in yahoo["ttm"]:
        day = date.fromisoformat(raw["provider_asOfDate"])
        if day.day != calendar.monthrange(day.year, day.month)[1]:
            continue
        candidates = [r for r in history if abs((date.fromisoformat(r["period_end"]) - day).days) <= 7
                      and abs(raw["eps"] - r["eps"]) <= VENDOR_EPS_TOLERANCE]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if raw["provider_asOfDate"] in resolved and resolved[raw["provider_asOfDate"]] != row["period_end"]:
            raise Unavailable("conflicting_actual_period_label_evidence")
        resolved[raw["provider_asOfDate"]] = row["period_end"]
        audit.append({"method": "official_full_year_eps_matches_provider_ttm_month_end_label",
                      "provider_period_end": raw["provider_asOfDate"], "official_period_end": row["period_end"],
                      "provider_eps": raw["eps"], "official_eps": row["eps"],
                      "source_accession": row["accession"], "direct_ttm_relabelled": False})
    return resolved, audit


def choose_anchor(rows, history, mappings, yahoo, member, share_basis, availability, as_of, yahoo_source):
    latest_sec_end = max(r["end"] for r in rows)
    latest_annual = max(history, key=lambda r: r["period_end"])
    date_map, period_audit = actual_period_reconciliation(rows, history, mappings, yahoo)
    yahoo_latest = max(date_map.get(r["provider_asOfDate"], r["provider_asOfDate"])
                       for observations in yahoo.values() for r in observations)
    if latest_annual["period_end"] == latest_sec_end and yahoo_latest <= latest_sec_end:
        selected = latest_annual
        component = {"period_start": selected["period_start"], "period_end": selected["period_end"],
                     "available_at": availability, "eps": selected["eps"], "eps_basis": "GAAP_diluted",
                     "share_basis": share_basis, "currency": "USD", "source_url": selected["source_url"],
                     "financial_filed_date": selected["financial_filed_date"], "accession": selected["accession"]}
        return {"symbol": member["symbol"], "cik": member["cik"], "as_of": as_of,
                "eps": selected["eps"], "period_start": selected["period_start"], "period_end": selected["period_end"],
                "available_at": availability, "eps_basis": "GAAP_diluted", "share_basis": share_basis,
                "currency": "USD", "method": "annual_report_latest_ttm", "components": [component],
                "latest_report_period_end": latest_sec_end, "primary_period_evidence": selected,
                "latest_yahoo_effective_period_end": yahoo_latest,
                "actual_period_reconciliation": period_audit,
                "availability_rule": "latest_required_source_observation_utc_date_plus_one_day; filing_date_retained_separately",
                "external_earnings_release_completeness_verified": False}
    quarter_review = {"eligible": False, "reason": "newer_yahoo_period_than_latest_sec_eps"}
    if yahoo_latest <= latest_sec_end:
        try:
            anchor = primary_quarter_anchor(rows, history, yahoo, member, share_basis, availability, as_of, latest_sec_end)
            anchor["actual_period_reconciliation"] = period_audit
            return anchor
        except Unavailable as exc:
            if str(exc) in {"four_quarter_preference_conflicting_latest_single_quarter",
                            "four_quarter_preference_current_vendor_quarter_basis_conflict"}:
                raise  # A known contradiction must not disappear behind a vendor TTM fallback.
            quarter_review = {"eligible": False, "reason": str(exc),
                              "action": "Retain independently sourced direct-provider TTM method if its existing checks pass."}
    if not yahoo["ttm"]:
        raise Unavailable("no_direct_yahoo_ttm_and_latest_sec_annual_not_current")
    ttm = yahoo["ttm"][-1]
    end = ttm["provider_asOfDate"]
    raw_latest = max(r["provider_asOfDate"] for observations in yahoo.values() for r in observations)
    if end != latest_sec_end:
        raise Unavailable("direct_ttm_end_does_not_exactly_match_latest_sec_eps_end")
    if end != raw_latest:
        raise Unavailable("direct_ttm_is_older_than_another_yahoo_eps_period")
    endpoint_rows = [r for r in rows if r["end"] == end and (84 <= r["duration_days"] <= 98 or r["duration_days"] in FULL_YEAR_DAYS)]
    if not endpoint_rows:
        raise Unavailable("no_exact_sec_quarter_or_full_year_endpoint_for_direct_ttm")
    prior_dates = {r["end"] for r in rows if r["end"] < end
                   and (84 <= r["duration_days"] <= 98 or r["duration_days"] in FULL_YEAR_DAYS)
                   and (date.fromisoformat(end) - date.fromisoformat(r["end"])).days in FULL_YEAR_DAYS}
    if len(prior_dates) != 1:
        raise Unavailable("direct_ttm_prior_year_sec_boundary_missing_or_ambiguous")
    prior_end = next(iter(prior_dates))
    start = (date.fromisoformat(prior_end) + timedelta(days=1)).isoformat()
    primary = max(endpoint_rows, key=lambda r: (r["filed"], r["accn"]))
    prior = max((r for r in rows if r["end"] == prior_end), key=lambda r: (r["filed"], r["accn"]))
    component = {"period_start": start, "period_end": end, "available_at": availability,
                 "eps": ttm["eps"], "eps_basis": "GAAP_diluted", "share_basis": share_basis,
                 "currency": "USD", "source_url": yahoo_source["url"],
                 "raw_provider_observation": ttm["raw"], "primary_period_end_evidence": primary,
                 "primary_prior_year_boundary_evidence": prior}
    return {"symbol": member["symbol"], "cik": member["cik"], "as_of": as_of, "eps": ttm["eps"],
            "period_start": start, "period_end": end, "available_at": availability,
            "eps_basis": "GAAP_diluted", "share_basis": share_basis, "currency": "USD",
            "method": "direct_reported_ttm", "components": [component], "latest_report_period_end": end,
            "availability_rule": "latest_required_source_observation_utc_date_plus_one_day; not_historical_publication_date",
            "primary_filing_source_url": filing_url(member["cik"], primary["accn"]),
            "primary_quarter_preference_review": quarter_review,
            "provider_methodology_independently_recomputed": False,
            "external_earnings_release_completeness_verified": False}


def normalize(member, sec, yahoo_payload, sec_source, yahoo_source, as_of, *, history_policy="latest_available", entity_override=None,
              quarter_supplements=None, information_cutoff=None):
    if str(sec.get("cik", "")).zfill(10) != member["cik"]:
        raise Unavailable("sec_cik_identity_mismatch")
    if information_cutoff is not None and utc(information_cutoff).date().isoformat() != as_of:
        raise Unavailable("information_cutoff_date_mismatch")
    if information_cutoff is not None:
        ensure_observed_by_cutoff(member.get("selected_price_evidence", member.get("source", {}).get("ivv")), information_cutoff)
    legacy_basis_date = max(observed_available(sec_source), observed_available(yahoo_source))
    availability = max(observed_available(sec_source, information_cutoff), observed_available(yahoo_source, information_cutoff))
    if availability > as_of:
        raise Unavailable("source_observation_not_yet_available_under_next_utc_day_rule")
    cutoff = min(datetime.fromisoformat(sec_source["retrieved_at"]).date().isoformat(), as_of)
    yahoo = parse_yahoo(yahoo_payload, member["symbol"], cutoff)
    # Preserve the existing unit identifier for reviewed split/annual bridges.
    # It is not the financial or retrieval availability of the new snapshot.
    basis = f"{member['symbol']}_research_current_vendor_basis_observed_{legacy_basis_date}"
    rows_by_tag = {tag: sec_rows(sec, tag, cutoff) for tag in TAGS}
    supplementary = []
    if quarter_supplements:
        if information_cutoff is None:
            raise Unavailable("quarter_supplements_require_explicit_information_cutoff")
        supplementary = validate_reviewed_quarters(quarter_supplements, symbol=member["symbol"], cik=member["cik"],
            share_basis=basis, as_of=as_of, information_cutoff=information_cutoff, base_dir=ROOT)
        availability = max(availability, *(utc(r["reviewed_quarter_evidence"]["reviewed_at"]).date().isoformat() for r in supplementary))
        rows_by_tag[TAGS[0]] = merge_reviewed_quarters(rows_by_tag[TAGS[0]], supplementary)
    split_disclosures = []
    for tag, fact in sec.get("facts", {}).get("us-gaap", {}).items():
        if "StockSplitConversionRatio" not in tag:
            continue
        for unit, items in fact.get("units", {}).items():
            for item in items:
                if item.get("filed") and item["filed"] <= cutoff and item.get("accn"):
                    split_disclosures.append({**item, "tag": tag, "unit": unit,
                                              "source_url": filing_url(member["cik"], item["accn"])})
    all_sec_rows = [row for rows in rows_by_tag.values() for row in rows]
    global_latest_sec_end = max((row["end"] for row in all_sec_rows), default=None)
    attempts = []
    for tag in TAGS:
        attempt = {"tag": tag}
        try:
            rows = rows_by_tag[tag]
            if not rows:
                raise Unavailable("sec_tag_unavailable")
            attempt["sec_raw_eps_observation_count"] = len(rows)
            attempt["latest_sec_period_end"] = max(row["end"] for row in rows)
            if attempt["latest_sec_period_end"] != global_latest_sec_end:
                raise Unavailable("selected_sec_eps_tag_is_older_than_another_diluted_eps_tag")
            history, chain_audit, seed = annual_chain(rows, member, basis, as_of, history_policy=history_policy,
                                                      entity_override=entity_override, split_disclosures=split_disclosures)
            attempt["annual_history_count"] = len(history)
            attempt["annual_history_chain"] = chain_audit
            mappings = match_annual_basis(yahoo, history)
            attempt["annual_vendor_sec_basis_crosscheck"] = mappings
            anchor = choose_anchor(rows, history, mappings, yahoo, member, basis, availability, as_of, yahoo_source)
            if information_cutoff is not None:
                anchor["availability_rule"] = "observed_by_decision_timestamp; original financial publication dates retained per source"
                anchor["information_cutoff"] = information_cutoff
            snapshot = {"symbol": member["symbol"], "company_name": member["name"], "cik": member["cik"],
                        "as_of": as_of, "asset_type": "stock", "price": member["price"],
                        "price_date": member["price_date"], "price_kind": member["price_kind"], "currency": "USD",
                        "eps_basis": "GAAP_diluted", "share_basis": basis, "share_basis_verified": False,
                        "history": history, "consensus": [], "context": {},
                        "source": {"price": member.get("selected_price_evidence", member["source"]["ivv"]),
                                   "sec": sec_source, "yahoo_actual_eps": yahoo_source},
                        "price_selection_status": member.get("price_selection_status", "ivv_vendor_price_fallback_quote_pending"),
                        "normalization_status": "research_input_contract_passed_external_verification_incomplete",
                        "information_cutoff": information_cutoff,
                        "reviewed_quarter_supplements": supplementary,
                        "annual_history_provenance": chain_audit,
                        "annual_vendor_sec_basis_crosscheck": mappings,
                        "normalization_limitations": [
                            "Three recent matching annual EPS values support a research basis join; not complete share-class/corporate-action verification.",
                            ("Latest financial vintage wins within compatible issuer/share/accounting context; unresolved same-day conflicts stay missing and mixed-vintage comparability is unverified."
                             if history_policy == "latest_available" else "Older years join only through equal overlapping annual EPS; conflicting or disconnected years remain absent."),
                            "Annual source dates are exact SEC fiscal dates; vendor month-end mappings are explicit and bounded.",
                            "No independently complete search of earnings releases earlier than SEC filing has been performed.",
                            "No verified same-basis institutional consensus supplied; default AI consensus gate remains unchanged."]}
            snapshot = prepare_ttm_snapshot(snapshot, anchor)
            snapshot = validate_snapshot(snapshot)
            audit = {"symbol": member["symbol"], "as_of": as_of, "status": "research_normalized",
                     "model_input_contract_passed": True, "full_economic_basis_verified": False,
                     "eps_tag": tag, "eps_measure": "ttm", "ttm_method": anchor["method"],
                     "eps0": snapshot["eps0"], "period_start": snapshot["eps_period_start"],
                     "period_end": snapshot["eps_period_end"], "available_at": availability,
                     "annual_history_count": len(history), "history_growth_observations": chain_audit["history_growth_observations"],
                     "missing_annual_calendar_years": chain_audit["missing_calendar_years_in_window"],
                     "nonpositive_annual_eps_count": sum(row["eps"] <= 0 for row in history),
                     "consensus_count": 0, "share_basis_verified": False,
                     "source_sha256": {"sec": sec_source["sha256"], "yahoo": yahoo_source["sha256"]},
                     "annual_vendor_sec_basis_crosscheck": mappings, "annual_history_chain": chain_audit,
                     "quarter_supplement_count": len(supplementary),
                     "actual_period_reconciliation": anchor.get("actual_period_reconciliation", []),
                     "prior_tag_attempts": attempts, "limitations": snapshot["normalization_limitations"]}
            return snapshot, audit
        except (Unavailable, DataError, HistoryError, TTMError) as exc:
            attempts.append({**attempt, "reason": str(exc)})
    raise Unavailable(json.dumps({"tag_attempts": attempts}, ensure_ascii=False))


def build(study, as_of, *, history_policy="latest_available", output_dir=None, entity_overrides=None,
          quarter_supplements=None, information_cutoff=None):
    date.fromisoformat(as_of)
    if history_policy not in {"latest_available", "strict_overlap"}:
        raise ValueError("Unknown history policy")
    if information_cutoff is not None and (utc(information_cutoff) > datetime.now(timezone.utc)
            or utc(information_cutoff).date().isoformat() != as_of):
        raise ValueError("Information cutoff must already have occurred on as_of UTC date")
    study = Path(study).resolve()
    target = Path(output_dir).resolve() if output_dir is not None else study / ("normalized_" + history_policy)
    if not target.is_relative_to(study) or target == study or target == study / "normalized":
        raise ValueError("New normalization output must be a dedicated directory inside this study, never the frozen normalized directory")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Refusing to overwrite an existing normalization output; choose a new --output directory")
    overrides_path = Path(entity_overrides) if isinstance(entity_overrides, (str, Path)) else None
    overrides = read_json(overrides_path) if overrides_path is not None else (entity_overrides or {})
    if not isinstance(overrides, dict):
        raise ValueError("entity_overrides must map symbols to evidenced constraints")
    supplements_path = Path(quarter_supplements) if isinstance(quarter_supplements, (str, Path)) else None
    supplements = read_json(supplements_path) if supplements_path else (quarter_supplements or {})
    if not isinstance(supplements, dict):
        raise ValueError("quarter_supplements must map symbols to reviewed official observations")
    universe = read_json(study / "input/universe.json")
    price_path = study / "normalized/prices.json"
    price_bytes = price_path.read_bytes() if price_path.exists() else None
    prices = json.loads(price_bytes).get("prices", {}) if price_bytes else {}
    sec_records, sec_bytes = read_manifest(study / "evidence/sec/manifest.jsonl")
    yahoo_records, yahoo_bytes = read_manifest(study / "evidence/public_actual_eps/manifest.jsonl")
    snapshots, audits = [], []
    for index, member in enumerate(universe["members"], 1):
        source_hashes = {}
        try:
            member = dict(member)
            if member["symbol"] in prices:
                quote = prices[member["symbol"]]
                if quote.get("splits_after_price_before_asof"):
                    raise Unavailable("split_after_price_requires_share_basis_review")
                if quote.get("currency") != "USD" or quote.get("price_date", "9999") > as_of:
                    raise Unavailable("invalid_normalized_price_currency_or_date")
                if quote.get("source_available_at", as_of) > as_of:
                    raise Unavailable("normalized_price_observation_not_yet_available")
                member.update({key: quote[key] for key in ("price", "price_date", "price_kind", "currency")})
                member["selected_price_evidence"] = quote
                member["price_selection_status"] = "latest_completed_session_nominal_close"
            sr = newest(sec_records, "cik", member["cik"])
            yr = newest(yahoo_records, "symbol", member["symbol"])
            source_hashes = {"sec": sr["sha256"], "yahoo": yr["sha256"]}
            snapshot, audit = normalize(member, load_source(study, sr), load_source(study, yr), sr, yr, as_of,
                                         history_policy=history_policy, entity_override=overrides.get(member["symbol"]),
                                         quarter_supplements=supplements.get(member["symbol"]), information_cutoff=information_cutoff)
            snapshots.append(snapshot)
            audits.append(audit)
        except (Unavailable, DataError, HistoryError, TTMError, EvidenceError, OSError) as exc:
            details = json.loads(str(exc)) if str(exc).startswith('{"tag_attempts":') else None
            audits.append({"symbol": member["symbol"], "cik": member["cik"], "as_of": as_of,
                           "status": "data_unavailable", "model_input_contract_passed": False,
                           "full_economic_basis_verified": False,
                           "reason": "no_eligible_sec_eps_tag_and_ttm_combination" if details else str(exc),
                           "details": details, "source_sha256": source_hashes, "consensus_count": 0})
        if index % 50 == 0:
            print(json.dumps({"processed": index, "research_normalized": len(snapshots)}), flush=True)
    summary = {"as_of": as_of, "universe_security_count": len(universe["members"]),
               "history_revision_policy": history_policy, "output_directory": str(target),
               "entity_overrides": overrides,
               "entity_overrides_sha256": hashlib.sha256(overrides_path.read_bytes()).hexdigest() if overrides_path else None,
               "quarter_supplements_sha256": hashlib.sha256(supplements_path.read_bytes()).hexdigest() if supplements_path else None,
               "information_cutoff": information_cutoff,
               "source_universe_observation_date": universe["as_of"],
               "research_normalized_count": len(snapshots), "data_unavailable_count": len(audits) - len(snapshots),
               "fully_verified_economic_basis_count": 0, "institutional_consensus_count": 0,
               "ttm_method_counts": dict(Counter(a["ttm_method"] for a in audits if a["status"] == "research_normalized")),
               "annual_history_count_distribution": dict(sorted(Counter(len(s["history"]) for s in snapshots).items())),
               "history_growth_count_distribution": dict(sorted(Counter(a["history_growth_observations"] for a in audits if a["status"] == "research_normalized").items())),
               "nonpositive_current_ttm_count": sum(s["eps0"] <= 0 for s in snapshots),
               "price_dates": dict(Counter(s["price_date"] for s in snapshots)),
               "price_selection_counts": dict(Counter(s["price_selection_status"] for s in snapshots)),
               "normalized_prices_sha256": hashlib.sha256(price_bytes).hexdigest() if price_bytes else None,
               "availability_policy": ("observed_by_decision_timestamp" if information_cutoff else
                   "current source retrieval UTC date plus one; historical annual filings separately retain filed+1 availability"),
               "source_manifest_hashes": {"sec": hashlib.sha256(sec_bytes).hexdigest(), "yahoo": hashlib.sha256(yahoo_bytes).hexdigest()},
               "model_parameters_changed": False, "uses_test_data": False,
               "limitations": ["These are research-normalized inputs, not full economic-basis certification.",
                               "Missing consensus leaves default AI queue gate closed.",
                               "No probabilities are computed by this script; negative EPS remains present.",
                               "Latest-report checks cover the archived EPS sources, not a complete independent earnings-release search."]}
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "snapshots.json", {"snapshots": snapshots})
    write_json(target / "normalization_audit.json", {"summary": summary, "stocks": audits})
    write_json(target / "missing_reasons.json", {"as_of": as_of, "stocks": [a for a in audits if a["status"] != "research_normalized"]})
    write_json(target / "missing_reasons_all_stocks.json", {"as_of": as_of, "stocks": [
        {"symbol": a["symbol"], "status": a["status"],
         "missing_reasons": ([a["reason"]] if a["status"] != "research_normalized" else [])
                            + ["same_basis_institutional_consensus_missing", "full_economic_basis_not_verified"]}
        for a in audits]})
    write_json(target / "summary.json", summary)
    (target / "sec_manifest_snapshot.jsonl").write_bytes(sec_bytes)
    (target / "yahoo_actual_manifest_snapshot.jsonl").write_bytes(yahoo_bytes)
    if price_bytes:
        (target / "prices_input_snapshot.json").write_bytes(price_bytes)
    if overrides:
        write_json(target / "entity_overrides_input_snapshot.json", overrides)
    if supplements:
        write_json(target / "quarter_supplements_input_snapshot.json", supplements)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=STUDY)
    parser.add_argument("--as-of", default="2026-11-07")
    parser.add_argument("--history-policy", choices=("latest_available", "strict_overlap"), default="latest_available")
    parser.add_argument("--entity-overrides", type=Path, help="Symbol-keyed entity boundary or reviewed share-basis bridge JSON")
    parser.add_argument("--output", type=Path, help="New isolated normalization directory; existing outputs are never overwritten")
    parser.add_argument("--quarter-supplements", type=Path, help="Symbol-keyed reviewed official single-quarter EPS with archived evidence")
    parser.add_argument("--information-cutoff", help="Explicit observed-information UTC timestamp; omitting preserves legacy next-day policy")
    args = parser.parse_args()
    print(json.dumps(build(args.study.resolve(), args.as_of, history_policy=args.history_policy,
                           output_dir=args.output, entity_overrides=args.entity_overrides,
                           quarter_supplements=args.quarter_supplements, information_cutoff=args.information_cutoff), indent=2), flush=True)
