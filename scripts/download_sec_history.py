"""Archive SEC quarterly ZIPs and retain dated, unjoined annual EPS filings.

Network access is explicit (--download or --probe). --normalize-only and local
archive imports never contact a server. ZIPs are streamed, never extracted.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_TEMPLATE = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{quarter}.zip"
FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
TAGS = {"EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"}
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
QUARTER = re.compile(r"^(\d{4})q([1-4])$")


class HistoryDownloadError(ValueError):
    pass


def iso(value):
    value = str(value).strip()
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    result = date.fromisoformat(value)
    if value != result.isoformat():
        raise ValueError("Dates must be YYYYMMDD or YYYY-MM-DD")
    return result.isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path):
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def inspect_zip(path, *, max_bytes=MAX_ARCHIVE_BYTES, max_expanded=MAX_EXPANDED_BYTES):
    """Check archive bounds and entries without extracting any paths."""
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise HistoryDownloadError("ZIP exceeds compressed byte limit")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 100:
            raise HistoryDownloadError("Unexpectedly many ZIP entries")
        if sum(entry.file_size for entry in members) > max_expanded:
            raise HistoryDownloadError("ZIP exceeds expanded byte limit")
        names, tables, entries = set(), {}, []
        for entry in members:
            name = entry.filename
            pure = PurePosixPath(name)
            if (pure.is_absolute() or ".." in pure.parts or "\\" in name
                    or ":" in name or name.lower() in names or entry.flag_bits & 1):
                raise HistoryDownloadError("Unsafe, duplicate or encrypted ZIP entry")
            names.add(name.lower())
            if entry.file_size > max(1, entry.compress_size) * MAX_COMPRESSION_RATIO:
                raise HistoryDownloadError("ZIP compression expansion ratio exceeds limit")
            if not entry.is_dir() and pure.name.lower() in {"sub.txt", "num.txt"}:
                if pure.name.lower() in tables:
                    raise HistoryDownloadError("Ambiguous duplicate SEC table names")
                tables[pure.name.lower()] = name
            entries.append({"name": name, "bytes": entry.file_size,
                            "compressed_bytes": entry.compress_size})
        if set(tables) != {"sub.txt", "num.txt"}:
            raise HistoryDownloadError("ZIP must contain sub.txt and num.txt")
    return {"tables": tables, "entries": entries}


def _rows(archive, name, required):
    # A giant cell cannot force unbounded CSV allocation; the normal tables have
    # much smaller fields. Streams stay inside the ZipFile context.
    csv.field_size_limit(4 * 1024 * 1024)
    with archive.open(name) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", errors="strict", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise HistoryDownloadError(f"{name} lacks columns: {sorted(missing)}")
            for row in reader:
                if None in row:
                    raise HistoryDownloadError(f"Malformed row in {name}")
                yield row


def extract_archive(path, *, as_of="2012-12-31", member_ciks=None,
                    archive_origin=None, max_bytes=MAX_ARCHIVE_BYTES):
    """Return accession-specific candidates; never concatenate EPS vintages."""
    cutoff = iso(as_of)
    path = Path(path)
    inspection = inspect_zip(path, max_bytes=max_bytes)
    digest = sha256_file(path)
    source = {"path": display_path(path), "bytes": path.stat().st_size,
              "sha256": digest, "origin": archive_origin or "local_archive_origin_unverified",
              "entries": inspection["entries"]}
    submissions, facts, rejected = {}, {}, Counter()
    with zipfile.ZipFile(path) as archive:
        for row in _rows(archive, inspection["tables"]["sub.txt"],
                         {"adsh", "cik", "name", "form", "filed", "period"}):
            if row["form"] not in FORMS:
                continue
            try:
                published, period = iso(row["filed"]), iso(row["period"])
                cik = str(int(row["cik"])).zfill(10)
            except (ValueError, TypeError):
                rejected["invalid_filing_dates_or_cik"] += 1
                continue
            if published > cutoff or period > published or (member_ciks is not None and cik not in member_ciks):
                continue
            accession = row["adsh"].strip()
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                rejected["invalid_accession"] += 1
                continue
            candidate = {"cik": cik, "name": row["name"], "accession": accession,
                         "form": row["form"], "period_end": period, "available_at": published,
                         "accepted": row.get("accepted"),
                         "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/",
                         "source_archive": source["path"], "source_archive_sha256": digest,
                         "archive_origin": source["origin"], "share_basis_verified": False,
                         "eps_basis": "GAAP_diluted_as_reported_at_cutoff",
                         "share_basis": "filing_as_reported:" + accession,
                         "context": {"coreg": "", "segments": ""}}
            if accession in submissions and candidate != submissions[accession]:
                raise HistoryDownloadError("Conflicting submission rows for one accession")
            submissions[accession] = candidate
        for row in _rows(archive, inspection["tables"]["num.txt"],
                         {"adsh", "tag", "version", "ddate", "qtrs", "uom", "coreg", "value"}):
            filing = submissions.get(row["adsh"])
            if not filing or row["tag"] not in TAGS or row["qtrs"] != "4":
                continue
            if not row["version"].startswith("us-gaap/"):
                rejected["nonstandard_taxonomy"] += 1
                continue
            if row["uom"] not in {"USD/shares", "USD"}:
                rejected["unsupported_unit"] += 1
                continue
            if row.get("coreg", "").strip() or row.get("segments", "").strip():
                rejected["dimensional_or_coregistered"] += 1
                continue
            try:
                period, value = iso(row["ddate"]), float(row["value"])
                if not math.isfinite(value):
                    raise ValueError("Nonfinite EPS")
            except (ValueError, TypeError):
                rejected["invalid_value_or_period"] += 1
                continue
            if period > filing["available_at"] or period > cutoff or period > filing["period_end"]:
                rejected["future_period"] += 1
                continue
            context = {"coreg": "", "segments": "",
                       "segments_field_present": "segments" in row,
                       "dimension_status": "non_dimensional" if "segments" in row else "legacy_no_segments_field_coreg_empty"}
            observation = {"period_end": period, "available_at": filing["available_at"],
                           "eps": value, "tag": row["tag"], "unit_as_reported": row["uom"],
                           "taxonomy_version": row["version"], "context": context,
                           "source_url": filing["source_url"], "source_archive": source["path"],
                           "source_archive_sha256": digest, "accession": filing["accession"],
                           "eps_basis": filing["eps_basis"], "share_basis": filing["share_basis"],
                           "share_basis_verified": False}
            facts.setdefault(row["adsh"], []).append(observation)
    filings = []
    for accession, records in sorted(facts.items()):
        by_period = {}
        for record in records:
            by_period.setdefault(record["period_end"], []).append(record)
        history, conflicts = [], []
        for period, alternatives in sorted(by_period.items()):
            preferred = [r for r in alternatives if r["tag"] == "EarningsPerShareDiluted"] or alternatives
            if len({r["eps"] for r in preferred}) != 1:
                conflicts.append({"period_end": period, "reason": "conflicting_preferred_tag_values",
                                  "facts": preferred})
                continue
            chosen = dict(preferred[0])
            chosen["supporting_facts"] = preferred
            history.append(chosen)
        filing = dict(submissions[accession], history=history, conflicts=conflicts,
                      has_current_report_eps=any(r["period_end"] == submissions[accession]["period_end"] for r in history))
        filings.append(filing)
    return {"source": source, "filings": filings, "rejected_fact_counts": dict(rejected)}


def selected_quarters(start_year, end_year, as_of, quarters=None):
    cutoff = date.fromisoformat(iso(as_of))
    if start_year < 2009 or end_year < start_year:
        raise HistoryDownloadError("SEC quarterly XBRL history starts in 2009; check the year range")
    choices = quarters or [f"{year}q{quarter}" for year in range(start_year, end_year + 1) for quarter in range(1, 5)]
    result = []
    for label in choices:
        match = QUARTER.fullmatch(label.lower())
        if not match:
            raise HistoryDownloadError("Quarter labels must look like 2011q1")
        year, quarter = map(int, match.groups())
        if not start_year <= year <= end_year:
            raise HistoryDownloadError("Explicit quarter falls outside start/end years")
        if date(year, 3 * quarter - 2, 1) > cutoff:
            continue
        result.append(f"{year}q{quarter}")
    return sorted(set(result))


def source_request(label, template=None, user_agent=None):
    year, quarter = map(int, QUARTER.fullmatch(label).groups())
    try:
        url = (template or OFFICIAL_TEMPLATE).format(year=year, quarter=quarter, period=label)
    except (KeyError, ValueError, IndexError) as exc:
        raise HistoryDownloadError("Source template accepts {year}, {quarter}, {period}") from exc
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise HistoryDownloadError("Archive source must be an HTTPS URL without credentials or fragment")
    official = parsed.hostname == "sec.gov" or parsed.hostname.endswith(".sec.gov")
    if official:
        if not user_agent or "@" not in user_agent or "\n" in user_agent or "\r" in user_agent:
            raise HistoryDownloadError("Official SEC access requires an existing real SEC_USER_AGENT contact; none was invented")
        headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    else:
        if not template:
            raise HistoryDownloadError("A third-party source requires an explicit --source-template")
        headers = {"User-Agent": "stocking-history-research/1.0", "Accept-Encoding": "identity"}
    return url, headers, "official_SEC_quarterly_dataset" if official else "explicit_third_party_mirror_unverified"


def probe_source(url, headers, *, opener=urlopen):
    with opener(Request(url, headers=headers, method="HEAD"), timeout=30) as response:
        return {"status": getattr(response, "status", None),
                "content_length": response.headers.get("Content-Length"),
                "content_type": response.headers.get("Content-Type")}


def download_archive(url, destination, headers, *, max_bytes=MAX_ARCHIVE_BYTES, opener=urlopen):
    """Stream to a temporary file and publish with exclusive creation; never overwrite."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        inspect_zip(destination, max_bytes=max_bytes)
        return {"path": display_path(destination), "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination), "status": "reused_existing"}
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
    try:
        total = 0
        with os.fdopen(fd, "wb") as stream:
            with opener(Request(url, headers=headers), timeout=45) as response:
                claimed = response.headers.get("Content-Length")
                if claimed and int(claimed) > max_bytes:
                    raise HistoryDownloadError("Remote ZIP exceeds compressed byte limit")
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HistoryDownloadError("Download exceeded compressed byte limit")
                    stream.write(chunk)
        inspect_zip(temporary, max_bytes=max_bytes)
        digest = sha256_file(temporary)
        # Both paths are on the same volume. link() fails if destination exists,
        # preventing a concurrent process from having a completed archive replaced.
        os.link(temporary, destination)
        return {"path": display_path(destination), "bytes": total, "sha256": digest,
                "status": "downloaded"}
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def normalize_archives(paths, output, *, as_of, member_ciks=None, origins=None, max_bytes=MAX_ARCHIVE_BYTES):
    """Keep conflicting duplicate accessions distinct for upstream reconciliation."""
    paths = sorted({Path(path).resolve() for path in paths})
    if not paths:
        raise HistoryDownloadError("No raw ZIP archives found; existing normalized results were left unchanged")
    sources, filings, rejected, seen = [], [], Counter(), set()
    for path in paths:
        parsed = extract_archive(path, as_of=as_of, member_ciks=member_ciks,
                                 archive_origin=(origins or {}).get(str(path)), max_bytes=max_bytes)
        digest = parsed["source"]["sha256"]
        if digest in seen:
            continue
        seen.add(digest)
        sources.append(parsed["source"])
        filings.extend(parsed["filings"])
        rejected.update(parsed["rejected_fact_counts"])
    payload = {"schema_version": 1, "as_of": iso(as_of), "filings": filings,
               "source_files": sources, "summary": {"archives": len(sources), "filing_candidates": len(filings),
               "companies": len({f["cik"] for f in filings}), "annual_eps_observations": sum(len(f["history"]) for f in filings),
               "conflicting_periods": sum(len(f["conflicts"]) for f in filings), "rejected_fact_counts": dict(rejected)},
               "limitations": ["Filing candidates are not joined across vintages or declared share-basis compatible.",
                               "SEC dataset period dates can be rounded to month ends; no exact period_start is invented.",
                               "USD-labelled historical EPS is retained with its original unit, not silently relabelled.",
                               "An imported or mirrored ZIP is not certified byte-identical to the SEC original.",
                               "A current retrieval with original filed dates is not a contemporaneously archived historical feed.",
                               "Missing periods remain missing; XBRL ZIP coverage starting in 2009 cannot guarantee eleven annual EPS observations."]}
    atomic_json(Path(output) / "candidate_filings.json", payload)
    return payload


def read_member_ciks(path):
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = payload.get("members", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise HistoryDownloadError("Members input must contain a nonempty members list")
    return {str(int(row["cik"])).zfill(10) for row in rows if row.get("cik")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2009)
    parser.add_argument("--end-year", type=int, default=2012)
    parser.add_argument("--as-of", default="2012-12-31")
    parser.add_argument("--output", type=Path, default=ROOT / "data/raw/sec_history")
    parser.add_argument("--members", type=Path, default=ROOT / "data/universe_2012_members.json")
    parser.add_argument("--quarters", nargs="+", help="Limit remote requests, e.g. 2011q1 2011q2")
    parser.add_argument("--source-template", help="Explicit public HTTPS mirror; placeholders: {year}, {quarter}, {period}")
    parser.add_argument("--local-archive", action="append", type=Path, default=[])
    parser.add_argument("--zip-dir", action="append", type=Path, default=[])
    parser.add_argument("--max-archive-mb", type=int, default=200)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="Explicitly download missing selected quarterly archives")
    mode.add_argument("--probe", action="store_true", help="HEAD requests only; no archive download or normalization")
    mode.add_argument("--normalize-only", action="store_true", help="No network; normalize local and already-downloaded archives")
    args = parser.parse_args(argv)
    if not 1 <= args.max_archive_mb <= 200:
        parser.error("max-archive-mb must be between 1 and 200")
    try:
        quarters = selected_quarters(args.start_year, args.end_year, args.as_of, args.quarters)
        paths = list(args.local_archive)
        for directory in [args.output, *args.zip_dir]:
            paths.extend(directory.glob("*.zip"))
        origins = {}
        manifest_path = args.output / "download_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"downloads": []}
        for item in manifest["downloads"]:
            location = (ROOT / item["path"]).resolve() if not Path(item["path"]).is_absolute() else Path(item["path"]).resolve()
            if location.exists() and sha256_file(location) != item["sha256"]:
                raise HistoryDownloadError("An archived ZIP no longer matches its recorded hash")
            origins[str(location)] = item["origin"]
        if args.download or args.probe:
            for request_index, quarter in enumerate(quarters):
                if request_index:
                    time.sleep(0.2)  # At most five sequential requests per second.
                url, headers, origin = source_request(quarter, args.source_template, os.environ.get("SEC_USER_AGENT"))
                if args.probe:
                    print(json.dumps({"quarter": quarter, "url": url, "origin": origin,
                                      **probe_source(url, headers)}, ensure_ascii=False), flush=True)
                    continue
                destination = args.output / f"{quarter}.zip"
                existed = destination.exists()
                result = download_archive(url, destination, headers, max_bytes=args.max_archive_mb * 1024 * 1024)
                paths.append(destination)
                recorded = next((r for r in manifest["downloads"] if r["sha256"] == result["sha256"]), None)
                if recorded is None:
                    actual_origin = "preexisting_local_archive_origin_unverified" if existed else origin
                    record = {**result, "quarter": quarter, "origin": actual_origin,
                              "retrieved_at": datetime.now(timezone.utc).isoformat(),
                              "source_url": None if existed else url}
                    manifest["downloads"].append(record)
                    atomic_json(manifest_path, manifest)
                    origins[str(destination.resolve())] = actual_origin
                else:
                    origins[str(destination.resolve())] = recorded["origin"]
                print(json.dumps({"quarter": quarter, "status": result["status"], "bytes": result["bytes"]}), flush=True)
            if args.probe:
                return 0
        payload = normalize_archives(paths, args.output, as_of=args.as_of,
                                     member_ciks=read_member_ciks(args.members), origins=origins,
                                     max_bytes=args.max_archive_mb * 1024 * 1024)
        print(json.dumps(payload["summary"], ensure_ascii=False))
        return 0
    except (HistoryDownloadError, OSError, ValueError, zipfile.BadZipFile) as exc:
        # Never dump HTTP response bodies, headers or the contact User-Agent.
        if isinstance(exc, HistoryDownloadError):
            parser.exit(2, f"History acquisition failed: {exc}\n")
        parser.exit(2, f"History acquisition failed ({type(exc).__name__}); completed archives and prior results remain available\n")


if __name__ == "__main__":
    sys.exit(main())
