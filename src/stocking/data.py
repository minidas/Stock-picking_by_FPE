"""Vendor-neutral inputs and auditable raw downloads; no inferred EPS basis.

Dates in model inputs are end-of-day dates. Archive retrieval uses UTC timestamps.
Downloaded current estimates cannot be relabelled as historical estimates.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ttm import TTMError, validate_ttm_snapshot


class DataError(ValueError):
    """An input fails the explicit data contract."""


def _date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DataError(f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(f"Invalid {name}: {value}") from exc


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"{name} must be a finite JSON number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise DataError(f"Invalid {name}")
    return float(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise DataError(f"Non-finite JSON number: {value}")


def load_json(path: str | Path) -> Any:
    path = Path(path)
    if path.stat().st_size > 100_000_000:
        raise DataError("JSON input exceeds 100 MB; partition the input")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    except json.JSONDecodeError as exc:
        raise DataError(f"Invalid JSON in {path.name}: line {exc.lineno}") from exc


def validate_snapshot(snapshot: dict, require_provenance: bool = True) -> dict:
    """Validate format/bases; preserve future records for model exclusion/auditing."""
    if not isinstance(snapshot, dict):
        raise DataError("Each snapshot must be a JSON object")
    out = deepcopy(snapshot)
    for key in ("symbol", "currency", "eps_basis", "share_basis"):
        if not isinstance(out.get(key), str) or not out[key].strip():
            raise DataError(f"Missing/non-string {key}")
    if not re.fullmatch(r"[A-Za-z0-9._^=/-]{1,40}", out["symbol"]):
        raise DataError("Unsupported symbol syntax")
    if out["currency"] != "USD":
        raise DataError("This implementation requires USD price and EPS")
    as_of = _date(out.get("as_of"), "as_of")
    price_date = _date(out.get("price_date"), "price_date")
    period = _date(out.get("eps_period_end"), "eps_period_end")
    available = _date(out.get("eps_available_at"), "eps_available_at")
    if price_date > as_of or period > available or available > as_of:
        raise DataError("Price/EPS chronology invalid or uses future information")
    _number(out.get("price"), "price", positive=True)
    _number(out.get("eps0"), "eps0")  # <=0 is handled by model abstention, not discarded.
    if require_provenance and out.get("synthetic") is not True:
        source = out.get("source")
        if not isinstance(source, (str, dict)) or not source:
            raise DataError("Real snapshots require source provenance")
    for collection, date_key, value_key in (("history", "period_end", "eps"),
                                             ("consensus", "target_date", "eps_mean")):
        rows = out.setdefault(collection, [])
        if not isinstance(rows, list):
            raise DataError(f"{collection} must be a list")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DataError(f"{collection} row must be an object")
            target = _date(row.get(date_key), f"{collection}.{date_key}")
            publication = _date(row.get("available_at"), f"{collection}.available_at")
            _number(row.get(value_key), f"{collection}.{value_key}")
            if collection == "history" and target > publication:
                raise DataError("Historical earnings published before period end")
            for key in ("eps_basis", "share_basis"):
                if collection == "consensus" and key not in row:
                    raise DataError(f"Consensus requires explicit {key}")
                if row.get(key, out[key]) != out[key]:
                    raise DataError(f"Mixed {key} in {collection}; reconcile upstream")
            if row.get("currency", "USD") != "USD":
                raise DataError("Mixed EPS currency")
            if collection == "consensus":
                count = row.get("analysts")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise DataError("analysts must be a positive integer")
                if row.get("eps_std") is not None and _number(row["eps_std"], "eps_std") < 0:
                    raise DataError("eps_std cannot be negative")
            identity = (target, publication)
            if identity in seen:
                raise DataError(f"Ambiguous duplicate {collection} period/vintage")
            seen.add(identity)
    try:
        validate_ttm_snapshot(out)
    except TTMError as exc:
        raise DataError(str(exc)) from exc
    return out


def load_snapshots(path: str | Path, require_provenance: bool = True) -> list[dict]:
    payload = load_json(path)
    if isinstance(payload, dict):
        payload = payload["snapshots"] if "snapshots" in payload else [payload]
    if not isinstance(payload, list) or not payload:
        raise DataError("Expected nonempty snapshot list")
    rows = [validate_snapshot(row, require_provenance) for row in payload]
    identities = [(row["symbol"], row["as_of"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise DataError("Duplicate symbol/as_of snapshots")
    return rows


def load_memberships(path: str | Path) -> list[dict]:
    """Read inclusive entry/exclusive exit intervals, not today's survivor list."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = load_json(path)
        if isinstance(rows, dict):
            rows = rows.get("memberships")
    if not isinstance(rows, list):
        raise DataError("Memberships must be a list or a CSV table")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol") or not row.get("source"):
            raise DataError("Membership row requires symbol and source")
        start = _date(row.get("effective_from"), "effective_from")
        _date(row.get("available_at"), "available_at")
        end = _date(row["effective_to"], "effective_to") if row.get("effective_to") else None
        if end and end <= start:
            raise DataError("Membership exit must be after entry")
        result.append(dict(row, effective_to=end.isoformat() if end else None))
    return result


