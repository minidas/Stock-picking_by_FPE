"""Run one separately evidenced NVDA case, without changing the frozen index run."""
from __future__ import annotations

from argparse import Namespace
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.cli import screen
from stocking.data import validate_snapshot
from stocking.risk import DEFAULT_RISK_POLICY
from stocking.ttm import prepare_ttm_snapshot

CASE = ROOT / "outputs/current_sp500_2026-11-06/case_studies/NVDA_2026-11-07"
# Frozen case reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Evidence observation timestamp requires timezone")
    return timestamp.astimezone(timezone.utc)


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def evidence_paths(review, cutoff):
    paths = []
    if not review.get("evidence_files"):
        raise ValueError("Reviewed case requires archived evidence_files")
    for source in review["evidence_files"]:
        path = (ROOT / source.get("path", source.get("file", ""))).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file() or sha(path) != source.get("sha256"):
            raise ValueError("Reviewed evidence hash/path mismatch: " + str(path))
        if source.get("retrieved_at") and utc(source["retrieved_at"]) > cutoff:
            raise ValueError("Evidence was retrieved after information cutoff")
        paths.append(path)
    return paths


def assemble_snapshot(review, information_cutoff):
    cutoff = utc(information_cutoff)
    as_of = review["as_of"]
    if review["symbol"] != "NVDA" or str(review["cik"]).zfill(10) != "0001045810":
        raise ValueError("This isolated case requires NVIDIA's reviewed identity")
    if cutoff.date().isoformat() != as_of or utc(review["information_cutoff"]) > cutoff:
        raise ValueError("Decision cutoff must cover all reviewed information on the same UTC date")
    basis = review.get("share_basis", "NVDA_reviewed_post_2024_split_research_basis_2026-11-07")
    accounting = review.get("eps_basis", "GAAP_diluted")
    if accounting != "GAAP_diluted" or review.get("currency", "USD") != "USD":
        raise ValueError("Case requires USD GAAP diluted EPS")
    price = review["price"]
    source = deepcopy(price["source"])
    if price.get("retrieved_at") and utc(price["retrieved_at"]) > cutoff:
        raise ValueError("Price observation follows decision cutoff")
    history = deepcopy(review["annual_history"])
    for row in history:
        if row.get("retrieved_at") and utc(row["retrieved_at"]) > cutoff:
            raise ValueError("Annual observation follows decision cutoff")
        row.update(eps_basis=accounting, share_basis=basis, currency="USD", measure="annual")
    forecasts = deepcopy(review["consensus"])
    for row in forecasts:
        if utc(row["retrieved_at"]) > cutoff:
            raise ValueError("Forecast follows decision cutoff")
        if not row.get("as_reported_target_date") or not row.get("original_staged_available_at"):
            raise ValueError("Forecast requires its original staged date and reported target")
        row.update(available_at=utc(row["retrieved_at"]).date().isoformat(), eps_basis=accounting,
                   share_basis=basis, currency="USD", eps_std=None,
                   information_cutoff=information_cutoff, availability_policy="observed_by_decision_timestamp",
                   full_economic_basis_verified=False)
        if row["target_date"] != row["as_reported_target_date"] and not review.get("fiscal_calendar_evidence"):
            raise ValueError("Non-identical future fiscal dates require reviewed calendar evidence")
    snapshot = {"symbol": "NVDA", "company_name": "NVIDIA Corporation", "cik": "0001045810",
                "asset_type": "stock", "as_of": as_of, "information_cutoff": information_cutoff,
                "currency": "USD", "eps_basis": accounting, "share_basis": basis,
                "share_basis_verified": False, "full_economic_basis_verified": False,
                "price": price["price"], "price_date": price["price_date"],
                "price_kind": price.get("price_kind", "reviewed_yahoo_regular_market_price_completed_session"),
                "history": history, "consensus": forecasts,
                "source": {"price": source, "official_financial_evidence": deepcopy(review["evidence_files"]),
                           "fiscal_calendar_evidence": deepcopy(review.get("fiscal_calendar_evidence"))},
                "case_scope": "independent_single_security_case_not_a_revised_index_ranking",
                "limitations": deepcopy(review.get("limitations", []))}
    anchor = deepcopy(review["ttm"])
    anchor.update(symbol="NVDA", cik="0001045810", currency="USD", eps_basis=accounting, share_basis=basis)
    if anchor.get("method") != "four_quarter_diluted_eps_sum" or len(anchor.get("components", [])) != 4:
        raise ValueError("Case requires four actual contiguous quarterly EPS observations")
    for component in anchor["components"]:
        if component.get("retrieved_at") and utc(component["retrieved_at"]) > cutoff:
            raise ValueError("Quarterly observation follows cutoff")
        component.update(eps_basis=accounting, share_basis=basis, currency="USD")
    if not math.isclose(anchor["eps"], math.fsum(c["eps"] for c in anchor["components"]), abs_tol=1e-10):
        raise ValueError("Quarterly EPS sum does not equal the declared TTM")
    snapshot = prepare_ttm_snapshot(snapshot, anchor)
    return validate_snapshot(snapshot)


