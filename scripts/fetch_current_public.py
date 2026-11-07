"""Archive public source documents for the current study, outside test data/.

No credentials, SEC identity fabrication, subscription bypass, or modelling.
Run the explicitly named source set; every response is logged with its hash.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "current_sp500_2026-11-06" / "evidence"
SOURCES = {
    "ivv": "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
    "spy": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx",
    "wikipedia": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "mu_income": "https://stockanalysis.com/stocks/mu/financials/income-statement/",
    "mu_quarterly": "https://stockanalysis.com/stocks/mu/financials/income-statement/?p=quarterly",
    "mu_forecast": "https://stockanalysis.com/stocks/mu/forecast/",
    "c_analysis": "https://finance.yahoo.com/quote/C/analysis/",
    "c_financials": "https://finance.yahoo.com/quote/C/financials/",
    "c_timeseries": "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/C?symbol=C&type=annualDilutedEPS,quarterlyDilutedEPS,trailingDilutedEPS&period1=1420070400&period2=1793923200",
    "c_gaap": "https://query2.finance.yahoo.com/v10/finance/quoteSummary/C?modules=earningsTrendGaap,earningsHistoryGaap&formatted=false&symbol=C",
    "all_q3_official": "https://www.allstateinvestors.com/static-files/546cf0d4-5def-4140-97f4-c5b1eb0fbc5e",
    "echo_q3_official": "https://www.sec.gov/Archives/edgar/data/1415404/000110465926121675/sats-20260930x10q.htm",
    "fis_q3_official": "https://www.investor.fisglobal.com/news-releases/news-release-details/fis-reports-third-quarter-2026-results",
    "hig_q3_official": "https://ir.thehartford.com/news/news-details/2026/The-Hartford-Reports-Strong-Third-Quarter-2026-Financial-Results/",
    "hig_q2_official": "https://ir.thehartford.com/news/news-details/2026/The-Hartford-Reports-Strong-Second-Quarter-2026-Financial-Results/",
    "mkc_q3_official": "https://ir.mccormick.com/node/34076",
    "gl_q3_official": "https://investors.globelifeinsurance.com/news-releases/2026/october/globe-life-inc-reports-third-quarter-2026-results",
}


def fetch(name: str, url: str, transport: str = "requests") -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    record = {"name": name, "url": url,
              "retrieved_at": datetime.now(timezone.utc).isoformat()}
    try:
        headers = {
            "User-Agent": "StockPickingFPE/1.0 (public financial research; Python requests)",
            "Accept": "text/html,application/json,text/csv,*/*", "Accept-Encoding": "gzip, deflate"}
        if urlsplit(url).hostname.endswith("sec.gov"):
            identity = os.environ.get("SEC_USER_AGENT", "")
            if "@" not in identity:
                raise ValueError("SEC contact identity required; not logged")
            headers["User-Agent"] = identity
        if transport == "curl":
            response = curl_requests.get(url, timeout=45, headers=headers)
            raw = response.content
        else:
            response = requests.get(url, timeout=45, stream=True, headers=headers)
            raw = response.raw.read(decode_content=False)
        wire_hash = hashlib.sha256(raw).hexdigest()
        # Some public responses declare gzip while an intermediary already
        # decoded the body. Inspect the magic rather than decoding it twice.
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        suffix = ".xlsx" if name == "spy" else ".csv" if name == "ivv" else ".pdf" if "application/pdf" in response.headers.get("Content-Type", "") else ".html"
        digest = hashlib.sha256(raw).hexdigest()
        path = OUT / (name + "_" + digest[:12] + suffix)
        if not path.exists():
            path.write_bytes(raw)
        record.update(status=response.status_code, bytes=len(raw), sha256=digest,
                      wire_sha256=wire_hash, content_encoding=response.headers.get("Content-Encoding"),
                      file=str(path.relative_to(ROOT)), content_type=response.headers.get("Content-Type"),
                      success=response.ok)
    except (requests.RequestException, curl_requests.RequestsError) as exc:
        record.update(success=False, error=type(exc).__name__)
    with (OUT / "download_manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+", choices=sorted(SOURCES))
    parser.add_argument("--transport", choices=("requests", "curl"), default="requests")
    args = parser.parse_args()
    for name in args.names:
        record = fetch(name, SOURCES[name], args.transport)
        if record.get("status") == 429:
            break
        time.sleep(0.5)
