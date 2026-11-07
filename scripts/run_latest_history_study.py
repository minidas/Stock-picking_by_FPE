"""Screen the latest-available annual history revision with strict loss risk.

Requires separately prepared normalized_latest_available_reviewed inputs. Keeps every
earlier input, baseline, report and audit log intact.
"""
from __future__ import annotations

from argparse import Namespace
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.cli import screen, adjust
from stocking.risk import DEFAULT_RISK_POLICY
from rescreen_current_risk import read, write, sha, complete_archive, compact

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
# Frozen batch reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"
PRIOR = STUDY / "strict_risk_v2"
NORM = STUDY / "normalized_latest_available_reviewed"
REVISION = STUDY / "latest_available_v4"


def log(record):
    path = REVISION / "ai_adjustment_log.jsonl"
    old = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    for r in old:
        if (r["run_id"], r["event_id"]) == (record["run_id"], record["event_id"]):
            without_time = lambda x: {k: v for k, v in x.items() if k != "recorded_at"}
            if without_time(r) != without_time(record):
                raise ValueError("Audit event changed; use a new superseding event ID")
            return
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def history(row):
    return [{k: r.get(k) for k in ("period_start", "period_end", "eps", "available_at", "accession", "financial_filed_date")}
            for r in row.get("input", {}).get("history", [])]


