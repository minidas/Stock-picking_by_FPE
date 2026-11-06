"""Reproducible, explicitly limited historical S&P composite proxy validation.

Uses the production model in history-only mode, never fabricated analyst forecasts.
The downloaded Shiller workbook is a revised vintage, NOT a point-in-time database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))
import numpy as np
import pandas as pd
import requests

SOURCE_PAGE = "https://shillerdata.com/"
SOURCE_URL = "https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/1449043e-a1ed-4e2e-8e9c-7b9a2a064493/ie_data.xls?ver=1791297298731"


def download(path: Path) -> None:
    """Fetch exact audited source; a future URL change requires explicit source review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(SOURCE_URL, timeout=60)
    response.raise_for_status()
    if not response.content.startswith(bytes.fromhex("d0cf11e0")):
        raise ValueError("Expected legacy Excel workbook, refusing unexpected response")
    path.write_bytes(response.content)
    path.with_name("shiller_manifest.json").write_text(json.dumps({
        "source_page": SOURCE_PAGE, "url": SOURCE_URL,
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")


def load_monthly(path: Path, end_year: int = 2025) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="Data", header=7)
    # Positional source columns are checked against workbook headings.
    if list(raw.columns[:5]) != ["Date", "P", "D", "E", "CPI"]:
        raise ValueError("Shiller workbook layout changed")
    if "Return" not in str(pd.read_excel(path, sheet_name="Data", header=None).iloc[6, 9]):
        raise ValueError("Total return column not recognized")
    out = pd.DataFrame({
        "raw_date": pd.to_numeric(raw.iloc[:, 0], errors="coerce"),
        "price": pd.to_numeric(raw.iloc[:, 1], errors="coerce"),
        "dividend": pd.to_numeric(raw.iloc[:, 2], errors="coerce"),
        "eps": pd.to_numeric(raw.iloc[:, 3], errors="coerce"),
        "cpi": pd.to_numeric(raw.iloc[:, 4], errors="coerce"),
        "real_total_return": pd.to_numeric(raw.iloc[:, 9], errors="coerce"),
    }).dropna(subset=["raw_date", "price", "cpi", "real_total_return"])
    out["year"] = np.floor(out.raw_date).astype(int)
    out["month"] = np.round((out.raw_date - out.year) * 100).astype(int)
    if not out.month.between(1, 12).all():
        raise ValueError("Invalid month in workbook")
    out["date"] = pd.to_datetime(dict(year=out.year, month=out.month, day=1)) + pd.offsets.MonthEnd(0)
    out["nominal_total_return"] = out.real_total_return * out.cpi
    return out.loc[out.year <= end_year].sort_values("date").reset_index(drop=True)


def matured_rows(predictions: pd.DataFrame, origin_year: int, publication_lag_years: int = 1) -> pd.DataFrame:
    """Only labels whose six-year outcomes AND assumed reporting lag have elapsed."""
    return predictions.loc[predictions.year + 6 + publication_lag_years <= origin_year]


def moving_block_ci(values: np.ndarray, block: int = 6, seed: int = 819, repetitions: int = 2000) -> list[float]:
    values = np.asarray(values, dtype=float)
    if len(values) < block * 2:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(values), size=(repetitions, int(np.ceil(len(values) / block))))
    indices = ((starts[..., None] + np.arange(block)) % len(values)).reshape(repetitions, -1)[:, :len(values)]
    return np.quantile(values[indices].mean(axis=1), [.025, .975]).tolist()


def reliability(probabilities: np.ndarray, outcomes: np.ndarray) -> list[dict]:
    bins = np.minimum((np.asarray(probabilities) * 10).astype(int), 9)
    return [{"bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "n": int((bins == i).sum()),
             "mean_probability": float(np.asarray(probabilities)[bins == i].mean()),
             "event_frequency": float(np.asarray(outcomes)[bins == i].mean())}
            for i in range(10) if (bins == i).any()]


def produce_predictions(monthly: pd.DataFrame, first_year: int = 1957, config: dict | None = None) -> pd.DataFrame:
    from stocking.model import build_baseline, evaluate
    annual = monthly.loc[monthly.month == 12].dropna(subset=["eps"]).set_index("year")
    rows = []
    for year in annual.index:
        if year < first_year:
            continue
        known = annual.loc[(annual.index <= year - 1) & (annual.index >= 1926)].tail(21)
        if len(known) < 10:
            continue
        history = [{"period_end": f"{int(y)}-12-31", "available_at": f"{int(y)+1}-06-30", "eps": float(r.eps)}
                   for y, r in known.iterrows()]
        snapshot = {
            "as_of": f"{year}-12-31", "symbol": "SP_COMPOSITE_PROXY", "price": float(annual.loc[year, "price"]),
            "price_date": f"{year}-12-31", "currency": "USD", "eps_basis": "GAAP_diluted",
            "share_basis": "index_points", "eps0": history[-1]["eps"],
            "eps_period_end": history[-1]["period_end"], "eps_available_at": history[-1]["available_at"],
            "history": history, "consensus": [],
        }
        model = build_baseline(snapshot, config)
        result = evaluate(model)
        # Explicit field names are bound to the production model's public API below.
        row = {"year": int(year), "price": snapshot["price"], "eps_anchor": snapshot["eps0"],
               "p_below_6": result["p_below_6"], "p_above_6_5": result["p_above_6_5_positive"],
               "p_loss": result["p_loss"], "eps_p05": result["positive_conditional_eps_interval_90"][0],
               "eps_p95": result["positive_conditional_eps_interval_90"][1]}
        if year + 6 in annual.index:
            actual = float(annual.loc[year + 6, "eps"])
            row.update(actual_eps6=actual, y_below_6=int(actual > snapshot["price"] / 6),
                       y_above_6_5=int(0 < actual < snapshot["price"] / 6.5),
                       covered90=int(row["eps_p05"] <= actual <= row["eps_p95"]))
        rows.append(row)
    return pd.DataFrame(rows)


def score_predictions(predictions: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    valid = predictions.dropna(subset=["actual_eps6"]).copy()
    for suffix in ["below_6", "above_6_5"]:
        valid[f"base_{suffix}"] = [
            (matured_rows(valid, int(y))[f"y_{suffix}"].sum() + 1) / (len(matured_rows(valid, int(y))) + 2)
            if len(matured_rows(valid, int(y))) >= 10 else np.nan for y in valid.year]
    scored = valid.dropna(subset=["base_below_6"]).copy()
    metrics = {"all_mature_forecasts": len(valid), "scored_forecasts": len(scored),
               "evaluation_years": [int(scored.year.min()), int(scored.year.max())],
               "conditional_positive_eps_90_interval_coverage": float(scored.covered90.mean()),
               "approx_independent_six_year_blocks": int(len(scored) // 6),
               "baselines": "Expanding event rate with Beta(1,1) smoothing; only outcomes at least 7 years old; minimum 10 mature labels"}
    metrics["maximum_buy_probability_all_origins"] = float(predictions.p_below_6.max())
    metrics["buy_probability_at_least_70pct_count"] = int((predictions.p_below_6 >= .7).sum())
    for suffix in ["below_6", "above_6_5"]:
        p, y, b = (scored[f"{prefix}_{suffix}"].to_numpy() for prefix in ["p", "y", "base"])
        loss_difference = (p - y)**2 - (b - y)**2
        metrics[suffix] = {
            "events": int(y.sum()), "event_rate": float(y.mean()), "brier": float(np.mean((p-y)**2)),
            "causal_base_rate_brier": float(np.mean((b-y)**2)),
            "brier_difference_95_moving_block_ci": moving_block_ci(loss_difference),
            "reliability": reliability(p, y),
            "nonoverlap_brier_by_six_offsets": [
                {"offset": i, "n": len(scored.iloc[i::6]), "brier": float(np.mean((p[i::6]-y[i::6])**2))}
                for i in range(6)],
        }
    return metrics, valid


def strategy(monthly: pd.DataFrame, forecasts: pd.DataFrame, threshold: float = .7,
             cost_bps: float = 10, start_year: int = 1958) -> tuple[dict, pd.DataFrame]:
    """December signal; January average-price proxy trade; exposure earns Jan->Feb return.

    Supplied total-return series is used, not a newly invented dividend model.
    Trading at a monthly average is not executable; report this as a diagnostic only.
    """
    m = monthly.loc[monthly.year >= start_year].copy().reset_index(drop=True)
    lookup = forecasts.set_index("year")
    position = 0
    wealth = benchmark = 1.0
    changes = entries = 0
    records = []
    for i, row in m.iterrows():
        growth = 1.0 if i == 0 else float(row.nominal_total_return / m.iloc[i-1].nominal_total_return)
        wealth *= growth if position else 1.0
        benchmark *= growth
        # At January's average-price point, apply signal from completed prior December.
        turnover = 0
        if row.month == 1 and row.year - 1 in lookup.index:
            signal = lookup.loc[row.year - 1]
            target = position
            if signal.p_below_6 >= threshold:
                target = 1
            elif signal.p_above_6_5 >= threshold:
                target = 0
            turnover = abs(target - position)
            entries += int(target == 1 and position == 0)
            changes += turnover
            position = target
            wealth *= 1 - turnover * cost_bps / 10000
        if i == 0:
            benchmark *= 1 - cost_bps / 10000
        records.append({"date": row.date.date().isoformat(), "position_after_trade": position,
                        "turnover": turnover, "strategy_wealth": wealth, "benchmark_wealth": benchmark})
    equity = pd.DataFrame(records)
    years = (len(equity) - 1) / 12
    def stats(column: str) -> dict:
        curve = np.r_[1.0, equity[column].to_numpy()]
        return {"cagr": float(curve[-1] ** (1 / years) - 1),
                "max_drawdown": float(np.min(curve / np.maximum.accumulate(curve) - 1)),
                "terminal_wealth": float(curve[-1])}
    return {"period": [equity.date.iloc[0], equity.date.iloc[-1]], "threshold": threshold,
            "one_way_cost_bps": cost_bps, "cash_nominal_return": 0,
            "strategy": stats("strategy_wealth"), "buy_and_hold": stats("benchmark_wealth"),
            "position_changes": int(changes), "entries": int(entries),
            "invested_month_fraction": float(equity.position_after_trade.mean()),
            "insufficient_trades_for_strategy_validation": bool(entries < 20)}, equity


def write_report(metrics: dict, source: dict, output: Path) -> None:
    score = metrics["forecast_scoring"]
    trade = metrics["strategy_diagnostic"]
    lines = ["# 历史验证：指数级代理，不是完整选股回测", "",
             "**结论：该实验不能证明标普500个股、机构共识或AI事件调整策略可靠。它用真实历史数据检验生产模型的纯历史回退分支，并检查固定阈值在指数级代理上的行为。**", "",
             "![历史指数代理：买入概率和净值诊断](historical_summary.png)", "",
             f"数据来自 [Robert Shiller 官方页面]({SOURCE_PAGE}) 的 [ie_data.xls]({source['url']})。下载时间：{source['downloaded_at']}；SHA-256：`{source['sha256']}`。", "",
             "采用1926年以后历史盈利、1957年以后每年12月预测；只使用预测年前一年12月及更早的盈利，并假定次年6月底可获得（保守约一年滞后）。六年终点是预测年+6的12月。机构预测为空；没有使用或生成历史AI事件判断。输入是指数点数，不是某家公司的EPS。", "",
             "价格为12月月均价；盈利为四季合计及插值序列；工作簿为现行修订版本，不含当时版本。因此虽然计算严格截断未来记录，不能消除修订和插值信息污染。只保留截至2025年12月的完整年度，排除2026年暂估数据。", "",
             "模型参数使用程序默认值，没有用测试结果优化。概率基准只使用已成熟六年结果并额外等待一年，至少10个成熟标签；基准为加一平滑的历史事件率。六年窗口重叠，95%区间使用长度6的循环移动块bootstrap（2000次），不是独立样本置信区间；仅少量历史块、分布变化和修订数据会削弱区间可信度。", "",
             f"共同评分期：{score['evaluation_years'][0]}–{score['evaluation_years'][1]}；{score['scored_forecasts']}次年度预测，约{score['approx_independent_six_year_blocks']}个互不重叠六年块。", "",
             "| 六年结果事件 | 次数 | 模型Brier（越低越好） | 因果历史频率基准 | 模型减基准的95%块bootstrap区间 |",
             "|---|---:|---:|---:|---|" ]
    for key, label in [("below_6", "0 < P₀/EPS₆ < 6"), ("above_6_5", "P₀/EPS₆ > 6.5，且EPS₆ > 0")]:
        s = score[key]
        ci = s["brier_difference_95_moving_block_ci"]
        lines.append(f"| {label} | {s['events']} | {s['brier']:.4f} | {s['causal_base_rate_brier']:.4f} | [{ci[0]:.4f}, {ci[1]:.4f}] |")
    lines += ["", f"正盈利条件下90% EPS区间覆盖率：{score['conditional_positive_eps_90_interval_coverage']:.1%}。指数历史盈利均为正，不能检验个股亏损/退市概率。各概率分箱的实际发生率和六种非重叠抽样结果见 `historical_metrics.json`；小样本分箱不支持概率校准结论。", "",
              "## 简化交易诊断", "",
              "每年12月信号，次年1月月均价代理成交；持仓从1月至2月的收益区间开始生效，避免用信号形成当月收益。买入概率≥70%进入指数；高估概率≥70%退出，之间保持持仓。初始现金，现金收益0；每次单向换仓收10基点成本。使用原始工作簿实际提供的实质总回报指数乘CPI还原名义总回报比例。没有融资、卖空或调参。月均价不能实际执行，现金0收益也不是真实货币市场收益，所以以下是机制诊断。", "",
              f"期间：{trade['period'][0]} 至 {trade['period'][1]}。", "",
              "| 指标 | 阈值策略 | 指数买入持有 |", "|---|---:|---:|",
              f"| 年化收益 | {trade['strategy']['cagr']:.2%} | {trade['buy_and_hold']['cagr']:.2%} |",
              f"| 最大回撤 | {trade['strategy']['max_drawdown']:.2%} | {trade['buy_and_hold']['max_drawdown']:.2%} |",
              f"| 期末净值（初始1） | {trade['strategy']['terminal_wealth']:.2f} | {trade['buy_and_hold']['terminal_wealth']:.2f} |", "",
              f"开仓{trade['entries']}次，持仓变化{trade['position_changes']}次；持股月占比{trade['invested_month_fraction']:.1%}。所有年度买入概率最高仅{score['maximum_buy_probability_all_origins']:.1%}。**零成交并不是低风险策略获胜，而是这套阈值在本指数代理上没有产生可验证的买卖。**", "",
              "此处为阈值机制诊断：主动允许历史回退结果参与交易，并统一买卖门槛70%；正式程序默认禁用缺少共识的候选，且默认卖出门槛80%，因此不能把诊断称为默认生产策略回测。60%/70%/80%门槛、0/10/25基点成本，以及年度波动下限15%/20%/30%、长期对数增长2%/4%/6%的预设敏感性结果均保留在JSON；不能挑选最佳组合后作为样本外证据。", "",
              "## 不能由本报告推出的结论", "",
              "没有验证500只股票的横截面排序、当期成分股/退市收益、机构共识历史快照、AI事件调整的增量收益、行业风险或真实可交易收益。指数聚合会大幅平滑个股风险，而且成分变更使指数盈利不等同于一直持有一组固定公司。完整验证需要点时成分、原始公告版本、历史共识、拆股/分红/退市数据，以及只在训练窗内估计的误差和相关性。将AI分析应用到过去还会受到语言模型训练记忆的污染，应优先做冻结版本的前瞻纸面测试。", "",
              "现有证据只支持将工具作为研究筛选器；概率仍是模型输出。固定6/6.5阈值与收益之间并没有定义上的必然关系，也不保证未来股价或退出倍数。", "",
              "复现：`python scripts/validate_history.py`（使用缓存）或 `python scripts/validate_history.py --download`（重新下载，URL版本变更需审阅）。"]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_figure(predictions: pd.DataFrame, equity: pd.DataFrame, metrics: dict, output: Path) -> None:
    """Static research figure from the same saved experiment; no numerical changes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter, FuncFormatter

    ink, blue, orange, grid = "#142437", "#256c9c", "#c8622c", "#dde4eb"
    trade = metrics["strategy_diagnostic"]
    entries = trade["entries"]
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.labelcolor": ink, "text.color": ink,
                         "xtick.color": ink, "ytick.color": ink,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.facecolor": "white"}):
        fig, (left, right) = plt.subplots(1, 2, figsize=(13.4, 5.8))
        fig.subplots_adjust(left=.065, right=.98, top=.76, bottom=.22, wspace=.25)
        headline = "the buy threshold never fired" if entries == 0 else f"{entries} entries under fixed thresholds"
        fig.text(.065, .93, f"S&P composite proxy: {headline}", fontsize=19, weight="bold")
        fig.text(.065, .87, "Real historical data  |  production model, history-only fallback  |  no analyst or AI validation", fontsize=11, color="#556677")
        left.plot(predictions.year, predictions.p_below_6, color=blue, linewidth=2, label="Model probability: EPS in 6 years > price / 6")
        left.axhline(.7, color=orange, linewidth=1.6, linestyle="--", label="70% entry threshold")
        peak = predictions.loc[predictions.p_below_6.idxmax()]
        left.scatter([peak.year], [peak.p_below_6], s=35, color=blue, zorder=3)
        left.annotate(f"Maximum: {peak.p_below_6:.1%}", xy=(peak.year, peak.p_below_6),
                      xytext=(peak.year+4, peak.p_below_6+.13), fontsize=10,
                      arrowprops={"arrowstyle": "-", "color": blue})
        left.set(title="Annual buy probability", xlabel="Forecast year", ylabel="Uncalibrated model probability",
                 xlim=(1957, 2026), ylim=(0, .85))
        left.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
        left.legend(loc="upper left", frameon=False, fontsize=8)
        left.grid(axis="y", color=grid, linewidth=.7)

        dates = pd.to_datetime(equity.date)
        right.plot(dates, equity.benchmark_wealth, color=blue, linewidth=2, label="Index buy and hold")
        right.plot(dates, equity.strategy_wealth, color=orange, linewidth=2, label=f"Threshold diagnostic: {entries} entries")
        right.set_yscale("log")
        right.set(title="Nominal total-return wealth", xlabel="Year", ylabel="Initial capital = 1  (log scale)", ylim=(.75, 2200))
        right.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}" if value >= 1 else f"{value:g}"))
        right.grid(axis="y", which="major", color=grid, linewidth=.7)
        right.legend(loc="upper left", frameon=False, fontsize=9)
        cagr = trade["buy_and_hold"]["cagr"]
        right.text(.98, .13, f"Index CAGR: {cagr:.2%}\nStrategy CAGR: {trade['strategy']['cagr']:.2%}", transform=right.transAxes,
                   ha="right", va="bottom", fontsize=10, color=ink)
        fig.text(.065, .11, "Source: Shiller's revised-vintage dataset. Monthly average prices are a trading proxy; cash return is assumed 0%.", fontsize=9, color="#556677")
        conclusion = "Zero entries means there is no evidence of tradable strategy performance." if entries == 0 else "This proxy does not establish tradable stock-selection performance."
        fig.text(.065, .065, f"1958–2025 wealth; 10 bps one-way trading cost. {conclusion}", fontsize=9, color="#556677")
        fig.savefig(output, dpi=180)
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--input", type=Path, default=ROOT / "data/raw/shiller_ie_data.xls")
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    if args.download or not args.input.exists():
        download(args.input)
    args.output.mkdir(parents=True, exist_ok=True)
    source = json.loads(args.input.with_name("shiller_manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(args.input.read_bytes()).hexdigest() != source["sha256"]:
        raise ValueError("Input checksum does not match source manifest")
    monthly = load_monthly(args.input)
    predictions = produce_predictions(monthly)
    scoring, labeled = score_predictions(predictions)
    trade, equity = strategy(monthly, predictions)
    sensitivity = [strategy(monthly, predictions, threshold=t, cost_bps=c)[0]
                   for t in [.6, .7, .8] for c in [0, 10, 25]]
    parameter_sensitivity = []
    for sigma in [.15, .20, .30]:
        for growth in [.02, .04, .06]:
            alternate = produce_predictions(monthly, config={"min_annual_log_sigma": sigma, "long_run_log_growth": growth})
            alt_score, _ = score_predictions(alternate)
            alt_trade, _ = strategy(monthly, alternate)
            parameter_sensitivity.append({"min_annual_log_sigma": sigma, "long_run_log_growth": growth,
                                          "below_6_brier": alt_score["below_6"]["brier"],
                                          "above_6_5_brier": alt_score["above_6_5"]["brier"],
                                          "coverage90": alt_score["conditional_positive_eps_90_interval_coverage"],
                                          "maximum_buy_probability": alt_score["maximum_buy_probability_all_origins"],
                                          "entries": alt_trade["entries"], "strategy_cagr": alt_trade["strategy"]["cagr"]})
    metrics = {"scope": "revised-vintage aggregate-index history-only proxy; NOT full stock/consensus/AI validation",
               "model": "stocking.model production default historical-only branch", "source": source,
               "model_sha256": hashlib.sha256((ROOT / "src/stocking/model.py").read_bytes()).hexdigest(),
               "forecast_scoring": scoring, "strategy_diagnostic": trade, "sensitivity": sensitivity,
               "parameter_sensitivity": parameter_sensitivity}
    (args.output / "historical_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    predictions.to_csv(args.output / "historical_predictions.csv", index=False)
    labeled.to_csv(args.output / "historical_scored_predictions.csv", index=False)
    equity.to_csv(args.output / "historical_equity.csv", index=False)
    write_figure(predictions, equity, metrics, args.output / "historical_summary.png")
    write_report(metrics, source, args.output / "historical_validation.md")
    print(json.dumps({"forecast_scoring": scoring, "strategy_diagnostic": trade}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
