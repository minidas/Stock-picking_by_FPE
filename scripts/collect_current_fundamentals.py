"""Archive public actual EPS only, never infer analyst forecasts from PE ratios.

Uses the anonymous fundamentals endpoint which returned a successful response
in the source probe. A 429 stops this run without rotating network identity.
"""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"


def main():
    members = json.loads((STUDY / "input/universe.json").read_text(encoding="utf-8"))["members"]
    target = STUDY / "evidence/public_actual_eps"
    target.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "StockPickingFPE/1.0 (public financial research; Python requests)",
                            "Accept-Encoding": "gzip, deflate"})
    errors = 0
    for index, member in enumerate(members, 1):
        symbol = member["symbol"]
        dest = target / (symbol + ".json")
        if dest.exists():
            continue
        url = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
               + symbol.replace(".", "-") + "?symbol=" + symbol.replace(".", "-")
               + "&type=annualDilutedEPS,quarterlyDilutedEPS,trailingDilutedEPS"
               + "&period1=1420070400&period2=1793923200")
        record = {"symbol": symbol, "url": url, "retrieved_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = session.get(url, timeout=30)
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            record.update(status=response.status_code, sha256=digest, bytes=len(raw),
                          retry_after=response.headers.get("Retry-After"))
            raw_dest = dest if response.ok else target / (symbol + "_error_" + digest[:12] + ".txt")
            raw_dest.write_bytes(raw)
            record["file"] = str(raw_dest.relative_to(ROOT))
            errors = 0 if response.ok else errors + 1
        except requests.RequestException as exc:
            record.update(status=None, error_type=type(exc).__name__)
            errors += 1
        with (target / "manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(json.dumps({"progress": f"{index}/{len(members)}", **record}), flush=True)
        if record.get("status") in (401, 403, 429) or errors >= 3:
            print("Access/rate-limit failure: stopped; no alternate identities or bypass.", flush=True)
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
