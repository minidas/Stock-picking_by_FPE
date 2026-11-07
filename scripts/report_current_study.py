"""Summarize the frozen current study and append factual-quality review notes."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from run_current_study import STUDY, ROOT, read, write, sha, append_log, prediction_fields


REVIEWS = {
    "ECHO": {"published_at": "2026-11-03", "source_url": "https://www.sec.gov/Archives/edgar/data/1415404/000110465926121675/sats-20260930x10q.htm",
             "note": "第三季度含频谱交易及撤销合并收益，业务范围发生重大变化；当前高TTM不能直接视为稳定经营盈利。5年历史仅1个有效正盈利增长样本，终点亏损先验24%，接近25%排除线。"},
    "ALL": {"published_at": "2026-11-04", "source_url": "https://www.allstateinvestors.com/static-files/546cf0d4-5def-4140-97f4-c5b1eb0fbc5e",
            "note": "采用已归档SEC四个实际季度14.37、9.25、12.51、8.03，合计44.16，替代第三方近似44.09。公司单独披露的加权股数TTM44.25是另一计算口径。历史亏损及保险承保周期仍影响结果。"},
    "FIS": {"published_at": "2026-11-04", "source_url": "https://www.investor.fisglobal.com/news-releases/news-release-details/fis-reports-third-quarter-2026-results",
            "note": "2026年前九个月含出售Worldpay持股收益约22.06亿美元，TTM受到明显一次性出售收益影响；4年历史仅1个有效增长样本。"},
    "HIG": {"published_at": "2026-10-26", "source_url": "https://ir.thehartford.com/news/news-details/2026/The-Hartford-Reports-Strong-Third-Quarter-2026-Financial-Results/",
            "note": "第三季度含再保险终止税前收益4.97亿美元；第二季度也有出售业务相关税收利益。GAAP盈利并不等同于可持续核心盈利。"},
    "MKC": {"published_at": "2026-10-01", "source_url": "https://ir.mccormick.com/node/34076",
            "note": "墨西哥业务原持股重估贡献EPS3.22美元，显著影响TTM5.53；不能把该一次性盈利当作每年重复发生。"},
    "GL": {"published_at": "2026-10-21", "source_url": "https://investors.globelifeinsurance.com/news-releases/2026/october/globe-life-inc-reports-third-quarter-2026-results",
           "note": "第三季度包含年度储备假设更新税前收益1.205亿美元；这不是可无依据直接剔除的错误数据。"},
}


def main():
    final = STUDY / "final"
    summary = read(final / "study_summary.json")
    ranking = read(final / "ranking.json")
    baseline = read(STUDY / "baseline/baseline.json")
    results = {r["symbol"]: r for r in baseline["results"]}
    manifests = [json.loads(line) for line in (STUDY / "evidence/download_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    review_rows = []
    for symbol, note in REVIEWS.items():
        matches = [r for r in manifests if r.get("url") == note["source_url"] and r.get("success")]
        source = max(matches, key=lambda r: r["retrieved_at"]) if matches else None
        review = {"symbol": symbol, **note, "source_raw_archive": source,
                  "raw_archive_status": "archived" if source else "web_read_verified_but_raw_download_unavailable_or_not_attempted",
                  "review_type": "actual_data_and_earnings_quality_only",
                  "future_earnings_adjustment_applied": False,
                  "reason_for_no_adjustment": "No compatible institutional consensus and no eligible AI candidate; preserve reported GAAP numbers and annotate nonrecurring effects instead of inventing a six-year adjustment."}
        review_rows.append(review)
        pred = results[symbol]["prediction"]
        append_log({"run_id": "current-sp500-2026-11-07-" + summary["baseline_sha256"][:16],
                    "recorded_at": datetime.now(timezone.utc).isoformat(), "as_of": summary["as_of"],
                    "symbol": symbol, "event_id": "actual-earnings-quality-review-" + symbol + ("-publication-date-correction" if symbol == "ECHO" else ""),
                    "supersedes_event_id": "actual-earnings-quality-review-ECHO" if symbol == "ECHO" else None,
                    "audit_metadata_correction": {"field": "evidence.published_at", "before": "2026-11-06", "after": "2026-11-03", "basis": "SEC Company Facts filing date for accession 0001104659-26-121675"} if symbol == "ECHO" else None,
                    "type": "factual_quality_annotation", "status": "reviewed_no_forecast_change", "applied": False,
                    "baseline_sha256": summary["baseline_sha256"], "evidence": review,
                    "already_in_consensus": None, "consensus_status": "unavailable",
                    "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1,
                    "loss_probability_delta": 0, "confidence": None,
                    "before": prediction_fields(pred), "after": prediction_fields(pred),
                    "reason": review["reason_for_no_adjustment"]})
    write(final / "primary_source_reviews.json", review_rows)
    table = ["| 排名 | 股票 | 股价（美元） | TTM EPS | 年度历史/有效增长数 | P(P₀/E₆<6) | 终点非正盈利先验 | P(0<E₆<P₀/6.5) |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in ranking[:10]:
        table.append(f"| {r['rank']} | {r['symbol']} | {r['price']:.2f} | {r['eps0']:.2f} | {r['annual_years']}/{r['growth_observations']} | {r['p_below_6']:.2%} | {r['p_loss']:.2%} | {r['p_above_6_5_positive']:.2%} |")
    lines = ["# 当前标普500个股六年盈利概率研究", "",
             "决策日：2026-11-07（北京时间任务跨日后固定）；目标日：2032-11-07。采集目录中的2026-11-06是任务开始日期。价格统一为2026-11-05最新已完成美股交易日的未做股息复权收盘价；采集时11月6日美股仍在交易，未把盘中价格当收盘价。", "",
             "**在本轮可计算且未排除的272只股票中，没有股票达到默认70%概率门槛。前五为ECHO、ALL、FIS、HIG、MKC。它们是未校准的历史回退模型排名，不是全503只股票的已验证置信率排名。210只数据不足，不能推断其中没有更高概率的股票。**", "",
             "目标为 `Pr(E6 > P0/6)`：今天的股价除以六年后的每股盈利低于6。不是六年后的交易市盈率P6/E6，也不是P0除以期望EPS；后两者不等于本概率。", "",
             "## 排名与解释", "", *table, "",
             "终点非正盈利先验是模型对六年后EPS≤0的未校准估计，不是六年内曾经亏损或破产的概率。负EPS不会被当作低PE机会。", "",
             "前三名只有1个有效相邻正盈利增长样本，其结果明显依赖默认先验与波动下限。ECHO的24%亏损先验已接近25%工程排除线；不能仅因其便宜概率最高就认为它最稳健。", "",
             "## 实际覆盖", "",
             "- IVV、SPY持仓与当前成分表交叉核对：503只证券、500家公司；未把现金、期货、权证及残余CVR纳入。", 
             "- 已保存503份公开EPS响应、500份SEC Company Facts及503份行情响应。原始资料下载齐全不等于模型输入全部通过。",
             "- 293只通过本轮研究输入契约：275只直接TTM、10只最新完整年度TTM、8只四个明确SEC实际季度求和。",
             "- 其中272只可计算且未排除；21只排除（14只当前TTM非正、7只终点亏损先验≥25%）。主表482行加排除表21行，合计503行。",
             "- 210只保留为data_unavailable：包括EPS股类/标签缺失、最新期间不一致、实际财政日期或年度口径无法匹配等。逐项原因保存在normalization_audit.json；未填零、未换成旧年报。",
             "- 年度历史最多11年：170只达到11年；其他输入有3—10年。历史拼接在重叠冲突、拆股口径冲突处停止，不从EPS比值猜拆股倍率。",
             "- 同口径机构共识0份，AI候选0只。Yahoo GAAP预测接口返回401/429，公开网页又有403；常见non-GAAP预测没有混入GAAP基准。没有把缺共识当成没有估值机会。",
             "- 本轮并未完成逐股全部经济范围、股份和最新业绩快报的独立认证，share_basis_verified仍为false。所得概率只能用于有明确假设的研究。", "",
             "## 前列公司的一手披露核查", ""]
    for r in review_rows:
        lines.append(f"- **{r['symbol']}**：{r['note']} [一手来源]({r['source_url']})（{r['published_at']}）。")
    lines += ["", "这些核查用于识别实际数据和盈利持续性问题，没有把一次性收益任意从GAAP历史中删除，也没有修改未来增长、波动或亏损概率。没有兼容共识时仍保留原AI门禁；基准与空事件adjust阶段的概率一致。", "",
              "ALL采用44.16的四季求和，是有完整SEC组件依据的数据来源调整；公司披露的44.25与季度和由于加权股数不同而不相同。原始44.09未被覆盖，调整前后EPS、价格、概率及来源hash均写入追加式日志。", "",
              "## 文件与复现", "",
              "- [全部272只可排名股票](ranking.csv)；[完整主表](all_stocks.csv)；[完整JSON](all_stocks.json)；[排除名单](excluded_stocks.csv)。",
              "- [逐股输入核对](../normalized/normalization_audit.json)；[全部缺失原因](../normalized/missing_reasons.json)；[模型与覆盖汇总](study_summary.json)。",
              "- [AI与事实调整日志](../ai_adjustment_log.jsonl)；[一手核查记录](primary_source_reviews.json)。部分IR原文下载失败，记录了网页核查及原文缺口；不冒充已保存完整原文。",
              "- [运行留存状态](run_lifecycle.json)。模式research，原始来源与hash已归档；未删除测试资料或旧研究，本轮清理删除数0。",
              "", "本轮使用原始默认模型参数，6/6.5估值门槛、70%/80%复核门槛及25%输出排除线均未改变。未校准概率不代表可靠度保证；没有执行交易。", "",
              "新的采集与归一化脚本固定于本轮目录；换研究日期应建立新目录和快照，不能把这份输入改标签当新数据。本轮离线复现入口（项目根目录）：", "",
              "```powershell", "& .venv/Scripts/python.exe scripts/build_current_prices.py",
              "& .venv/Scripts/python.exe scripts/build_current_snapshots.py --as-of 2026-11-07",
              "& .venv/Scripts/python.exe scripts/run_current_study.py",
              "& .venv/Scripts/python.exe scripts/report_current_study.py", "```", "",
              "已通过61项相关测试：原模型17、TTM15、导出10、当前输入5、归档14。新增Windows短暂文件锁重试保留原子替换和路径边界；数据与模型规则不变。磁盘不足后仅对本轮目录启用透明压缩，原始字节及hash不变；失败的中间运行仍保留，但不作为最终结果。", "",
              f"冻结基准SHA256：`{summary['baseline_sha256']}`。本次没有读取data/作为研究输入。", ""]
    (final / "report.md").write_text("\n".join(lines), encoding="utf-8")
    write(final / "delivery_manifest.json", {"baseline_sha256": summary["baseline_sha256"],
          "files": [{"path": str(p.relative_to(STUDY)), "sha256": sha(p)} for p in
                    [final / name for name in ("report.md", "all_stocks.json", "all_stocks.csv", "excluded_stocks.json", "excluded_stocks.csv", "ranking.json", "ranking.csv", "study_summary.json", "primary_source_reviews.json")] + [STUDY / "ai_adjustment_log.jsonl"]]})
    print(json.dumps({"report": str(final / "report.md"), "top5": [{"symbol": r["symbol"], "p_below_6": r["p_below_6"]} for r in ranking[:5]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
