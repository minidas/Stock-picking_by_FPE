"""Refresh nine observed newer reporting periods for the new consensus study."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import requests
import build_current_snapshots as b
from stocking.data import validate_snapshot

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
OUT = STUDY / "consensus_rerank_v5/ttm_refresh"
SYMBOLS = {"AEE", "AKAM", "APD", "ATO", "CPT", "DVN", "EMR", "MRNA", "TECH"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def fetch():
    agent = os.environ.get("SEC_USER_AGENT", "")
    if "@" not in agent:
        raise ValueError("SEC contact identity is required in the process environment only")
    members = [m for m in read(STUDY / "input/universe.json")["members"] if m["symbol"] in SYMBOLS]
    for member in members:
        ticker = member["symbol"].replace(".", "-")
        for provider, url in (("sec", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{member['cik']}.json"),
            ("yahoo", f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}?symbol={ticker}&type=annualDilutedEPS,quarterlyDilutedEPS,trailingDilutedEPS&period1=1420070400&period2=1794096000")):
            dest = OUT / provider / (member["symbol"] + ".json")
            metadata = dest.with_name(member["symbol"] + "_source.json")
            if dest.exists() and metadata.exists():
                assert sha(dest) == read(metadata)["sha256"]
                continue
            headers = {"User-Agent": agent if provider == "sec" else "StockPickingFPE/1.0 public financial research", "Accept-Encoding": "gzip, deflate"}
            response = requests.get(url, headers=headers, timeout=40)
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as f:
                f.write(raw)
            record = {"symbol": member["symbol"], "cik": member["cik"], "provider": provider,
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "url": url, "status": response.status_code,
                "file": str(dest.relative_to(ROOT)), "sha256": digest, "bytes": len(raw)}
            write(metadata, record)
            print(json.dumps({"symbol": member["symbol"], "provider": provider, "status": response.status_code}), flush=True)
            if response.status_code != 200:
                raise RuntimeError("Public acquisition failed; retained response, no automatic repeated request")
            time.sleep(.5)


def normalize(information_cutoff=None):
    original_path = STUDY / "normalized_latest_available_reviewed/snapshots.json"
    original = {x["symbol"]: x for x in read(original_path)["snapshots"]}
    members = {x["symbol"]: x for x in read(STUDY / "input/universe.json")["members"]}
    overrides = read(STUDY / "eps_reconciliation/entity_overrides_reviewed.json")
    normalized, audits, sources = [], [], []
    # Use the public timestamp contract; never replace a module-global function.
    cutoff = information_cutoff or datetime.now(timezone.utc).isoformat()
    for symbol in sorted(SYMBOLS):
            source = {provider: read(OUT / provider / (symbol + "_source.json")) for provider in ("sec", "yahoo")}
            payloads = {}
            for provider, record in source.items():
                path = ROOT / record["file"]
                assert sha(path) == record["sha256"] and record["status"] == 200
                sources.append({"path": str(path), "sha256": record["sha256"]})
                payloads[provider] = read(path)
            member = {**members[symbol], **{k: original[symbol][k] for k in ("price", "price_date", "price_kind", "currency")}}
            try:
                snapshot, audit = b.normalize(member, payloads["sec"], payloads["yahoo"], source["sec"], source["yahoo"], "2026-11-07",
                    history_policy="latest_available", entity_override=overrides.get(symbol), information_cutoff=cutoff)
                if snapshot["eps_period_end"] < "2026-09-30":
                    raise ValueError("New actual September quarter observed; old TTM cannot remain current")
                provenance = snapshot["ttm_provenance"]
                provenance["original_normalizer_availability_description"] = provenance.get("availability_rule")
                provenance["availability_rule"] = "actual_source_observed_by_new_decision_timestamp; original next-UTC-day stage preserved separately"
                snapshot["ttm_refresh_source_observations"] = source
                snapshot = validate_snapshot(snapshot)
                normalized.append(snapshot)
                audits.append({"symbol": symbol, "status": "refreshed", "before_eps0": original[symbol]["eps0"],
                    "before_period_end": original[symbol]["eps_period_end"], "after_eps0": snapshot["eps0"],
                    "after_period_end": snapshot["eps_period_end"], "normalization_audit": audit,
                    "source_observations": source, "fully_verified_economic_basis": False})
            except (b.Unavailable, ValueError) as exc:
                audits.append({"symbol": symbol, "status": "unavailable_newer_period_requires_review", "error": str(exc), "source_observations": source})
    write(OUT / "audit.json", {"stocks": audits, "expected_symbols": sorted(SYMBOLS)})
    write(OUT / "updates.json", {"base_snapshots_sha256": sha(original_path), "snapshots": normalized,
        "unavailable_symbols": [r["symbol"] for r in audits if r["status"] != "refreshed"],
        "validation": {"status": "verified_research_inputs", "evidence_files": sources + [{"path": str(OUT / "audit.json"), "sha256": sha(OUT / "audit.json")}],
            "rationale": "New September actual periods detected in archived explicit GAAP earnings history; re-normalized fresh SEC and actual EPS responses using unchanged TTM/history checks. Actual observation cutoff policy is separately authorized for this new run; no full economic certification is asserted."}})
    print(json.dumps({"refreshed": len(normalized), "unavailable": [r["symbol"] for r in audits if r["status"] != "refreshed"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "normalize"))
    parser.add_argument("--information-cutoff", help="Explicit actual UTC observation cutoff for normalization")
    args = parser.parse_args()
    fetch() if args.action == "fetch" else normalize(args.information_cutoff)