def derived_numbers(snapshot, model, prediction):
    threshold = snapshot["price"] / 6
    multiplier = threshold / snapshot["eps0"]
    endpoint = date.fromisoformat(model["target_date"])
    ttm_end, as_of = date.fromisoformat(snapshot["eps_period_end"]), date.fromisoformat(snapshot["as_of"])
    total_years = (endpoint - ttm_end).days / 365.2425
    result = {"eps_threshold_for_fixed_purchase_price_pe_below_6": threshold,
              "required_terminal_eps_multiple_of_ttm": multiplier, "nominal_six_year_required_cagr": multiplier ** (1 / 6) - 1,
              "target_date": endpoint.isoformat(), "ttm_end_to_decision_bridge_days": (as_of - ttm_end).days,
              "ttm_end_to_target_years": total_years, "required_cagr_over_model_ttm_span": multiplier ** (1 / total_years) - 1,
              "current_price_over_ttm_eps": snapshot["price"] / snapshot["eps0"],
              "fixed_growth_scenarios": [{"ordinary_annual_growth": growth, "years": 6,
                  "terminal_eps": snapshot["eps0"] * (1 + growth) ** 6,
                  "fixed_purchase_price_over_terminal_eps": snapshot["price"] / (snapshot["eps0"] * (1 + growth) ** 6)}
                  for growth in (.10, .15, .20, .25, .30)],
              "fixed_growth_scenarios_policy": "Pure deterministic teaching examples: six nominal years, no bridge, no assigned probability, never model inputs."}
    if model["status"] == "ready":
        sigma = math.sqrt(model["terminal_log_eps_variance"])
        z = (math.log(threshold) - model["terminal_log_eps_mean"]) / sigma
        result.update(terminal_log_standard_deviation=sigma, threshold_standard_normal_z=z,
                      positive_conditional_probability_of_eps_above_threshold=.5 * math.erfc(z / math.sqrt(2)),
                      terminal_log_eps_mean=model["terminal_log_eps_mean"], terminal_log_eps_variance=model["terminal_log_eps_variance"])
    return result


