"""Evidence-gated fiscal calendars for annual forecast labels only.

Rules calculate exact dates. They never relabel actual annual or TTM periods,
and a nearby date alone is not evidence that two periods are equivalent.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit


class FiscalCalendarError(ValueError):
    pass


def _date(value, field):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError) as exc:
        raise FiscalCalendarError(f"invalid_{field}") from exc


def _time(value, field):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise FiscalCalendarError(f"invalid_{field}") from exc


def _cik(value):
    raw = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", raw):
        raise FiscalCalendarError("invalid_issuer_cik")
    return raw.zfill(10)


def _rule(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "month", "weekday"}
            or value["kind"] not in {"last_weekday_in_month", "weekday_nearest_month_end"}
            or type(value["month"]) is not int or not 1 <= value["month"] <= 12
            or type(value["weekday"]) is not int or not 0 <= value["weekday"] <= 6):
        raise FiscalCalendarError("invalid_fiscal_calendar_rule")
    return deepcopy(value)


def derive_fiscal_end(rule, anchor_year):
    """Pure calendar calculation; anchor year identifies the reference month.

    This calculation alone does not authorize forecast or actual-data changes.
    Weekdays use Python's convention: Monday=0 through Sunday=6.
    """
    rule = _rule(rule)
    if type(anchor_year) is not int or not 2 <= anchor_year <= 9998:
        raise FiscalCalendarError("invalid_calendar_anchor_year")
    month_end = date(anchor_year, rule["month"], monthrange(anchor_year, rule["month"])[1])
    if rule["kind"] == "last_weekday_in_month":
        delta = -((month_end.weekday() - rule["weekday"]) % 7)
    else:
        delta = (rule["weekday"] - month_end.weekday() + 3) % 7 - 3
    return month_end + timedelta(days=delta)


def _anchor_year_for_exact_end(period, rule):
    years = [year for year in range(max(2, period.year - 1), min(9998, period.year + 1) + 1)
             if derive_fiscal_end(rule, year) == period]
    if len(years) != 1:
        raise FiscalCalendarError("actual_fiscal_end_disagrees_with_reviewed_rule")
    return years[0]


def review_calendar(config, snapshot, *, information_cutoff, evidence_root):
    """Validate identity, dated review evidence, and three actual fiscal years."""
    if not isinstance(config, dict) or config.get("review_status") != "reviewed":
        raise FiscalCalendarError("calendar_requires_explicit_review")
    cutoff = _time(information_cutoff, "information_cutoff")
    reviewed = _time(config.get("reviewed_at"), "calendar_review_timestamp")
    if reviewed > cutoff:
        raise FiscalCalendarError("calendar_review_after_information_cutoff")
    if (config.get("symbol", snapshot.get("symbol")) != snapshot.get("symbol")
            or _cik(config.get("cik")) != _cik(snapshot.get("cik"))):
        raise FiscalCalendarError("calendar_issuer_identity_mismatch")
    if (config.get("eps_basis") != "GAAP_diluted" or snapshot.get("eps_basis") != config["eps_basis"]
            or not config.get("share_basis") or config["share_basis"] != snapshot.get("share_basis")):
        raise FiscalCalendarError("calendar_accounting_or_share_basis_mismatch")
    rule = _rule(config.get("rule"))
    start, end = (_date(config.get(key), "calendar_" + key) for key in ("valid_from", "valid_through"))
    if not start <= cutoff.date() <= end:
        raise FiscalCalendarError("calendar_review_not_current_or_expired")
    evidence = config.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise FiscalCalendarError("calendar_requires_source_evidence")
    root, sources = Path(evidence_root).resolve(), []
    for item in evidence:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
            for key in ("source_url", "source_file", "sha256", "locator", "financial_available_at", "retrieved_at")):
            raise FiscalCalendarError("calendar_source_evidence_incomplete")
        url = urlsplit(item["source_url"])
        if url.scheme not in {"https", "http"} or not url.hostname:
            raise FiscalCalendarError("calendar_source_url_invalid")
        sec_cik = re.search(r"/Archives/edgar/data/(\d+)/", url.path)
        if url.hostname in {"www.sec.gov", "sec.gov"} and sec_cik and _cik(sec_cik.group(1)) != _cik(snapshot["cik"]):
            raise FiscalCalendarError("calendar_source_issuer_identity_mismatch")
        available = _date(item["financial_available_at"], "calendar_financial_availability")
        retrieved = _time(item["retrieved_at"], "calendar_source_retrieval")
        if available > cutoff.date() or available > retrieved.date() or retrieved > reviewed:
            raise FiscalCalendarError("calendar_source_unavailable_by_review_or_cutoff")
        path = (root / item["source_file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise FiscalCalendarError("calendar_source_file_missing_or_outside_evidence_root")
        computed = hashlib.sha256(path.read_bytes()).hexdigest()
        if computed != item["sha256"]:
            raise FiscalCalendarError("calendar_source_hash_mismatch")
        sources.append({**deepcopy(item), "source_file": str(path), "sha256_verified": True})
    latest = {}
    for row in snapshot.get("history", []):
        available = _date(row.get("available_at"), "annual_availability")
        if available > cutoff.date():
            continue
        source = row.get("source")
        observed = row.get("retrieved_at", source.get("retrieved_at") if isinstance(source, dict) else None)
        if observed is not None and _time(observed, "annual_source_retrieval") > cutoff:
            continue
        period = _date(row.get("period_end"), "annual_period_end")
        old = latest.get(period)
        if old is None or row["available_at"] > old["available_at"]:
            latest[period] = row
        elif row["available_at"] == old["available_at"] and row.get("period_start") != old.get("period_start"):
            raise FiscalCalendarError("ambiguous_actual_annual_period")
    selected = sorted(latest.items())[-3:]
    if len(selected) < 3:
        raise FiscalCalendarError("three_complete_actual_annual_periods_required")
    years, annuals = [], []
    previous_end = None
    for period, row in selected:
        annual_start = _date(row.get("period_start"), "annual_period_start")
        if (row.get("measure", "annual") != "annual" or (period - annual_start).days + 1 not in (364, 371)
                or (previous_end is not None and annual_start != previous_end + timedelta(days=1))):
            raise FiscalCalendarError("actual_period_not_contiguous_complete_week_based_year")
        if not start <= period <= end or period > _date(row["available_at"], "annual_availability"):
            raise FiscalCalendarError("actual_annual_period_outside_review_scope_or_publication")
        if any(row.get(key, snapshot[key]) != snapshot[key] for key in ("eps_basis", "share_basis")):
            raise FiscalCalendarError("actual_annual_accounting_or_share_basis_mismatch")
        year = _anchor_year_for_exact_end(period, rule)
        years.append(year)
        annuals.append({"period_start": annual_start.isoformat(), "period_end": period.isoformat(),
                       "available_at": row["available_at"], "calendar_anchor_year": year,
                       "source_url": row.get("source_url"), "accession": row.get("accession")})
        previous_end = period
    if years != list(range(years[-1] - 2, years[-1] + 1)):
        raise FiscalCalendarError("actual_fiscal_years_not_consecutive")
    return {"status": "reviewed_validated", "supported": True, "symbol": snapshot["symbol"], "cik": _cik(snapshot["cik"]),
            "eps_basis": config["eps_basis"], "share_basis": config["share_basis"], "rule": rule,
            "valid_from": start.isoformat(), "valid_through": end.isoformat(), "reviewed_at": reviewed.isoformat(),
            "information_cutoff": cutoff.isoformat(), "evidence": sources, "annual_sources": annuals,
            "method": "evidenced_fiscal_rule_validated_against_three_actual_years",
            "full_economic_basis_verified": False, "future_dates_directly_printed_in_source": False,
            "limitation": "Future dates implement the reviewed continuing issuer policy; no actual EPS period is relabelled."}


def map_forecast_target(as_reported_target_date, review, *, anchor_year=None, purpose="annual_forecast"):
    """Map a month-end forecast label, or preserve an already exact fiscal end."""
    if purpose != "annual_forecast":
        raise FiscalCalendarError("fiscal_calendar_mapping_is_for_forecasts_not_actual_or_ttm")
    if (not isinstance(review, dict) or review.get("status") != "reviewed_validated"
            or not review.get("evidence") or len(review.get("annual_sources", [])) < 3):
        raise FiscalCalendarError("validated_fiscal_calendar_review_required")
    reported = _date(as_reported_target_date, "as_reported_target_date")
    rule = _rule(review["rule"])
    candidates = [anchor_year] if anchor_year is not None else range(max(2, reported.year - 1), min(9998, reported.year + 1) + 1)
    matches = []
    for year in candidates:
        target = derive_fiscal_end(rule, year)
        month_end = date(year, rule["month"], monthrange(year, rule["month"])[1])
        if reported in {target, month_end}:
            matches.append((year, target, month_end))
    if len(matches) != 1:
        raise FiscalCalendarError("reported_date_is_neither_exact_fiscal_end_nor_configured_month_end")
    year, target, month_end = matches[0]
    if not _date(review["valid_from"], "calendar_valid_from") <= target <= _date(review["valid_through"], "calendar_valid_through"):
        raise FiscalCalendarError("forecast_fiscal_date_outside_review_validity")
    return {"as_reported_target_date": as_reported_target_date, "target_date": target.isoformat(),
            "calendar_anchor_year": year, "reference_month_end": month_end.isoformat(),
            "mapping_applied": reported != target, "method": "reviewed_calendar_rule" if reported != target else "already_exact_reviewed_fiscal_end",
            "rule": deepcopy(rule), "calendar_reviewed_at": review["reviewed_at"],
            "future_dates_directly_printed_in_source": False, "actual_or_ttm_period_changed": False}


def prior_actual_period_for_forecast(mapping, review):
    """Find the previous fiscal year by the rule, never subtract one date year."""
    if review.get("status") != "reviewed_validated":
        raise FiscalCalendarError("validated_fiscal_calendar_review_required")
    return derive_fiscal_end(review["rule"], mapping["calendar_anchor_year"] - 1).isoformat()
