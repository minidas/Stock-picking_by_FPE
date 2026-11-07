"""Retry public acquisition into a new archive; never overwrite frozen research.

Uses ordinary requests/yfinance sessions, keeps TLS verification enabled, and
stops the Yahoo session on a new rate-limit response. Authentication material
is neither printed nor included in the evidence archive.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
OUT = STUDY / "network_retry_2026-11-07"
PUBLIC = {
    "yahoo_analysis_C": "https://finance.yahoo.com/quote/C/analysis/",
    "yahoo_actual_C": "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/C?symbol=C&type=annualDilutedEPS,quarterlyDilutedEPS,trailingDilutedEPS&period1=1420070400&period2=1794009600",
    "stockanalysis_HIG": "https://stockanalysis.com/stocks/hig/forecast/",
    "stockanalysis_MKC": "https://stockanalysis.com/stocks/mkc/forecast/",
    "nasdaq_forecast_HIG": "https://api.nasdaq.com/api/analyst/HIG/earnings-forecast",
    "paccar_split_announcement": "https://investors.paccar.com/financial-news/news-details/2022/PACCAR-Announces-50-Stock-Dividend-and-Increases-Quarterly-Cash-Dividend-12-06-2022/default.aspx",
    "echostar_s4a": "https://ir.echostar.com/static-files/60c2957b-63da-4ba4-99fa-c63789dbb8f1",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def safe_url(url, params=None):
    p = urlsplit(url)
    values = dict(parse_qsl(p.query))
    values.update(params or {})
    forbidden = {"crumb", "token", "apikey", "api_key", "authorization", "cookie"}
    values = {k: v for k, v in values.items() if k.lower() not in forbidden}
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(values), ""))


def archive(group, name, url, status, raw, content_type=None, retry_after=None):
    digest = hashlib.sha256(raw).hexdigest()
    folder = OUT / group
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if raw.startswith(b"%PDF") else ".json" if "json" in (content_type or "") else ".html"
    dest = folder / (digest + suffix)
    if not dest.exists():
        dest.write_bytes(raw)
    record = {"name": name, "url": safe_url(url), "retrieved_at": now(), "status": status,
              "bytes": len(raw), "sha256": digest, "file": str(dest.relative_to(ROOT)),
              "content_type": content_type, "retry_after": retry_after,
              "http_success": 200 <= status < 300, "content_validated": False}
    append(folder / "manifest.jsonl", record)
    print(json.dumps({k: record[k] for k in ("name", "status", "bytes", "sha256")}), flush=True)
    return record


def public(names):
    stopped = set()
    for name in names or list(PUBLIC):
        url = PUBLIC[name]
        host = urlsplit(url).hostname
        if host in stopped:
            continue
        try:
            # Read bytes ourselves: a previous intermediary incorrectly declared
            # gzip after decoding the response. Never disable TLS verification.
            response = requests.get(url, stream=True, timeout=(12, 25), headers={
                "User-Agent": "StockPickingFPE/1.0 (public financial research; Python requests)",
                "Accept": "application/json,text/html,*/*", "Accept-Encoding": "gzip, deflate"})
            raw = response.raw.read(decode_content=False)
            wire_hash = hashlib.sha256(raw).hexdigest()
            if raw.startswith(b"\x1f\x8b"):
                raw = gzip.decompress(raw)
            record = archive("public", name, url, response.status_code, raw,
                             response.headers.get("Content-Type"), response.headers.get("Retry-After"))
            append(OUT / "public/transport_audit.jsonl", {"response_sha256": record["sha256"],
                "wire_sha256": wire_hash, "content_encoding": response.headers.get("Content-Encoding")})
            if response.status_code == 429:
                stopped.add(host)
        except (requests.RequestException, OSError, EOFError) as exc:
            record = {"name": name, "url": safe_url(url), "retrieved_at": now(),
                      "status": None, "error_type": type(exc).__name__, "content_validated": False}
            append(OUT / "public/manifest.jsonl", record)
            print(json.dumps(record), flush=True)
        time.sleep(1)


def yahoo(symbols):
    from curl_cffi import requests as curl_requests
    import yfinance as yf
    from yfinance.config import YfConfig

    class StopRateLimit(RuntimeError):
        pass

    class EvidenceSession(curl_requests.Session):
        def __init__(self):
            super().__init__(impersonate="chrome")
            self.records = []
            self.blocked = False

        def request(self, method, url, *args, **kwargs):
            if self.blocked:
                raise StopRateLimit("Rate-limited session stopped")
            response = super().request(method, url, *args, **kwargs)
            clean = safe_url(url, kwargs.get("params"))
            if "/finance/quoteSummary/" in urlsplit(url).path:
                self.records.append(archive("yahoo", "quoteSummary", clean, response.status_code,
                    response.content, response.headers.get("Content-Type"), response.headers.get("Retry-After")))
            else:
                # Session endpoints may contain credentials in bodies/headers.
                # Record only public host/path and response status.
                parts = urlsplit(url)
                append(OUT / "yahoo/session_status.jsonl", {"retrieved_at": now(), "host": parts.hostname,
                    "path": parts.path, "status": response.status_code})
            if response.status_code == 429:
                self.blocked = True
                raise StopRateLimit("Rate-limited session stopped")
            return response

    allowed = {r["symbol"] for r in json.loads((STUDY / "input/universe.json").read_text(encoding="utf-8"))["members"]}
    if set(symbols) - allowed:
        raise ValueError("Unknown study symbol")
    yf.set_tz_cache_location(str(OUT / "private_session_cache"))
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    YfConfig.network.retries = 0
    session = EvidenceSession()
    failures = 0
    for symbol in symbols:
        dest = OUT / "yahoo" / (symbol + "_collected.json")
        if dest.exists():
            if json.loads(dest.read_text(encoding="utf-8")).get("response_present"):
                continue
            raise ValueError("Refusing to overwrite prior retry result")
        start = len(session.records)
        record = {"symbol": symbol, "retrieval_started_at": now(), "yfinance_version": yf.__version__,
                  "modules_requested": ["earningsTrendGaap", "earningsHistoryGaap", "earningsTrend", "price"]}
        try:
            ticker = yf.Ticker(symbol.replace(".", "-"), session=session)
            record["response"] = ticker._analysis._fetch(record["modules_requested"])
        except Exception as exc:
            record["error_type"] = type(exc).__name__
        record.update(retrieval_finished_at=now(), sources=session.records[start:], rate_limited=session.blocked)
        record["response_present"] = bool(record.get("response"))
        write(dest, record)
        print(json.dumps({k: record.get(k) for k in ("symbol", "response_present", "error_type", "rate_limited")}), flush=True)
        failures = 0 if record["response_present"] else failures + 1
        if session.blocked or failures >= 2:
            break
        time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("public", "yahoo"))
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--names", nargs="+", choices=sorted(PUBLIC))
    parser.add_argument("--symbols", nargs="+", default=["C", "HIG", "MKC", "TROW", "DECK", "PYPL"])
    parser.add_argument("--all", action="store_true", help="Collect the frozen 503-security study list")
    args = parser.parse_args()
    OUT = args.output.resolve()
    if not OUT.is_relative_to(STUDY.resolve()) or OUT == STUDY.resolve():
        raise ValueError("Retry archive must remain in a dedicated study subdirectory")
    if args.group == "public":
        public(args.names)
    else:
        symbols = [r["symbol"] for r in json.loads((STUDY / "input/universe.json").read_text(encoding="utf-8"))["members"]] if args.all else args.symbols
        yahoo(symbols)
