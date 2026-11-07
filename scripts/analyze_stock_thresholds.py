"""Reproduce stock-only threshold sensitivity, without fitting or outcome labels."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import platform
import sys

import numpy as np
import scipy
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import load_json, load_snapshots
from stocking.model import DEFAULT_CONFIG, build_baseline, evaluate

# Frozen sensitivity experiment; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"
LEGACY_MODEL_CONFIG = {**DEFAULT_CONFIG, **load_json(LEGACY_CONFIG_PATH)}
THRESHOLDS = (6.0, 8.5, 10.0, 12.0, 16.0)
REQUIRED_RETURN = 0.10


def minimum_threshold(model: dict, probability: float = 0.70) -> dict:
    """Invert the unconditional positive-earnings event, not its conditional CDF.

    This is the boundary for the probability condition alone. Loss and
    institutional-consensus gates remain independent requirements.
    """
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("probability must be finite and strictly between zero and one")
    if model["status"] != "ready":
        return {"threshold": None, "status": "model_abstained"}
    positive = 1 - model["loss_probability"]
    if probability >= positive:
        return {"threshold": None, "status": "unattainable_at_finite_threshold"}
    quantile = norm.ppf(1 - probability / positive)
    log_threshold = (math.log(model["price"]) - model["terminal_log_eps_mean"]
                     - math.sqrt(model["terminal_log_eps_variance"]) * quantile)
    try:
        threshold = math.exp(log_threshold)
    except OverflowError:
        return {"threshold": None, "status": "outside_numeric_range"}
    if threshold == 0:
        return {"threshold": None, "status": "outside_numeric_range"}
    return {"threshold": threshold, "status": "finite_probability_boundary"}


def threshold_diagnostics(model: dict, threshold: float) -> dict:
    """Evaluate a buy cutoff while retaining the existing consensus gate."""
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("threshold must be finite and positive")
    scenario = deepcopy(model)
    # A valid threshold pair is required. The auxiliary sell cutoff is NOT a
    # recommendation or a sell-strategy experiment; only buy eligibility is used.
    overrides = {"buy_fpe_threshold": threshold,
                 "sell_fpe_threshold": max(model["config"]["sell_fpe_threshold"], threshold + 0.5)}
    scenario["config"].update(overrides)
    result = evaluate(scenario)
    probability = result["p_below_buy_threshold"]
    loss = result["p_loss"]
    probability_pass = probability is not None and probability >= model["config"]["buy_probability"]
    loss_pass = loss is not None and loss <= model["config"]["max_buy_loss_probability"]
    consensus_gate = bool(model.get("consensus_anchors")) or model["config"]["allow_history_only_candidates"]
    return {
        "buy_fpe_threshold": threshold,
        "configuration_overrides": overrides,
        "p_below_threshold": probability,
        "p_loss": loss,
        "probability_condition_passes": probability_pass,
        "loss_condition_passes": loss_pass,
        "probability_and_loss_conditions_pass": probability_pass and loss_pass,
        "consensus_gate_passes": consensus_gate,
        "formal_buy_eligible": result["candidate"] == "buy_review",
        "evaluation": result,
    }


def analyze_stock(snapshot: dict, thresholds=THRESHOLDS) -> dict:
    """Keep the source snapshot and baseline alongside each sensitivity result."""
    if (snapshot.get("asset_type", "stock") != "stock"
            or str(snapshot["symbol"]).startswith("^")
            or snapshot.get("share_basis") == "index_points"):
        raise ValueError("Only individual-stock snapshots are supported")
    baseline = build_baseline(snapshot, LEGACY_MODEL_CONFIG)
    default_evaluation = evaluate(baseline)
    distribution, history = None, None
    if baseline["status"] == "ready":
        variance = baseline["terminal_log_eps_variance"]
        mean = default_evaluation["positive_conditional_eps_mean"]
        count = baseline["history_growth_observations"]
        weight = count / (count + baseline["config"]["history_shrinkage_years"])
        distribution = {
            "positive_conditional_eps_mean": mean,
            "positive_conditional_eps_median": default_evaluation["positive_conditional_eps_median"],
            "positive_conditional_eps_std": mean * math.sqrt(math.expm1(variance)),
            "terminal_log_eps_std": math.sqrt(variance),
            "positive_conditional_eps_interval_90": default_evaluation["positive_conditional_eps_interval_90"],
        }
        history = {
            "valid_growth_observations": count,
            "observed_growth_median_weight": weight,
            "long_run_prior_weight": 1 - weight,
            "long_run_log_growth": baseline["config"]["long_run_log_growth"],
            "historical_log_growth_center_after_shrinkage": baseline["historical_log_growth_center"],
            "annual_log_sigma": baseline["annual_log_sigma"],
        }
    return {
        "symbol": snapshot["symbol"],
        "input_snapshot": deepcopy(snapshot),
        "baseline": baseline,
        "default_evaluation": default_evaluation,
        "distribution_summary": distribution,
        "history_shrinkage": history,
        "minimum_threshold_for_buy_probability": minimum_threshold(baseline, baseline["config"]["buy_probability"]),
        "threshold_scenarios": [threshold_diagnostics(baseline, cutoff) for cutoff in thresholds],
    }


def economic_scenarios() -> list[dict]:
    return [{"terminal_pe_assumption": pe, "annual_price_return_assumption": rate,
             "six_year_fpe_threshold": pe / (1 + rate) ** 6}
            for pe in (12, 15, 18, 20) for rate in (0.08, 0.10, 0.12, 0.15)]


def make_report(source: Path) -> dict:
    rows = [analyze_stock(snapshot) for snapshot in load_snapshots(source)]
    return {
        "report_kind": "uncalibrated_stock_threshold_sensitivity",
        "sample_count": len(rows),
        "snapshots_with_consensus": sum(bool(row["baseline"].get("consensus_anchors")) for row in rows),
        "outcome_labels_used": 0,
        "limitations": [
            "Small manually checked stock sample; not the full 2012 constituent universe",
            "No historical institutional consensus in the default input",
            "Annual EPS rather than reconstructed latest-quarter TTM",
            "No realized six-year outcomes, return backtest, parameter fitting, or calibration",
            "Input snapshots and code hashes do not replace archived original provider documents",
            "Core model buy eligibility is shown; this script does not run export risk filters or AI review",
        ],
        "provenance": {
            "input_path": str(source.resolve()),
            "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "input_document": json.loads(source.read_text(encoding="utf-8-sig")),
            "model_path": "src/stocking/model.py",
            "model_sha256": hashlib.sha256((ROOT / "src/stocking/model.py").read_bytes()).hexdigest(),
            "script_path": "scripts/analyze_stock_thresholds.py",
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "legacy_thresholds_sha256": hashlib.sha256(LEGACY_CONFIG_PATH.read_bytes()).hexdigest(),
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "scipy_version": scipy.__version__,
        },
        "default_config": deepcopy(LEGACY_MODEL_CONFIG),
        "thresholds": list(THRESHOLDS),
        "auxiliary_sell_threshold_rule": "max(default sell threshold, buy threshold + 0.5); sell strategy not evaluated",
        "user_selected_annual_price_return": REQUIRED_RETURN,
        "economic_formula": "T = terminal_PE / (1 + annual_price_return)^6; excludes dividends, costs and taxes",
        "economic_scenarios": economic_scenarios(),
        "results": rows,
    }


def render_markdown(report: dict) -> str:
    def number(value, decimals=2):
        return "—" if value is None else f"{value:.{decimals}f}"

    def percent(value):
        return "—" if value is None else f"{value:.2%}"

    def yes_no(value):
        return "是" if value else "否"

    thresholds = report["thresholds"]
    rows = report["results"]
    lines = [
        "# 2012 个股阈值敏感性：研究假设，不是策略验证", "",
        f"本次输入 {report['sample_count']} 只个股，其中 {report['snapshots_with_consensus']} 只有有效机构共识；没有输入真实六年终点标签。"
        "默认文件为 AAPL、IBM、COST、STX 四只人工核对的年度 EPS 快照，不代表 500 只历史成分。",
        "",
        "决策日：" + "、".join(sorted({row["baseline"]["as_of"] for row in rows}))
        + "；六年目标日：" + "、".join(sorted({row["baseline"]["target_date"] for row in rows})) + "。",
        "",
        "指标是 `Pr(E6 > P0/K)`：当前股价除以六年后年度 EPS 低于估值阈值 K 的概率，"
        "不是 EPS 低于 K，也不是六年累计盈利或未来交易市盈率。所有数值都是尚未校准的模型概率。",
        "",
        "本实验只改变估值阈值，不拟合模型，不运行指数代理，不开启历史回退正式候选。"
        "没有真实终点标签，不能确定最优阈值、校准概率或检验机构权重与 AI 增益。", "",
        "## 同一分布下改变估值阈值", "",
        "| 股票 | " + " | ".join(f"P(比率<{cutoff:g})" for cutoff in thresholds) + " | 终点亏损概率 |",
        "|---|" + "---:|" * (len(thresholds) + 1),
    ]
    for row in rows:
        lines.append("| " + row["symbol"] + " | " + " | ".join(percent(s["p_below_threshold"]) for s in row["threshold_scenarios"])
                     + " | " + percent(row["default_evaluation"]["p_loss"]) + " |")
    lines += ["", "增大 K 会机械地提高该概率；概率更高本身不能证明投资选择更好。", "",
              "## 概率与亏损条件，以及独立的共识门禁", "",
              "下表的通过仅指买入概率≥70%且终点亏损概率≤10%。正式模型买入候选还需通过共识门禁。", "",
              "| 股票 | " + " | ".join(f"K={cutoff:g} 条件通过" for cutoff in thresholds)
              + " | 共识门禁通过 | 任一阈值正式买入合格 | 达到70%的最低K |",
              "|---|" + "---|" * (len(thresholds) + 2) + "---:|"]
    for row in rows:
        scans = row["threshold_scenarios"]
        boundary = row["minimum_threshold_for_buy_probability"]
        lines.append("| " + row["symbol"] + " | "
                     + " | ".join(yes_no(s["probability_and_loss_conditions_pass"]) for s in scans)
                     + " | " + yes_no(scans[0]["consensus_gate_passes"])
                     + " | " + yes_no(any(s["formal_buy_eligible"] for s in scans))
                     + " | " + number(boundary["threshold"]) + " |")
    lines += ["", "最低 K 只反解70%的概率条件，不能绕过亏损条件或共识门禁，也不是建议阈值。"
              "缺共识时正式候选为空，不表示估值条件不合格。门禁结论指核心模型，未执行主表排除、AI 复核或交易。",
              "",
              "为满足配置 `buy < sell`，每个场景的辅助卖出阈值为 `max(6.5, K+0.5)`。"
              "它只供调用评估函数，不评价或推荐卖出策略；JSON 的 `formal_buy_eligible` 单独记录买入资格。"
              "旧字段 `p_below_6` 始终保持固定6的含义。", "",
              "## 正盈利情景的终点分布与历史收缩", "",
              "| 股票 | EPS均值 | EPS中位数 | EPS标准差 | 终点log(EPS)标准差 | 有效历史增长数 | 历史中位数权重 | 收缩后年化对数增长 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        dist, hist = row["distribution_summary"], row["history_shrinkage"]
        if dist is None:
            lines.append(f"| {row['symbol']} | — | — | — | — | — | — | — |")
            continue
        lines.append(f"| {row['symbol']} | {number(dist['positive_conditional_eps_mean'])}"
                     f" | {number(dist['positive_conditional_eps_median'])} | {number(dist['positive_conditional_eps_std'])}"
                     f" | {number(dist['terminal_log_eps_std'], 3)} | {hist['valid_growth_observations']}"
                     f" | {percent(hist['observed_growth_median_weight'])}"
                     f" | {percent(hist['historical_log_growth_center_after_shrinkage'])} |")
    for row in rows:
        if "sparse_earnings_history" in row["baseline"].get("flags", []):
            hist = row["history_shrinkage"]
            lines += ["", f"{row['symbol']} 仅有 {hist['valid_growth_observations']} 条有效正盈利增长率，"
                      "被标记为 `sparse_earnings_history`。不足3条时，历史波动估计直接使用0.20下限，"
                      "机构分歧仍可提高它；这不是从稀疏历史数据中可靠估计出的波动。"]
    lines += ["", "EPS均值、中位数和标准差均条件于六年终点正盈利，不包含亏损金额。"
              "历史增长先按 `n/(n+6)` 向长期4%对数增长收缩；无共识时，后续增长仍以两年尺度回归该长期先验。"
              "模型还保留历史波动、跨年相关和持续增长误差，因此提高均值不能直接替代概率计算。", "",
              "## 用价格收益假设解释阈值", "",
              "若终点股价 `P6=M×E6`，忽略股息、费用和税，六年年化价格收益至少 r 对应 "
              "`P0/E6 ≤ M/(1+r)^6`。M 是假设的终点交易 PE；本表不是历史或当前市况事实，也没有给 M 赋予概率。", "",
              "| 假设终点PE M | r=8% | r=10%（用户选择） | r=12% | r=15% |",
              "|---|---:|---:|---:|---:|"]
    for pe in (12, 15, 18, 20):
        values = [s["six_year_fpe_threshold"] for s in report["economic_scenarios"] if s["terminal_pe_assumption"] == pe]
        lines.append(f"| {pe} | " + " | ".join(number(value) for value in values) + " |")
    lines += ["", "用户选择10%年化价格收益作为中间情景：若终点 PE 分别假设15和18，"
              "对应 K 约8.47和10.16。因此8.5至10可作为带明确经济假设的研究区间，不能称为已验证的最优参数。"
              "固定 K=6 也不构成公式错误，它表示更严格的估值/价格收益条件。"
              "若未来 PE 与盈利共同变化，本模型的 EPS 概率不能直接当作实际收益达标概率。", "",
              "## 复现与审计", "",
              "运行：`python scripts/analyze_stock_thresholds.py`。默认只读取 `data/research_2012_snapshots.json`，"
              "写出本报告及同名 JSON，不覆盖既有诊断报告。", "",
              "JSON 包含输入文档副本、每股快照、默认配置、基准模型、逐阈值完整评估、"
              "代码/输入 SHA256 和运行环境版本；没有删除或改写原始资料。"
              "这些归一化副本不能代替供应商原始文档归档。", ""]
    for key in ("input_sha256", "model_sha256", "script_sha256"):
        lines.append(f"- `{key}`: `{report['provenance'][key]}`")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/research_2012_snapshots.json")
    parser.add_argument("--output-prefix", type=Path, default=ROOT / "reports/stocks_2012_threshold_sensitivity")
    args = parser.parse_args()
    report = make_report(args.input)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output_prefix.with_suffix(".json")
    markdown_path = args.output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Wrote {json_path} and {markdown_path}; {report['sample_count']} stocks, no outcome labels")


if __name__ == "__main__":
    main()