def main():
    delivered = REVISION / "final/delivery_manifest.json"
    if delivered.exists():
        frozen = read(delivered)
        for item in frozen["files"]:
            if sha(REVISION / item["path"]) != item["sha256"]:
                raise ValueError("Frozen latest-history delivery changed: " + item["path"])
        if sha(REVISION / "baseline/baseline.json") != frozen["baseline_sha256"]:
            raise ValueError("Frozen latest-history baseline changed")
        print(json.dumps({"status": "already_completed_frozen_delivery_verified",
                          "report": str(REVISION / "final/report.md")}, ensure_ascii=False))
        return
    if (REVISION / "baseline/baseline.json").exists():
        raise ValueError("Incomplete frozen run exists; inspect its archive and use a new revision directory instead of relabeling the old baseline")
    manifest = read(PRIOR / "final/delivery_manifest.json")
    for r in manifest["files"]:
        if sha(PRIOR / r["path"]) != r["sha256"]:
            raise ValueError("Prior frozen strict-risk delivery changed: " + r["path"])
    prior_baseline = read(PRIOR / "baseline/baseline.json")
    assert sha(PRIOR / "baseline/baseline.json") == manifest["baseline_sha256"]
    complete_archive(prior_baseline["run_archive"])
    norm = read(NORM / "summary.json")
    new_inputs = read(NORM / "snapshots.json")["snapshots"]
    old_inputs = {r["symbol"]: r["input"] for r in prior_baseline["results"]}
    for x in new_inputs:
        if x["symbol"] in old_inputs:
            old = old_inputs[x["symbol"]]
            for field in ("as_of", "price", "price_date", "eps0", "eps_measure", "eps_period_start", "eps_period_end"):
                if old.get(field) != x.get(field):
                    raise ValueError("This revision may change annual history only: " + x["symbol"] + " " + field)
    audits = read(NORM / "normalization_audit.json")["stocks"]
    by_audit = {r["symbol"]: r for r in audits}
    for x in new_inputs:
        if by_audit[x["symbol"]]["annual_history_chain"].get("conflict_policy") != "latest_available":
            raise ValueError("Normalize with the latest_available policy first")
    policy_path = REVISION / "risk_policy.json"
    write(policy_path, DEFAULT_RISK_POLICY)
    evidence_paths = [NORM / "normalization_audit.json", STUDY / "eps_reconciliation/report_reviewed.md",
                      STUDY / "eps_reconciliation/entity_overrides_reviewed.json",
                      ROOT / "src/stocking/history.py", ROOT / "scripts/build_current_snapshots.py"]
    cause_files = {"ALL": STUDY / "eps_reconciliation/ALL/conclusion.json",
                   "FIS": STUDY / "eps_reconciliation/FIS/reconciliation.json",
                   "ECHO": STUDY / "eps_reconciliation/ECHO/reconciliation.json",
                   "MKC": STUDY / "eps_reconciliation/MKC/conclusion.json",
                   "AMCR": STUDY / "eps_reconciliation/split_followup/bridge_audit.json",
                   "PCAR": STUDY / "eps_reconciliation/split_followup/bridge_audit.json"}
    evidence_paths.extend(cause_files.values())
    evidence_paths.extend(STUDY / "eps_reconciliation" / name for name in (
        "MKC/entity_override.json", "split_followup/share_basis_bridges.json",
        "split_followup/source_validation.json", "split_followup/extension_split_scan.json",
        "split_followup/report.md"))
    evidence_paths.append(STUDY / "eps_reconciliation/delivery_manifest_reviewed.json")
    evidence_paths.append(STUDY / "eps_reconciliation/ai_adjustment_log.jsonl")
    for source_manifest in (STUDY / "eps_reconciliation").glob("*/source_manifest.jsonl"):
        evidence_paths.append(source_manifest)
        for line in source_manifest.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("file"):
                raw = source_manifest.parent / record["file"]
                if sha(raw) != record["sha256"]:
                    raise ValueError("Reconciliation source hash changed")
                evidence_paths.append(raw)
    evidence_paths = list(dict.fromkeys(evidence_paths))
    reference = {"prior_baseline_sha256": manifest["baseline_sha256"],
                 "prior_baseline": str(PRIOR / "baseline/baseline.json"),
                 "prior_archive": prior_baseline["run_archive"],
                 "original_raw_evidence_archive": read(STUDY / "baseline/baseline.json")["run_archive"],
                 "input_sha256": sha(NORM / "snapshots.json"),
                 "universe_sha256": sha(STUDY / "input/memberships.csv"),
                 "evidence": [{"path": str(p), "sha256": sha(p)} for p in evidence_paths],
                 "history_policy": "latest_available", "risk_policy": DEFAULT_RISK_POLICY,
                 "instruction": "发生冲突时以最新的为准", "uses_test_data": False}
    intermediate = STUDY / "latest_available_v3"
    intermediate_baseline = read(intermediate / "baseline/baseline.json")
    reference["superseded_intermediate"] = {"baseline_sha256": sha(intermediate / "baseline/baseline.json"),
        "input_sha256": intermediate_baseline["input_sha256"], "archive": intermediate_baseline["run_archive"],
        "reason": "MKC, AMCR and PCAR earlier annual EPS needed verified current-share-unit bridges; intermediate report is not the final delivery."}
    write(REVISION / "prior_run_reference.json", reference)
    common = dict(mode="research", archive_root=str(REVISION / "run_archive"), retention_days=7)
    baseline_dir, final_dir = REVISION / "baseline", REVISION / "final"
    if not (baseline_dir / "baseline.json").exists():
        screen(Namespace(input=str(NORM / "snapshots.json"), universe=str(STUDY / "input/memberships.csv"),
                         config=str(LEGACY_CONFIG_PATH), risk_policy=str(policy_path), exclude_loss_probability=None,
                         output=str(baseline_dir), raw_input=[str(p) for p in evidence_paths] +
                         [str(REVISION / "prior_run_reference.json")], **common))
    baseline = read(baseline_dir / "baseline.json")
    if baseline["input_sha256"] != reference["input_sha256"] or baseline["export_policy"]["risk_policy"] != DEFAULT_RISK_POLICY:
        raise ValueError("Revision inputs differ from frozen baseline")
    complete_archive(baseline["run_archive"])
    queue = read(baseline_dir / "agent_queue.json")
    if queue["candidates"]:
        raise ValueError("Actual AI review required; do not clear a nonempty queue automatically")
    events = REVISION / "events.json"
    write(events, {"baseline_sha256": queue["baseline_sha256"], "events_by_symbol": {},
                   "reason": "No compatible institutional consensus; this revision reconciles historical facts, not future news scenarios."})
    if not (final_dir / "adjusted.json").exists():
        adjust(Namespace(baseline=str(baseline_dir / "baseline.json"), events=str(events),
                         output=str(final_dir), exclude_loss_probability=None, raw_input=[], **common))
    final = read(final_dir / "all_stocks.json")
    adjusted = read(final_dir / "adjusted.json")
    assert adjusted["baseline_sha256"] == queue["baseline_sha256"]
    complete_archive(adjusted["run_archive"])
    rows = final["stocks"] + read(final_dir / "excluded_stocks.json")["excluded_stocks"]
    old_product = read(PRIOR / "final/all_stocks.json")
    old_rows = old_product["stocks"] + read(PRIOR / "final/excluded_stocks.json")["excluded_stocks"]
    old_by = {r["symbol"]: r for r in old_rows}
    intermediate_rows = read(intermediate / "final/all_stocks.json")["stocks"] + read(intermediate / "final/excluded_stocks.json")["excluded_stocks"]
    intermediate_by = {r["symbol"]: r for r in intermediate_rows}
    assert len(rows) == len({r["symbol"] for r in rows}) == 503
    assert set(old_by) == {r["symbol"] for r in rows}
    ranked = sorted((r for r in final["stocks"] if r["export_status"] == "available"),
                    key=lambda r: r["prediction"]["p_below_6"], reverse=True)
    ranking = [{"rank": i, "symbol": r["symbol"], "company": r["input"].get("company_name"),
                "price": r["input"]["price"], "price_date": r["input"]["price_date"], "eps0": r["input"]["eps0"],
                "p_below_6": r["prediction"]["p_below_6"], "p_loss": r["prediction"]["p_loss"],
                "annual_years": len(r["input"]["history"]), "growth_observations": r["model"]["history_growth_observations"]}
               for i, r in enumerate(ranked, 1)]
    write(final_dir / "ranking.json", ranking)
    with (final_dir / "ranking.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ranking[0]) if ranking else ["rank", "symbol"])
        w.writeheader()
        w.writerows(ranking)
    now = datetime.now(timezone.utc).isoformat()
    observations = [json.loads(line)["retrieved_at"]
                    for path in (STUDY / "eps_reconciliation").glob("*/source_manifest.jsonl")
                    for line in path.read_text(encoding="utf-8").splitlines()]
    information_cutoff = max(observations, key=datetime.fromisoformat)
    audit_sha256 = sha(NORM / "normalization_audit.json")
    base = {"run_id": "latest-history-v4-" + queue["baseline_sha256"][:16], "recorded_at": now,
            "as_of": "2026-11-07", "information_cutoff": information_cutoff,
            "cutoff_basis": "Latest actual archived evidence retrieval; daily as_of remains separate. Historical EPS uses the previously frozen Companyfacts responses and already-published reconciliation evidence.",
            "baseline_sha256": manifest["baseline_sha256"], "resulting_baseline_sha256": queue["baseline_sha256"],
            "inputs": [{"path": str(NORM / "snapshots.json"), "sha256": reference["input_sha256"]}],
            "outputs": [{"path": str(final_dir / "adjusted.json"), "sha256": sha(final_dir / "adjusted.json")}],
            "type": "user_requested_latest_available_annual_history", "status": "applied", "applied": True,
            "instruction": "发生冲突时以最新的为准", "risk_policy_changed": False,
            "current_price_or_ttm_changed": False, "news_scenario_adjustment": False,
            "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1, "loss_probability_delta": 0,
            "parameter_note": "No discretionary future-growth or risk deltas. Historical model moments and loss prior are recomputed from the selected annual observations.",
            "confidence": None, "already_in_consensus": None}
    transitions = Counter()
    changed_history = []
    for row in rows:
        before = old_by[row["symbol"]]
        transitions[(before["export_status"], row["export_status"])] += 1
        changed = history(before) != history(row)
        if changed:
            changed_history.append(row["symbol"])
        audit = by_audit.get(row["symbol"], {})
        log({**base, "symbol": row["symbol"], "event_id": "latest-history-" + row["symbol"],
             "before": compact(before), "after": compact(row), "history_changed": changed,
             "before_annual_history": history(before), "after_annual_history": history(row),
             "superseded_intermediate_baseline_sha256": reference["superseded_intermediate"]["baseline_sha256"],
             "superseded_intermediate_prediction": compact(intermediate_by[row["symbol"]]),
             "superseded_intermediate_history": history(intermediate_by[row["symbol"]]) if row["symbol"] in {"MKC", "AMCR", "PCAR"} else None,
             "verified_difference_cause": {"path": str(cause_files[row['symbol']]), "sha256": sha(cause_files[row['symbol']])} if row["symbol"] in cause_files else None,
             "evidence": {"audit_file": str(NORM / "normalization_audit.json"), "audit_sha256": audit_sha256,
                          "symbol": row["symbol"], "source_sha256": audit.get("source_sha256")},
             "reason": "Latest disclosed actual EPS wins within each eligible fiscal period; preserve entity/share-basis constraints and retain superseded observations."})
    summary = {"as_of": "2026-11-07", "target_date": "2032-11-07", "price_date": "2026-11-05",
               "exports": final["summary"], "previous_exports": old_product["summary"],
               "normalization": norm, "risk_policy": DEFAULT_RISK_POLICY, "history_policy": "latest_available",
               "history_changed_count": len(changed_history), "history_changed_symbols": changed_history,
               "transitions": [{"before": a, "after": b, "count": n} for (a, b), n in sorted(transitions.items())],
               "ranking_count": len(ranking), "probability_above_70pct_count": sum(r["p_below_6"] >= .7 for r in ranking),
               "baseline_sha256": queue["baseline_sha256"], "previous_baseline_sha256": manifest["baseline_sha256"],
               "institutional_consensus_count": 0, "news_scenario_adjustments_applied": 0, "uses_test_data": False}
    write(final_dir / "study_summary.json", summary)
    by_symbol = {r["symbol"]: r for r in rows}
    focused = ["| 股票 | 原年度数/增长数 | 新年度数/增长数 | 新亏损先验 | 新状态 |", "| --- | ---: | ---: | ---: | --- |"]
    for symbol in ("ALL", "FIS", "ECHO"):
        before, after = old_by[symbol], by_symbol[symbol]
        p = after["prediction"].get("p_loss")
        focused.append(f"| {symbol} | {len(before['input']['history'])}/{before['model']['history_growth_observations']} | {len(after['input']['history'])}/{after['model']['history_growth_observations']} | {p:.2%} | {after['export_status']} |")
    table = ["| 排名 | 股票 | P(P₀/E₆<6) | 终点非正盈利先验 | 年度历史/有效增长数 |", "| --- | --- | ---: | ---: | ---: |"]
    table += [f"| {r['rank']} | {r['symbol']} | {r['p_below_6']:.2%} | {r['p_loss']:.2%} | {r['annual_years']}/{r['growth_observations']} |" for r in ranking[:10]]
    count = final["summary"]
    lines = ["# 最新披露优先的年度历史重建与严格风险复筛", "",
             "根据用户要求，同一财年EPS出现版本差异时，采用决策日前最新披露的实际财报值，逐项保存被替代版本。决策日2026-11-07，价格仍为2026-11-05收盘，预测目标日2032-11-07；本轮只重建年度历史，未更新价格或当前TTM。", "",
             "## 已查明的差异原因", "",
             "ALL的2021/2022 EPS变化是采用LDTI新会计准则的追溯重列；FIS的2022 EPS变化是前期不重大错报更正；ECHO是与DISH共同控制下合并，历史利润和股数追溯合并。另依据原文修正MKC、AMCR和PCAR的历史股份单位，并补录MKC最新年报摘要中Companyfacts缺失的实际年度数。详见[逐项原因、数值桥接和一手证据](../../eps_reconciliation/report_reviewed.md)。", "",
             "最新优先不再仅因重叠EPS数值不同而截断历史。它不证明所有旧年口径已经一致：未重列的早期年度仍有跨会计版本风险；同日不同值不能判定先后时保留歧义；未知股份换算不从EPS比值猜测。ECHO仅接入已披露的合并主体2021年起历史，不把旧EchoStar单体或并购备考数当作合并后实际值。", "",
             *focused, "",
             "## 复筛结果", "",
             f"503只证券中，{count['available_stock_count']}只可排名、{count['excluded_stock_count']}只排除、{count['data_unavailable_stock_count']}只数据不足或风险待核查。年度历史记录发生变化的股票为{len(changed_history)}只（包含披露版本/来源变化，不只EPS数值变化）。", "",
             "风险政策仍为先检查终点亏损先验<10%、最近3个连续完整年度EPS均为正，再计算估值。新历史可能改变增长、波动和历史亏损先验，因此风险资格和排名需整体重算；没有人为调整未来盈利情景。", "", *table, "",
             f"达到70%门槛的股票：{summary['probability_above_70pct_count']}只。上述概率仍未校准，缺同口径机构共识，并未逐股完成全部股份及经济范围认证；不能认定为全503只的已验证最高置信率排名。P₀/E₆采用固定当前价格除以六年后EPS。", "",
             "## 文件与复现", "",
             "- [可排名股票](ranking.csv)、[完整主表](all_stocks.csv)、[排除名单](excluded_stocks.csv)、[数量与历史覆盖](study_summary.json)。",
             "- [逐股调整日志](../ai_adjustment_log.jsonl)、[逐年度版本选择和缺失原因](../../normalized_latest_available_reviewed/normalization_audit.json)、[前一轮引用及输入hash](../prior_run_reference.json)。",
             "- [旧严格风险报告](../../strict_risk_v2/final/report.md)及更早报告、原始输入和日志均保留。未读取data/测试资料。",
             "- 中间v3运行因发现MKC、AMCR、PCAR早期EPS股份单位问题已被本轮替代，保留作审计，不作为最终排名。",
             "- 重建归一化使用`build_current_snapshots.py --history-policy latest_available --entity-overrides outputs/current_sp500_2026-11-06/eps_reconciliation/entity_overrides_reviewed.json --output outputs/current_sp500_2026-11-06/normalized_latest_available_reviewed`；已存在的冻结快照不能覆盖，复筛入口为`scripts/run_latest_history_study.py`。", "",
             f"冻结基准SHA256：`{queue['baseline_sha256']}`。", ""]
    (final_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    files = [final_dir / name for name in ("report.md", "all_stocks.json", "all_stocks.csv", "excluded_stocks.json", "excluded_stocks.csv", "ranking.json", "ranking.csv", "study_summary.json")]
    files += [REVISION / "ai_adjustment_log.jsonl", policy_path, REVISION / "prior_run_reference.json"]
    write(final_dir / "delivery_manifest.json", {"baseline_sha256": queue["baseline_sha256"],
          "files": [{"path": str(p.relative_to(REVISION)), "sha256": sha(p)} for p in files]})
    print(json.dumps({"summary": count, "history_changed": len(changed_history), "top5": ranking[:5], "report": str(final_dir / "report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
