"""Build a dated current-study universe from archived public evidence only.

Uses the Python standard library (including ZIP/XML for XLSX); never reads data/.
The observed membership is not a point-in-time historical membership archive.
Fund holdings prices retain the date inside their downloaded source document.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STUDY = ROOT / "outputs/current_sp500_2026-11-06"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def canonical_symbol(value: str) -> str:
    # Explicit observed share-class aliases, not generic ticker rewriting.
    return {"BRK B": "BRK.B", "BF B": "BF.B", "BRK-B": "BRK.B",
            "BF-B": "BF.B"}.get(value.strip(), value.strip())


def latest_evidence(study: Path, name: str) -> tuple[dict, bytes]:
    candidates = []
    for line in (study / "evidence/download_manifest.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("name") == name and row.get("success") and row.get("status") == 200:
            candidates.append(row)
    if not candidates:
        raise ValueError(f"No successful archived evidence for {name}")
    metadata = max(candidates, key=lambda row: datetime.fromisoformat(row["retrieved_at"]))
    path = ROOT / metadata["file"]
    if not path.resolve().is_relative_to((study / "evidence").resolve()):
        raise ValueError(f"Evidence path is outside this study: {name}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError(f"Evidence hash mismatch: {name}")
    return metadata, raw


def number(value: str) -> float:
    return float(value.replace(",", ""))


def parse_ivv(raw: bytes) -> tuple[str, dict, list, dict]:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    dated = [r for r in rows if r and r[0] == "Fund Holdings as of"]
    if len(dated) != 1:
        raise ValueError("IVV holdings date missing or ambiguous")
    source_date = datetime.strptime(dated[0][1], "%b %d, %Y").date().isoformat()
    header_index = next(i for i, r in enumerate(rows) if r and r[0] == "Ticker")
    header = rows[header_index]
    stocks, rejected, aliases = {}, [], {}
    for row in rows[header_index + 1:]:
        if len(row) != len(header):
            continue
        record = dict(zip(header, row))
        original = record["Ticker"]
        reason = None
        if record.get("Type") != "EQUITY":
            reason = "not_common_equity_type"
        elif record.get("Exchange", "").startswith("NO MARKET"):
            reason = "unlisted_residual_corporate_action_position"
        if reason:
            rejected.append({"symbol": original, "name": record["Name"],
                             "type": record.get("Type"), "reason": reason})
            continue
        symbol = canonical_symbol(original)
        if symbol in stocks:
            raise ValueError(f"Duplicate IVV common stock: {symbol}")
        if symbol != original:
            aliases[original] = symbol
        if record.get("Currency") != "USD" or record.get("Market Currency") != "USD":
            raise ValueError(f"Non-USD IVV equity requires review: {symbol}")
        price = number(record["Price"])
        if price <= 0:
            raise ValueError(f"Nonpositive vendor price: {symbol}")
        stocks[symbol] = {"name": record["Name"], "price": price,
                          "price_date": source_date, "currency": "USD",
                          "price_kind": "fund_holdings_vendor_price",
                          "exchange": record["Exchange"], "raw_symbol": original,
                          "quantity": number(record["Quantity"]),
                          "market_value": number(record["Market Value"])}
    return source_date, stocks, rejected, aliases


def spreadsheet_rows(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(node.itertext()) for node in
                       ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        worksheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        result = []
        for row in worksheet.findall("s:sheetData/s:row", NS):
            output = {}
            for cell in row:
                value = cell.find("s:v", NS)
                text = value.text if value is not None else ""
                if cell.get("t") == "s":
                    text = strings[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(cell.find("s:is", NS).itertext())
                output[re.sub(r"\d", "", cell.attrib["r"])] = text
            result.append(output)
    return result


def parse_spy(raw: bytes) -> tuple[str, dict, list, dict]:
    rows = spreadsheet_rows(raw)
    dated = [r.get("B", "") for r in rows if r.get("A") == "Holdings:"]
    if len(dated) != 1:
        raise ValueError("SPY holdings date missing or ambiguous")
    source_date = datetime.strptime(dated[0].removeprefix("As of "), "%d-%b-%Y").date().isoformat()
    first = next(i for i, r in enumerate(rows) if r.get("A") == "Name" and r.get("B") == "Ticker")
    stocks, rejected, aliases = {}, [], {}
    for row in rows[first + 1:]:
        ticker = row.get("B", "")
        if not ticker:
            continue
        if ticker == "-" and row.get("A") == "US DOLLAR":
            rejected.append({"symbol": ticker, "name": row["A"],
                             "reason": "cash_not_common_stock"})
            continue
        if "CONTRA" in row.get("A", "") or "CVR" in row.get("C", ""):
            rejected.append({"symbol": ticker, "name": row["A"], "identifier": row.get("C"),
                             "reason": "contingent_value_right_residual_not_common_stock"})
            continue
        if not re.fullmatch(r"[A-Z]+(?:[.][A-Z])?", ticker):
            raise ValueError(f"Unclassified SPY holding: {ticker}")
        symbol = canonical_symbol(ticker)
        if symbol in stocks:
            raise ValueError(f"Duplicate SPY common stock: {symbol}")
        if symbol != ticker:
            aliases[ticker] = symbol
        stocks[symbol] = {"name": row["A"], "raw_symbol": ticker,
                          "identifier": row.get("C"), "sedol": row.get("D"),
                          "weight_percent": number(row["E"]),
                          "shares_held": number(row["G"]), "currency": row.get("H")}
    return source_date, stocks, rejected, aliases


class ConstituentsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif attrs.get("id") == "constituents":
                self.depth = 1
        if not self.depth:
            return
        if tag == "sup":
            self.suppressed += 1
        if tag == "tr":
            self.row = []
        if tag in {"th", "td"}:
            self.cell = []

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == "sup":
            self.suppressed = max(0, self.suppressed - 1)
        if tag in {"th", "td"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None
        if tag == "table":
            self.depth -= 1

    def handle_data(self, text):
        if self.depth and self.cell is not None and not self.suppressed:
            self.cell.append(text)


def parse_wikipedia(raw: bytes) -> dict:
    parser = ConstituentsParser()
    parser.feed(raw.decode("utf-8"))
    if not parser.rows or parser.rows[0] != ["Symbol", "Security", "GICS Sector", "GICS Sub-Industry",
                                            "Headquarters Location", "Date added", "CIK", "Founded"]:
        raise ValueError("Wikipedia constituent table header changed")
    result = {}
    for row in parser.rows[1:]:
        if len(row) != 8:
            raise ValueError("Unexpected Wikipedia constituent row")
        symbol = canonical_symbol(row[0])
        if symbol in result:
            raise ValueError(f"Duplicate Wikipedia member: {symbol}")
        cik = row[6]
        if not re.fullmatch(r"\d{1,10}", cik):
            raise ValueError(f"Invalid CIK for {symbol}")
        added = row[5] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[5]) else None
        if added:
            date.fromisoformat(added)
        result[symbol] = {"name": row[1], "sector": row[2], "industry": row[3],
                          "headquarters": row[4], "date_added_reported": added,
                          "date_added_raw": row[5], "cik": cik.zfill(10)}
    return result


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def build(study: Path, as_of: str) -> dict:
    cutoff = date.fromisoformat(as_of)
    metadata, raw = {}, {}
    for name in ("ivv", "spy", "wikipedia"):
        metadata[name], raw[name] = latest_evidence(study, name)
        if datetime.fromisoformat(metadata[name]["retrieved_at"]).date() > cutoff:
            raise ValueError(f"Evidence retrieved after study date: {name}")
    ivv_date, ivv, ivv_excluded, ivv_aliases = parse_ivv(raw["ivv"])
    spy_date, spy, spy_excluded, spy_aliases = parse_spy(raw["spy"])
    wiki = parse_wikipedia(raw["wikipedia"])
    for name, source_date in (("ivv", ivv_date), ("spy", spy_date)):
        if date.fromisoformat(source_date) > cutoff:
            raise ValueError(f"Future dated holdings: {name}")
        metadata[name]["document_as_of"] = source_date
    metadata["wikipedia"]["document_as_of"] = None
    metadata["wikipedia"]["observation_date"] = as_of
    sets = {"ivv": set(ivv), "spy": set(spy), "wikipedia": set(wiki)}
    differences = {}
    for left, right in (("ivv", "spy"), ("ivv", "wikipedia"), ("spy", "wikipedia")):
        differences[f"{left}_vs_{right}"] = {
            f"only_{left}": sorted(sets[left] - sets[right]),
            f"only_{right}": sorted(sets[right] - sets[left])}
    full_agreement = sets["ivv"] == sets["spy"] == sets["wikipedia"]
    stocks, memberships = [], []
    for symbol, info in sorted(wiki.items()):
        effective = info["date_added_reported"] or as_of
        if date.fromisoformat(effective) > cutoff:
            raise ValueError(f"Future Wikipedia entry for {symbol}: {effective}")
        stock = {"symbol": symbol, "as_of": as_of, "asset_type": "stock", **info,
                 "observed_current_member": True,
                 "membership_status": "three_source_agreement" if symbol in ivv and symbol in spy else "source_difference_requires_review",
                 "membership_available_at": as_of,
                 "membership_effective_from": effective,
                 "membership_effective_from_basis": "wikipedia_reported_date_not_independently_historical_verified" if info["date_added_reported"] else "current_observation_only",
                 "source": {name: metadata[name]["url"] for name in metadata},
                 "ivv_holding": ivv.get(symbol), "spy_holding": spy.get(symbol)}
        if symbol in ivv:
            stock.update({key: ivv[symbol][key] for key in ("price", "price_date", "currency", "price_kind")})
            stock["price_source"] = metadata["ivv"]["url"]
        stocks.append(stock)
        memberships.append({"symbol": symbol, "effective_from": effective,
                            "effective_to": "", "available_at": as_of,
                            "source": metadata["wikipedia"]["url"]})
    audit = {"as_of": as_of, "method": "archived_wikipedia_members_crosschecked_against_filtered_IVV_and_SPY_holdings",
             "sources": metadata, "counts": {name: len(values) for name, values in sets.items()},
             "universe_security_count": len(stocks), "unique_cik_count": len({s["cik"] for s in stocks}),
             "nominal_vendor_price_count": sum("price" in s for s in stocks),
             "price_date": ivv_date, "all_three_sources_agree": full_agreement,
             "differences": differences,
             "excluded_non_constituent_positions": {"ivv": ivv_excluded, "spy": spy_excluded},
             "symbol_aliases": {"ivv": ivv_aliases, "spy": spy_aliases},
             "limitations": [
                 "Current observation, not an S&P-licensed historical constituent certification.",
                 "Wikipedia is the requested target list; all source differences remain explicit and require review.",
                 "Membership available_at is this study's observation date, never the download relabelled as an old announcement date.",
                 "Reported entry dates are preserved without asserting historical announcement availability.",
                 "IVV prices are fund holdings vendor prices dated within the CSV; no independent exchange-close verification.",
                 "SPY workbook has weights and share quantities, not per-stock prices; prices are not inferred from weights.",
                 "Fund-held corporate-action residuals, warrants, cash, collateral and futures are not common-stock constituents."]}
    target = study / "input"
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "universe.json", {"as_of": as_of, "members": stocks})
    write_json(target / "universe_audit.json", audit)
    with (target / "memberships.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "effective_from", "effective_to", "available_at", "source"])
        writer.writeheader()
        writer.writerows(memberships)
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    parser.add_argument("--as-of", default="2026-11-06")
    args = parser.parse_args()
    audit = build(args.study.resolve(), args.as_of)
    print(json.dumps({k: audit[k] for k in ("as_of", "counts", "universe_security_count", "unique_cik_count",
                                            "nominal_vendor_price_count", "price_date", "all_three_sources_agree", "differences")}, indent=2))
