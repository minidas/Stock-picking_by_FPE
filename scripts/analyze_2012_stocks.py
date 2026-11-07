"""Reproduce a small sourced 2012 snapshot diagnostic, never a full-universe scan."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import load_json, load_snapshots
from stocking.model import build_baseline, evaluate

# Frozen 2012 reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG = load_json(ROOT / "examples/thresholds_legacy.json")

def main():
    source = ROOT / "data/research_2012_snapshots.json"
    snapshots = load_snapshots(source)
    rows = []
    for snapshot in snapshots:
        baseline = evaluate(build_baseline(snapshot, LEGACY_CONFIG))
        diagnostic = evaluate(build_baseline(snapshot, {**LEGACY_CONFIG, "allow_history_only_candidates": True}))
        custom = evaluate(build_baseline(snapshot, {"allow_history_only_candidates": True,
                           "buy_fpe_threshold": 8.5, "sell_fpe_threshold": 10.5}))
        rows.append({"symbol": snapshot["symbol"], "price": snapshot["price"],
                     "eps0": snapshot["eps0"], "strict_default": baseline,
                     "historical_only_diagnostic": diagnostic,
                     "research_8_5_10_5": custom, "source": snapshot["source"]})
    payload = {"as_of": "2012-12-31", "target_date": "2018-12-31",
               "scope": "Four manually sourced stocks, no institutional consensus, annual rather than latest TTM earnings",
               "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "model_sha256": hashlib.sha256((ROOT / "src/stocking/model.py").read_bytes()).hexdigest(),
               "results": rows}
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "stocks_2012_diagnostic.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# 2012年末四只个股：有限历史数据诊断", "",
             "**这不是标普500全量筛选，也没有2012年机构共识快照。不能凭四只样本回答全市场有没有买入机会。**", "",
             "决策日2012-12-31，六年目标2018-12-31。采用截止日前已公开年报中的年度稀释EPS；没有使用2013年才公布的2012全年盈利。IBM锚点为2011年年度盈利，其他为2012财政年度。没有重建更近期季度TTM，因此只是有限年度数据诊断。每家只整理了五个历史年度，短历史与年度桥接会影响结果。价格、EPS及历史股份口径一致；前三只价格来自FT年末表，仅精确到0.1美元。", "",
             "由于机构共识为空，原程序严格门禁下四只均为none（数据不足以形成正式候选），**不能把这个none解释为估值不合格**。下表特意打开历史回退候选，只用于观察概率门槛：买入70%且亏损≤10%，卖出80%或亏损≥25%。", "",
             "| 股票 | 年末价格 | 已知年度EPS | P(比率<6) | P(比率>6.5且盈利) | P(亏损) | 历史回退诊断 |",
             "|---|---:|---:|---:|---:|---:|---|" ]
    for row in rows:
        p = row["historical_only_diagnostic"]
        lines.append(f"| {row['symbol']} | {row['price']:.2f} | {row['eps0']:.2f} | {p['p_below_6']:.2%} | {p['p_above_6_5_positive']:.2%} | {p['p_loss']:.2%} | {p['candidate']} |")
    lines += ["", "本样本没有70%概率的买入条件，COST达到历史回退的卖出复核条件。卖出复核不是做空建议，更不表示已证实未来收益会差。STX当时静态PE低，但历史亏损和盈利波动阻止了简单的低PE买入推论。", "",
              "## 8.5/10.5研究假设", "",
              "保留同一模型和概率门槛，只改变估值阈值。这个参数来自退出PE和要求收益的假设，不是最优参数。", "",
              "| 股票 | P(比率<8.5) | P(比率>10.5且盈利) | 历史回退诊断 |", "|---|---:|---:|---|"]
    for row in rows:
        p = row["research_8_5_10_5"]
        lines.append(f"| {row['symbol']} | {p['p_below_buy_threshold']:.2%} | {p['p_above_sell_threshold_positive']:.2%} | {p['candidate']} |")
    lines += ["", "## 来源与重建边界", "",
              "历史成分关系以[Alex Chinco的2008–2012成员重建](https://gist.github.com/alexchinco/1ef9cb653334839f3c24)交叉确认；它是次级重建，不是官方全集审计。年末价格允许用之后发布的历史行情记录核对；盈利预测输入严格限于当时已公开财报。未输入任何2018年结果，未调用当代AI重构历史新闻观点。", ""]
    for row in rows:
        src = row["source"]
        lines.append(f"- {row['symbol']}：[财务报表]({src['financial_statement']})；[历史价格]({src['price']})；完整日期和限制见输入JSON。")
    lines += ["", "没有历史机构共识、完整成分样本、最新TTM或退市收益，不能证明原策略是否在2012年有买入标的，也不能估计横截面收益。不要用2018年实现EPS倒推2012年当时的预测概率。", "",
              "复现：`python scripts/analyze_2012_stocks.py`。阈值经济含义和指数误差模型问题见[阈值诊断](threshold_analysis.md)。"]
    (out / "stocks_2012_diagnostic.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for row in rows:
        p = row["historical_only_diagnostic"]
        print(row["symbol"], p["p_below_6"], p["p_above_6_5_positive"], p["candidate"])


if __name__ == "__main__":
    main()
