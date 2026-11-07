"""Freeze and deliver a new consensus-based ranking with the strict risk policy.

This requested ranking does not claim to finish the separate sell-review queue.
No discretionary news scenarios are fabricated to clear pending candidates.
"""
from __future__ import annotations
from argparse import ArgumentParser, Namespace
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.cli import screen, adjust
from stocking.model import build_baseline, evaluate, blocked_prediction
from stocking.risk import DEFAULT_RISK_POLICY
from rescreen_current_risk import read, write, sha, complete_archive, compact

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
# Frozen batch reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"
RUN = STUDY / "consensus_rerank_v5"
INPUT = RUN / "input"
PRIOR = STUDY / "latest_available_v4"


def verify_prior():
    manifest = read(PRIOR / "final/delivery_manifest.json")
    for row in manifest["files"]:
        if sha(PRIOR / row["path"]) != row["sha256"]:
            raise ValueError("Frozen prior delivery changed: " + row["path"])
    if sha(PRIOR / "baseline/baseline.json") != manifest["baseline_sha256"]:
        raise ValueError("Prior baseline changed")
    return manifest


def source_paths():
    files = [INPUT / name for name in ("admission_audit.json", "policy.json", "summary.json", "source_manifest.json", "ttm_update_audit.json")]
    for row in read(INPUT / "source_manifest.json")["files"]:
        path = Path(row["path"])
        if sha(path) != row["sha256"]:
            raise ValueError("New input evidence changed: " + str(path))
        files.append(path)
    files.extend(sorted((RUN / "reconciliation").glob("*.json")))
    files.append(RUN / "top_candidates_fact_review.json")
    files.extend(p for p in sorted((RUN / "evidence").rglob("*")) if p.is_file() and "__pycache__" not in p.parts)
    files.extend([Path(__file__).resolve(), ROOT / "scripts/refresh_rerank_ttm.py"])
    return list(dict.fromkeys(files))


def make_baseline():
    verify_prior()
    if (RUN / "baseline/baseline.json").exists():
        raise ValueError("Baseline exists; use finalize, never overwrite a frozen baseline")
    evidence = source_paths()
    write(RUN / "risk_policy.json", DEFAULT_RISK_POLICY)
    policy = read(INPUT / "policy.json")
    write(RUN / "run_context.json", {"requested_task": "现在用这些新数据，重新完成这个排名",
        "information_cutoff": policy["information_cutoff"], "availability_policy": policy["availability_policy"],
        "prior_baseline_sha256": sha(PRIOR / "baseline/baseline.json"),
        "input_sha256": sha(INPUT / "snapshots.json"), "input_policy_sha256": sha(INPUT / "policy.json"),
        "scope": "Rank positive future EPS with P0/E6 below 6 using newly observed consensus and prices, after strict loss risk. Preserve the separate sell review queue as pending; no trade or news scenario decision is implied.",
        "risk_policy_changed": False, "discretionary_news_adjustments_requested": False,
        "old_inputs_and_logs_modified": False, "uses_test_data": False})
    screen(Namespace(input=str(INPUT / "snapshots.json"), universe=str(STUDY / "input/memberships.csv"),
        config=str(LEGACY_CONFIG_PATH), risk_policy=str(RUN / "risk_policy.json"), exclude_loss_probability=None,
        output=str(RUN / "baseline"), raw_input=[str(p) for p in evidence] + [str(RUN / "run_context.json")],
        mode="research", archive_root=str(RUN / "run_archive"), retention_days=7))
    baseline = read(RUN / "baseline/baseline.json")
    complete_archive(baseline["run_archive"])
    queue = read(RUN / "baseline/agent_queue.json")
    print(json.dumps({"baseline_sha256": queue["baseline_sha256"],
        "candidate_types": dict(Counter(r["prediction"]["candidate"] for r in queue["candidates"]))}))