def main(case_dir=CASE):
    case_dir = Path(case_dir).resolve()
    if not case_dir.is_relative_to(ROOT / "outputs/current_sp500_2026-11-06/case_studies"):
        raise ValueError("Single-stock output must stay in the isolated case_studies directory")
    if any((case_dir / name).exists() for name in ("case_summary.json", "adjustment_log.jsonl", "report.md", "baseline", "input")):
        raise ValueError("Case artifacts already exist; preserve them and use a separately reviewed revision")
    reviewed_path = case_dir / "reviewed_inputs.json"
    review = read(reviewed_path)
    cutoff = datetime.now(timezone.utc)
    files = evidence_paths(review, cutoff)
    snapshot = assemble_snapshot(review, cutoff.isoformat())
    input_path, risk_path = case_dir / "input/snapshots.json", case_dir / "input/risk_policy.json"
    write_new(input_path, {"snapshots": [snapshot]})
    write_new(risk_path, DEFAULT_RISK_POLICY)
    policy = {"scope": "independent_NVDA_case_not_full_universe_reranking", "as_of": snapshot["as_of"],
              "information_cutoff": cutoff.isoformat(), "availability_policy": "observed_by_decision_timestamp",
              "original_staged_consensus_availability_preserved": True, "full_economic_basis_verified": False,
              "subjective_news_parameter_adjustments": 0, "old_v5_outputs_modified": False,
              "financial_changes": "Official actual quarters, recent annual EPS, and evidenced fiscal-calendar target mapping only."}
    policy_path = case_dir / "input/policy.json"
    write_new(policy_path, policy)
    raw_paths = list(dict.fromkeys(files + [reviewed_path, Path(__file__).resolve(), policy_path]))
    screen(Namespace(input=str(input_path), universe=None, config=str(LEGACY_CONFIG_PATH), risk_policy=str(risk_path),
                     exclude_loss_probability=None, output=str(case_dir / "baseline"), mode="research",
                     archive_root=str(case_dir / "run_archive"), retention_days=7,
                     raw_input=[str(path) for path in raw_paths]))
    baseline_path = case_dir / "baseline/baseline.json"
    baseline = read(baseline_path)
    row = baseline["results"][0]
    if "model" not in row:
        raise ValueError("Case model failed; inspect frozen baseline diagnostics")
    model, prediction = row["model"], row["prediction"]
    derived = derived_numbers(snapshot, model, prediction)
    summary = {"schema_version": 1, **policy, "symbol": "NVDA", "price": snapshot["price"], "price_date": snapshot["price_date"],
               "eps0": snapshot["eps0"], "ttm_period_start": snapshot["eps_period_start"], "ttm_period_end": snapshot["eps_period_end"],
               "ttm_method": snapshot["ttm_provenance"]["method"], "quarterly_components": snapshot["ttm_provenance"]["components"],
               "annual_history_count": len(snapshot["history"]), "annual_history": snapshot["history"],
               "consensus": snapshot["consensus"], "risk_policy": DEFAULT_RISK_POLICY, "risk_screen": model.get("risk_screen"),
               "model_parameters": {key: model.get(key) for key in ("model_version", "history_growth_observations", "historical_log_growth_center",
                   "annual_log_sigma", "consensus_anchors", "interval_years", "annual_log_growth_mean", "annual_log_growth_covariance", "config")},
               "prediction": prediction, "derived": derived, "event_review": row["event_review"],
               "candidate_is_not_an_order_or_completed_review": True,
               "baseline_sha256": sha(baseline_path), "reviewed_inputs_sha256": sha(reviewed_path), "run_archive": baseline["run_archive"],
               "limitations": list(dict.fromkeys(review.get("limitations", []) + [
                   "Only four annual observations, not the desired eleven-year history; three historical growth rates.",
                   "Quarterly diluted EPS sum is an approximate TTM, not a recomputed annual GAAP weighted-share denominator.",
                   "Explicit GAAP and reviewed calendar evidence do not constitute complete share-class/economic-basis certification.",
                   "GAAP earnings can contain substantial unrealized equity investment gains; their persistence is not separately modelled.",
                   "No non-GAAP substitution or discretionary subtraction from forecasts was made.",
                   "Terminal probabilities are uncalibrated research outputs and do not predict future share prices.",
                   "This single-stock case does not revise the frozen v5 ranking."]))}
    write_new(case_dir / "case_summary.json", summary)
    events = [
        {"event_id": "nvda_official_quarterly_ttm_admission", "before": {"v5_status": "data_unavailable", "case_ttm": None},
         "after": {"eps0": snapshot["eps0"], "period_start": snapshot["eps_period_start"], "period_end": snapshot["eps_period_end"],
                   "quarter_eps": [r["eps"] for r in snapshot["ttm_provenance"]["components"]]},
         "rationale": "Four official contiguous quarterly GAAP diluted EPS disclosures support a separately labelled sum-based TTM case."},
        {"event_id": "nvda_recent_annual_and_fiscal_calendar_mapping", "before": {"v5_input_included": False},
         "after": {"annual_observations": len(snapshot["history"]), "forecast_date_mappings": [
             {"as_reported": r["as_reported_target_date"], "used": r["target_date"]} for r in snapshot["consensus"]]},
         "rationale": "Use reviewed official annual share units and explicit 52/53-week fiscal rules; do not infer future fiscal dates from month ends."},
        {"event_id": "nvda_new_observed_information_policy", "before": {"staged_available_at": [r["original_staged_available_at"] for r in snapshot["consensus"]]},
         "after": {"information_cutoff": cutoff.isoformat(), "available_at": [r["available_at"] for r in snapshot["consensus"]]},
         "rationale": "New user-requested case observes information by its actual timestamp; does not backfill the previous frozen run."},
    ]
    with (case_dir / "adjustment_log.jsonl").open("x", encoding="utf-8") as handle:
        for event in events:
            record = {**event, "symbol": "NVDA", "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "information_cutoff": cutoff.isoformat(), "type": "data_reconciliation_not_subjective_forecast_adjustment",
                      "annual_log_growth_delta": [0.0] * 6, "variance_multiplier": 1.0, "loss_probability_delta": 0.0,
                      "subjective_news_adjustment": False, "reviewed_inputs_sha256": sha(reviewed_path),
                      "source_files": [{"path": str(path), "sha256": sha(path)} for path in files]}
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    p = prediction.get("p_below_6")
    lines = ["# 英伟达独立计算案例", "", "本案例单独补录官方季度与财年日期证据；没有重排或改写此前 v5 全榜。", "",
             f"决策日：{snapshot['as_of']}；实际信息截止：{cutoff.isoformat()}。股价采用 {snapshot['price_date']} 的 {snapshot['price']:.2f} 美元。", "",
             f"四季 EPS 合计 {snapshot['eps0']:.2f}，覆盖 {snapshot['eps_period_start']} 至 {snapshot['eps_period_end']}。这是季度稀释EPS求和的近似TTM。", "",
             f"以当前买入价计算，六年后 PE＜6 要求 EPS＞{derived['eps_threshold_for_fixed_purchase_price_pe_below_6']:.4f}，是当前TTM的 {derived['required_terminal_eps_multiple_of_ttm']:.4f} 倍。", "",
             f"名义六年所需复合增长为 {derived['nominal_six_year_required_cagr']:.2%}；模型另含TTM期末到决策日的 {derived['ttm_end_to_decision_bridge_days']} 天桥接。", "",
             "| 固定普通年增长 | 六年EPS | 当前价格/六年EPS |", "|---:|---:|---:|"]
    lines.extend(f"| {r['ordinary_annual_growth']:.0%} | {r['terminal_eps']:.4f} | {r['fixed_purchase_price_over_terminal_eps']:.4f} |" for r in derived["fixed_growth_scenarios"])
    lines.extend(["", "上述为纯确定性教学情景，不计入模型，也未分配概率。", "",
                  f"模型 PE＜6 概率：{p:.2%}。" if p is not None else "模型风险门槛未通过，不计算估值概率。",
                  f"候选标记：`{prediction.get('candidate')}`；事件复核：`{row['event_review']}`。没有主观新闻参数调整，也不代表已完成买入或卖出审阅。", "",
                  "只有4个年度而非11年；股份和经济口径完整认证仍为false。GAAP利润中的投资公允价值收益可能缺乏持续性，本案例未擅自删除此类收益或替换成non-GAAP。", "",
                  "详细来源、日期映射、风险政策和概率见 `reviewed_inputs.json`、`case_summary.json` 与 `baseline/baseline.json`；数据处理日志见 `adjustment_log.jsonl`。", ""])
    (case_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    outputs = [path for path in (case_dir / "case_summary.json", case_dir / "adjustment_log.jsonl", case_dir / "report.md", input_path, risk_path, policy_path, baseline_path,
                                case_dir / "baseline/agent_queue.json")]
    write_new(case_dir / "delivery_manifest.json", {"information_cutoff": cutoff.isoformat(), "run_archive": baseline["run_archive"],
              "files": [{"path": str(path.relative_to(case_dir)), "sha256": sha(path)} for path in outputs],
              "source_files": [{"path": str(path), "sha256": sha(path)} for path in raw_paths]})
    print(json.dumps({"case": str(case_dir), "p_below_6": p, "p_loss": prediction.get("p_loss"),
                      "candidate": prediction.get("candidate"), "event_review": row["event_review"],
                      "six_year_required_eps": derived["eps_threshold_for_fixed_purchase_price_pe_below_6"],
                      "nominal_six_year_required_cagr": derived["nominal_six_year_required_cagr"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", type=Path, default=CASE)
    args = parser.parse_args()
    main(args.case_dir)
