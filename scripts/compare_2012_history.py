"""Compare saved original/expanded history runs without using outcome labels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_run(directory):
    rows, sources = {}, {}
    for stem in ("all_stocks", "excluded_stocks"):
        path = directory / f"{stem}.json"
        for row in json.loads(path.read_text(encoding="utf-8"))["stocks"]:
            if row["symbol"] in rows:
                raise ValueError("Duplicate symbol across retained and excluded outputs")
            rows[row["symbol"]] = row
        sources[stem] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return rows, sources


def compact(row):
    model = row.get("model") or {}
    return {"status": row["test_status"], "eps0": row["input"].get("eps0"),
            "annual_observations": len(row["input"].get("history", [])),
            "growth_observations": model.get("history_growth_observations"),
            "historical_log_growth_center": model.get("historical_log_growth_center"),
            "annual_log_sigma": model.get("annual_log_sigma"),
            "loss_probability": row.get("loss_probability"),
            "p_below_6": next((s["p_below_threshold"] for s in row["threshold_scenarios"] if s["buy_fpe_threshold"] == 6), None),
            "p_below_8_5": next((s["p_below_threshold"] for s in row["threshold_scenarios"] if s["buy_fpe_threshold"] == 8.5), None)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/history_expansion_impact.json")
    args = parser.parse_args()
    modes = {}
    for mode, original, expanded in (
        ("ttm", "stocks_2012_ttm_probability_test", "stocks_2012_ttm_history_expanded"),
        ("annual_comparison_only", "stocks_2012_probability_test", "stocks_2012_annual_history_expanded"),
    ):
        before, before_sources = read_run(ROOT / "outputs" / original)
        after, after_sources = read_run(ROOT / "outputs" / expanded)
        if set(before) != set(after):
            raise ValueError("Original and expanded runs must have identical rosters")
        comparisons = []
        for symbol in sorted(before):
            old, new = before[symbol], after[symbol]
            for field in ("as_of", "cik"):
                if old[field] != new[field]:
                    raise ValueError(f"Changed identity/date: {symbol}")
            for field in ("price", "price_date", "eps0", "eps_period_end", "eps_available_at"):
                if old["input"].get(field) != new["input"].get(field):
                    raise ValueError(f"Changed non-history input {field}: {symbol}")
            if old.get("model") and new.get("model"):
                old_config, new_config = dict(old["model"]["config"]), dict(new["model"]["config"])
                # The archived annual run predates TTM support. This added
                # freshness setting is not used by either annual calculation.
                if mode == "annual_comparison_only":
                    old_config.pop("max_ttm_age_days", None)
                    new_config.pop("max_ttm_age_days", None)
                if old_config != new_config:
                    raise ValueError(f"Changed model configuration: {symbol}")
            comparisons.append({"symbol": symbol, "before": compact(old), "after": compact(new)})
        modes[mode] = {"source_files": {"before": before_sources, "after": after_sources},
                       "roster_count": len(after), "stocks": comparisons}
    payload = {"as_of": "2012-12-31", "scope": "input_history_sensitivity_not_outcome_validation",
               "model_parameters_changed": False, "ttm_anchor_changed": False, "realized_outcomes_used": False,
               "configuration_comparison_note": "The archived annual run lacks max_ttm_age_days; this TTM-only freshness setting is irrelevant to both annual runs and excluded from their config comparison.",
               "modes": modes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    clf = next(x for x in modes["ttm"]["stocks"] if x["symbol"] == "CLF")
    lines = ["# 历史补样本前后对照", "", "保持价格、TTM起点和模型参数相同，仅扩展可用年度历史。", "",
             "| CLF项目 | 原3年历史 | 新11年历史 |", "|---|---:|---:|"]
    for label, field, percent in (("有效增长率", "growth_observations", False),
                                  ("历史增长中心（对数）", "historical_log_growth_center", True),
                                  ("年度对数波动", "annual_log_sigma", True),
                                  ("终点亏损先验", "loss_probability", True),
                                  ("阈值6概率", "p_below_6", True),
                                  ("阈值8.5概率", "p_below_8_5", True)):
        a, b = clf["before"][field], clf["after"][field]
        lines.append(f"| {label} | {a:.2%} | {b:.2%} |" if percent else f"| {label} | {a} | {b} |")
    lines += ["", "CLF仍保留测试：17.14%低于本轮25%风险排除线，但高于原买入研究条件的10%上限。"
              "模型概率仍未经校准；这次变化说明样本窗口影响很大，不证明能够预测股价长期贬值。", "",
              "完整JSON包含500只的原历史/新历史对照。年度模式用于研究历史扩展影响，不冒充缺失的全名单TTM结果。", ""]
    args.output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(clf, ensure_ascii=True))


if __name__ == "__main__":
    main()
