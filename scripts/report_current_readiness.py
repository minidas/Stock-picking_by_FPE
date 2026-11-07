"""Report current-study data readiness without constructing stock predictions.

Reads only this study's input/ and evidence/. Raw EPS observations remain raw:
provider asOfDate is not a public disclosure date or verified fiscal period end.
Rerun after acquisition advances. No data/ samples, model, or AI queue is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.export import PROBABILITY_FIELDS, write_exports

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
SERIES = {"annual": "annualDilutedEPS", "quarterly": "quarterlyDilutedEPS",
          "ttm": "trailingDilutedEPS"}
CSV_FIELDS = ["symbol", "company_name", "cik", "price", "price_date", "price_kind",
              "acquisition_status", "http_status", "raw_sha256", "raw_source_file",
              "raw_annual_count", "raw_quarterly_count", "raw_ttm_count",
              "raw_annual_distinct_period_count", "annual_history_target_count",
              "raw_annual_target_shortfall", "latest_annual_asOfDate",
              "latest_quarterly_asOfDate", "latest_ttm_asOfDate",
              "latest_annual_newer_than_raw_ttm", "future_raw_observation_count",
              "non_usd_raw_observation_count", "disclosure_dates_verified",
              "fiscal_period_bounds_verified", "share_basis_verified",
              "validated_ttm_ready", "validated_annual_history_count",
              "validated_consensus_count", "validated_model_ready", "missing_reasons"]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_manifest(raw: bytes) -> list[dict]:
    lines = raw.decode("utf-8").splitlines()
    result = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # A running sequential collector may be appending its final line.
            if index != len(lines) - 1:
                raise
    return result


def manifest_rows(path: Path) -> list[dict]:
    return parse_manifest(path.read_bytes()) if path.exists() else []


def evidence_path(study: Path, value: str) -> Path:
    path = ROOT / value
    if not path.resolve().is_relative_to((study / "evidence").resolve()):
        raise ValueError("Manifest path is not in this study's evidence directory")
    return path


def inspect_eps(study: Path, member: dict, records: list[dict], as_of: str) -> tuple[dict, dict]:
    symbol = member["symbol"]
    matched = [r for r in records if r.get("symbol") == symbol]
    successful = [r for r in matched if r.get("status") == 200 and r.get("file")]
    record = max(successful or matched, key=lambda r: r.get("retrieved_at", "")) if matched else None
    audit = {"symbol": symbol, "company_name": member["name"], "cik": member["cik"],
             "price": member.get("price"), "price_date": member.get("price_date"),
             "price_kind": member.get("price_kind"),
             "acquisition_status": "not_retrieved", "http_status": record.get("status") if record else None,
             "raw_sha256": record.get("sha256") if record else None,
             "raw_source_file": record.get("file") if record else None,
             "raw_annual_count": 0, "raw_quarterly_count": 0, "raw_ttm_count": 0,
             "raw_annual_distinct_period_count": 0, "annual_history_target_count": 11,
             "raw_annual_target_shortfall": 11,
             "latest_annual_asOfDate": None, "latest_quarterly_asOfDate": None, "latest_ttm_asOfDate": None,
             "latest_annual_newer_than_raw_ttm": False,
             "future_raw_observation_count": 0, "non_usd_raw_observation_count": 0,
             "disclosure_dates_verified": False, "fiscal_period_bounds_verified": False,
             "share_basis_verified": False, "validated_ttm_ready": False,
             "validated_annual_history_count": 0, "validated_consensus_count": 0,
             "validated_model_ready": False, "missing_reasons": []}
    observations = {name: [] for name in SERIES}
    reasons = []
    if record and record.get("status") != 200:
        audit["acquisition_status"] = "retrieval_failed"
        reasons.append("raw_eps_retrieval_failed")
    elif record:
        path = evidence_path(study, record["file"])
        if not path.exists() or digest(path) != record.get("sha256"):
            audit["acquisition_status"] = "archive_missing_or_hash_mismatch"
            reasons.append("raw_archive_integrity_not_verified")
        else:
            try:
                payload = read_json(path)
                timeseries = payload["timeseries"]
                results = timeseries.get("result")
                if timeseries.get("error") or not isinstance(results, list):
                    raise ValueError("Provider result is an error or not a list")
                for block in results:
                    identifiers = block.get("meta", {}).get("symbol", [])
                    if identifiers and set(identifiers) != {symbol.replace(".", "-")}:
                        raise ValueError("Raw EPS security identity mismatch")
                    for label, field in SERIES.items():
                        for item in block.get(field, []):
                            if not isinstance(item, dict):
                                raise ValueError("Observation is not an object")
                            raw_value = item.get("reportedValue", {}).get("raw")
                            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not math.isfinite(raw_value):
                                raise ValueError("Nonfinite raw EPS")
                            day = item.get("asOfDate")
                            date.fromisoformat(day)
                            observations[label].append(item)
                audit["acquisition_status"] = "retrieved_raw_unvalidated" if any(observations.values()) else "empty_provider_response"
            except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                audit["acquisition_status"] = "raw_response_requires_review"
                audit["parse_error_type"] = type(exc).__name__
                reasons.append("raw_response_structure_or_identity_unverified")
    else:
        reasons.append("raw_eps_not_retrieved")
    for label, rows in observations.items():
        audit[f"raw_{label}_count"] = len(rows)
        dates = sorted({row["asOfDate"] for row in rows})
        audit[f"latest_{label}_asOfDate"] = dates[-1] if dates else None
        audit["future_raw_observation_count"] += sum(row["asOfDate"] > as_of for row in rows)
        audit["non_usd_raw_observation_count"] += sum(row.get("currencyCode") != "USD" for row in rows)
    annual_dates = {r["asOfDate"] for r in observations["annual"] if r["asOfDate"] <= as_of}
    # This is raw coverage only; no period boundaries or annual comparability are asserted.
    if annual_dates:
        last_year = max(int(day[:4]) for day in annual_dates)
        annual_dates = {day for day in annual_dates if int(day[:4]) >= last_year - 10}
    audit["raw_annual_distinct_period_count"] = len(annual_dates)
    audit["raw_annual_target_shortfall"] = max(0, 11 - len(annual_dates))
    annual_latest, ttm_latest = audit["latest_annual_asOfDate"], audit["latest_ttm_asOfDate"]
    audit["latest_annual_newer_than_raw_ttm"] = bool(annual_latest and ttm_latest and annual_latest > ttm_latest)
    if not observations["ttm"]:
        reasons.append("raw_ttm_not_retrieved")
    if audit["raw_annual_target_shortfall"]:
        reasons.append("raw_annual_history_short_of_11_year_target")
    if audit["latest_annual_newer_than_raw_ttm"]:
        reasons.append("raw_ttm_not_latest_reported_period")
    reasons += ["disclosure_dates_unknown", "actual_fiscal_period_bounds_unverified",
                "share_split_and_accounting_basis_unverified", "annual_overlap_and_restatement_review_incomplete",
                "ttm_latest_report_completeness_unverified", "same_basis_dated_institutional_consensus_missing"]
    audit["missing_reasons"] = reasons
    return audit, {"source_manifest_record": record, "raw_observations": observations,
                   "semantics": "Provider asOfDate labels; no disclosure dates or verified period boundaries supplied. Not model inputs."}


def generate(study: Path):
    universe_path = study / "input/universe.json"
    universe = read_json(universe_path)
    members, as_of = universe["members"], universe["as_of"]
    universe_audit = read_json(study / "input/universe_audit.json")
    if len({m["symbol"] for m in members}) != len(members):
        raise ValueError("Duplicate universe securities")
    eps_manifest_path = study / "evidence/public_actual_eps/manifest.jsonl"
    eps_manifest_bytes = eps_manifest_path.read_bytes() if eps_manifest_path.exists() else b""
    records = parse_manifest(eps_manifest_bytes)
    results, audits = [], []
    for member in members:
        audit, evidence = inspect_eps(study, member, records, as_of)
        audits.append(audit)
        partial_input = {key: member[key] for key in
                         ("symbol", "as_of", "asset_type", "price", "price_date", "currency", "price_kind", "source") if key in member}
        partial_input["input_status"] = "partial_acquisition_record_not_normalized_snapshot"
        partial_input["source"] = {**partial_input["source"], "actual_eps": evidence["source_manifest_record"]}
        reasons = ";".join(audit["missing_reasons"])
        results.append({"symbol": member["symbol"], "as_of": as_of, "company_name": member["name"],
                        "cik": member["cik"], "synthetic": False, "input": partial_input,
                        "event_review": "not_available", "error": reasons,
                        "download_status": audit, "price_evidence": member.get("ivv_holding"),
                        "raw_eps_and_split_observations": evidence,
                        "prediction": {"status": "data_unavailable", "candidate": "abstain",
                                       "target_date": date.fromisoformat(as_of).replace(year=date.fromisoformat(as_of).year + 6).isoformat(),
                                       "probability_status": "not_computed", "reason": reasons,
                                       "flags": audit["missing_reasons"], **{key: None for key in PROBABILITY_FIELDS}}})
    count = len(members)
    coverage = {"as_of": as_of, "scope": "partial_acquisition_records_not_validated_model_snapshots",
                "expected_count": count, "supplied_count": count, "covered_count": count,
                "missing_symbols": [], "extra_symbols": [], "complete": True,
                "validated_model_snapshot_count": 0, "validated_model_coverage_complete": False}
    out = study / "readiness"
    out.mkdir(parents=True, exist_ok=True)
    (out / "actual_eps_manifest_snapshot.jsonl").write_bytes(eps_manifest_bytes)
    exported = write_exports(out, results, coverage)
    status_counts = dict(sorted(Counter(a["acquisition_status"] for a in audits).items()))
    summary = {"as_of": as_of, "stage": "data_readiness_only", "generated_at": datetime.now(timezone.utc).isoformat(),
               "universe_security_count": count, "unique_cik_count": len({m["cik"] for m in members}),
               "membership_three_source_agreement": universe_audit["all_three_sources_agree"],
               "vendor_price_count": sum(m.get("price") is not None for m in members),
               "vendor_price_dates": dict(Counter(m.get("price_date") for m in members)),
               "raw_eps_acquisition_status_counts": status_counts,
               "raw_eps_retrieved_security_count": status_counts.get("retrieved_raw_unvalidated", 0),
               "raw_annual_eps_security_count": sum(a["raw_annual_count"] > 0 for a in audits),
               "raw_quarterly_eps_security_count": sum(a["raw_quarterly_count"] > 0 for a in audits),
               "raw_ttm_eps_security_count": sum(a["raw_ttm_count"] > 0 for a in audits),
               "raw_observation_totals": {label: sum(a[f"raw_{label}_count"] for a in audits) for label in SERIES},
               "raw_annual_count_distribution": dict(sorted(Counter(a["raw_annual_count"] for a in audits).items())),
               "raw_annual_11_year_shortfall_distribution": dict(sorted(Counter(a["raw_annual_target_shortfall"] for a in audits).items())),
               "raw_annual_newer_than_raw_ttm_count": sum(a["latest_annual_newer_than_raw_ttm"] for a in audits),
               "validated_ttm_count": 0, "validated_annual_history_count": 0,
               "validated_consensus_count": 0, "validated_model_ready_count": 0,
               "probabilities_computed_count": 0, "ai_candidate_queue_created": False,
               "ai_event_adjustment_count": 0, "risk_assessed_count": 0,
               "data_unavailable_count": count, "raw_observation_dates_are_disclosure_dates": False,
               "collection_snapshot_complete": len({r.get("symbol") for r in records}) == count,
               "raw_acquisition_complete": status_counts.get("retrieved_raw_unvalidated", 0) == count,
               "collection_snapshot_note": "Snapshot of archived evidence at report generation; rerun after collector finishes.",
               "ranking_status": "not_possible_no_validated_model_inputs", "run_mode": "readiness_audit_no_screen_or_adjust",
               "raw_retention": "All current-study source bytes retained; no cleanup performed.",
               "cleanup_deleted_count": 0, "exports": exported}
    write_json(out / "summary.json", summary)
    write_json(out / "data_availability.json", {"as_of": as_of, "summary": summary, "stocks": audits})
    with (out / "data_availability.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in audits:
            writer.writerow({**row, "missing_reasons": ";".join(row["missing_reasons"])})
    public_attempts = manifest_rows(study / "evidence/download_manifest.jsonl")
    yahoo_attempts = manifest_rows(study / "evidence/yahoo/manifest.jsonl")
    write_json(out / "source_status.json", {"public_document_attempts": public_attempts,
                                             "yahoo_authenticated_path_attempts": yahoo_attempts,
                                             "actual_eps_manifest_record_count": len(records),
                                             "actual_eps_http_status_counts": dict(Counter(str(r.get("status")) for r in records))})
    write_json(out / "readiness_run.json", {"generated_at": summary["generated_at"], "stage": summary["stage"],
                                             "source_universe_sha256": digest(universe_path),
                                             "source_eps_manifest_sha256": hashlib.sha256(eps_manifest_bytes).hexdigest(),
                                             "source_eps_manifest_snapshot": "actual_eps_manifest_snapshot.jsonl",
                                             "source_script_sha256": digest(Path(__file__)),
                                             "uses_test_data": False, "ran_model": False, "ran_event_adjustment": False})
    table = "\n".join(f"| {state} | {n} |" for state, n in status_counts.items())
    history_distribution = "、".join(f"{years}条：{n}只" for years, n in summary["raw_annual_count_distribution"].items())
    report = f"""# 当前标普500数据准备审计（{as_of}）