def finalize():
    if (RUN / "final/delivery_manifest.json").exists():
        raise ValueError("Delivery already frozen")
    prior = verify_prior()
    source_paths()
    baseline_path = RUN / "baseline/baseline.json"
    baseline = read(baseline_path)
    context = read(RUN / "run_context.json")
    if baseline["input_sha256"] != sha(INPUT / "snapshots.json") or context["input_sha256"] != baseline["input_sha256"]:
        raise ValueError("Frozen new input changed")
    if baseline["export_policy"]["risk_policy"] != DEFAULT_RISK_POLICY:
        raise ValueError("Strict risk policy changed")
    complete_archive(baseline["run_archive"])
    queue = read(RUN / "baseline/agent_queue.json")
    if any(r["prediction"]["candidate"] == "buy_review" for r in queue["candidates"]):
        raise ValueError("A buy-review candidate needs actual event review before completing the requested low-PE ranking")
    events_path = RUN / "events.json"
    if not events_path.exists():
        write(events_path, {"baseline_sha256": queue["baseline_sha256"], "events_by_symbol": {},
            "reason": "Requested consensus-data reranking. No buy-review candidates. Sell-review candidates remain explicitly pending outside this low-PE ranking deliverable; no discretionary events applied or falsely marked reviewed."})
    if not (RUN / "final/adjusted.json").exists():
        adjust(Namespace(baseline=str(baseline_path), events=str(events_path), output=str(RUN / "final"),
            exclude_loss_probability=None, raw_input=[], mode="research", archive_root=str(RUN / "run_archive"), retention_days=7))
    final = read(RUN / "final/all_stocks.json")
    excluded = read(RUN / "final/excluded_stocks.json")["excluded_stocks"]
    rows = final["stocks"] + excluded
    assert len(rows) == len({r["symbol"] for r in rows}) == 503
    adjusted = read(RUN / "final/adjusted.json")
    complete_archive(adjusted["run_archive"])
    old_rows = read(PRIOR / "final/all_stocks.json")["stocks"] + read(PRIOR / "final/excluded_stocks.json")["excluded_stocks"]
    old = {r["symbol"]: r for r in old_rows}
    audit = {r["symbol"]: r for r in read(INPUT / "admission_audit.json")["securities"]}
    with_consensus = sorted([r for r in final["stocks"] if r["export_status"] == "available"
        and (r.get("input") or {}).get("consensus_rerank_group") == "consensus_research_join"],
        key=lambda r: r["prediction"]["p_below_6"], reverse=True)
    history_only = [r for r in final["stocks"] if r["export_status"] == "available" and not r["input"].get("consensus")]
    old_rank = {r["symbol"]: r["rank"] for r in read(PRIOR / "final/ranking.json")}
    ranked = []
    for index, row in enumerate(with_consensus, 1):
        x, p = row["input"], row["prediction"]
        ranked.append({"rank": index, "symbol": row["symbol"], "company": x.get("company_name"),
            "price": x["price"], "price_date": x["price_date"], "eps0": x["eps0"], "eps_period_end": x["eps_period_end"],
            "p_below_6": p["p_below_6"], "p_loss": p["p_loss"], "p_above_6_5_positive": p["p_above_6_5_positive"],
            "annual_history_count": len(x["history"]), "growth_count": row["model"]["history_growth_observations"],
            "consensus_count": len(x["consensus"]), "consensus": [{k: r.get(k) for k in ("target_date", "eps_mean", "analysts", "eps_std")} for r in x["consensus"]],
            "prior_history_only_rank": old_rank.get(row["symbol"]), "prior_p_below_6": old[row["symbol"]]["prediction"].get("p_below_6"),
            "candidate": p["candidate"], "full_economic_basis_verified": False})
    write(RUN / "final/ranking.json", ranked)
    with (RUN / "final/ranking.csv").open("w", encoding="utf-8-sig", newline="") as f:
        columns = [k for k in ranked[0] if k != "consensus"] if ranked else ["rank", "symbol"]
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ranked)
    write(RUN / "final/history_only_separate.json", history_only)
    attribution = []
    now = datetime.now(timezone.utc).isoformat()
    log_path = RUN / "ai_adjustment_log.jsonl"
    if log_path.exists():
        raise ValueError("Adjustment log already exists; do not overwrite")
    input_sha256 = sha(INPUT / "snapshots.json")
    adjusted_sha256 = sha(RUN / "final/adjusted.json")
    with log_path.open("x", encoding="utf-8") as log:
        for row in rows:
            symbol = row["symbol"]
            comparison = None
            if row.get("input"):
                snapshot = deepcopy(row["input"])
                snapshot["consensus"] = []
                model = build_baseline(snapshot, read(LEGACY_CONFIG_PATH), risk_policy=DEFAULT_RISK_POLICY)
                comparison = evaluate(model) if model["status"] == "ready" else blocked_prediction(model)
                attribution.append({"symbol": symbol, "frozen_prior_prediction": old[symbol].get("prediction"),
                    "refreshed_price_and_actuals_without_consensus": comparison, "new_consensus_prediction": row["prediction"],
                    "note": "Same new date/price/actuals/risk policy, with consensus removed only for attribution; not a second investment ranking."})
            record = {"run_id": "consensus-rerank-v5-" + queue["baseline_sha256"][:16], "event_id": "new-consensus-and-price-" + symbol,
                "recorded_at": now, "as_of": "2026-11-07", "information_cutoff": context["information_cutoff"], "symbol": symbol,
                "type": "user_requested_new_observation_baseline", "status": "new_data_baseline_completed",
                "baseline_sha256": prior["baseline_sha256"], "resulting_baseline_sha256": queue["baseline_sha256"],
                "inputs": [{"path": str(INPUT / "snapshots.json"), "sha256": input_sha256}],
                "outputs": [{"path": str(RUN / "final/adjusted.json"), "sha256": adjusted_sha256}],
                "before": compact(old[symbol]), "after": compact(row), "new_price_without_consensus_prediction": comparison,
                "before_price": (old[symbol].get("input") or {}).get("price"), "after_price": (row.get("input") or {}).get("price"),
                "before_consensus": (old[symbol].get("input") or {}).get("consensus", []),
                "after_consensus": (row.get("input") or {}).get("consensus", []),
                "admission_audit": audit.get(symbol), "availability_policy": context["availability_policy"],
                "risk_policy_changed": False, "news_scenario_adjustment": False,
                "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1, "loss_probability_delta": 0,
                "parameter_note": "No discretionary event deltas; model moments are recomputed from new consensus. Zero scenario delta does not mean unchanged model probability.",
                "confidence": None, "already_in_consensus": None,
                "event_review": row.get("event_review"), "reason": "User requested immediate new-data reranking at an actually observed cutoff; original next-day stage remains unchanged. Independent price/consensus attribution and strict risk gate retained."}
            log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    write(RUN / "final/attribution.json", attribution)
    summary = {"as_of": "2026-11-07", "information_cutoff": context["information_cutoff"], "target_date": "2032-11-07",
        "input_admission": read(INPUT / "summary.json"), "exports": final["summary"], "consensus_ranked_count": len(ranked),
        "history_only_separate_count": len(history_only), "buy_review_count": 0, "sell_review_pending_count": len(queue["candidates"]),
        "discretionary_event_adjustments": 0, "old_baseline_sha256": prior["baseline_sha256"], "new_baseline_sha256": queue["baseline_sha256"],
        "ranking_event": "EPS6 > 0 and P0/EPS6 < 6, equivalently EPS6 > P0/6", "uses_test_data": False,
        "probabilities_calibrated": False, "full_universe_economic_certification": False}
    write(RUN / "final/study_summary.json", summary)
    table = ["| 排名 | 股票 | P(EPS₆>P₀/6) | 终点非正盈利先验 | 旧历史模型排名 |", "| --- | --- | ---: | ---: | ---: |"]
    table += [f"| {r['rank']} | {r['symbol']} | {r['p_below_6']:.2%} | {r['p_loss']:.2%} | {r['prior_history_only_rank']} |" for r in ranked[:15]]
    counts = final["summary"]
    lines = ["# 新机构共识的六年低PE概率排名", "",
        f"决策标签2026-11-07，精确数据截止 {context['information_cutoff']}；使用11月6日Yahoo常规时段末报价。目标2032-11-07，排名事件为六年后EPS为正且当前固定股价除以该EPS小于6，不预测六年后的交易市盈率。", "",
        "用户明确要求立即用新数据重排，因此本轮采用实际取得时点的新观察政策。原暂存11月8日标记逐条保留，原11月7日冻结基准不改；本结果不代表11月6日收盘当时已经知道新预测，也不是盘中回测。", "",
        f"503只名单中，{counts['available_stock_count']}只可计算（其中{len(ranked)}只具备本轮接入的机构共识，另{len(history_only)}只仅历史模型、单独留档），{counts['excluded_stock_count']}只风险排除，{counts['data_unavailable_stock_count']}只数据不足。仅机构共识组参加下列新排名。", "",
        "严格风险规则保持不变：最近3个连续完整年度EPS均为正，终点非正盈利先验低于10%，当前及有效机构预测EPS必须为正；被排除股票不计算估值概率。", "", *table, "",
        "没有股票达到70%的买入复核门槛。最高的相对名次不等于高置信度，概率尚未校准。多数机构锚点仅覆盖本财年和下一财年，之后由既定模型向长期增长假设回归，并非机构直接预测了六年后的盈利。", "",
        "## 接入与限制", "",
        "采用显式GAAP模块内部EPS均值及对应人数；未知分歧标准差留空，不采用可能混用默认口径的顶层growth。固定财政日期依据连续3个SEC年度核对；52/53周未完成映射者不接入，已结束年度不当未来锚点。股份及经济范围仍是支持性研究接入，未宣称逐股完整认证。", "",
        "FTV、GE、PNR的新比较值为持续经营EPS，现有历史为总EPS，暂不拼接。9只已出现更新实际季度却不能形成合格最新TTM的证券留数据不足：AEE、AKAM、APD、ATO、CPT、DVN、EMR、MRNA、TECH。完整原因在输入审计；没有用旧季度盈利掩盖缺口。", "",
        "前列公司的一次性项目及口径核对见 [事实复核](../top_candidates_fact_review.json)。GM、FITB、HBAN需区分GAAP和调整后EPS；VICI为REIT，GAAP信用损失计提与AFFO口径差异显著，排名未将AFFO替代EPS。事实核对不等于新增模型情景。", "",
        f"本轮完成的是新数据基准排序，主观新闻增量为0。买入复核候选为0；另有{len(queue['candidates'])}只卖出复核候选保留pending，未冒称审阅完成，不据此作卖出建议。原始基准与adjust输出数值相同；全部事件状态如实保留。", "",
        "## 文件", "", "- [排名表](ranking.csv)与[逐股机构锚点](ranking.json)",
        "- [全部非排除证券](all_stocks.json)、[风险排除名单](excluded_stocks.json)、[仅历史模型单列](history_only_separate.json)",
        "- [503条逐股接入及概率变化日志](../ai_adjustment_log.jsonl)、[同价无共识归因对照](attribution.json)",
        "- [共识接入和缺失原因](../input/admission_audit.json)、[数据时点政策](../input/policy.json)、[统计汇总](study_summary.json)", "",
        f"新基准SHA256：`{queue['baseline_sha256']}`。旧基准及所有旧日志均保留；没有使用data/测试资料作为研究输入。", ""]
    (RUN / "final/report.md").write_text("\n".join(lines), encoding="utf-8")
    files = [RUN / "final" / name for name in ("report.md", "ranking.json", "ranking.csv", "history_only_separate.json", "attribution.json",
        "all_stocks.json", "all_stocks.csv", "excluded_stocks.json", "excluded_stocks.csv", "study_summary.json", "adjusted.json")]
    files += [RUN / "ai_adjustment_log.jsonl", RUN / "run_context.json", RUN / "risk_policy.json", events_path,
              RUN / "top_candidates_fact_review.json"]
    write(RUN / "final/delivery_manifest.json", {"baseline_sha256": queue["baseline_sha256"],
        "files": [{"path": str(p.relative_to(RUN)), "sha256": sha(p)} for p in files]})
    print(json.dumps({"summary": counts, "consensus_ranked": len(ranked), "top5": ranked[:5],
        "sell_review_pending": len(queue["candidates"]), "report": str(RUN / "final/report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("screen", "finalize"))
    make_baseline() if parser.parse_args().phase == "screen" else finalize()