def universe_at(memberships: list[dict], as_of: str) -> list[str]:
    cutoff = _date(as_of, "as_of")
    members = []
    for row in memberships:
        if (_date(row["available_at"], "available_at") <= cutoff
                and _date(row["effective_from"], "effective_from") <= cutoff
                and (not row.get("effective_to") or cutoff < _date(row["effective_to"], "effective_to"))):
            members.append(row["symbol"])
    if len(members) != len(set(members)):
        raise DataError("Overlapping membership rows at selected date")
    return sorted(members)


def coverage_report(snapshots: list[dict], memberships: list[dict], as_of: str) -> dict:
    expected = set(universe_at(memberships, as_of))
    supplied = {row["symbol"] for row in snapshots if row["as_of"] == as_of}
    return {"as_of": as_of, "expected_count": len(expected), "supplied_count": len(supplied),
            "covered_count": len(expected & supplied), "missing_symbols": sorted(expected - supplied),
            "extra_symbols": sorted(supplied - expected),
            "complete": bool(expected) and expected == supplied}


def _download_json(url: str, *, headers: dict | None = None, secret: str | None = None) -> tuple[dict, bytes]:
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=45) as response:
            raw = response.read(50_000_001)
        if len(raw) > 50_000_000:
            raise DataError("Provider response exceeded 50 MB")
        payload = json.loads(raw, parse_constant=_bad_constant)
        if not isinstance(payload, dict):
            raise DataError("Provider response is not an object")
        return payload, raw
    except DataError:
        raise
    except Exception as exc:
        # URLs and remote exception messages may contain the API credential.
        raise DataError(f"Provider request failed ({type(exc).__name__}); check network/entitlement") from None


