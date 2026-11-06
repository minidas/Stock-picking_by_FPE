"""Build auditable annual EPS extensions; never synthesize absent years.

The original 2012 snapshots anchor the accounting/share basis. Older filings
join only through consistent overlapping years under an explicitly unverified
research assumption. Reviewed source blocks can carry evidenced split bridges.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_2012_universe import AS_OF, assemble
from stocking.data import load_json, load_snapshots
from stocking.history import extend_annual_history
from stocking.model import _history

BASIS = "GAAP_diluted_as_reported_at_cutoff"
KNOWN_BASES = {BASIS, "annual_diluted_EPS_coherent_filing_research_only",
               "GAAP_diluted_annual_as_reported_at_cutoff"}


def history_digest(history):
    return hashlib.sha256(json.dumps(history, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def file_evidence(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def context_of(company):
    selected = (company or {}).get("context_selection", {})
    return {key: selected.get(key) or "" for key in ("segments", "coreg")}


def approve_legacy_identity(member, filing):
    """Reuse the exact reviewed accession/name evidence, never fuzzy tickers."""
    if member.get("2012_filing_identity_confirmation", {}).get("identity_join_allowed") is not True:
        return False
    checked = member.get("historical_filing_name_confirmation", {})
    return checked.get("status") == "name_consistent_for_legacy_filing" and any(
        item.get("accession") == filing["accession"] and item.get("name") == filing.get("name")
        for item in checked.get("evidence", [])
    )


def prepare_candidate(raw, base_row):
    """Declare, rather than certify, the existing as-reported assumption."""
    filing = deepcopy(raw)
    member, snapshot = base_row["membership_evidence"], base_row["input"]
    filing["identity_join_allowed"] = approve_legacy_identity(member, filing)
    filing["identity_evidence"] = deepcopy(member.get("historical_filing_name_confirmation", {}))
    filing["native_share_basis"] = filing.get("share_basis")
    filing["research_share_basis_assumption"] = (
        "Comparable only if all overlapping annual EPS agree; this does not verify corporate actions."
    )
    filing["share_basis"] = snapshot["share_basis"]
    filing["share_basis_verified"] = False
    # The original annual adjustment was accession-specific. An older filing
    # gets a separate, explicit bridge only when every value predates the event.
    # Earlier unreviewed actions still have to pass the overlap test.
    adjustment = base_row.get("adjustment")
    if adjustment and filing["available_at"] < adjustment["effective_date"] and all(
        row["available_at"] < adjustment["effective_date"] for row in filing["history"]
    ):
        factor = adjustment["eps_multiplier"]
        for row in filing["history"]:
            row["eps_as_reported_before_2012_split_bridge"] = row["eps"]
            row["eps"] *= factor
        filing["already_mapped"] = {
            "target_share_basis": snapshot["share_basis"], "eps_multiplier": factor,
            "source_url": adjustment["source_url"], "available_at": adjustment["effective_date"],
            "availability_date_kind": "corporate_action_effective_date_research_assumption_not_announcement_timestamp",
            "rationale": "Apply the evidenced 2012 action to this earlier filing, then require consistent overlaps.",
            "original_adjustment": deepcopy(adjustment),
        }
    return filing


def build_panels(base_rows, filings, supplements, max_years=11):
    by_cik, reviewed_by_cik = defaultdict(list), defaultdict(list)
    for filing in filings:
        by_cik[filing["cik"]].append(filing)
    for block in supplements:
        reviewed_by_cik[block["cik"]].append(block)
    panels = []
    for row in base_rows:
        snapshot = deepcopy(row["input"])
        original_history = deepcopy(snapshot.get("history", []))
        if snapshot.get("eps_basis") not in KNOWN_BASES:
            raise ValueError(f"Unknown annual accounting basis for {row['symbol']}")
        snapshot.update(cik=row["cik"], eps_basis=BASIS,
                        context=context_of(row["annual_eps_evidence_before_adjustment"]))
        # Panel hash binds to the unmodified source history, including its
        # accession-specific split adjustment. It does not bind to the TTM EPS.
        candidates = {}
        for raw in by_cik[row["cik"]]:
            candidate = prepare_candidate(raw, row)
            accession = candidate["accession"]
            if accession in candidates and candidates[accession] != candidate:
                raise ValueError(f"Conflicting duplicate candidate accession: {accession}")
            candidates[accession] = candidate
        superseded = []
        for block in reviewed_by_cik[row["cik"]]:
            if block["symbol"] != row["symbol"]:
                raise ValueError("Reviewed source symbol/CIK mismatch")
            candidate = deepcopy(block)
            if candidate["accession"] in candidates:
                superseded.append(candidates[candidate["accession"]])
            candidates[candidate["accession"]] = candidate
        identity_allowed = row["membership_evidence"].get(
            "2012_filing_identity_confirmation", {}).get("identity_join_allowed") is True
        if not identity_allowed:
            for candidate in candidates.values():
                candidate["identity_join_allowed"] = False
        history, audit = extend_annual_history(snapshot, list(candidates.values()), max_years=max_years)
        old_history, old_growth = _history(snapshot, date.fromisoformat(AS_OF), set())
        audit.update(original_history_count=len(old_history), original_growth_count=len(old_growth),
                     superseded_bulk_candidates=superseded,
                     identity_join_allowed=identity_allowed,
                     additional_observations=len(audit.get("added_periods", [])))
        if not candidates:
            audit["coverage_reason"] = "no_older_filing_available_in_local_candidate_bank"
        elif audit["additional_observations"] == 0:
            audit["coverage_reason"] = "no_compatible_additional_years_from_available_filings"
        else:
            audit["coverage_reason"] = "extended_with_audited_unverified_joins"
        panels.append({"symbol": row["symbol"], "cik": row["cik"], "as_of": AS_OF,
                       "base_history_sha256": history_digest(original_history),
                       "target_share_basis": snapshot["share_basis"], "eps_basis": BASIS,
                       "context": snapshot["context"], "share_basis_verified": False,
                       "history": history, "audit": audit})
    return panels


def summarize_panels(panels):
    return {
        "roster_count": len(panels),
        "extended_company_count": sum(p["audit"]["additional_observations"] > 0 for p in panels),
        "added_annual_observations": sum(p["audit"]["additional_observations"] for p in panels),
        "before_annual_count_distribution": dict(sorted(Counter(p["audit"]["original_history_count"] for p in panels).items())),
        "after_annual_count_distribution": dict(sorted(Counter(len(p["history"]) for p in panels).items())),
        "after_growth_count_distribution": dict(sorted(Counter(p["audit"]["history_growth_observations"] for p in panels).items())),
        "pending_filing_reason_counts": dict(Counter(reason for p in panels for filing in p["audit"]["pending_filings"] for reason in filing["reasons"])),
        "full_eleven_year_count": sum(len(p["history"]) == 11 for p in panels),
        "fewer_than_three_growth_observations_count": sum(p["audit"]["history_growth_observations"] < 3 for p in panels),
        "before_fewer_than_three_growth_observations_count": sum(p["audit"]["original_growth_count"] < 3 for p in panels),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, default in (("members", "universe_2012_members.json"),
                          ("prices", "research_2012_prices.json"),
                          ("companies", "research_2012_sec_eps.json"),
                          ("manual", "research_2012_snapshots.json"),
                          ("adjustments", "research_2012_share_adjustments.json"),
                          ("supplemental-companies", "research_2012_supplemental_eps.json"),
                          ("reviewed-supplements", "research_2012_history_supplements.json")):
        parser.add_argument("--" + flag, type=Path, default=ROOT / "data" / default)
    parser.add_argument("--candidates", type=Path, default=ROOT / "data/raw/sec_history/candidate_filings.json")
    parser.add_argument("--max-years", type=int, default=11)
    parser.add_argument("--output", type=Path, default=ROOT / "data/research_2012_history_panels.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/annual_history_expansion.json")
    args = parser.parse_args()
    candidates, supplements = load_json(args.candidates), load_json(args.reviewed_supplements)
    if candidates.get("as_of") != AS_OF or supplements.get("as_of") != AS_OF:
        parser.error("Historical sources must use as_of=2012-12-31")
    base_rows = assemble(load_json(args.members)["members"], load_json(args.prices)["prices"],
                         load_json(args.companies)["companies"], load_snapshots(args.manual),
                         adjustments=load_json(args.adjustments)["adjustments"],
                         supplemental=load_json(args.supplemental_companies)["companies"],
                         eps_anchor="annual")
    panels = build_panels(base_rows, candidates["filings"], supplements["source_blocks"], args.max_years)
    summary = summarize_panels(panels)
    sources = {key: file_evidence(getattr(args, key)) for key in (
        "members", "prices", "companies", "manual", "adjustments", "supplemental_companies", "candidates", "reviewed_supplements")}
    metadata = {"schema_version": 1, "as_of": AS_OF, "max_years": args.max_years,
                "scope": "unverified_as_reported_annual_history_extension_research",
                "summary": summary, "source_files": sources,
                "code_files": [file_evidence(Path(__file__)), file_evidence(ROOT / "src/stocking/history.py")],
                "limitations": [
                    "A consistent overlap is a research assumption, not full share-basis or accounting verification.",
                    "Missing/conflicting years remain pending; zero/negative EPS are retained as observations.",
                    "Only existing archives and explicitly reviewed source blocks are used; coverage is not eleven years for every issuer.",
                    "Current TTM, prices, consensus, cash flow and financing inputs are not changed.",
                    "No post-cutoff EPS outcomes, return labels or calibration enter these panels."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({**metadata, "panels": panels}, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report = {**metadata, "panel_file": file_evidence(args.output), "companies": [
        {"symbol": p["symbol"], "annual_count_before": p["audit"]["original_history_count"],
         "annual_count_after": len(p["history"]), "growth_count": p["audit"]["history_growth_observations"],
         "added_periods": p["audit"].get("added_periods", []), "pending_filings": p["audit"]["pending_filings"],
         "missing_years": p["audit"].get("missing_calendar_years_in_window", [])} for p in panels]}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# 2012 年度 EPS 历史补样本", "",
             f"500只名单中，本轮为 {summary['extended_company_count']} 家增加 {summary['added_annual_observations']} 个年度观察；"
             f"补满11年的有 {summary['full_eleven_year_count']} 家。未以零、插值或后来披露的数据填空。", "",
             "| 年度观察数 | 补齐前公司数 | 补齐后公司数 |", "|---:|---:|---:|"]
    for count in sorted(set(summary["before_annual_count_distribution"]) | set(summary["after_annual_count_distribution"])):
        lines.append(f"| {count} | {summary['before_annual_count_distribution'].get(count, 0)} | {summary['after_annual_count_distribution'].get(count, 0)} |")
    lines += ["", "跨申报接入要求同主体、同会计/股类上下文，且重叠年份一致。拆股须有单独证据；CLF旧报告仅在明确拆股链下允许记录的微小量化差异。"
              "所有新增历史仍保留 share_basis_verified=false，不等于完整审计。", "",
              "缺口及冲突见同名JSON。TTM基准与年度历史分开；本轮不补造其他股票的TTM，也不修改模型参数。", ""]
    args.report.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    main()
