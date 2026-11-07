"""Apply the stricter risk-first policy to frozen current-study inputs.

No new prices, EPS, analyst estimates or news assumptions are introduced.
The original research and its audit log remain immutable comparison evidence.
"""
from __future__ import annotations

from argparse import Namespace
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
# Frozen batch reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"
sys.path.insert(0, str(ROOT / "src"))
from stocking.cli import screen, adjust
from stocking.model import build_baseline, evaluate, blocked_prediction
from stocking.risk import DEFAULT_RISK_POLICY

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
REVISION = STUDY / "strict_risk_v2"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def append_log(record):
    path = REVISION / "ai_adjustment_log.jsonl"
    previous = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    for old in previous:
        if old["run_id"] == record["run_id"] and old["event_id"] == record["event_id"]:
            strip_time = lambda x: {k: v for k, v in x.items() if k != "recorded_at"}
            if strip_time(old) != strip_time(record):
                raise ValueError("Existing audit ID has different content; append a superseding event instead")
            return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def compact(row):
    pred = row.get("prediction", {})
    return {"export_status": row["export_status"], "risk_assessment": row["risk_assessment"],
            "exclusion_reasons": row["exclusion_reasons"],
            "p_loss": pred.get("p_loss"), "p_below_6": pred.get("p_below_6"),
            "candidate": pred.get("candidate")}


def complete_archive(path):
    manifest = Path(path) / ".stocking-run.json"
    if not manifest.exists() or read(manifest).get("status") != "complete":
        raise ValueError("Research archive is not complete: " + str(path))
    return {"path": str(manifest), "sha256": sha(manifest)}


def validate_current_source_replay(baseline):
    """Keep run provenance intact; prove current code still reproduces it."""
    checked = 0
    policy = baseline["export_policy"]["risk_policy"]
    for row in baseline["results"]:
        if "model" not in row:
            raise ValueError("Cannot certify a replay of an incomplete model record")
        model = build_baseline(row["input"], row["model"]["config"], risk_policy=policy)
        prediction = evaluate(model) if model["status"] == "ready" else blocked_prediction(model)
        if model != row["model"] or prediction != row["prediction"]:
            raise ValueError("Current source changes this frozen result; start a separately logged revision: " + row["symbol"])
        checked += 1
    archive = read(Path(baseline["run_archive"]) / ".stocking-run.json")
    generated_hashes = archive["metadata"]["code_sha256"]
    current_hashes = {name: sha(ROOT / "src/stocking" / name) for name in generated_hashes}
    return {"input_sha256": baseline["input_sha256"], "models_and_predictions_replayed": checked,
            "all_models_and_predictions_identical": True,
            "generated_run_code_sha256": generated_hashes,
            "validated_current_code_sha256": current_hashes,
            "changed_code_files": [name for name in generated_hashes if generated_hashes[name] != current_hashes[name]],
            "note": "Original archives retain the code hashes used to generate them. This separate exact replay validates current source without relabelling that provenance."}


