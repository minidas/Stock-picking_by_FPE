"""Reconstruct the 2012-12-31 closing-session universe with source audit.

These are retrospectively reconstructed memberships, not an archived vendor
point-in-time feed. The original source tables are intentionally preserved.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/universe_2012"
AS_OF = "2012-12-31"
GIST = "https://gist.githubusercontent.com/alexchinco/1ef9cb653334839f3c24/raw/7bc543ff53dd60191a5a6b1fc66fe045a68a0205/ticker-data.csv"
LAWCAL = "https://raw.githubusercontent.com/lawcal/sp500-components-history/main/data/components_history.csv"
BOUNDARY = "https://press.spglobal.com/2012-12-20-AbbVie-Set-to-Join-S-P-100-500-Federated-Investors-Set-to-Join-S-P-MidCap-400-ITT-Educational-Services-Procera-Networks-to-Join-S-P-SmallCap-600"
ALIASES = {"ACT": "WPI", "AGL": "GAS", "BF": "BF.B", "BRK": "BRK.B", "GHC": "WPO", "LB": "LTD", "MHFI": "MHP"}
# The source's current or successor CIK must never silently replace the 2012
# issuer. Historical filing matching is still required for unverified rows.
CIK_CORRECTIONS = {
    "GOOG": ("0001288776", "Google Inc.", "https://www.sec.gov/Archives/edgar/data/1288776/000119312512160666/0001193125-12-160666-index.htm"),
    "NWSA": ("0001308161", "News Corporation", "https://www.sec.gov/Archives/edgar/data/1308161/000119312512460872/R1.htm"),
    "IR": ("0001466258", "Ingersoll-Rand plc", "https://www.sec.gov/Archives/edgar/data/1000275/000121465913007308/j1227130424b2.htm"),
    "APA": ("0000006769", "Apache Corporation", "https://www.sec.gov/Archives/edgar/data/6769/000119312512086217/R1.htm"),
    "BHI": ("0000808362", "Baker Hughes Inc.", "https://www.sec.gov/Archives/edgar/data/808362/000080836213000017/R1.htm"),
    "BLK": ("0001364742", "BlackRock Inc.", "https://www.sec.gov/Archives/edgar/data/1364742/000119312512343728/R1.htm"),
    "CI": ("0000701221", "Cigna Corporation", "https://www.sec.gov/Archives/edgar/data/701221/000104746912007677/R1.htm"),
    "DIS": ("0001001039", "Walt Disney Company", "https://www.sec.gov/Archives/edgar/data/1001039/000119312512479027/0001193125-12-479027-index.htm"),
    "FTI": ("0001135152", "FMC Technologies Inc.", "https://www.sec.gov/Archives/edgar/data/1135152/000113515213000007/R1.htm"),
    "MDT": ("0000064670", "Medtronic Inc.", "https://www.sec.gov/Archives/edgar/data/64670/000089710112001524/R1.htm"),
    "MYL": ("0000069499", "Mylan Inc.", "https://www.sec.gov/Archives/edgar/data/83246/000114420412045415/v723931-1_424b2.htm"),
    "PRGO": ("0000820096", "Perrigo Company", "https://www.sec.gov/Archives/edgar/data/820096/000082009612000019/R1.htm"),
    "XOM": ("0000034088", "Exxon Mobil Corporation", "https://www.sec.gov/Archives/edgar/data/34088/000119312512078102/R1.htm"),
    "XRX": ("0000108772", "Xerox Corporation", "https://www.sec.gov/Archives/edgar/data/108772/0000108772-13-000006-index.htm"),
    "CB": ("0000020171", "Chubb Corporation", "https://www.sec.gov/Archives/edgar/data/20171/000119312512460479/R1.htm"),
    "JCI": ("0000053669", "Johnson Controls Inc.", "https://www.sec.gov/Archives/edgar/data/53669/000005366913000028/jci201310-k.htm"),
    "CLF": ("0000764065", "Cliffs Natural Resources Inc.", "https://www.sec.gov/Archives/edgar/data/764065/000076406512000007/R1.htm"),
    "LLL": ("0001056239", "L-3 Communications Holdings Inc.", "https://www.sec.gov/Archives/edgar/data/1056239/000119312512219534/R1.htm"),
}
# Plausible predecessor identifiers retained for subsequent filing validation;
# these do not authorize mixing financial observations across legal entities.
CIK_PENDING = {
    "ICE": "0001174746",
}


def normalized_name(value):
    value = re.sub(r"[/\\][A-Z]+[/\\]?", "", value.upper())
    words = re.findall(r"[A-Z0-9]+", value.replace("&", " AND "))
    return "".join(word for word in words if word not in {"THE", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LTD", "LIMITED", "PLC", "INCORPORATED", "AND"})


def historical_name_audit(members):
    path = ROOT / "data/research_2012_legacy_eps.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    records = json.loads(raw)["records"]
    by_cik = {}
    for record in records:
        by_cik.setdefault(record["cik"], []).append(record)
    # Explicitly different historical names remain unresolved even when a CIK
    # is identical: a rename/spin-off must not authorize mixed EPS/share bases.
    different_names = {"BEAM", "MDLZ", "NEE"}
    aliases = {"ABT": "ABBOTT LABORATORIES", "BCR": "BARD C R", "CA": "CA", "DISCA": "DISCOVERY COMMUNICATIONS", "GWW": "GRAINGER W W", "HNZ": "HEINZ H J", "LTD": "LIMITED BRANDS", "MMC": "MARSH MCLENNAN", "UPS": "UNITED PARCEL SERVICE", "WMT": "WAL MART STORES"}
    counts = {}
    for member in members:
        matches = by_cik.get(member["cik"], [])
        names = sorted({r["name"] for r in matches})
        expected = normalized_name(aliases.get(member["symbol"], member["name"]))
        compatible = bool(names) and all(expected == normalized_name(n) or (min(len(expected), len(normalized_name(n))) >= 4 and (expected in normalized_name(n) or normalized_name(n) in expected)) for n in names)
        if member["symbol"] in different_names:
            compatible = False
        status = "name_consistent_for_legacy_filing" if compatible else ("same_cik_name_requires_review" if matches else "no_legacy_filing_in_downloaded_batches")
        counts[status] = counts.get(status, 0) + 1
        documents = {r["accession"]: {key: r[key] for key in ("accession", "name", "available_at", "source_url", "raw_archive")} for r in matches}
        member["historical_filing_name_confirmation"] = {
            "status": status, "historical_filing_names": names,
            "evidence": list(documents.values()),
            "limitation": "Only identifies matching names in downloaded 2009/2010 filing batches. Does not validate 2012 issuer continuity, share basis, reorganizations, or price/EPS comparability.",
        }
    return {"input": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(), "counts": counts}


def holdings_crosscheck(members):
    path = ROOT / "data/research_2012_prices.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    prices = json.loads(raw)["prices"]
    symbols = [r.get("symbol") for r in prices]
    expected = {r["symbol"] for r in members}
    actual = {s for s in symbols if s}
    return {"evidence_path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(), "count": len(prices), "unique_symbols": len(actual), "unmapped": symbols.count(None), "missing_members": sorted(expected - actual), "extra_holdings": sorted(actual - expected), "exact_symbol_set_match": expected == actual and len(actual) == len(prices), "limitation": "IVV's retrospectively published fund holdings provide independent documentary corroboration, not an official S&P constituent certificate. Price-to-symbol mapping uses this reconstruction plus explicit reviewed aliases."}


def identity_name(value):
    """Normalize punctuation/legal suffixes, without fuzzy edit-distance matching."""
    value = re.sub(r"\b(?:Class|Series) [AB]\b.*$", "", value, flags=re.I)
    value = re.sub(r"\bRegistered\b", "", value, flags=re.I)
    value = re.sub(r"\bCor p\b", "Corp", value, flags=re.I)
    value = re.sub(r"\bBancorporation\b", "Bancorp", value, flags=re.I)
    value = re.sub(r"/\s*SWITZERLAND", "", value, flags=re.I)
    return normalized_name(value)


def filing_identity_audit(members):
    path = ROOT / "data/raw/sec_2012_bulk/submissions_2012.json"
    price_path = ROOT / "data/research_2012_prices.json"
    if not path.exists() or not price_path.exists():
        return None
    raw = path.read_bytes()
    rows = [r for r in json.loads(raw) if "2012-01-01" <= r["filed"] <= AS_OF]
    prices = {r["symbol"]: r["company_name"] for r in json.loads(price_path.read_text(encoding="utf-8"))["prices"] if r.get("symbol")}
    # Reviewed name-order variants and legal-name expansions. Their exact names
    # occur under the same historical CIK; they are never applied to other CIKs.
    aliases = {
        "BCR": ["BARD C R INC /NJ/"], "CVC": ["CABLEVISION SYSTEMS CORP /NY"],
        "DD": ["DUPONT E I DE NEMOURS & CO"], "DHI": ["HORTON D R INC /DE/"],
        "GWW": ["GRAINGER W W INC"], "HNZ": ["HEINZ H J CO"],
        "HOT": ["STARWOOD HOTEL & RESORTS WORLDWIDE, INC"],
        "LLY": ["LILLY ELI & CO"], "RHI": ["HALF ROBERT INTERNATIONAL INC /DE/"],
        "SCHW": ["SCHWAB CHARLES CORP"], "SJM": ["SMUCKER J M CO"],
        "TROW": ["PRICE T ROWE GROUP INC"],
    }
    by_cik = {}
    by_name = {}
    for row in rows:
        by_cik.setdefault(row["cik"], []).append(row)
        by_name.setdefault(identity_name(row["name"]), []).append(row)
    counts = {}
    unresolved = []
    for member in members:
        symbol = member["symbol"]
        matches = by_cik.get(member["cik"], [])
        expected = {identity_name(member["name"]), identity_name(prices.get(symbol, ""))}
        expected.update(identity_name(n) for n in aliases.get(symbol, []))
        expected.discard("")
        names = sorted({r["name"] for r in matches})
        recorded_change = None
        if symbol == "MDLZ":
            evidence = [r for r in matches if identity_name(r["name"]) in expected and identity_name(r.get("former") or "") == "KRAFTFOODS"]
            if evidence:
                expected.add("KRAFTFOODS")
                recorded_change = {"from": "KRAFT FOODS INC", "to": "MONDELEZ INTERNATIONAL, INC.", "evidence_accession": evidence[0]["adsh"], "note": "Same CIK and former-name field establish issuer-name continuity; the 2012 spin-off still requires separate earnings/share-basis treatment."}
        compatible = bool(names) and all(identity_name(n) in expected for n in names)
        status = "confirmed_cik_and_name" if compatible else ("same_cik_name_requires_review" if matches else "no_2012_record_in_downloaded_batch")
        if compatible and recorded_change:
            status = "confirmed_cik_and_recorded_name_change"
        candidates = {}
        if not compatible:
            for name in expected:
                for row in by_name.get(name, []):
                    candidates[row["cik"]] = {"cik": row["cik"], "name": row["name"], "example_accession": row["adsh"], "match_method": "exact_normalized_name_candidate_only"}
        evidence = []
        for row in matches:
            evidence.append({"accession": row["adsh"], "cik": row["cik"], "name": row["name"], "form": row["form"], "filed": row["filed"], "report_period": row["period"], "instance": row["instance"], "former": row.get("former"), "additional_ciks": row.get("aciks"), "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{row['adsh'].replace('-', '')}/"})
        member["2012_filing_identity_confirmation"] = {
            "status": status, "identity_join_allowed": compatible,
            "financial_values_or_share_basis_validated": False,
            "2012_filing_names": names, "evidence": evidence,
            "reviewed_legal_name_aliases": aliases.get(symbol, []),
            "recorded_name_change": recorded_change, "candidate_ciks": list(candidates.values()),
            "limitation": "Checks issuer identity only. Source is downloaded SEC submission metadata via a third-party dataset. EPS units, annual duration, GAAP tag, fiscal period, split basis, special distributions and filing availability need separate checks before forecasting.",
        }
        counts[status] = counts.get(status, 0) + 1
        if not compatible:
            unresolved.append({"symbol": symbol, "cik": member["cik"], "status": status, "candidate_ciks": list(candidates.values())})
    return {"input": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(), "downloaded_records": len(rows), "counts": counts, "unresolved": unresolved, "automatic_fuzzy_matching": False, "corrections": {"CLF": {"from": "0000076406", "to": "0000764065", "reason": "Source CIK digit error; 2012 SEC filing confirms corrected CIK."}, "LLL": {"from": "0001039101", "to": "0001056239", "reason": "Use listed parent, not the additional co-registrant subsidiary CIK."}}}


def now():
    return datetime.now(timezone.utc).isoformat()


def active(row):
    return row["date_added"][:10] <= AS_OF and (not row["date_removed"] or AS_OF < row["date_removed"][:10]) and row["created_at"] <= AS_OF


def fetch(name, url):
    path = RAW / name
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".partial")
        subprocess.run(["curl.exe", "--location", "--connect-timeout", "15", "--max-time", "60", "--fail", "--silent", "--show-error", "--output", str(temporary), url], check=True)
        temporary.replace(path)
    raw = path.read_bytes()
    return {"url": url, "path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "verified_at": now()}


def build():
    RAW.mkdir(parents=True, exist_ok=True)
    sources = [fetch("alexchinco_ticker-data.csv", GIST), fetch("lawcal_components_history.csv", LAWCAL)]
    boundary_path = RAW / "spglobal_abbvie_boundary.html"
    if boundary_path.exists() and "after the close of trading" in boundary_path.read_text(encoding="utf-8", errors="replace"):
        sources.append(fetch(boundary_path.name, BOUNDARY))
    auxiliary_sources = []
    helper_path = RAW / "lawcal_update.py.txt"
    if helper_path.exists():
        auxiliary_sources.append({**fetch(helper_path.name, "https://raw.githubusercontent.com/lawcal/sp500-components-history/main/bin/update.py"), "role": "source filtering-method audit only; never executed"})
    partial_path = RAW / "hanshof_sp_500_historical_components.csv.partial"
    if partial_path.exists():
        payload = partial_path.read_bytes()
        auxiliary_sources.append({"url": "https://raw.githubusercontent.com/hanshof/sp500_constituents/main/sp_500_historical_components.csv", "path": str(partial_path.relative_to(ROOT)), "status": "partial_download_not_used", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "error": "curl transfer timed out; partial source retained for audit only", "quality_note": "Observed historical rows use later ticker aliases; not used in the reconstruction."})
    with (RAW / "alexchinco_ticker-data.csv").open(encoding="utf-8-sig", newline="") as f:
        gist = list(csv.DictReader(f))
    with (RAW / "lawcal_components_history.csv").open(encoding="utf-8-sig", newline="") as f:
        lawcal = list(csv.DictReader(f))
    selected_gist = [r for r in gist if r["t1"] <= AS_OF <= r["t2"]]
    selected_lawcal = {r["symbol"]: r for r in lawcal if active(r)}
    by_symbol = {}
    for row in selected_gist:
        if row["ticker"] == "ABBV":
            continue
        symbol = ALIASES.get(row["ticker"], row["ticker"])
        record = selected_lawcal.get(symbol)
        if record is None:
            matches = [x for x in lawcal if x["symbol"] == symbol]
            record = matches[0] if matches else None
        by_symbol[symbol] = {
            "symbol": symbol, "name": row["name"].strip('"').lstrip("'"),
            "cik": record["cik"] if record else None,
            "cik_mapping_quality": "historical_mapping_unverified",
            "gist_symbol": row["ticker"], "gist_interval": {"from": row["t1"], "through": row["t2"]},
            "lawcal_record": record,
            "source": [GIST] + ([LAWCAL] if record else []),
            "membership_quality": "secondary_reconstruction",
        }
        if symbol in ALIASES.values():
            by_symbol[symbol]["name"] = record["name"]
            by_symbol[symbol]["ticker_note"] = "Gist alias or later ticker replaced by historical lawcal ticker."
    for symbol in ("AVB", "CCE", "OI", "TWX"):
        record = selected_lawcal[symbol]
        by_symbol[symbol] = {"symbol": symbol, "name": record["name"], "cik": record["cik"], "cik_mapping_quality": "historical_mapping_unverified", "lawcal_record": record, "source": [LAWCAL], "membership_quality": "secondary_reconstruction", "membership_note": "Missing in gist; retained from historical lawcal interval and cross-checked against December 2012 IVV holdings."}
    by_symbol["GOOG"] = {"symbol": "GOOG", "source": [CIK_CORRECTIONS["GOOG"][2]], "membership_quality": "secondary_reconstruction", "membership_note": "Both source filters omit Google due to ticker-history errors; Google Class A is present in December 2012 IVV holdings. GOOG was the 2012 ticker."}
    for symbol, (cik, name, source) in CIK_CORRECTIONS.items():
        by_symbol[symbol].update(cik=cik, name=name, cik_mapping_quality="historical_identifier_source_checked", cik_source=source)
    for symbol, cik in CIK_PENDING.items():
        by_symbol[symbol].update(cik=cik, cik_mapping_quality="predecessor_identifier_requires_filing_validation", cik_note="Source table points at a successor or different issuer; validate historical filing name before using financial facts.")
    by_symbol["TWC"]["name"] = "Time Warner Cable Inc."
    by_symbol["TWC"]["name_note"] = "Gist's short name Time Warner is ambiguous; lawcal and December 2012 IVV holdings distinguish TWC from TWX."
    members = sorted(by_symbol.values(), key=lambda x: x["symbol"])
    if len(members) != len({r["symbol"] for r in members}):
        raise ValueError("Duplicate symbols")
    for row in members:
        row["as_of"] = AS_OF
        row["reconstruction_only"] = True
    legacy_audit = historical_name_audit(members)
    identity_audit = filing_identity_audit(members)
    holdings_audit = holdings_crosscheck(members)
    holdings_pdf = ROOT / "data/raw/prices_2012/ishares_holdings_requests.pdf"
    if holdings_pdf.exists():
        payload = holdings_pdf.read_bytes()
        sources.append({"url": "https://announcements.asx.com.au/asxpdf/20130304/pdf/42dfsw4vv9s2vp.pdf", "path": str(holdings_pdf.relative_to(ROOT)), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "verified_at": now(), "role": "Independent fund-holdings evidence, physical PDF pages 59-66; not an official index membership archive.", "source_publication_date": "2013-03-04"})
    audit = {
        "schema_version": 1, "as_of": AS_OF,
        "session_convention": "2012-12-31 regular trading session through closing auction, before after-close constituent changes",
        "reconstruction_only": True, "created_at": now(), "source_files": sources,
        "count": len(members), "full_official_membership_certified": False,
        "legacy_filing_name_audit": legacy_audit,
        "2012_filing_identity_audit": identity_audit,
        "holdings_crosscheck": holdings_audit,
        "source_counts": {"gist_date_inclusive": len(selected_gist), "lawcal_date_filter": len(selected_lawcal)},
        "boundary": {"included": "FII", "excluded": "ABBV", "source": BOUNDARY, "reason": "S&P December 20 announcement makes replacement after the December 31 close; AbbVie regular-way trading starts January 2, 2013."},
        "corrections": {"ticker_aliases": ALIASES, "gist_missing_added": ["AVB", "CCE", "OI", "TWX", "GOOG"], "lawcal_false_positive_not_added": ["ACS"], "lawcal_later_date_overwrite_recovered_from_gist": ["D", "IR", "NWSA", "SRE", "TROW"]},
        "limitations": ["Retrospective community source reconciliation is not a licensed S&P constituent archive.", "500 records is a coverage count, not a proof of historical correctness.", "CIKs marked unverified require agreement with historical SEC filing identity before numerical use.", "The source tables contain later company names and sector labels; historical names use gist where possible.", "A date-only snapshot cannot represent the December 31 after-close transition without an explicit convention."],
        "members": members,
    }
    destination = ROOT / "data/universe_2012_members.json"
    destination.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    memberships = {
        "as_of": AS_OF, "reconstruction_only": True, "session_convention": audit["session_convention"],
        "effective_interval_note": "The one-day effective interval is the validity of this reconstructed snapshot only; it is not the company's actual index entry/exit interval.",
        "availability_note": "available_at is an assumed historical eligibility date for reconstruction-mode research, not the download/publication date of these modern source files; do not claim strict PIT validation.",
        "memberships": [{"symbol": r["symbol"], "effective_from": AS_OF, "effective_to": "2013-01-01", "available_at": AS_OF, "reconstruction_only": True, "source": "retrospective-reconstruction:" + str(destination.relative_to(ROOT))} for r in members],
    }
    (ROOT / "data/universe_2012_memberships.json").write_text(json.dumps(memberships, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RAW / "manifest.json").write_text(json.dumps({"schema_version": 1, "verified_at": now(), "sources": sources, "auxiliary_sources": auxiliary_sources}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"members": len(members), "cik_source_checked": sum(r["cik_mapping_quality"] == "historical_identifier_source_checked" for r in members), "output": str(destination)}


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(build(), ensure_ascii=False))
