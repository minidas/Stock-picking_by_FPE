"""Archive original bytes behind the four-stock 2012 research snapshots.

Downloads are sequential and paced. Failed HTTP/challenge pages are recorded as
errors, never saved as source documents. Verified report mirrors can preserve
the same fiscal report when the original endpoint blocks automated access, but
are explicitly labelled substitutes rather than original endpoint responses.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
SOURCE_KEYS = ("financial_statement", "price")
DEFAULT_USER_AGENT = "stocking-research-source-archiver/0.1 (historical source preservation)"
MAX_BYTES = 40_000_000

# These exact alternatives were verified against their fiscal-year cover text
# before being added; no ticker-derived guessed download URLs are constructed.
VERIFIED_FALLBACKS = {
    "https://www.sec.gov/Archives/edgar/data/320193/000119312512444068/d411355d10k.htm": [{
        "url": "https://www.annualreports.com/HostedData/AnnualReportArchive/a/NASDAQ_AAPL_2012.pdf",
        "verification": "Apple Form 10-K, fiscal year ended September 29, 2012; cover verified 2026-11-06 through web search indexed PDF text",
        "relationship": "same-fiscal-year company report mirror; not byte-identical SEC HTML",
    }],
    "https://www.sec.gov/Archives/edgar/data/909832/000119312512428890/d388097d10k.htm": [{
        "url": "https://s201.q4cdn.com/287523651/files/doc_financials/2012/ar/2012-Annual-Report.pdf",
        "verification": "Costco Wholesale Annual Report 2012, year ended September 2, 2012; cover verified 2026-11-06 through web search indexed PDF text",
        "relationship": "issuer annual report with same fiscal-year statements; may include later shareholder material, not original SEC HTML",
    }, {
        "url": "https://www.annualreports.com/HostedData/AnnualReportArchive/c/NASDAQ_COST_2012.pdf",
        "verification": "Costco Wholesale Annual Report 2012, year ended September 2, 2012; cover verified 2026-11-06 through web search indexed PDF text",
        "relationship": "same-fiscal-year company annual report mirror; may include later shareholder material, not original SEC HTML",
    }],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def collect_sources(payload):
    rows = payload["snapshots"] if isinstance(payload, dict) else payload
    unique = {}
    for snapshot in rows:
        for key in SOURCE_KEYS:
            url = snapshot["source"][key]
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Source archive accepts HTTPS source URLs only")
            entry = unique.setdefault(url, {"source_url": url, "references": []})
            entry["references"].append({"symbol": snapshot["symbol"], "role": key})
    return list(unique.values())


def classify_bytes(url, content_type, raw):
    if raw.lstrip().startswith(b"%PDF-"):
        return "pdf"
    if urlparse(url).path.lower().endswith(".pdf"):
        raise ValueError("Expected PDF magic bytes; response is not the requested PDF")
    preview = raw[:200_000].decode("utf-8", errors="replace").lower()
    prefix = preview[:20_000]
    blocked = ("<title>access denied", "<title>just a moment", "request rate threshold exceeded",
               "your request originates from an undeclared automated tool", "<title>attention required")
    if any(marker in prefix for marker in blocked):
        raise ValueError("HTTP success contained an access/challenge page")
    if ("html" in content_type.lower() or "<html" in preview or "<!doctype html" in preview) and len(raw) > 500:
        return "html"
    raise ValueError("Expected a nontrivial HTML document or PDF")


def download_one(session, url, output, timeout):
    attempted_at = utc_now()
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}
    if urlparse(url).hostname in {"www.sec.gov", "sec.gov", "data.sec.gov"}:
        headers["User-Agent"] = os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT)
    attempt = {"url": url, "attempted_at": attempted_at}
    try:
        with session.get(url, headers=headers, timeout=(min(10, timeout), timeout), stream=True) as response:
            attempt.update(http_status=response.status_code, final_url=response.url)
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Source document exceeds 40 MB archive limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            content_type = response.headers.get("Content-Type", "")
        kind = classify_bytes(url, content_type, raw)
        digest = hashlib.sha256(raw).hexdigest()
        parsed = urlparse(url)
        label = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{parsed.hostname}_{Path(parsed.path).stem}")[:100]
        path = output / f"{label}_{digest[:16]}.{kind}"
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("Existing archive name has different bytes")
        else:
            with path.open("xb") as handle:
                handle.write(raw)
        attempt.update(status="success", retrieved_at=utc_now(), path=path.name,
                       sha256=digest, bytes=len(raw), content_type=content_type, file_type=kind)
    except Exception as exc:
        # Only public document URLs are used; never write headers/credentials.
        attempt.update(status="error", error_type=type(exc).__name__, error=str(exc)[:500])
    return attempt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/research_2012_snapshots.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/raw/stocks_2012")
    parser.add_argument("--refresh", action="store_true", help="Fetch again; preserve earlier content-addressed raw files")
    parser.add_argument("--timeout", type=float, default=45, help="Read timeout seconds, maximum 45")
    args = parser.parse_args()
    if not 0 < args.timeout <= 45:
        parser.error("--timeout must be in (0, 45]")
    sources = collect_sources(json.loads(args.input.read_text(encoding="utf-8-sig")))
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    cached = {row["source_url"]: row for row in previous.get("sources", [])}
    manifest = {"schema_version": 1, "created_at": utc_now(), "input_path": str(args.input.resolve()),
                "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
                "scope": "original source document bytes for four historical stocks; not a complete market raw dataset",
                "notes": ["All retrieval timestamps are current retrieval times, not historical publication dates.",
                          "Fallback reports are explicitly labelled and do not replace original source attribution or declared cutoff dates.",
                          "Error responses and challenge pages are not saved as financial documents."], "sources": []}
    session = requests.Session()
    try:
        for source in sources:
            old = cached.get(source["source_url"])
            if not args.refresh and old and old.get("status") in ("success", "fallback_success"):
                # Resolve and confine manifest paths before reading; malformed
                # manifests must not point the archiver at unrelated files.
                path = (args.output / old["path"]).resolve()
                if path.is_relative_to(args.output.resolve()) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == old["sha256"]:
                    manifest["sources"].append({**old, "references": source["references"], "cache_verified_at": utc_now()})
                    continue
            # Keep earlier failures when retrying only an unresolved source;
            # a later successful mirror must not erase the acquisition history.
            row = {**source, "attempts": list(old.get("attempts", [])) if old else []}
            alternatives = [{"url": source["source_url"]}] + VERIFIED_FALLBACKS.get(source["source_url"], [])
            for index, alternative in enumerate(alternatives):
                attempt = download_one(session, alternative["url"], args.output, args.timeout)
                row["attempts"].append(attempt)
                time.sleep(.3)  # At most one request at a time; no SEC retry storm.
                if attempt["status"] == "success":
                    row.update({key: attempt[key] for key in ("path", "sha256", "bytes", "file_type", "content_type", "retrieved_at", "final_url")})
                    row["download_url"] = alternative["url"]
                    row["status"] = "success" if index == 0 else "fallback_success"
                    if index:
                        row["fallback_verification"] = alternative["verification"]
                        row["fallback_relationship"] = alternative["relationship"]
                    break
            else:
                row["status"] = "error"
            manifest["sources"].append(row)
            # Save progress so a later timeout/interruption cannot hide already
            # downloaded source bytes and their provenance.
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"source_url": row["source_url"], "status": row["status"], "path": row.get("path")}), flush=True)
    finally:
        session.close()
    manifest["summary"] = {"unique_sources": len(sources),
                           "original_endpoint_success": sum(r["status"] == "success" for r in manifest["sources"]),
                           "verified_fallback_success": sum(r["status"] == "fallback_success" for r in manifest["sources"]),
                           "unresolved_sources": sum(r["status"] == "error" for r in manifest["sources"])}
    manifest["completed_at"] = utc_now()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["summary"]), flush=True)


if __name__ == "__main__":
    main()
