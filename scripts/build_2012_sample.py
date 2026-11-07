"""Join the full historical roster to downloaded evidence, keeping every gap.

Data acquisition is not model validation. Newly downloaded filing EPS and fund
valuations are exported, but cannot silently claim verified split/share bases.
Only separately audited snapshots receive model probabilities.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import load_json, load_snapshots
from stocking.export import write_exports
from stocking.model import build_baseline, evaluate
from stocking.retention import archive_run, finalize_run

AS_OF = "2012-12-31"
OUT = ROOT / "outputs/stocks_2012_full"
# Frozen 2012 reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG = load_json(ROOT / "examples/thresholds_legacy.json")


def unique_by(rows, key):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not identity:
            continue
        if identity in result:
            raise ValueError(f"Ambiguous duplicate {key}: {identity}")
        result[identity] = row
    return result


def fresh_anchor(company):
    rows = [r for r in company.get("history", [])
            if r["period_end"] <= r["available_at"] <= AS_OF]
    if not rows:
        return None
    anchor = max(rows, key=lambda r: r["period_end"])
    if company.get("period_end") and anchor["period_end"] != company["period_end"]:
        # A conflicting/missing current-period fact must not silently fall back
        # to a prior comparative year from the same annual report.
        return None
    if (date.fromisoformat(AS_OF) - date.fromisoformat(anchor["period_end"])).days > 550:
        return None
    return anchor


def financial_status(member, company, complete=False, raw_facts=None):
    identity = member.get("2012_filing_identity_confirmation", {})
    if identity.get("identity_join_allowed") is False:
        return "no_confirmed_2012_filing_identity_in_downloaded_batch"
    if company:
        if fresh_anchor(company) is None:
            return "current_annual_anchor_missing_conflicting_or_stale"
        return "annual_eps_downloaded_share_basis_review_pending"
    if not complete:
        return "eps_download_incomplete"
    eps = [r for r in (raw_facts or []) if r.get("tag") in {"EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"}]
    if eps:
        if all(r.get("segments") or r.get("coreg") for r in eps):
            return "eps_requires_share_class_or_coregistrant_selection"
        return "eps_requires_nonannual_filing_or_context_review"
    return "no_supported_unambiguous_annual_eps_in_completed_query"


def assemble(members, prices, companies, manual, legacy, retrieval_complete=False, raw_facts=None):
    price_by_symbol = unique_by(prices, "symbol")
    company_by_cik = unique_by(companies, "cik")
    audited = unique_by(manual, "symbol")
    legacy_by_cik = defaultdict(list)
    raw_by_cik = defaultdict(list)
    for fact in raw_facts or []:
        raw_by_cik[str(fact["cik"]).zfill(10)].append(fact)
    for observation in legacy:
        if observation["period_end"] <= observation["available_at"] <= AS_OF:
            legacy_by_cik[observation["cik"]].append(observation)
    results = []
    for member in members:
        symbol, cik = member["symbol"], member["cik"]
        price, company = price_by_symbol.get(symbol), company_by_cik.get(cik, {})
        identity = member.get("2012_filing_identity_confirmation", {})
        if identity.get("identity_join_allowed") is False:
            company = {}
        anchor = fresh_anchor(company)
        blockers = []
        if symbol in audited:
            snapshot = audited[symbol]
            model = build_baseline(snapshot, LEGACY_CONFIG)
            result = {"symbol": symbol, "as_of": AS_OF, "input": snapshot,
                      "model": model, "prediction": evaluate(model), "event_review": "not_selected"}
        else:
            if not price:
                blockers.append("price_not_matched")
            if not anchor:
                blockers.append("fresh_annual_eps_not_acquired")
            if company:
                blockers.append("historical_issuer_and_price_eps_share_basis_require_review")
            blockers.append("historical_consensus_missing")
            snapshot = {
                "symbol": symbol, "as_of": AS_OF, "currency": "USD",
                "price": price["price"] if price else None,
                "price_date": price["date"] if price else None,
                "price_kind": price["price_kind"] if price else None,
                "unreviewed_annual_eps": anchor["eps"] if anchor else None,
                "eps_period_end": anchor["period_end"] if anchor else None,
                "eps_available_at": anchor["available_at"] if anchor else None,
                "eps_basis": "annual_diluted_EPS_from_historical_filing_pending_identity_review",
                "share_basis": "unreconciled_filing_EPS_vs_nominal_fund_valuation",
                "history": company.get("history", []), "consensus": [],
                "source": {"membership": "data/universe_2012_members.json",
                           "price": price["source"] if price else None,
                           "financial_statement": company.get("source_url"),
                           "financial_data": "data/research_2012_sec_eps.json" if company else None,
                           "limitations": blockers},
            }
            # The numerical observations are retained without treating a guessed
            # issuer mapping or unreconciled share basis as a valid model input.
            # Unreviewed EPS never occupies eps0: this partial input must fail
            # normal snapshot validation instead of entering a model by accident.
            result = {"symbol": symbol, "as_of": AS_OF, "input": snapshot,
                      "prediction": {"candidate": "abstain"},
                      "error": ";".join(blockers), "event_review": "not_eligible"}
        result.update({
            "company_name": member["name"], "cik": cik, "membership_evidence": member,
            "price_evidence": price, "annual_eps_evidence": company or None,
            "raw_eps_and_split_observations": raw_by_cik[cik],
            "older_filing_eps_observations": legacy_by_cik[cik],
            "download_status": {
                "price": "downloaded_fund_valuation" if price else "missing",
                "annual_eps": "downloaded_mirrored_filing" if company else "missing",
                "annual_eps_reason": financial_status(member, company, retrieval_complete, raw_by_cik[cik]),
                "fresh_annual_eps": bool(anchor),
                "older_eps_count": len(legacy_by_cik[cik]),
                "historical_consensus": "missing",
                "model_input": "audited_manual_snapshot" if symbol in audited else "not_yet_verified",
                "blockers": blockers,
            },
        })
        results.append(result)
    return results


def safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        value = "'" + value
    return value


def write_status(path, results):
    fields = ["symbol", "company_name", "cik", "price_status", "fund_valuation_usd",
              "price_kind", "financial_status", "financial_status_reason", "filing_issuer_name", "filing_date",
              "annual_eps_latest", "annual_eps_period", "annual_eps_count", "raw_eps_split_fact_count", "older_eps_count",
              "model_input_status", "historical_consensus_status", "blockers"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in results:
            status = row["download_status"]
            financial, price = row["annual_eps_evidence"] or {}, row["price_evidence"] or {}
            anchor = fresh_anchor(financial)
            writer.writerow({k: safe_cell(v) for k, v in {
                "symbol": row["symbol"], "company_name": row["company_name"], "cik": row["cik"],
                "price_status": status["price"], "fund_valuation_usd": price.get("price"),
                "price_kind": price.get("price_kind"), "financial_status": status["annual_eps"],
                "financial_status_reason": status["annual_eps_reason"],
                "filing_issuer_name": financial.get("name"), "filing_date": financial.get("filing_date"),
                "annual_eps_latest": anchor["eps"] if anchor else None,
                "annual_eps_period": anchor["period_end"] if anchor else None,
                "annual_eps_count": len(financial.get("history", [])),
                "raw_eps_split_fact_count": len(row["raw_eps_and_split_observations"]),
                "older_eps_count": status["older_eps_count"], "model_input_status": status["model_input"],
                "historical_consensus_status": status["historical_consensus"], "blockers": status["blockers"],
            }.items()})


def main():
    input_paths = {
        "members": ROOT / "data/universe_2012_members.json",
        "prices": ROOT / "data/research_2012_prices.json",
        "manual": ROOT / "data/research_2012_snapshots.json",
        "legacy": ROOT / "data/research_2012_legacy_eps.json",
    }
    financial_path = ROOT / "data/research_2012_sec_eps.json"
    financial = load_json(financial_path) if financial_path.exists() else {"companies": []}
    if financial_path.exists():
        input_paths["annual_eps"] = financial_path
    raw_facts_path = ROOT / "data/raw/sec_2012_bulk/annual_eps_and_splits_2012.json"
    raw_facts = load_json(raw_facts_path) if raw_facts_path.exists() else []
    members = load_json(input_paths["members"])
    price_payload = load_json(input_paths["prices"])
    legacy = load_json(input_paths["legacy"])
    rows = assemble(members["members"], price_payload["prices"], financial["companies"],
                    load_snapshots(input_paths["manual"]), legacy["records"], financial.get("retrieval_complete", False), raw_facts)
    unique_by(rows, "symbol")
    OUT.mkdir(parents=True, exist_ok=True)
    coverage = {"as_of": AS_OF, "expected_count": len(rows), "supplied_count": len(rows),
                "covered_count": len(rows), "missing_symbols": [], "extra_symbols": [],
                "complete": True, "scope": "All roster rows are present; complete does not mean all numerical inputs are verified",
                "membership_reconstruction_only": True,
                "official_membership_certified": members.get("full_official_membership_certified", False)}
    export_summary = write_exports(OUT, rows, coverage)
    write_status(OUT / "download_status.csv", rows)
    write_status(OUT / "remaining_samples.csv", [r for r in rows if not r["annual_eps_evidence"]])
    summary = {
        "as_of": AS_OF, "created_at": datetime.now(timezone.utc).isoformat(),
        "roster_count": len(rows),
        "matched_price_count": sum(r["price_evidence"] is not None for r in rows),
        "downloaded_price_rows": len(price_payload["prices"]),
        "eps_retrieval_complete": financial.get("retrieval_complete", False),
        "eps_limited_query": financial.get("limited_query", True),
        "eps_source_summary": financial.get("summary", {}),
        "raw_eps_facts_sha256": hashlib.sha256(raw_facts_path.read_bytes()).hexdigest() if raw_facts_path.exists() else None,
        "matched_annual_eps_company_count": sum(r["annual_eps_evidence"] is not None for r in rows),
        "fresh_annual_eps_company_count": sum(r["download_status"]["fresh_annual_eps"] for r in rows),
        "raw_eps_observation_company_count": sum(any(f.get("tag") in {"EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"}
                                                      for f in r["raw_eps_and_split_observations"]) for r in rows),
        "older_eps_company_count": sum(bool(r["older_filing_eps_observations"]) for r in rows),
        "audited_model_input_count": sum(r["download_status"]["model_input"] == "audited_manual_snapshot" for r in rows),
        "historical_consensus_company_count": 0,
        "financial_status_counts": dict(Counter(r["download_status"]["annual_eps_reason"] for r in rows)),
        "not_yet_verified_symbols": [r["symbol"] for r in rows if r["download_status"]["model_input"] != "audited_manual_snapshot"],
        "financial_missing_symbols": [r["symbol"] for r in rows if not r["annual_eps_evidence"]],
        "export_summary": export_summary,
        "limitations": ["Fund valuations are not independently verified exchange closing prices.",
                        "Mirrored EPS rows are downloaded evidence, not reconciled model snapshots.",
                        "Historical consensus and six-year outcome validation remain unavailable.",
                        "Unreviewed negative EPS stays in the acquisition table; only audited risk exclusions leave the main model table."],
        "input_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in input_paths.items()},
    }
    (OUT / "coverage_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# 2012年末全名单个股样本采集", "",
              "本报告区分原始数据采集与可用模型输入，不是收益回测或策略有效性证明。", "",
              "| 项目 | 名单内股票数 |", "|---|---:|",
              f"| 历史成员记录 | {summary['roster_count']} |",
              f"| 已匹配年末基金持仓估值 | {summary['matched_price_count']} |",
              f"| 已批量取得2012年披露的年度EPS序列 | {summary['matched_annual_eps_company_count']} |",
              f"| 有EPS原始数值（含尚未选定股类/申报类型的记录） | {summary['raw_eps_observation_company_count']} |",
              f"| 其中年度EPS锚点未超过550天 | {summary['fresh_annual_eps_company_count']} |",
              f"| 另有较早报表EPS原始记录 | {summary['older_eps_company_count']} |",
              f"| 单独核对过的可运行模型输入 | {summary['audited_model_input_count']} |",
              "| 2012年机构一致预期快照 | 0 |", "",
              "逐股状态见 `../outputs/stocks_2012_full/download_status.csv`；完整数据与来源见同目录 `all_stocks.json`。",
              "尚未得到可直接归一化年度序列的股票另列 `remaining_samples.csv`，保留股类、共同申报人及特殊申报类型的原始观察。",
              f"EPS镜像查询是否完整返回：{summary['eps_retrieval_complete']}；数量受查询上限影响的批次不能据缺失行推断公司没有财报。",
              "`all_stocks.csv`保留未通过输入核验的记录与空概率；JSON还包含逐条价格证据、财报与较早历史。已确认高风险/亏损另进排除表；未核对的负EPS保留为待复核，不能按未知风险直接删样本。", "",
              "价格来自[当年IVV官方基金报告](https://announcements.asx.com.au/asxpdf/20130304/pdf/42dfsw4vv9s2vp.pdf)的市值/股数，500条市值合计与原报告一致。这是基金估值口径，和最后成交价可能不同。",
              "名单在两份公开历史成员表基础上修正，并与IVV持仓交叉核对；采用12月31日交易收盘时点，包含FII、排除收盘后新加入的ABBV，尚非S&P授权历史名单认证。", "",
              "SEC官方下载本次遇到HTTP403；2012镜像行按申报日期筛选，记录其来源而不冒充原始官方ZIP。DERA财政期末存在月末归整，发行人身份、股份类别、拆股和会计重述仍需逐项核对。较早2009/2010数据不能冒充2012新盈利，也不能跨版本直接拼接。", "",
              "当前只有原四只已核对快照计算历史回退概率，其余不会因取得数字就自动进入模型。尚未取得2012机构共识、六年实际结果标签及完整公司行动，不能据此给出可靠的买卖结论或优化6/6.5阈值。", "",
              "原始文件及SHA清单在 `data/raw/universe_2012/`、`data/raw/prices_2012/`、`data/raw/sec_2012_bulk/`、`data/raw/sec_legacy_2010/`。研究模式保留原文；合并运行建立独立研究归档。", "",
              "复现：完成各来源下载后运行 `python scripts/extract_legacy_sec_eps.py` 与 `python scripts/build_2012_sample.py`。", ""]
    report_path = ROOT / "reports/stocks_2012_acquisition.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    # Research raw originals stay in data/raw. Copy the exact used source files,
    # avoiding transient HTTP signed URLs and incomplete trial downloads.
    raw_paths = [p for p in (ROOT / "data/raw/sec_legacy_2010").glob("*.zip")]
    for folder in ("universe_2012", "prices_2012", "sec_2012_bulk"):
        manifest_path = ROOT / "data/raw" / folder / "manifest.json"
        if manifest_path.exists():
            raw_paths.append(manifest_path)
    for file in members.get("source_files", []):
        path = ROOT / file["path"]
        if path.exists():
            raw_paths.append(path)
    pdf = ROOT / "data/raw/prices_2012/ishares_holdings_requests.pdf"
    if pdf.exists():
        raw_paths.append(pdf)
    for pattern in ("submissions_2012.json", "annual_eps_and_splits_2012.json", "partial_eps*.json", "*facts*.json"):
        raw_paths.extend((ROOT / "data/raw/sec_2012_bulk").glob(pattern))
    # Freeze complete JSONL lines before archiving an actively growing download
    # journal. Retries can repeat observations; the journal is evidence, not a
    # deduplicated financial panel or a statement of completed universe coverage.
    sec_raw = ROOT / "data/raw/sec_2012_bulk"
    for stream_path in sec_raw.glob("*.stream.jsonl"):
        content = stream_path.read_bytes()
        if not content.endswith(b"\n"):
            content = content[:content.rfind(b"\n") + 1]
        if content:
            digest = hashlib.sha256(content).hexdigest()
            checkpoint_dir = sec_raw / "checkpoints"
            checkpoint_dir.mkdir(exist_ok=True)
            checkpoint = checkpoint_dir / f"{stream_path.stem}_{digest[:16]}.jsonl"
            if not checkpoint.exists():
                checkpoint.write_bytes(content)
            raw_paths.append(checkpoint)
    for pattern in ("*.pdf", "*.html", "manifest.json"):
        raw_paths.extend((ROOT / "data/raw/stocks_2012").glob(pattern))
    run = archive_run(ROOT / "outputs/run_archive", input_paths, mode="research",
                      raw_paths=list(dict.fromkeys(raw_paths)))
    finalize_run(run, artifacts={"coverage": OUT / "coverage_summary.json", "download_status": OUT / "download_status.csv",
                                 "remaining_samples": OUT / "remaining_samples.csv", "report": report_path},
                 metadata={"scope": "2012 full individual-stock acquisition audit", "summary": summary},
                 working_files={name.replace(".", "_"): OUT / name for name in ("all_stocks.json", "all_stocks.csv", "excluded_stocks.json", "excluded_stocks.csv")})
    (OUT / "run_lifecycle.json").write_text(json.dumps({"mode": "research", "run_archive": str(run),
                                                       "raw_originals_retained": True}, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k.endswith("count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
