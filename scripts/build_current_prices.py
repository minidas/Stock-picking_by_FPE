"""Normalize archived daily quote.close values for the current study, offline.

Only completed US regular sessions are eligible: 16:00 America/New_York plus
15 minutes must precede actual acquisition.  Dividend-adjusted adjclose is
never read.  Missing downloads remain explicit, so this script can be rerun
after the separate collector finishes.  No files under data/ are read.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
QUOTES = STUDY / "evidence/quotes"
OUTPUT = STUDY / "normalized/prices.json"
AS_OF = date(2026, 11, 7)
MARKET_TZ = ZoneInfo("America/New_York")
CLIENT_TZ = ZoneInfo("Asia/Shanghai")
MAX_PRICE_AGE_DAYS = 7


class PriceError(ValueError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def numeric(value, label: str, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise PriceError(f"invalid_{label}")
    if positive and value <= 0:
        raise PriceError(f"nonpositive_{label}")
    return float(value)


def timestamp(value, label: str) -> datetime:
    seconds = numeric(value, label)
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise PriceError(f"invalid_{label}") from exc


def read_manifest() -> tuple[list[dict], dict]:
    path = QUOTES / "manifest.jsonl"
    if not path.exists():
        return [], {"path": rel(path), "sha256": None, "missing": True}
    raw = path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    rows, ignored_partial_line = [], False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith(b"\n"):
                ignored_partial_line = True
                break
            raise PriceError("invalid_manifest_jsonl") from None
        if not isinstance(row, dict):
            raise PriceError("invalid_manifest_record")
        rows.append(row)
    return rows, {"path": rel(path), "sha256": sha256(raw),
                  "record_count": len(rows), "ignored_partial_last_line": ignored_partial_line}


def normalize(symbol: str, raw: bytes, source: dict) -> dict:
    try:
        retrieved = datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            raise ValueError("timezone missing")
    except (ValueError, TypeError, KeyError) as exc:
        raise PriceError("invalid_retrieval_timestamp") from exc
    retrieved = retrieved.astimezone(timezone.utc)
    available = retrieved.date() + timedelta(days=1)
    if available > AS_OF:
        raise PriceError("retrieval_not_yet_available_under_next_utc_day_rule")
    if retrieved.astimezone(CLIENT_TZ).date() > AS_OF:
        raise PriceError("retrieval_after_decision_date")
    try:
        payload = json.loads(raw)
        chart = payload["chart"]
        if chart.get("error") is not None or len(chart["result"]) != 1:
            raise PriceError("chart_error_or_ambiguous_results")
        result = chart["result"][0]
        meta = result["meta"]
        quotes = result["indicators"]["quote"]
        if len(quotes) != 1:
            raise PriceError("ambiguous_quote_series")
        closes = quotes[0]["close"]
        times = result["timestamp"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PriceError("invalid_chart_schema") from exc
    expected_alias = symbol.replace(".", "-")
    if meta.get("symbol") not in {symbol, expected_alias}:
        raise PriceError("provider_symbol_mismatch")
    if meta.get("currency") != "USD":
        raise PriceError("non_usd_price")
    if meta.get("instrumentType") != "EQUITY":
        raise PriceError("non_equity_instrument")
    if meta.get("exchangeTimezoneName") != "America/New_York":
        raise PriceError("unsupported_exchange_timezone")
    if meta.get("dataGranularity") != "1d":
        raise PriceError("non_daily_bar_interval")
    if not isinstance(times, list) or not isinstance(closes, list) or len(times) != len(closes):
        raise PriceError("unaligned_timestamp_close_arrays")
    candidates, ineligible = [], []
    for index, seconds in enumerate(times):
        observed = timestamp(seconds, "bar_timestamp")
        local_day = observed.astimezone(MARKET_TZ).date()
        close_at = datetime.combine(local_day, time(16), MARKET_TZ)
        eligible_at = close_at + timedelta(minutes=15)
        if local_day.weekday() >= 5 or local_day > AS_OF or observed > retrieved or eligible_at > retrieved:
            ineligible.append(local_day.isoformat())
            continue
        candidates.append((local_day, index, observed, close_at, eligible_at))
    if not candidates:
        raise PriceError("no_completed_regular_session_bar")
    latest_day = max(row[0] for row in candidates)
    latest = [row for row in candidates if row[0] == latest_day]
    if len(latest) != 1:
        raise PriceError("duplicate_latest_session_bars")
    local_day, index, observed, close_at, eligible_at = latest[0]
    # A missing latest completed close is not silently replaced by an older one.
    price = numeric(closes[index], "latest_completed_close", positive=True)
    if (AS_OF - local_day).days > MAX_PRICE_AGE_DAYS:
        raise PriceError("price_stale_over_seven_days")

    events = result.get("events", {}).get("splits", {})
    if not isinstance(events, dict):
        raise PriceError("invalid_split_events")
    splits = []
    for event_id, event in events.items():
        if not isinstance(event, dict):
            raise PriceError("invalid_split_event")
        effective = timestamp(event.get("date"), "split_timestamp")
        numerator = numeric(event.get("numerator"), "split_numerator", positive=True)
        denominator = numeric(event.get("denominator"), "split_denominator", positive=True)
        splits.append({
            "provider_event_id": event_id,
            "effective_at": effective.isoformat(),
            "effective_date": effective.astimezone(MARKET_TZ).date().isoformat(),
            "numerator": numerator, "denominator": denominator,
            "new_shares_per_old_share": numerator / denominator,
            "provider_split_ratio": event.get("splitRatio"),
            "effective_after_retrieval": effective > retrieved,
        })
    splits.sort(key=lambda event: (event["effective_date"], event["provider_event_id"]))
    intervening = [event for event in splits if local_day.isoformat() < event["effective_date"] <= AS_OF.isoformat()]
    return {
        "symbol": symbol, "as_of": AS_OF.isoformat(), "status": "validated_price_observation",
        "price": price, "price_date": local_day.isoformat(), "currency": "USD",
        "price_kind": "yahoo_chart_daily_regular_session_quote_close",
        "price_source": source["url"], "provider_symbol": meta["symbol"],
        "exchange": meta.get("exchangeName"), "exchange_timezone": "America/New_York",
        "source_available_at": available.isoformat(),
        "session_close_at": close_at.isoformat(), "eligible_at": eligible_at.isoformat(),
        "bar_timestamp": observed.isoformat(), "bar_array_index": index,
        "price_basis_note": "Exact provider quote.close, without rounding or dividend adjustment; adjclose was not read. Corporate-action consistency requires the separate split review.",
        "splits_observed": splits, "splits_after_price_before_asof": intervening,
        "share_basis_review_required": bool(intervening),
        "split_coverage_note": "Only split events present in the archived one-month response; absence is not exhaustive corporate-action verification through the decision date.",
        "discarded_incomplete_or_future_bar_dates": sorted(set(ineligible)),
        "source": {"raw_path": source["file"].replace("\\", "/"), "sha256": source["sha256"],
                   "retrieved_at": source["retrieved_at"], "url": source["url"],
                   "http_status": source["status"]},
    }


def main() -> None:
    universe_path = STUDY / "input/universe.json"
    universe_raw = universe_path.read_bytes()
    members = json.loads(universe_raw)["members"]
    symbols = [member["symbol"] for member in members]
    if len(symbols) != len(set(symbols)):
        raise PriceError("duplicate_universe_symbols")
    records, manifest = read_manifest()
    prices, missing = {}, {}
    for symbol in sorted(symbols):
        path = QUOTES / (symbol + ".json")
        matching = [r for r in records if r.get("symbol") == symbol]
        if not path.exists():
            missing[symbol] = {"reason": "quote_download_missing", "latest_attempt": matching[-1] if matching else None}
            continue
        raw = path.read_bytes()
        raw_hash = sha256(raw)
        eligible = [r for r in matching if r.get("status") == 200 and r.get("sha256") == raw_hash
                    and r.get("file", "").replace("\\", "/") == rel(path)]
        if not eligible:
            missing[symbol] = {"reason": "no_successful_manifest_record_matching_raw_hash", "raw_path": rel(path), "sha256": raw_hash}
            continue
        source = sorted(eligible, key=lambda r: r["retrieved_at"])[-1]
        try:
            prices[symbol] = normalize(symbol, raw, source)
        except PriceError as exc:
            missing[symbol] = {"reason": str(exc), "raw_path": rel(path), "sha256": raw_hash,
                               "retrieved_at": source.get("retrieved_at"), "source_url": source.get("url")}
    payload = {
        "as_of": AS_OF.isoformat(), "decision_timezone": "Asia/Shanghai",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "normalization": "Latest completed daily US regular session; 16:00 America/New_York plus 15 minutes <= actual retrieval time. Conservative for shortened sessions. Exact quote.close, never adjclose.",
        "input_universe": {"path": rel(universe_path), "sha256": sha256(universe_raw)},
        "manifest_snapshot": manifest, "prices": prices, "missing": missing,
        "summary": {"expected_count": len(symbols), "validated_price_count": len(prices),
                    "missing_count": len(missing),
                    "price_date_counts": dict(sorted(Counter(p["price_date"] for p in prices.values()).items())),
                    "missing_reason_counts": dict(sorted(Counter(p["reason"] for p in missing.values()).items())),
                    "split_review_symbols": sorted(s for s, p in prices.items() if p["share_basis_review_required"])},
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": rel(OUTPUT), **payload["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
