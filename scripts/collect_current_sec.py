"""Archive current SEC companyfacts with a user-supplied fair-access identity.

SEC_USER_AGENT stays in the process environment, never in artifacts/logs.
One sequential request per unique CIK; no legacy data/ inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import requests
from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--transport", choices=("requests", "curl"), default="requests")
    args = parser.parse_args()
    agent = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in agent or len(agent) < 10:
        raise ValueError("A real SEC_USER_AGENT contact is required; it will not be logged")
    members = json.loads((STUDY / "input/universe.json").read_text(encoding="utf-8"))["members"]
    selected = [m for m in members if not args.symbols or m["symbol"] in args.symbols]
    if args.symbols and set(args.symbols) - {m["symbol"] for m in selected}:
        raise ValueError("Requested symbol outside current universe")
    by_cik = {}
    for member in selected:
        by_cik.setdefault(member["cik"], []).append(member["symbol"])
    target = STUDY / "evidence/sec"
    target.mkdir(parents=True, exist_ok=True)
    session = requests.Session() if args.transport == "requests" else curl_requests.Session()
    session.headers.update({"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})
    failures = 0
    for index, (cik, symbols) in enumerate(by_cik.items(), 1):
        dest = target / (cik + ".json")
        if dest.exists():
            continue
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        record = {"cik": cik, "symbols": symbols, "url": url,
                  "retrieved_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = session.get(url, timeout=60)
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            record.update(status=response.status_code, sha256=digest, bytes=len(raw),
                          retry_after=response.headers.get("Retry-After"))
            if response.ok:
                content = json.loads(raw)
                if str(content.get("cik", "")).zfill(10) != cik or "facts" not in content:
                    raise ValueError("SEC response identity/schema mismatch")
                dest.write_bytes(raw)
                record["file"] = str(dest.relative_to(ROOT))
                failures = 0
            else:
                dest = target / (cik + "_error_" + digest[:12] + ".txt")
                dest.write_bytes(raw)
                record["file"] = str(dest.relative_to(ROOT))
                failures += 1
        except (requests.RequestException, curl_requests.RequestsError, ValueError) as exc:
            record.update(error_type=type(exc).__name__, error_detail=str(exc).replace(agent, "[redacted]"))
            failures += 1
        with (target / "manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(json.dumps({"progress": f"{index}/{len(by_cik)}", **record}), flush=True)
        if record.get("status") in (401, 403, 429) or failures >= 3:
            print("SEC access/rate-limit failure: stopped, preserving partial coverage.", flush=True)
            break
        time.sleep(0.3)


if __name__ == "__main__":
    main()
