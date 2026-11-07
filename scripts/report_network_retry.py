"""Deliver the acquisition retry without changing the frozen model or ranking."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
OUT = STUDY / "network_retry_2026-11-07"
PRIOR = STUDY / "latest_available_v4"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def main():
    if any((OUT / name).exists() for name in ("report.md", "ai_adjustment_log.jsonl", "delivery_manifest.json")):
        raise ValueError("Acquisition delivery already exists; use a new revision")
    context = read(OUT / "request_context.json")
    old_manifest = PRIOR / "final/delivery_manifest.json"
    assert sha(old_manifest) == context["old_delivery_manifest_sha256"]
    manifest = read(old_manifest)
    for item in manifest["files"]:
        assert sha(PRIOR / item["path"]) == item["sha256"], item["path"]
    assert sha(PRIOR / "baseline/baseline.json") == manifest["baseline_sha256"]
    data = read(OUT / "normalized_consensus_observations.json")
    summary = read(OUT / "coverage_summary.json")
    old_rows = read(PRIOR / "final/all_stocks.json")["stocks"] + read(PRIOR / "final/excluded_stocks.json")["excluded_stocks"]
    old = {r["symbol"]: r for r in old_rows}
    assert len(data["securities"]) == len(old) == 503
    assert {s["symbol"] for s in data["securities"]} == set(old)
    now = datetime.now(timezone.utc).isoformat()
    data_hash = sha(OUT / "normalized_consensus_observations.json")
    fields = ("period", "as_reported_target_date", "eps_mean", "analysts", "currency", "eps_std", "available_at", "raw_complete", "model_admitted")
    with (OUT / "ai_adjustment_log.jsonl").open("x", encoding="utf-8") as log:
        for security in data["securities"]:
            symbol = security["symbol"]
            before = old[symbol]
            prediction = before.get("prediction")
            record = {"run_id": "network-retry-2026-11-07", "event_id": "public-consensus-retry-" + symbol,
                "recorded_at": now, "symbol": symbol, "type": "data_acquisition_not_an_earnings_scenario",
                "status": "collected_and_staged" if security["raw_complete_annual_count"] else "raw_data_incomplete",
                "model_as_of": "2026-11-07", "information_cutoff": security["retrieval_finished_at"],
                "baseline_sha256": manifest["baseline_sha256"], "resulting_baseline_sha256": manifest["baseline_sha256"],
                "applied_to_model": False, "news_scenario_adjustment": False,
                "before_consensus_used_count": len((before.get("input") or {}).get("consensus", [])),
                "after_consensus_used_count": len((before.get("input") or {}).get("consensus", [])),
                "new_raw_complete_annual_count": security["raw_complete_annual_count"],
                "before_export_status": before["export_status"], "after_export_status": before["export_status"],
                "before_prediction": prediction, "after_prediction": prediction,
                "prediction_note": "Both reference the unchanged frozen model; they are not recomputed with new forecasts.",
                "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1, "loss_probability_delta": 0,
                "confidence": None, "already_in_consensus": None,
                "source_audit": security["source_audit"], "collected_archive": security["collected_archive"],
                "new_observations": [{k: r[k] for k in fields} for r in security["observations"]],
                "admission_reasons": sorted({v for r in security["observations"] for v in r["model_admission_reasons"]}),
                "outputs": [{"path": str(OUT / "normalized_consensus_observations.json"), "sha256": data_hash}],
                "reason": "User requested network retry. Explicit GAAP observations are staged with actual acquisition dates; current November 7 observations are available November 8 under the existing rule. Exact fiscal, dilution and share basis review remains separate. No backdating or change to the frozen ranking."}
            log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    by_symbol = {s["symbol"]: s for s in data["securities"]}
    table = ["| 股票 | 本财年期末／GAAP EPS／人数 | 下财年期末／GAAP EPS／人数 |", "| --- | --- | --- |"]
    for symbol in ("HIG", "MKC", "TROW", "DECK", "PYPL"):
        cells = []
        for r in by_symbol[symbol]["observations"]:
            cells.append(f"{r['as_reported_target_date']}／{r['eps_mean']:.2f}／{r['analysts']}" if r["raw_complete"] else "缺失或不完整")
        table.append(f"| {symbol} | {cells[0]} | {cells[1]} |")
    lines = ["# 开启虚拟网卡后的网络数据重试", "",
        "2026-11-07重试已成功。使用现有主机网络和普通公开会话，没有修改网络设置、关闭TLS验证或绕过订阅/认证。此前受限执行环境的失败与主机网络请求分别留档。", "",
        f"503只证券中，已采集{summary['collected_security_count']}只，取得响应{summary['response_present_security_count']}只，核验原始响应及SHA成功{summary['verified_response_security_count']}只。",
        f"共取得{summary['raw_complete_annual_observation_count']}条字段完整的年度预测；{summary['raw_complete_two_year_security_count']}只具备两年，{summary['raw_complete_one_or_more_year_security_count']}只至少具备一年。这里的完整指原始字段齐全，不等于全部经济口径已经认证或已进入模型。", "",
        "## 数据实例", "", "以下均取显式 earningsTrendGaap 内部的 earningsEstimate；人数对应该年度EPS。日期保留供应商原报期末，尚不能将所有月末标签认定为公司真实财政日期。", "", *table, "",
        "## 核对结果", "",
        "首批六只的GAAP年度历史比较值与SEC财报相符。defaultMethodology是股票默认展示口径元数据，不能用它覆盖显式GAAP模块。顶层growth可能仍对应默认口径，未用于计算；分析师人数取自同一GAAP预测行。标准差未知保留null，不从高低值或人数编造。详见 [口径复核](yahoo_basis_review.json)。", "",
        "StockAnalysis和Nasdaq公开响应也已取得，但前者明确为调整后EPS，后者缺精确日期及口径等字段。PCAR公告和EchoStar文件的旧403已变为200并保存原文；下载成功单独记录，不自动升级为全部经济内容已验证。[其他来源复核](public_source_review_host_followup.json)。", "",
        "## 与现有模型的关系", "",
        "本轮新抓取发生于11月7日UTC。现行规则规定次一UTC日可用，因此新观察的available_at为11月8日，未倒填进冻结的11月7日基准。模型接入数为0代表尚未应用本批数据，不代表没有取得共识。原219只研究排序不变，也不能称为已使用新共识后的结果。", "",
        "同时仍需核对具体财政期末、稀释/股份基准；取得预测不能替代原有缺失TTM或风险筛选。后续使用需另建符合可用日的新基准，再执行相同严格风险门槛及候选事件复核。", "",
        "- [完整503只年度观察与缺失原因](normalized_consensus_observations.json)",
        "- [覆盖与字段检查汇总](coverage_summary.json)",
        "- [503条采集及零模型调整日志](ai_adjustment_log.jsonl)",
        "- [来源文件和哈希清单](delivery_manifest.json)", "",
        "旧模型、价格、年度历史、风险政策和概率日志均保持冻结；本轮研究未使用data/测试资料。", ""]
    with (OUT / "report.md").open("x", encoding="utf-8") as f:
        f.write("\n".join(lines))
    files = [p for p in OUT.rglob("*") if p.is_file() and not {"private_session_cache", "__pycache__"}.intersection(p.parts)
             and p.name not in {"delivery_manifest.json", "validation.json"}]
    write(OUT / "delivery_manifest.json", {"created_at": now, "prior_baseline_sha256": manifest["baseline_sha256"],
        "old_model_outputs_unchanged": True, "credentials_and_private_session_cache_excluded": True,
        "source_scripts": [{"path": str(p.relative_to(ROOT)), "sha256": sha(p)} for p in
            (ROOT / "scripts/retry_current_network.py", ROOT / "scripts/normalize_current_consensus.py", Path(__file__))],
        "files": [{"path": str(p.relative_to(OUT)), "sha256": sha(p)} for p in sorted(files)]})
    print(json.dumps({"report": str(OUT / "report.md"), "audit_records": 503, "frozen_old_model_verified": True}))


if __name__ == "__main__":
    main()