本次尚不能给出“六年后 P0/E6 < 6 的最高概率股票”排名：已验证可建模输入为 **0只**，同口径、带时点的机构共识为 **0只**。全部概率保留为空值，未运行 screen、adjust，也未生成基准或AI候选队列。下载成功不等于通过TTM、历史和股份口径核验。

成员名单覆盖 **{count}只证券 / {summary['unique_cik_count']}家公司CIK**。归档的Wikipedia与过滤后的IVV、SPY普通股集合完全一致；基金残余权利、权证、现金等保留在[名单审计](../input/universe_audit.json)的剔除记录中。当前观察可用日为{as_of}，不伪称历史成员快照。IVV文件内价格日为 **{universe_audit['price_date']}**，共{summary['vendor_price_count']}条供应商价格；SPY文件内持仓日为 **{universe_audit['sources']['spy']['document_as_of']}**。这些价格未完成独立交易所收盘价核验。

## 原始EPS下载与模型输入分开统计

| 下载状态 | 证券数 |
| --- | ---: |
{table}

取得非空原始年度EPS的证券有{summary['raw_annual_eps_security_count']}只，季度EPS {summary['raw_quarterly_eps_security_count']}只，供应商标记TTM {summary['raw_ttm_eps_security_count']}只。观察条数合计分别为{summary['raw_observation_totals']['annual']}、{summary['raw_observation_totals']['quarterly']}、{summary['raw_observation_totals']['ttm']}条。年度原始条数分布：{history_distribution}。逐股11年目标缺口、各类最新asOfDate和下载证据在[data_availability.csv](data_availability.csv)及JSON中。

