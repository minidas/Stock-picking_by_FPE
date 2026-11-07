"""Collect dated public Yahoo GAAP responses using its ordinary session flow.

The raw response archive excludes cookies and authentication/crumb endpoints.
No legacy data/ files are read. Acquisition is sequential and resumable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from curl_cffi import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
EVIDENCE = STUDY / "evidence/yahoo"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


class ArchivedSession(requests.Session):
    def __init__(self):
        super().__init__(impersonate="chrome")
        self.records = []

    def request(self, method, url, *args, **kwargs):
        response = super().request(method, url, *args, **kwargs)
        parts = urlsplit(url)
        if any(part in parts.path for part in ("/finance/quoteSummary/", "/fundamentals-timeseries/", "/finance/chart/")):
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            path = EVIDENCE / (digest + ".json")
            if not path.exists():
                path.write_bytes(raw)
            params = dict(parse_qsl(parts.query))
            params.update(kwargs.get("params") or {})
            params = {k: v for k, v in params.items() if k.lower() not in {"crumb", "apikey", "api_key", "token"}}
            record = {"retrieved_at": datetime.now(timezone.utc).isoformat(),
                      "url": urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), "")),
                      "status": response.status_code, "sha256": digest, "bytes": len(raw),
                      "file": str(path.relative_to(ROOT))}
            self.records.append(record)
            with (EVIDENCE / "manifest.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    members = json.loads((STUDY / "input/universe.json").read_text(encoding="utf-8"))["members"]
    allowed = {row["symbol"] for row in members}
    symbols = args.symbols or sorted(allowed)
    if set(symbols) - allowed:
        raise ValueError("Symbol outside current verified universe")
    yf.set_tz_cache_location(str(STUDY / "private_session_cache"))
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    session = ArchivedSession()
    modules = ["earningsTrendGaap", "earningsHistoryGaap", "price", "calendarEvents"]
    failures = 0
    for index, symbol in enumerate(symbols, 1):
        path = STUDY / "collected" / (symbol + ".json")
        if path.exists() and not args.force:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("complete"):
                continue
        ticker_name = symbol.replace(".", "-")
        ticker = yf.Ticker(ticker_name, session=session)
        start = len(session.records)
        collected = {"symbol": symbol, "provider_symbol": ticker_name,
                     "retrieval_started_at": datetime.now(timezone.utc).isoformat(),
                     "yfinance_version": yf.__version__, "complete": False, "errors": []}
        try:
            collected["summary"] = ticker._analysis._fetch(modules)
        except Exception as exc:
            collected["errors"].append({"stage": "summary", "error_type": type(exc).__name__})
        if any(r["status"] == 429 for r in session.records[start:]):
            collected["sources"] = session.records[start:]
            collected["retrieval_finished_at"] = datetime.now(timezone.utc).isoformat()
            dump(path, collected)
            print("Rate limit received; collection stopped. Retry only after an appropriate backoff.", flush=True)
            break
        try:
            url = f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker_name}"
            params = {"symbol": ticker_name,
                      "type": "annualDilutedEPS,quarterlyDilutedEPS,trailingDilutedEPS",
                      "period1": 1420070400, "period2": 1794009600}
            collected["timeseries"] = ticker._data.get_raw_json(url, params=params)
        except Exception as exc:
            collected["errors"].append({"stage": "timeseries", "error_type": type(exc).__name__})
        collected["sources"] = session.records[start:]
        collected["retrieval_finished_at"] = datetime.now(timezone.utc).isoformat()
        collected["complete"] = bool(collected.get("timeseries")) and bool(collected.get("summary"))
        dump(path, collected)
        failures = 0 if collected["complete"] else failures + 1
        print(json.dumps({"progress": f"{index}/{len(symbols)}", "symbol": symbol,
                          "complete": collected["complete"], "errors": collected["errors"],
                          "http_statuses": [r["status"] for r in collected["sources"]]}), flush=True)
        if any(r["status"] == 429 for r in collected["sources"]):
            print("Rate limit received; collection stopped.", flush=True)
            break
        if failures >= 5:
            print("Stopped after five consecutive failures; inspect access rather than bypassing controls.", flush=True)
            break
        time.sleep(0.4)


if __name__ == "__main__":
    main()
