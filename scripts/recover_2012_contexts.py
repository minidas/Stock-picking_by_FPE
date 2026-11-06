"""Recover explicitly reviewed stock contexts from the existing 2012 raw facts.

The selections below were checked against the linked contemporary filings.
This resolves a specific share-class/entity selection, not every share-basis,
corporate-action or forecasting-quality question.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/sec_2012_bulk/annual_eps_and_splits_2012.json"
OUT = ROOT / "data/research_2012_supplemental_eps.json"

# Exact context matches deliberately exclude extra dimensions such as an
# accounting-estimate effect, other classes, or another co-registrant.
SELECTIONS = [
    ("DTV", "0001465112", "0001047469-12-001379", "ClassOfStock=CommonClassA;", None,
     "https://www.sec.gov/Archives/edgar/data/1465112/000104746912001379/a2207454z10-k.htm",
     "The annual filing identifies DTV as the traded Class A stock. Use total Class A diluted EPS, not the separate service-life-estimate effect."),
    ("HST", "0001070750", "0001193125-12-072499", "LegalEntity=ParentCompany;", "ParentCompany",
     "https://www.sec.gov/Archives/edgar/data/1061937/000119312512072499/d226381d10k.htm",
     "The joint filing separates Host Inc. common shares from Host L.P. units. The ParentCompany EPS matches Host Inc. diluted loss per common share."),
    ("HSY", "0000047111", "0001193125-12-067143", "ClassOfStock=CommonStock;", None,
     "https://www.sec.gov/Archives/edgar/data/47111/000119312512067143/d251393d10k.htm",
     "HSY is the publicly traded Common Stock; Class B stock is not publicly traded. Select diluted Common Stock EPS."),
    ("KMI", "0001506307", "0001135017-12-000009", "ClassOfStock=ClassP;", None,
     "https://www.sec.gov/Archives/edgar/data/1506307/000113501712000009/kmi10k2011.htm",
     "KMI is the listed Class P stock. Class P diluted 2011 EPS is 0.74, not the Class A value 0.68. Only one EPS year is available; growth and loss probabilities rely on priors."),
    ("LM", "0000704051", "0000704051-12-000046", "LegalEntity=ConsolidatedLeggMasonGroup;", "ConsolidatedLeggMasonGroup",
     "https://www.sec.gov/Archives/edgar/data/704051/000070405112000046/lm10k3312012.htm",
     "The consolidated statement labels these diluted EPS values as attributable to Legg Mason Inc. common shareholders; this is not a subsidiary-only measure."),
    ("PRU", "0001137774", "0001193125-12-078116", "Scenario=FinancialServicesBusiness;", None,
     "https://www.sec.gov/Archives/edgar/data/1137774/000119312512078116/d291665d10k.htm",
     "NYSE PRU Common Stock tracks Financial Services Businesses. The privately placed Class B stock tracks Closed Block Business; select Financial Services diluted GAAP EPS."),
    ("STZ", "0000016918", "0001193125-12-195350", "ClassOfStock=CommonClassA;", None,
     "https://www.sec.gov/Archives/edgar/data/16918/000119312512195350/d285276d10k.htm",
     "The archived IVV price row explicitly identifies Class A. The filing provides separate Class A and Class B diluted EPS; select Class A only."),
    ("V", "0001403161", "0001384108-12-000011", "ClassOfStock=CommonClassA;", None,
     "https://www.sec.gov/Archives/edgar/data/1403161/000138410812000011/v09301210-k.htm",
     "The archived IVV price row explicitly identifies Visa Class A. Select the 2012 filing's Class A diluted GAAP EPS, not adjusted earnings or Class B."),
    ("WPX", "0001518832", "0001193125-12-235045", None, None,
     "https://www.sec.gov/Archives/edgar/data/1518832/000119312512235045/d336783dex991.htm",
     "The May 2012 8-K exhibit includes full annual consolidated statements. Its three annual EPS values use 197.1 million weighted-average shares consistently; this is annual data despite the 8-K form. Negative base EPS still makes the positive-log-growth model abstain."),
]


def recover(raw):
    companies = []
    for symbol, cik, accession, segments, coreg, url, rationale in SELECTIONS:
        facts = [f for f in raw if str(f["cik"]).zfill(10) == cik
                 and f["adsh"] == accession and f.get("segments") == segments
                 and f.get("coreg") == coreg and f["qtrs"] == 4
                 and f["tag"] in ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")
                 and f["filed"] <= "2012-12-31" and f["version"].startswith("us-gaap/")]
        if not facts:
            raise ValueError(f"No exact reviewed context for {symbol}")
        dates = {}
        for fact in facts:
            if fact["uom"] not in ("USD", "USD/shares"):
                raise ValueError(f"Unsupported per-share unit for {symbol}")
            if fact["ddate"] > fact["filed"]:
                raise ValueError(f"Future financial period for {symbol}")
            if fact["ddate"] in dates and dates[fact["ddate"]]["value"] != fact["value"]:
                raise ValueError(f"Conflicting EPS in reviewed context for {symbol}")
            dates[fact["ddate"]] = fact
        first = facts[0]
        history = [{"period_end": day, "available_at": f["filed"], "eps": f["value"],
                    "tag": f["tag"], "unit": f["uom"], "accession": accession,
                    "period_date_precision": "nearest_month_end_in_SEC_dataset"}
                   for day, f in sorted(dates.items())]
        if history[-1]["period_end"] != first["period"]:
            raise ValueError(f"Current annual anchor missing for {symbol}")
        companies.append({
            "symbol": symbol, "cik": cik, "name": first["name"], "form": first["form"],
            "filing_date": first["filed"], "period_end": first["period"], "accession": accession,
            "history": history, "conflicts": [], "split_facts": [],
            "share_basis_verified": False, "context_selection_reviewed": True,
            "source_url": url, "source_kind": "reviewed_context_selection_from_existing_2012_raw_mirror",
            "context_selection": {"segments": segments, "coreg": coreg, "rationale": rationale,
                                  "evidence_url": url, "review_date": "2026-11-06"},
            "raw_selected_facts": facts,
            "limitations": ["Context selection does not certify all post-filing corporate actions or price/EPS share comparability.",
                            "No institutional consensus or six-year realized outcome was supplied."],
        })
    return companies


def main():
    companies = recover(json.loads(RAW.read_text(encoding="utf-8")))
    payload = {"as_of": "2012-12-31", "scope": "Nine reviewed context selections; research inputs, not fully audited snapshots",
               "raw_path": str(RAW.relative_to(ROOT)), "raw_sha256": hashlib.sha256(RAW.read_bytes()).hexdigest(),
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "companies": companies}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Recovered {len(companies)} explicitly selected stock contexts into {OUT}")


if __name__ == "__main__":
    main()