def _archive(payload: dict, raw: bytes, out_dir: str | Path, provider: str,
             identifier: str, endpoint: str) -> Path:
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(raw).hexdigest()
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{provider}_{identifier}_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{digest[:12]}.json"
    archive = {"metadata": {"provider": provider, "endpoint": endpoint,
                            "retrieved_at": now.isoformat(), "sha256_raw_response": digest,
                            "vintage_type": "current_retrieval_not_historical_consensus",
                            "normalization_required": True}, "payload": payload}
    # Keep exact response bytes alongside the parsed archive so its hash is verifiable.
    raw_path = path.with_suffix(".raw.json")
    with raw_path.open("xb") as handle:
        handle.write(raw)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(archive, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def fetch_alpha_vantage(symbol: str, out_dir: str | Path,
                       function: str = "EARNINGS_ESTIMATES", api_key: str | None = None) -> Path:
    """Archive current vendor response. Key is never written to metadata/errors."""
    if not re.fullmatch(r"[A-Za-z0-9.^-]{1,20}", symbol):
        raise DataError("Invalid provider ticker")
    if function not in {"EARNINGS_ESTIMATES", "EARNINGS", "GLOBAL_QUOTE", "SPLITS"}:
        raise DataError("Unsupported Alpha Vantage function")
    key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key or key == "demo":
        raise DataError("Set ALPHAVANTAGE_API_KEY to an entitled API key; demo data cannot power this tool")
    base = "https://www.alphavantage.co/query"
    payload, raw = _download_json(base + "?" + urlencode({"function": function, "symbol": symbol, "apikey": key}), secret=key)
    for error_key in ("Error Message", "Information", "Note"):
        if error_key in payload:
            raise DataError(f"Alpha Vantage returned {error_key}; check entitlement/rate limit; no data archived")
    if not payload:
        raise DataError("Alpha Vantage returned empty data")
    # Do not persist an accidentally echoed credential, including in raw bytes.
    if key.encode() in raw:
        raise DataError("Provider response unexpectedly contains credential; refused archive")
    return _archive(payload, raw, out_dir, "alphavantage", f"{symbol}_{function}",
                    base + "?" + urlencode({"function": function, "symbol": symbol}))


def fetch_sec_companyfacts(cik: str | int, out_dir: str | Path,
                          user_agent: str | None = None) -> Path:
    """Archive filings. Caller must reconcile restatements, duration and stock splits."""
    digits = str(cik)
    if not re.fullmatch(r"\d{1,10}", digits):
        raise DataError("CIK must contain 1-10 digits")
    agent = user_agent or os.environ.get("SEC_USER_AGENT")
    if not agent or "@" not in agent:
        raise DataError("Set SEC_USER_AGENT to your app/contact email per SEC fair-access guidance")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{digits.zfill(10)}.json"
    payload, raw = _download_json(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
    if "facts" not in payload or "cik" not in payload:
        raise DataError("SEC response is missing company facts")
    return _archive(payload, raw, out_dir, "sec", digits.zfill(10), url)


def normalize_consensus_rows(rows: list[dict], *, field_map: dict, retrieved_at: str,
                             eps_basis: str, share_basis: str, source: str,
                             annual_only_confirmed: bool = False) -> list[dict]:
    """Explicit mapping after annual-period/basis review; never guess vendor semantics.

    field_map maps target_date/eps_mean/analysts and optional eps_std to raw keys.
    For a current download, availability is conservatively the following UTC date.
    This function cannot reconstruct historical vintages from today's response.
    """
    if not annual_only_confirmed:
        raise DataError("Confirm rows are annual EPS forecasts, not quarterly/revenue values")
    if not eps_basis or not share_basis or not source:
        raise DataError("Explicit EPS/share bases and source required")
    if not all(key in field_map for key in ("target_date", "eps_mean", "analysts")):
        raise DataError("field_map requires target_date, eps_mean and analysts")
    try:
        timestamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Missing timezone")
    except (ValueError, AttributeError) as exc:
        raise DataError("retrieved_at requires an ISO timestamp with timezone") from exc
    availability = (timestamp.astimezone(timezone.utc).date() + timedelta(days=1)).isoformat()
    normalized = []
    for row in rows:
        try:
            target = str(row[field_map["target_date"]])
            _date(target, "target_date")
            mean = float(row[field_map["eps_mean"]])
            count_float = float(row[field_map["analysts"]])
            if not math.isfinite(count_float) or not count_float.is_integer() or count_float < 1:
                raise ValueError("Invalid analyst count")
            record = {"target_date": target, "available_at": availability, "eps_mean": mean,
                      "analysts": int(count_float), "eps_basis": eps_basis,
                      "share_basis": share_basis, "source": source}
            _number(mean, "eps_mean")
            if "eps_std" in field_map and row.get(field_map["eps_std"]) not in (None, "", "None"):
                record["eps_std"] = float(row[field_map["eps_std"]])
                if _number(record["eps_std"], "eps_std") < 0:
                    raise ValueError("Negative dispersion")
            normalized.append(record)
        except (ValueError, TypeError, KeyError) as exc:
            raise DataError(f"Cannot normalize consensus row: {type(exc).__name__}") from exc
    return normalized
