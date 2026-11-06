"""Fixed-threshold diagnostics; no optimization or claim of a blind holdout.

This is an aggregate-index, historical-only proxy. It cannot answer which of the
2012 constituent stocks qualified with contemporaneous institutional forecasts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

import numpy as np
import pandas as pd
from scipy.stats import norm

from stocking.model import build_baseline, evaluate
from validate_history import load_monthly

PAIRS = [(6, 6.5), (8, 10), (10, 12), (12, 15), (15, 18), (20, 25)]


def index_snapshots(monthly):
    annual = monthly.loc[monthly.month == 12].dropna(subset=["eps"]).set_index("year")
    snapshots = []
    for year in annual.index:
        if year < 1957:
            continue
        known = annual.loc[(annual.index <= year - 1) & (annual.index >= 1926)].tail(21)
        if len(known) < 10:
            continue
        history = [{"period_end": f"{int(y)}-12-31", "available_at": f"{int(y)+1}-06-30", "eps": float(r.eps)}
                   for y, r in known.iterrows()]
        snapshots.append({
            "symbol": "SP_COMPOSITE_PROXY", "as_of": f"{year}-12-31",
            "price": float(annual.loc[year, "price"]), "price_date": f"{year}-12-31",
            "currency": "USD", "eps_basis": "GAAP_diluted", "share_basis": "index_points",
            "eps0": history[-1]["eps"], "eps_period_end": history[-1]["period_end"],
            "eps_available_at": history[-1]["available_at"], "history": history, "consensus": [],
        })
    return snapshots


def trade_window(monthly, forecasts, first_year, last_year, cost_bps=10):
    """Reset to cash for each descriptive window; execute prior December in January."""
    panel = monthly.loc[monthly.year.between(first_year, last_year)].reset_index(drop=True)
    lookup = {int(r["as_of"][:4]): r for r in forecasts}
    position = 0
    strategy_wealth = benchmark_wealth = 1.0
    entries = changes = 0
    values, exposure = [], []
    for i, row in panel.iterrows():
        change = 1.0 if i == 0 else float(row.nominal_total_return / panel.iloc[i - 1].nominal_total_return)
        strategy_wealth *= change if position else 1.0
        benchmark_wealth *= change
        if row.month == 1 and row.year - 1 in lookup:
            signal = lookup[row.year - 1]["candidate"]
            target = 1 if signal == "buy_review" else (0 if signal == "sell_review" else position)
            turnover = abs(target - position)
            entries += int(position == 0 and target == 1)
            changes += turnover
            strategy_wealth *= 1 - turnover * cost_bps / 10000
            position = target
        if i == 0:
            benchmark_wealth *= 1 - cost_bps / 10000
        values.append([strategy_wealth, benchmark_wealth])
        exposure.append(position)
    values = np.array(values)
    years = (len(panel) - 1) / 12
    def statistics(column):
        curve = np.r_[1.0, values[:, column]]
        return {"cagr": float(curve[-1] ** (1 / years) - 1),
                "max_drawdown": float(np.min(curve / np.maximum.accumulate(curve) - 1)),
                "terminal_wealth": float(curve[-1])}
    return {"first_year": first_year, "last_year": last_year, "starts_in_cash": True,
            "one_way_cost_bps": cost_bps, "cash_return": 0.0, "entries": entries,
            "position_changes": changes, "invested_month_fraction": float(np.mean(exposure)),
            "strategy": statistics(0), "buy_and_hold": statistics(1)}


def economics():
    return [{"terminal_pe": multiple, "required_annual_price_return": rate,
             "fpe_threshold": multiple / (1 + rate) ** 6}
            for multiple in (12, 15, 18) for rate in (.08, .10, .12)]


def report(result):
    diagnostic = result["index_2012_distribution"]
    lines = ["# 2012年指数代理与六年估值阈值诊断", "",
             "**本报告不能回答2012年哪些个股满足原始机构共识交易条件。它沿用Shiller指数数据及生产模型历史回退分支，检验改变阈值的含义。没有拟合最优阈值，没有把指数结论推广到500只个股。**", "",
             "决策日取2012-12-31，价格仍为原始数据12月月均价格代理，不是当日收盘价。只使用2011年12月及以前盈利，机构共识为空。为观察门槛机制，主动允许历史回退候选；买入概率门槛70%，卖出门槛80%，亏损限制保持生产默认值。", "",
             "## 2012年改变6与6.5后发生什么", "",
             "| 买入/卖出FPE阈值 | 低于买阈值概率 | 高于卖阈值且正盈利概率 | 历史回退代理候选 |",
             "|---|---:|---:|---|" ]
    for row in result["threshold_pairs"]:
        lines.append(f"| {row['buy_threshold']:g} / {row['sell_threshold']:g} | {row['p_buy_2012']:.2%} | {row['p_sell_2012']:.2%} | {row['candidate_2012']} |")
    lines += ["", f"2012年终点正盈利分支均值为{diagnostic['positive_eps_mean']:.2f}，中位数仅{diagnostic['positive_eps_median']:.2f}；90%区间为[{diagnostic['eps90'][0]:.2f}, {diagnostic['eps90'][1]:.2f}]，终点对数标准差{diagnostic['terminal_log_sd']:.3f}。", "",
              f"固定这个分布与70%概率门槛，要使2012年指数代理刚好触发买入，买入FPE阈值需要约**{diagnostic['required_buy_threshold_for_70pct']:.2f}**，这不是合理估值建议，而是分布过宽、强右偏的诊断。仅把6调到8、10或12解决不了这个问题。", "",
              "模型使用过去年度增长波动及正的跨年误差相关。2008年前后极端EPS波动被带入2012年；在保持条件EPS均值时，宽对数正态分布会把大部分概率推向低EPS，同时靠极小概率的高EPS维持均值。这是数学结果，不是市场真的有如此高的长期盈利崩塌概率。应先用可比口径的历史六年预测残差校准方差和相关结构，不能靠放松估值阈值掩盖模型不确定性问题。", "",
              "## 阈值应对应经济回报假设", "",
              "忽略股息，设六年后退出PE为M，今日价格/六年EPS为K，则年化价格回报为 `(M/K)^(1/6)-1`，所以要求年化回报r对应 `K=M/(1+r)^6`。这是假设下的恒等式，不是预测退出PE或保证回报。", "",
              "| 假设退出PE | 要求8%价格回报 | 要求10% | 要求12% |", "|---|---:|---:|---:|"]
    for multiple in (12, 15, 18):
        cells = [multiple / (1 + r) ** 6 for r in (.08, .10, .12)]
        lines.append(f"| {multiple} | {cells[0]:.2f} | {cells[1]:.2f} | {cells[2]:.2f} |")
    lines += ["", "例如退出PE固定15，6/6.5相当于要求约16.50%/14.96%的年化价格回报，确实较严格。若预先要求新买入10%、低于6%复核卖出，则研究起点为8.47/10.57；若退出PE18，相应为10.16/12.69。它们只是有经济解释的待验证假设，不是本次检验选出的最佳参数。分红应作为单独现金流建模；行业、利率、退出估值、稀释及交易成本也影响阈值。", "",
              "6至6.5的原始数值间隔虽窄，但70%/80%两个概率门槛还会扩大实际不交易区域；不能只凭0.5的差距断言必然频繁交易。", "",
              "## 固定六组参数的分期机制诊断", "",
              "每年12月形成信号、次年1月月均价格代理换仓，持仓收益从1月至2月区间开始生效；每次单向10基点费用、现金收益0。两个区间分别从现金起步，终点按市值计价、不假定强制清仓。下面只是分期描述，研究者已看过全期数据，**不是盲测或样本外策略证据**。月均价不可直接执行；成分变更及现行修订版本限制同原报告。", "",
              "| 阈值 | 1958–2012开仓数 | 策略年化 | 2013–2025开仓数 | 策略年化 |", "|---|---:|---:|---:|---:|"]
    for row in result["threshold_pairs"]:
        early, late = row["windows"]
        lines.append(f"| {row['buy_threshold']:g}/{row['sell_threshold']:g} | {early['entries']} | {early['strategy']['cagr']:.2%} | {late['entries']} | {late['strategy']['cagr']:.2%} |")
    first = result["threshold_pairs"][0]["windows"]
    lines += ["", f"相同期段买入持有指数代理年化分别{first[0]['buy_and_hold']['cagr']:.2%}、{first[1]['buy_and_hold']['cagr']:.2%}。JSON保留回撤、持股比例、完整假设和所有参数组，不筛掉表现差的组合。", "",
              "增加交易数量不等于策略变好。合理策略还需点时个股样本、退出/退市回报、行业约束、成本、独立测试和概率校准；参数搜索还会造成选择偏差。[回测过拟合原论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。", "",
              "来源：[Shiller官方数据](https://shillerdata.com/)。复现：`python scripts/analyze_thresholds.py`。原始数据及模型文件hash见 `threshold_analysis.json`。"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/raw/shiller_ie_data.xls")
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    manifest = json.loads(args.input.with_name("shiller_manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(args.input.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Source checksum mismatch")
    monthly = load_monthly(args.input)
    snapshots = index_snapshots(monthly)
    snapshot2012 = next(s for s in snapshots if s["as_of"] == "2012-12-31")
    baseline = build_baseline(snapshot2012)
    prediction = evaluate(baseline)
    mu, sd = baseline["terminal_log_eps_mean"], math.sqrt(baseline["terminal_log_eps_variance"])
    loss = baseline["loss_probability"]
    # Unconditional positive-event probability is (1-loss)*survival function.
    quantile = norm.ppf(1 - .7 / (1 - loss))
    required = baseline["price"] / math.exp(mu + sd * quantile)
    result = {"scope": "aggregate index / historical fallback only; not 2012 constituent screening",
              "source": manifest, "model_sha256": hashlib.sha256((ROOT / "src/stocking/model.py").read_bytes()).hexdigest(),
              "parameters_optimized": False, "blind_holdout": False,
              "index_2012_distribution": {"decision_date": baseline["as_of"], "price": baseline["price"],
                 "positive_eps_mean": prediction["positive_conditional_eps_mean"],
                 "positive_eps_median": prediction["positive_conditional_eps_median"],
                 "eps90": prediction["positive_conditional_eps_interval_90"],
                 "terminal_log_sd": sd, "p_loss": loss, "annual_log_sigma": baseline["annual_log_sigma"],
                 "required_buy_threshold_for_70pct": required},
              "economics": economics(), "threshold_pairs": []}
    for buy, sell in PAIRS:
        config = {"buy_fpe_threshold": buy, "sell_fpe_threshold": sell, "allow_history_only_candidates": True}
        forecasts = [evaluate(build_baseline(s, config)) for s in snapshots]
        current = next(p for p in forecasts if p["as_of"] == "2012-12-31")
        windows = [trade_window(monthly, forecasts, start, end) for start, end in [(1958, 2012), (2013, 2025)]]
        result["threshold_pairs"].append({"buy_threshold": buy, "sell_threshold": sell,
                                         "p_buy_2012": current["p_below_buy_threshold"],
                                         "p_sell_2012": current["p_above_sell_threshold_positive"],
                                         "candidate_2012": current["candidate"], "windows": windows})
    assert abs(result["threshold_pairs"][0]["p_buy_2012"] - prediction["p_below_6"]) < 1e-12
    assert all(a["p_buy_2012"] <= b["p_buy_2012"] for a, b in zip(result["threshold_pairs"], result["threshold_pairs"][1:]))
    assert abs((1-loss)*norm.sf((math.log(baseline["price"]/required)-mu)/sd) - .7) < 1e-12
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "threshold_analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    pd.DataFrame([{k: v for k, v in row.items() if k != "windows"} for row in result["threshold_pairs"]]).to_csv(args.output / "threshold_analysis.csv", index=False)
    (args.output / "threshold_analysis.md").write_text(report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
