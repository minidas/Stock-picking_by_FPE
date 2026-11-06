"""Preserve early SEC annual EPS observations without joining incompatible vintages.

The public ZIP mirrors are real filing data distributed as a library's test
fixtures, not a certified complete SEC archive. They extend the raw history;
their old earnings must not become a fresh 2012 earnings anchor.
"""
from __future__ import annotations

import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TAGS = {"EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"}


def iso(value):
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()


def extract(path, cutoff="2012-12-31"):
    with zipfile.ZipFile(path) as archive:
        submissions = {
            r["adsh"]: r for r in csv.DictReader(
                io.TextIOWrapper(archive.open("sub.txt"), encoding="utf-8-sig"), delimiter="\t")
            if r["form"] in {"10-K", "10-K/A", "20-F", "20-F/A"}
            and iso(r["filed"]) <= cutoff
        }
        records = []
        for fact in csv.DictReader(
                io.TextIOWrapper(archive.open("num.txt"), encoding="utf-8-sig"), delimiter="\t"):
            filing = submissions.get(fact["adsh"])
            if (not filing or fact["tag"] not in TAGS or fact["qtrs"] != "4"
                    or fact.get("coreg") or fact.get("segments") or not fact["value"]
                    or not fact["version"].startswith("us-gaap/")):
                continue
            period, published = iso(fact["ddate"]), iso(filing["filed"])
            if period > published:
                continue
            records.append({
                "cik": filing["cik"].zfill(10), "name": filing["name"],
                "accession": fact["adsh"], "form": filing["form"],
                "period_end": period, "available_at": published,
                "eps": float(fact["value"]), "tag": fact["tag"],
                "unit_as_reported": fact["uom"], "taxonomy_version": fact["version"],
                "raw_archive": str(path.relative_to(ROOT)),
                "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(filing['cik'])}/{fact['adsh'].replace('-', '')}/",
                "share_basis_verified": False,
            })
    return records


def main():
    raw_dir = ROOT / "data/raw/sec_legacy_2010"
    records, sources = [], []
    for path in sorted(raw_dir.glob("*.zip")):
        records.extend(extract(path))
        sources.append({"path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    payload = {"as_of": "2012-12-31", "source_files": sources,
               "scope": "Older filing observations for historical research, not fresh 2012 anchors",
               "limitations": [
                   "Third-party copies; quarterly completeness and byte identity to SEC are not certified.",
                   "Different filing vintages can have split or accounting restatements; do not concatenate their EPS blindly.",
                   "Annual diluted-EPS tags are retained with their original units; no automatic price/share-basis reconciliation.",
               ], "records": records,
               "summary": {"observations": len(records), "companies": len({r['cik'] for r in records}),
                           "filings": len({r['accession'] for r in records})}}
    target = ROOT / "data/research_2012_legacy_eps.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(json.dumps(payload["summary"]))


if __name__ == "__main__":
    main()
