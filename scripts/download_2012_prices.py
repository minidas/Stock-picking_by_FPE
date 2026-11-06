"""Download and parse IVV's 2012-12-31 holdings as historical price evidence.

These are fund-valuation-derived prices, not a historical exchange-close feed.
The report was published in 2013 and is used only to reconstruct 2012 prices.
No EPS or institution expectations are inferred from this report.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/prices_2012"
URL = "https://announcements.asx.com.au/asxpdf/20130304/pdf/42dfsw4vv9s2vp.pdf"
PDF = RAW / "ishares_holdings_requests.pdf"
OUTPUT = ROOT / "data/research_2012_prices.json"

# Explicitly reviewed legal-name/short-name equivalences, never fuzzy matching.
# Other names must match the reconstructed membership names after normalization.
NAME_ALIASES = {
    "altriagroup": "MO", "capitalonefinancial": "COF", "goldmansachsgroup": "GS",
    "pncfinancialservicesgroup": "PNC", "molsoncoorsbrewing": "TAP", "biogenidec": "BIIB",
    "airproductsandchemicals": "APD", "cfindustriesholdings": "CF", "eidupontdenemoursand": "DD",
    "lyondellbasellindustries": "LYB", "roberthalfinternational": "RHI", "esteelaudercompanies": "EL",
    "etradefinancial": "ETFC", "nasdaqomxgroup": "NDAQ", "trowepricegroup": "TROW",
    "hjheinz": "HNZ", "mccormickcoincnvs": "MKC", "bectondickinsonand": "BDX",
    "davitahealthcarepartners": "DVA", "laboratorycorpofamericaholdings": "LH",
    "harmaninternationalindustries": "HAR", "newellrubbermaid": "NWL", "ace": "ACE",
    "hartfordfinancialservicesgroup": "HIG", "marshmclennancompanies": "MMC",
    "starwoodhotelsresortsworldwide": "HOT", "eatoncorp": "ETN", "cablevisionnygroup": "CVC",
    "discoverycommunicationsincseriesa": "DISCA", "mcgrawhillcompanies": "MHP", "washingtonpost": "WPO",
    "freeportmcmorancoppergold": "FCX", "rowancompanies": "RDC", "tesoro": "TSO",
    "dentsplyinternational": "XRAY", "elilillyand": "LLY", "expressscriptsholding": "ESRX",
    "meadjohnsonnutrition": "MJN", "apartmentinvestmentandmanagement": "AIV", "costcowholesale": "COST",
    "cvscaremark": "CVS", "limitedbrands": "LTD", "lowescompanies": "LOW", "walmartstores": "WMT",
    "citrixsystems": "CTXS", "ciscosystems": "CSCO", "crowncastleinternational": "CCI",
    "verizoncommunications": "VZ", "windstream": "WIN", "chrobinsonworldwide": "CHRW",
    "expeditorsinternationalofwashington": "EXPD",
}


def normalized_name(name):
    name = re.sub("[^a-z0-9]", "", name.lower())
    for marker in ["classanvs", "classbnvs", "classa", "classb", "registered", "the"]:
        name = name.replace(marker, "")
    for suffix in ["incorporated", "corporation", "company", "inc", "corp", "plc", "ltd", "co", "nv"]:
        name = re.sub(suffix + "$", "", name)
    return name


def match_tickers(rows, membership_path):
    members = json.loads(membership_path.read_text(encoding="utf-8"))["members"]
    by_symbol = {row["symbol"]: row for row in members}
    lookup = {}
    for member in members:
        for name in [member["name"], member.get("lawcal_record", {}).get("name", "")]:
            key = normalized_name(name)
            if key:
                lookup.setdefault(key, set()).add(member["symbol"])
    for row in rows:
        key = normalized_name(row["company_name"])
        candidates = lookup.get(key, set())
        if key in NAME_ALIASES:
            symbol = NAME_ALIASES[key]
            method = "reviewed_legal_name_alias"
        elif len(candidates) == 1:
            symbol = next(iter(candidates))
            method = "normalized_company_name_match"
        else:
            continue
        if symbol not in by_symbol:
            raise ValueError(f"Price ticker {symbol} absent from historical universe")
        row.update(symbol=symbol, ticker_match_status=method,
                   membership_company_name=by_symbol[symbol]["name"],
                   ticker_mapping_source=str(membership_path.relative_to(ROOT)))
    symbols = [row["symbol"] for row in rows if row["symbol"]]
    if len(symbols) != len(set(symbols)):
        raise ValueError("Multiple fund holdings map to one ticker; review share classes")
    return {"matched": len(symbols), "unmatched_prices": [x["company_name"] for x in rows if not x["symbol"]],
            "missing_members": sorted(set(by_symbol) - set(symbols))}


def parse_holdings(pdf: Path):
    reader = PdfReader(pdf)
    records = []
    text_pages = []
    for index in range(58, 66):
        text = reader.pages[index].extract_text()
        if "CORE S&P 500 ETF" not in text or "December 31, 2012" not in text:
            raise ValueError(f"Unexpected fund or date on PDF page {index + 1}")
        text_pages.append(f"PAGE {index + 1}\n{text}")
        pending = ""
        sector = None
        in_table = False
        for line in text.splitlines():
            line = line.strip()
            if "Security" in line and "Shares" in line and "Value" in line:
                in_table = True
                pending = ""
                continue
            if "Total COMMON STOCKS" in line:
                in_table = False
            if not in_table or not line:
                continue
            if re.search(r" — \d+\.\d+%", line):
                sector = line.split(" — ")[0]
                pending = ""
                continue
            # Pypdf inserts isolated spaces inside numbers and words.  Columns
            # have at least two spaces; one-space digit fragments are joined.
            number = r"\d[\d,]*(?: [\d,]+)*"
            match = re.match(rf"^(.*?)\s+({number})\s{{2,}}\$?\s*({number})$", line)
            if match:
                name, shares, value = match.groups()
                name = (pending + " " + name).strip()
                shares = int(re.sub(r"[ ,]", "", shares))
                value = int(re.sub(r"[ ,]", "", value))
                if not name or shares <= 0 or value <= 0:
                    raise ValueError(f"Invalid holding: {line}")
                records.append({
                    "company_name": name, "symbol": None,
                    "date": "2012-12-31", "currency": "USD",
                    "fund_shares": shares, "fund_value_usd": value,
                    "price": round(value / shares, 8),
                    "price_rounded_cents": round(value / shares, 2),
                    "one_dollar_reported_value_increment_per_share": 1.0 / shares,
                    "price_kind": "fund_valuation_divided_by_reported_shares",
                    "share_basis": "nominal_shares_at_2012_12_31",
                    "source": URL, "source_publication_date": "2013-03-04",
                    "source_pdf_page": index + 1, "source_sector": sector,
                    "source_row": line, "ticker_match_status": "unmatched",
                })
                pending = ""
            elif (re.match(r"^[A-Z]", line) and
                  not any(k in line for k in ["For personal", "Security", "NVS", "Total", "COMMON STOCKS"]) and
                  not re.search(r"\d{2,}", line)):
                pending = line
            else:
                pending = ""
    # A full equality check catches dropped/misaligned rows, not just row counts.
    total = sum(row["fund_value_usd"] for row in records)
    if total != 34_986_901_401:
        raise ValueError(f"Parsed common-stock values {total:,} differ from report total 34,986,901,401; rows={len(records)}")
    if len({row["company_name"] for row in records}) != len(records):
        raise ValueError("Duplicate company names in holdings")
    return records, "\n\n".join(text_pages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    if args.refresh or not PDF.exists():
        response = requests.get(URL, timeout=(15, 180))
        response.raise_for_status()
        data = response.content
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise ValueError("Incomplete or invalid PDF")
        PDF.write_bytes(data)
    raw = PDF.read_bytes()
    rows, extracted = parse_holdings(PDF)
    membership_path = ROOT / "data/universe_2012_members.json"
    mapping = match_tickers(rows, membership_path) if membership_path.exists() else {"matched": 0}
    (RAW / "ivv_extracted.txt").write_text(extracted, encoding="utf-8")
    payload = {
        "as_of": "2012-12-31", "scope": "individual stock price evidence from IVV holdings",
        "price_count": len(rows), "prices": rows,
        "source": {"url": URL, "publication_date": "2013-03-04", "local_file": str(PDF.relative_to(ROOT)),
                   "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        "validation": {"sum_common_stock_value_usd": sum(x["fund_value_usd"] for x in rows),
                       "reported_common_stock_value_usd": 34_986_901_401,
                       "sum_matches": True, "ticker_mapping": mapping,
                       "independent_fund_crosschecks": {
                           "source": "https://www.sec.gov/Archives/edgar/data/36405/000093247113005759/index_final.htm",
                           "source_access": "SEC HTML verified through web browser text; direct local download returned HTTP 403",
                           "source_date": "2012-12-31", "valuation_currency": "USD", "printed_value_unit": "USD_thousands",
                           "observations": [
                               {"symbol": "AAPL", "shares": 8735602, "value_usd_thousands": 4656338,
                                "derived_price": 4656338000 / 8735602},
                               {"symbol": "STX", "shares": 3120898, "value_usd_thousands": 95125,
                                "derived_price": 95125000 / 3120898},
                               {"symbol": "IBM", "shares": 9863306, "value_usd_thousands": 1889316,
                                "derived_price": 1889316000 / 9863306},
                           ],
                           "interpretation": "Independent fund valuations agree after cent rounding; this does not establish exchange consolidated closing trade prices.",
                       }},
        "limitations": [
            "2013 publication reconstructs 2012-12-31 nominal holding valuations; not contemporaneous forecast information.",
            "Fund valuation per share is not guaranteed to equal the exchange closing trade; issuer permits fair-value methods.",
            "Some valuations differ from FT/CRN price evidence (e.g. AAPL 533.03 vs FT 532.2; STX 30.48 vs CRN 30.42). Retain both sources; do not silently overwrite.",
            "Fund holdings may contain non-index residual positions; membership must be joined to independently reconstructed historical constituents.",
            "Nominal December 2012 share basis must be reconciled with any EPS split basis before model use.",
        ],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RAW / "manifest.json").write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "research", "raw_preserved": True, "source": payload["source"],
        "validation": payload["validation"], "price_count": len(rows),
        "attempts": [
            {"transport": "curl", "timeout_seconds": 60, "status": "partial_timeout"},
            {"transport": "curl_resume", "timeout_seconds": 240, "status": "partial_timeout"},
            {"transport": "requests", "status": "success", "http_status": 200},
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"prices": len(rows), "sum_verified": True, "output": str(OUTPUT)}))


if __name__ == "__main__":
    main()