def main():
    prior_manifest = read(STUDY / "final/delivery_manifest.json")
    for entry in prior_manifest["files"]:
        if sha(STUDY / entry["path"]) != entry["sha256"]:
            raise ValueError("Original frozen delivery changed: " + entry["path"])
    original = read(STUDY / "baseline/baseline.json")
    if sha(STUDY / "baseline/baseline.json") != prior_manifest["baseline_sha256"]:
        raise ValueError("Original frozen baseline differs from its delivery manifest")
    original_archive = complete_archive(original["run_archive"])
    original_log = [json.loads(line) for line in (STUDY / "ai_adjustment_log.jsonl").read_text(encoding="utf-8").splitlines()]
    input_cutoff = max(r["information_cutoff"] for r in original_log if r.get("information_cutoff"))
    input_path = STUDY / "normalized/snapshots.json"
    if sha(input_path) != original["input_sha256"]:
        raise ValueError("This is a policy comparison; original inputs must remain unchanged")
    policy_path = REVISION / "risk_policy.json"
    policy = dict(DEFAULT_RISK_POLICY)
    if policy_path.exists() and read(policy_path) != policy:
        raise ValueError("Frozen revision policy differs; create a new revision directory")
    write(policy_path, policy)
    reference = {"original_baseline": str(STUDY / "baseline/baseline.json"),
                 "original_baseline_sha256": sha(STUDY / "baseline/baseline.json"),
                 "original_delivery_manifest": str(STUDY / "final/delivery_manifest.json"),
                 "original_delivery_manifest_sha256": sha(STUDY / "final/delivery_manifest.json"),
                 "original_raw_archive": original["run_archive"],
                 "original_archive_manifest": original_archive,
                 "universe": str(STUDY / "input/memberships.csv"),
                 "universe_sha256": sha(STUDY / "input/memberships.csv"),
                 "input": str(input_path), "input_sha256": sha(input_path),
                 "raw_evidence_policy": "Original research archives are retained; this revision references them without duplicating provider responses.",
                 "requested_change": "User requested stricter loss-risk exclusion before valuation and ranking.",
                 "uses_test_data": False}
    write(REVISION / "prior_run_reference.json", reference)
    common = dict(mode="research", archive_root=str(REVISION / "run_archive"), retention_days=7)
    baseline_dir, final_dir = REVISION / "baseline", REVISION / "final"
    if not (baseline_dir / "baseline.json").exists():
        screen(Namespace(input=str(input_path), universe=str(STUDY / "input/memberships.csv"),
                         config=str(LEGACY_CONFIG_PATH), risk_policy=str(policy_path), exclude_loss_probability=None,
                         output=str(baseline_dir), raw_input=[str(REVISION / "prior_run_reference.json"),
                         str(STUDY / "normalized/normalization_audit.json")], **common))
    baseline = read(baseline_dir / "baseline.json")
    if baseline["input_sha256"] != sha(input_path) or baseline["export_policy"]["risk_policy"] != policy:
        raise ValueError("Existing revision baseline does not match input and policy")
    if not (baseline_dir / "run_lifecycle.json").exists():
        raise ValueError("Incomplete baseline; inspect run before resuming")
    complete_archive(baseline["run_archive"])
    validation = validate_current_source_replay(baseline)
    write(REVISION / "validation.json", validation)
    queue = read(baseline_dir / "agent_queue.json")
    if queue["candidates"]:
        raise ValueError("Actual AI candidate review required; cannot auto-clear a nonempty queue")
    events_path = REVISION / "events.json"
    write(events_path, {"baseline_sha256": queue["baseline_sha256"], "events_by_symbol": {},
                        "reason": "Compatible consensus is absent; risk-policy changes are not news forecasts."})
    if not (final_dir / "run_lifecycle.json").exists():
        adjust(Namespace(baseline=str(baseline_dir / "baseline.json"), events=str(events_path),
                         output=str(final_dir), exclude_loss_probability=None, raw_input=[], **common))
    adjusted = read(final_dir / "adjusted.json")
    complete_archive(adjusted["run_archive"])
    if adjusted["baseline_sha256"] != queue["baseline_sha256"]:
        raise ValueError("Final revision does not belong to this baseline")
    product = read(final_dir / "all_stocks.json")
    excluded = read(final_dir / "excluded_stocks.json")["excluded_stocks"]
    all_rows = product["stocks"] + excluded
    old_product = read(STUDY / "final/all_stocks.json")
    old_rows = old_product["stocks"] + read(STUDY / "final/excluded_stocks.json")["excluded_stocks"]
    previous = {r["symbol"]: r for r in old_rows}
    assert len(all_rows) == len({r["symbol"] for r in all_rows}) == 503
    assert set(previous) == {r["symbol"] for r in all_rows}
    ranked = sorted((r for r in product["stocks"] if r["export_status"] == "available"),
                    key=lambda r: r["prediction"]["p_below_6"], reverse=True)
    for row in ranked:
        before, after = previous[row["symbol"]]["prediction"], row["prediction"]
        for field in ("p_loss", "p_below_6", "p_above_6_5_positive", "positive_conditional_eps_mean"):
            if abs(before[field] - after[field]) > 1e-12:
                raise ValueError("Risk filtering must not change the surviving valuation model")
        assert after["p_loss"] < policy["max_loss_probability"]
    for row in all_rows:
        if row["export_status"] != "available":
            assert row.get("prediction", {}).get("p_below_6") is None
    ranking = [{"rank": i, "symbol": r["symbol"], "company": r["input"]["company_name"],
                "price": r["input"]["price"], "price_date": r["input"]["price_date"],
                "eps0": r["input"]["eps0"], "p_below_6": r["prediction"]["p_below_6"],
                "p_loss": r["prediction"]["p_loss"], "annual_years": len(r["input"]["history"]),
                "growth_observations": r["model"]["history_growth_observations"]}
               for i, r in enumerate(ranked, 1)]
    write(final_dir / "ranking.json", ranking)
    with (final_dir / "ranking.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ranking[0]) if ranking else ["rank", "symbol"])
        writer.writeheader()
        writer.writerows(ranking)
    transitions = Counter((previous[r["symbol"]]["export_status"], r["export_status"]) for r in all_rows)
    changed = [r for r in all_rows if compact(previous[r["symbol"]]) != compact(r)]
    run_id = "current-risk-v2-" + queue["baseline_sha256"][:16]
    base_log = {"run_id": run_id, "recorded_at": datetime.now(timezone.utc).isoformat(),
                "as_of": "2026-11-07", "baseline_sha256": queue["baseline_sha256"],
                "information_cutoff": input_cutoff,
                "information_cutoff_basis": "Inherited latest input observation cutoff from the original frozen study log; no new provider data fetched.",
                "superseded_baseline_sha256": reference["original_baseline_sha256"],
                "source_input_sha256": reference["input_sha256"],
                "outputs": [{"path": str(final_dir / "adjusted.json"), "sha256": sha(final_dir / "adjusted.json")}],
                "type": "user_requested_screening_policy_change", "applied": True,
                "evidence": {"user_instruction": "程序应当首先排除高概率亏损的企业，这些为什么没有排除掉？这一步筛选应当更加严格。",
                             "input": reference["input"], "source_hash": reference["input_sha256"]},
                "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1,
                "loss_probability_delta": 0, "confidence": None, "already_in_consensus": None,
                "loss_probability_model_changed": False, "news_forecast_adjustment": False,
                "source_prices_or_eps_changed": False}
    append_log({**base_log, "event_id": "risk-policy-v2", "symbol": None,
                "before": {"policy": original["export_policy"], "summary": old_product["summary"]},
                "after": {"policy": product["policy"], "summary": product["summary"]},
                "reason": "The 25% export cutoff admitted the former top three; stricter risk-first eligibility is separate from probability calibration."})
    for row in all_rows:
        append_log({**base_log, "event_id": "risk-policy-v2-" + row["symbol"], "symbol": row["symbol"],
                    "before": compact(previous[row["symbol"]]), "after": compact(row),
                    "risk_screen": row.get("model", {}).get("risk_screen"),
                    "reason": "Apply the same predeclared risk policy across the full current universe; retain unavailable and excluded records."})
    summary = {"as_of": "2026-11-07", "target_date": "2032-11-07", "price_date": "2026-11-05",
               "universe_count": 503, "policy": product["policy"], "exports": product["summary"],
               "previous_exports": old_product["summary"], "ranking_count": len(ranking),
               "transitions": [{"before": a, "after": b, "count": n} for (a, b), n in sorted(transitions.items())],
               "changed_record_count": len(changed), "eligible_ai_candidate_count": len(queue["candidates"]),
               "probability_above_70pct_count": sum(r["p_below_6"] >= .7 for r in ranking),
               "baseline_sha256": queue["baseline_sha256"], "input_sha256": reference["input_sha256"],
               "surviving_probabilities_unchanged": True, "uses_test_data": False,
               "limitations": ["Stricter risk eligibility is not calibrated risk probability.",
                               "Compatible institutional consensus remains unavailable.",
                               "Unresolved economic-basis verification and missing inputs prevent a certified full-universe ranking."]}
    write(final_dir / "study_summary.json", summary)
    by_symbol = {r["symbol"]: r for r in all_rows}
    old_top = []
    for symbol in ("ECHO", "ALL", "FIS"):
        row = by_symbol[symbol]
        assert row["export_status"] == "excluded"
        old_top.append(f"| {symbol} | {previous[symbol]['prediction']['p_loss']:.2%} | 排除 | {'、'.join(row['exclusion_reasons'])} |")
    table = ["| 排名 | 股票 | P(P₀/E₆<6) | 终点非正盈利先验 | 年度历史/有效增长数 |",
             "| --- | --- | ---: | ---: | ---: |"]
    table += [f"| {r['rank']} | {r['symbol']} | {r['p_below_6']:.2%} | {r['p_loss']:.2%} | {r['annual_years']}/{r['growth_observations']} |" for r in ranking[:10]]
    counts = product["summary"]
    lines = ["# 更严格的亏损风险优先筛选", "",
             "本轮仅根据用户要求改变筛选政策，复用原冻结输入；决策日2026-11-07、价格为2026-11-05收盘、目标日2032-11-07。未更新行情或财报，也未使用data/测试数据。", "",
             "## 为什么原前三名没有被排除", "",
             "原主表门槛为25%，买入候选亏损门槛才是10%。ECHO、ALL、FIS的终点亏损先验分别为24%、17.33%、18.57%，均低于25%，因此虽无正式买入资格，仍进入了全量估值排名。", "",
             "该先验按(历史非正EPS年数+0.06×10)/(年度样本数+10)计算。ECHO为(3+0.6)/15=24%，ALL为(2+0.6)/15=17.33%，FIS为(2+0.6)/14=18.57%。少量历史受默认先验收缩明显，不能把该数字当作已验证的低风险证明。", "",
             "## 新政策与运行顺序", "",
             "1. 当前TTM或有效机构预测EPS非正、明确高风险标记仍直接排除。",
             "2. 终点非正盈利先验达到10%即排除，不以估值便宜抵消风险。",
             "3. 最近3个连续完整年度须全部EPS>0；其中任何一年非正即排除。年度不足、断档或无法确认最近期间列为待核查，不伪装成高风险，也不参与排名。",
             "4. 先验证输入和风险，再对通过者计算估值概率、排序及生成AI队列。排除或待核查记录的估值概率留空，保留风险统计与原因。",
             "5. 完整政策随基准冻结，adjust继承；要改变新政策须重新screen。历史基准按原政策解释，不借调整步骤把旧排除股票救回。", "",
             "这次收紧是用户要求的工程准入规则，没有修改历史EPS、亏损先验公式、增长参数或新闻情景；通过筛选者的估值概率与原结果完全一致。", "",
             "| 原前三名 | 原终点亏损先验 | 新状态 | 排除原因代码 |", "| --- | ---: | --- | --- |", *old_top, "",
             "## 新结果", "",
             f"503只证券中，{counts['available_stock_count']}只可排名、{counts['excluded_stock_count']}只排除、{counts['data_unavailable_stock_count']}只数据不足或风险待核查。原可排名272只。新可排名样本中达到70%概率门槛的股票为{summary['probability_above_70pct_count']}只。", "",
             *table, "",
             "概率仍为未校准的历史回退研究结果，机构共识仍缺失，且并未完成每家公司的全部股份和经济范围核验；不能称为完整标普500的已验证最高置信率名单。这里的PE是固定P₀除以六年后EPS，不是未来交易市盈率。", "",
             "盈利持续性问题仍需单独核查。HIG、MKC等一次性收益问题见[原报告的一手披露核查](../../final/report.md)；通过风险筛选不代表可持续盈利已得到认证。", "",
             "## 审计与复现", "",
             "- [可排名股票](ranking.csv)、[完整主表](all_stocks.csv)、[排除记录](excluded_stocks.csv)、[政策及数量汇总](study_summary.json)。",
             "- [逐股政策变化日志](../ai_adjustment_log.jsonl)、[冻结政策](../risk_policy.json)、[原始研究引用](../prior_run_reference.json)。",
             "- [原始输入及缺失原因核对](../../normalized/normalization_audit.json)。缺失成员仍逐项保留，不把未评估记录计入低风险股票。",
             "- [当前源码重放核验](../validation.json)：293份模型及预测与冻结基准逐项一致；生成时源码hash与重放时源码hash分别记录，不改写原运行版本。",
             "- [原冻结报告](../../final/report.md)和原日志均未覆盖；原始证据继续保存在原research归档，不重复下载。",
             "- 复现命令：`& .venv/Scripts/python.exe scripts/rescreen_current_risk.py`。本脚本限本次固定输入，不可改标签用作新的行情日期。", "",
             f"新基准SHA256：`{queue['baseline_sha256']}`；输入SHA256：`{reference['input_sha256']}`。", ""]
    (final_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    deliver = [final_dir / n for n in ("report.md", "all_stocks.json", "all_stocks.csv", "excluded_stocks.json",
               "excluded_stocks.csv", "ranking.json", "ranking.csv", "study_summary.json")]
    deliver += [REVISION / "ai_adjustment_log.jsonl", policy_path, REVISION / "prior_run_reference.json", REVISION / "validation.json"]
    write(final_dir / "delivery_manifest.json", {"baseline_sha256": queue["baseline_sha256"],
          "files": [{"path": str(p.relative_to(REVISION)), "sha256": sha(p)} for p in deliver]})
    print(json.dumps({"summary": counts, "top5": ranking[:5], "report": str(final_dir / "report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
