"""Archive reviewed issuer-hosted filings without changing disclosure dates."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[3]
DIRECTORY = Path(__file__).resolve().parent
SOURCE = ROOT / "data/research_2012_history_supplements.json"
MANIFEST = DIRECTORY / "manifest.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    prior = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    old = {row["source_accession"]: row for row in prior.get("files", [])}
    records = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; HistoricalFinancialResearch/1.0)",
                            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"})
    for block in payload["source_blocks"]:
        accession = block["source_accession"]
        target = DIRECTORY / f"CLF_FY{block['source_fiscal_year']}_{accession}.html"
        record = {
            "symbol": block["symbol"], "cik": block["cik"], "source_accession": accession,
            "source_fiscal_year": block["source_fiscal_year"],
            "url": block["source_url"], "path": target.relative_to(ROOT).as_posix(),
            "date": block["available_at"], "date_kind": "original_filing_public_availability_date",
            "available_at": block["available_at"], "attempted_at": now(),
        }
        previous = old.get(accession)
        if previous and previous.get("status") == "downloaded" and target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == previous.get("sha256"):
                record = dict(previous)
                record["cache_verified_at"] = now()
                records.append(record)
                block["raw_evidence_file"] = record["path"]
                block["raw_evidence_sha256"] = digest
                print(f"cached {accession}", flush=True)
                continue
            record["cache_validation"] = "existing_file_hash_mismatch; kept untouched"
            record["status"] = "error"
            record["error"] = "Existing archived bytes differ from manifest; manual review required."
            records.append(record)
            print(f"error {accession}: existing archived hash mismatch", flush=True)
            continue
        if target.exists() and not previous:
            record["status"] = "error"
            record["error"] = "Existing unregistered file kept untouched; manual review required."
            records.append(record)
            print(f"error {accession}: unregistered existing file", flush=True)
            continue
        try:
            response = session.get(block["source_url"], timeout=(15, 45))
            record["http_status"] = response.status_code
            record["resolved_url"] = response.url
            record["content_type"] = response.headers.get("Content-Type")
            response.raise_for_status()
            content = response.content
            text = content.decode(response.encoding or "utf-8", errors="replace").lower()
            if len(content) < 10000 or "cleveland" not in text or "selected financial data" not in text:
                raise ValueError("Response did not pass filing-content checks; no file written")
            target.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            record.update(status="downloaded", retrieved_at=now(), sha256=digest, bytes=len(content))
            block["raw_evidence_file"] = record["path"]
            block["raw_evidence_sha256"] = digest
            print(f"downloaded {accession}: {len(content)} bytes", flush=True)
        except (requests.RequestException, ValueError) as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}", completed_at=now())
            print(f"error {accession}: {type(exc).__name__}: {exc}", flush=True)
        records.append(record)
    manifest = {
        "schema_version": 1, "created_at": prior.get("created_at", now()), "updated_at": now(),
        "purpose": "Byte archive of six issuer-hosted historical annual filings used for CLF annual history.",
        "availability_note": "date/available_at are historical filing dates; retrieved_at is the actual modern download time. Downloading now does not create historical publication evidence.",
        "source_blocks_file": SOURCE.relative_to(ROOT).as_posix(),
        "user_agent": session.headers["User-Agent"], "files": records,
        "downloaded_count": sum(r["status"] == "downloaded" for r in records),
        "error_count": sum(r["status"] == "error" for r in records),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    SOURCE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("downloaded_count", "error_count")}), flush=True)


if __name__ == "__main__":
    main()
