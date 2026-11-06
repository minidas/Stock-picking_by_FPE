"""Explicit trailing-twelve-month EPS contracts and auditable anchor updates.

The sum of four reported quarterly diluted EPS values is a defined research
measure. It is not a recomputed annual GAAP diluted EPS: weighted-average
shares and dilution decisions can differ by quarter. Annual growth history
remains a separate, nonoverlapping series and is never rescaled or extended
by this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
from urllib.parse import urlparse


class TTMError(ValueError):
    """A declared EPS measure violates its period or provenance contract."""


METHODS = {
    "four_quarter_diluted_eps_sum": "Sum of four reported quarterly diluted EPS values; not recomputed annual GAAP diluted EPS.",
    "direct_reported_ttm": "A directly reported twelve-month diluted EPS measure; the stated source methodology still requires review.",
    "annual_report_latest_ttm": "A full annual EPS observation declared to be the latest reported twelve-month period.",
}
FULL_YEAR_DAYS = {364, 365, 366, 371}  # 52/53 fiscal weeks or calendar years.


def _date(value, label):
    if not isinstance(value, str):
        raise TTMError(f"{label} must be an ISO date")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise TTMError(f"{label} must be an ISO date") from exc
    if result.isoformat() != value:
        raise TTMError(f"{label} must use YYYY-MM-DD")
    return result


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TTMError(f"{label} must be a finite number")
    return float(value)


def _same_number(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)


def _year_interval(start, end, label):
    if (end - start).days + 1 not in FULL_YEAR_DAYS:
        raise TTMError(f"{label} must span one calendar year or 52/53 fiscal weeks")


def _history_measure(row):
    measures = [row[key] for key in ("measure", "eps_measure") if key in row]
    if any(not isinstance(measure, str) for measure in measures):
        raise TTMError("Explicit history measure must be annual")
    if len(set(measures)) > 1:
        raise TTMError("Conflicting history measure labels")
    return measures[0] if measures else None


def validate_annual_history(snapshot, asof):
    """Validate explicit period metadata without reinterpreting legacy history."""
    latest = {}
    rows = snapshot.get("history", [])
    if not isinstance(rows, list):
        raise TTMError("history must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise TTMError("history rows must be objects")
        published = _date(row.get("available_at"), "history.available_at")
        if published > asof:
            continue  # Existing model marks future history as excluded.
        end = _date(row.get("period_end"), "history.period_end")
        measure = _history_measure(row)
        if measure not in (None, "annual"):
            raise TTMError("history must contain nonoverlapping annual observations, not TTM or quarterly rows")
        if measure == "annual":
            start = _date(row.get("period_start"), "history.period_start")
            _year_interval(start, end, "Explicit annual history")
        previous = latest.get(end)
        if previous is None or published > previous[0]:
            latest[end] = (published, row)
        elif published == previous[0] and (measure is not None or _history_measure(previous[1]) is not None):
            if row.get("period_start") != previous[1].get("period_start") or row.get("eps") != previous[1].get("eps"):
                raise TTMError("Conflicting annual history records with identical dates")
    previous_end = None
    for end, (_, row) in sorted(latest.items()):
        if _history_measure(row) == "annual":
            start = _date(row["period_start"], "history.period_start")
            if previous_end is not None and start <= previous_end:
                raise TTMError("Explicit annual history periods overlap")
        previous_end = end
    return latest


def validate_ttm_snapshot(snapshot: dict) -> None:
    """Validate TTM metadata and explicit history measures at every API entry.

    An absent ``eps_measure`` remains a legacy annual input. No inference from
    a free-text ``eps_basis`` label can silently turn it into validated TTM.
    The contract verifies declarations, dates and arithmetic, not external
    reporting completeness or the truth of a source URL.
    """
    asof = _date(snapshot.get("as_of"), "as_of")
    history = validate_annual_history(snapshot, asof)
    measure = snapshot.get("eps_measure", "annual")
    if measure not in ("annual", "ttm"):
        raise TTMError("eps_measure must be annual or ttm")
    if measure != "ttm":
        if "ttm_provenance" in snapshot:
            raise TTMError("TTM provenance requires eps_measure='ttm'")
        if snapshot.get("eps_measure") == "annual" and "eps_period_start" in snapshot:
            _year_interval(_date(snapshot["eps_period_start"], "eps_period_start"),
                           _date(snapshot.get("eps_period_end"), "eps_period_end"), "Annual base EPS")
        return

    start = _date(snapshot.get("eps_period_start"), "eps_period_start")
    end = _date(snapshot.get("eps_period_end"), "eps_period_end")
    available = _date(snapshot.get("eps_available_at"), "eps_available_at")
    if not start <= end <= available <= asof:
        raise TTMError("TTM base EPS chronology is invalid or uses future information")
    _year_interval(start, end, "TTM base EPS")
    base_eps = _number(snapshot.get("eps0"), "eps0")
    provenance = snapshot.get("ttm_provenance")
    if not isinstance(provenance, dict):
        raise TTMError("TTM EPS requires ttm_provenance")
    method = provenance.get("method")
    if not isinstance(method, str) or method not in METHODS:
        raise TTMError("Unsupported TTM construction method")
    components = provenance.get("components")
    expected_count = 4 if method == "four_quarter_diluted_eps_sum" else 1
    if not isinstance(components, list) or len(components) != expected_count:
        raise TTMError(f"{method} requires exactly {expected_count} component(s)")

    selected, seen = [], set()
    for row in components:
        if not isinstance(row, dict):
            raise TTMError("TTM components must be objects")
        component_start = _date(row.get("period_start"), "component.period_start")
        component_end = _date(row.get("period_end"), "component.period_end")
        published = _date(row.get("available_at"), "component.available_at")
        if not component_start <= component_end <= published <= asof:
            raise TTMError("TTM component chronology is invalid or uses future information")
        identity = (component_start, component_end)
        if identity in seen:
            raise TTMError("Duplicate or conflicting TTM component periods; select one as-of vintage")
        seen.add(identity)
        if method == "four_quarter_diluted_eps_sum":
            if not 84 <= (component_end - component_start).days + 1 <= 98:
                raise TTMError("Each TTM quarterly component must cover one 12-to-14-week fiscal quarter")
        else:
            _year_interval(component_start, component_end, "Direct TTM component")
        for key in ("eps_basis", "share_basis"):
            if not isinstance(row.get(key), str) or not row[key].strip() or row[key] != snapshot.get(key):
                raise TTMError(f"TTM component must explicitly match {key}")
        if row.get("currency", snapshot.get("currency")) != snapshot.get("currency"):
            raise TTMError("TTM component currency differs from snapshot")
        url = urlparse(str(row.get("source_url", "")))
        if url.scheme not in ("http", "https") or not url.netloc:
            raise TTMError("TTM component requires an HTTP(S) source_url")
        eps = _number(row.get("eps"), "component.eps")
        selected.append((component_start, component_end, published, eps))
    selected.sort()
    if selected[0][0] != start or selected[-1][1] != end:
        raise TTMError("TTM base period must match the component coverage")
    for previous, current in zip(selected, selected[1:]):
        if current[0] != previous[1] + timedelta(days=1):
            raise TTMError("TTM quarterly components must be contiguous without gaps or overlap")
    if available != max(component[2] for component in selected):
        raise TTMError("TTM availability must equal the latest component availability")
    if not _same_number(base_eps, math.fsum(component[3] for component in selected)):
        raise TTMError("TTM eps0 differs from the sum of the declared components")

    latest = provenance.get("latest_report_period_end")
    if method == "annual_report_latest_ttm" and latest is None:
        raise TTMError("Annual EPS used as latest TTM requires latest_report_period_end")
    if latest is not None and _date(latest, "latest_report_period_end") != end:
        raise TTMError("Declared latest reported period differs from TTM base period")
    # Duplicated anchor metadata must not silently disagree with the snapshot.
    for key, snapshot_key in (("period_start", "eps_period_start"), ("period_end", "eps_period_end"),
                              ("available_at", "eps_available_at"), ("symbol", "symbol"),
                              ("eps_basis", "eps_basis"), ("share_basis", "share_basis")):
        if key in provenance and provenance[key] != snapshot.get(snapshot_key):
            raise TTMError(f"TTM provenance {key} differs from the base snapshot")
    if "as_of" in provenance and provenance["as_of"] != snapshot.get("as_of"):
        raise TTMError("TTM provenance belongs to another as_of date")
    if "eps" in provenance and not _same_number(_number(provenance["eps"], "provenance.eps"), base_eps):
        raise TTMError("TTM provenance EPS differs from eps0")
    # Four-quarter EPS sums need not equal reported annual EPS because their
    # share denominators differ. Only a declared annual-report anchor must agree.
    if method == "annual_report_latest_ttm" and end in history:
        historical_eps = _number(history[end][1].get("eps"), "history.eps")
        if not _same_number(base_eps, historical_eps):
            raise TTMError("Annual-report TTM EPS conflicts with same-period annual history")


def prepare_ttm_snapshot(snapshot: dict, anchor: dict) -> dict:
    """Deep-copy a snapshot and replace only its current EPS anchor and audit.

    ``anchor`` requires symbol, eps, period_start/end, available_at,
    eps_basis/share_basis, method and components. Annual-report anchors also
    require latest_report_period_end. All extra evidence is preserved in
    ttm_provenance; annual history and price are copied without modification.
    """
    if not isinstance(snapshot, dict) or not isinstance(anchor, dict):
        raise TTMError("snapshot and TTM anchor must be objects")
    if anchor.get("symbol") != snapshot.get("symbol"):
        raise TTMError("TTM anchor belongs to another symbol")
    if "cik" in snapshot and anchor.get("cik") != snapshot["cik"]:
        raise TTMError("TTM anchor belongs to another CIK")
    for key in ("eps_basis", "share_basis"):
        if anchor.get(key) != snapshot.get(key) or not isinstance(anchor.get(key), str) or not anchor[key].strip():
            raise TTMError(f"TTM anchor must match snapshot {key}")
    if anchor.get("currency", snapshot.get("currency")) != snapshot.get("currency"):
        raise TTMError("TTM anchor currency differs from snapshot")
    result = deepcopy(snapshot)
    result.update(eps0=anchor.get("eps"), eps_period_start=anchor.get("period_start"),
                  eps_period_end=anchor.get("period_end"), eps_available_at=anchor.get("available_at"),
                  eps_measure="ttm", ttm_provenance=deepcopy(anchor))
    result["ttm_provenance"]["definition"] = METHODS.get(anchor.get("method"), "")
    result["ttm_provenance"]["external_latest_period_completeness_verified"] = False
    validate_ttm_snapshot(result)
    return result