供应商的 `asOfDate` 是其报表期间标签，**不是财报公开日期**；本响应也未提供真实季度起止日、逐项拆股桥接及重述审计。本次有{summary['raw_annual_newer_than_raw_ttm_count']}只股票的年度标签晚于其TTM标签，更不能将末条TTM自动认作最新完整十二个月。滚动TTM不加入年度历史增加样本数。负EPS原文保留，但在核验前不据此进行风险排除或生成亏损概率。

| 验证结果 | 证券数 |
| --- | ---: |
| 通过最新TTM核验 | 0 |
| 通过扩展年度历史核验 | 0 |
| 具备已验证同口径机构共识 | 0 |
| 可进入现有模型 | 0 |
| 已计算概率 / 已进行AI事件调整 | 0 / 0 |
| 风险未评估 | {count} |

## 文件与后续工作

[all_stocks.json](all_stocks.json)和[all_stocks.csv](all_stocks.csv)保留全{count}只股票的价格、来源、原始EPS记录及缺失原因，`export_status=data_unavailable`、`risk_assessment=not_assessed`；它们是数据准备表，不是已完成预测。排除表为空意味着尚未进行风险判定，不代表全部股票风险合格。未伪造分析师数量、机构预测、披露日期、EPS口径或概率。

下一步逐股补真实财报公开日期和财政期间，核对拆股/主体/重述，补充最多11年年度历史；取得同EPS口径的有日期机构预测后，以 `prepare_ttm_snapshot` 装配，再按指南运行基准初筛和候选事件复核。AI基于时事作出的任何调整应另写事件与前后变更日志；本轮实际调整数为0。

本审计只读取当前研究的 `input/` 和 `evidence/`，未使用 `data/` 测试数据。原始字节与SHA256留存；没有执行清理，删除数量0。生成时刻为{summary['generated_at']}。采集可能仍在进行，本脚本可重跑；[summary.json](summary.json)记录这一轮确切计数，[source_status.json](source_status.json)保留下载通道响应状态。
"""
    (out / "report.md").write_text(report, encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=STUDY)
    args = parser.parse_args()
    summary = generate(args.study.resolve())
    print(json.dumps({key: summary[key] for key in ("universe_security_count", "raw_eps_acquisition_status_counts",
                                                    "raw_observation_totals", "validated_model_ready_count",
                                                    "ranking_status")}, indent=2))
