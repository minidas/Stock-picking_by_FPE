"""Run the unchanged stock workflow on this study's independently collected data.

The date-stamped study directory is an acquisition identifier; its decision date
is 2026-11-07. Never reads legacy data/ research samples.
"""
from __future__ import annotations

from argparse import Namespace
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stocking.cli import screen, adjust
from stocking.model import build_baseline, evaluate, DEFAULT_CONFIG

STUDY = ROOT / "outputs/current_sp500_2026-11-06"
# Frozen batch reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def append_log(value):
    target = STUDY / "ai_adjustment_log.jsonl"
    old = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()] if target.exists() else []
    if any(r.get("event_id") == value["event_id"] and r.get("run_id") == value["run_id"] for r in old):
        return
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def prediction_fields(value):
    return {key: value.get(key) for key in ("p_below_6", "p_above_6_5_positive", "p_loss", "candidate")}


def main():
    norm = read(STUDY / "normalized/summary.json")
    prices = read(STUDY / "normalized/prices.json")
    if prices["summary"]["validated_price_count"] != 503 or norm["price_dates"] != {"2026-11-05": norm["research_normalized_count"]}:
        raise ValueError("Finish all dated quote collection and rerun normalization before screening")
    if not norm["research_normalized_count"]:
        raise ValueError("No valid normalized research inputs")
    source_paths = set()
    observation_times = []
    for manifest in (STUDY / "evidence").rglob("*manifest.jsonl"):
        source_paths.add(manifest)
        for line in manifest.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("status") == 200 and record.get("file"):
                observation_times.append(record["retrieved_at"])
                path = ROOT / record["file"]
                if not path.resolve().is_relative_to((STUDY / "evidence").resolve()):
                    raise ValueError("Raw evidence reference escaped current study")
                if sha(path) != record["sha256"]:
                    raise ValueError("Raw evidence hash mismatch")
                source_paths.add(path)
    source_paths.update((STUDY / "normalized/normalization_audit.json", STUDY / "normalized/prices.json",
                         STUDY / "input/universe_audit.json"))
    baseline_dir = STUDY / "baseline"
    final_dir = STUDY / "final"
    common = dict(mode="research", archive_root=str(STUDY / "run_archive"), retention_days=7)
    if (baseline_dir / "baseline.json").exists():
        frozen = read(baseline_dir / "baseline.json")
        if frozen["input_sha256"] != sha(STUDY / "normalized/snapshots.json") or not (baseline_dir / "run_lifecycle.json").exists():
            raise ValueError("Existing baseline is incomplete or belongs to different inputs; never overwrite it")
    else:
        screen(Namespace(input=str(STUDY / "normalized/snapshots.json"),
                         universe=str(STUDY / "input/memberships.csv"), config=str(LEGACY_CONFIG_PATH),
                         risk_policy=str(ROOT / "examples/risk_policy_legacy.json"),
                         exclude_loss_probability=.25, output=str(baseline_dir),
                         raw_input=[str(path) for path in sorted(source_paths)], **common))
    queue = read(baseline_dir / "agent_queue.json")
    if queue["candidates"]:
        raise ValueError("Unexpected candidates require actual event review before adjustment")
    events = {"baseline_sha256": queue["baseline_sha256"], "events_by_symbol": {},
              "reason": "No eligible AI candidates because compatible institutional consensus is missing; not an all-company event clearance."}
    write(STUDY / "events.json", events)
    if not (final_dir / "run_lifecycle.json").exists():
        adjust(Namespace(baseline=str(baseline_dir / "baseline.json"), events=str(STUDY / "events.json"),
                         output=str(final_dir), exclude_loss_probability=None, raw_input=[], **common))
    elif read(final_dir / "adjusted.json")["baseline_sha256"] != queue["baseline_sha256"]:
        raise ValueError("Completed final output belongs to a different baseline")
    baseline = read(baseline_dir / "baseline.json")
    final = read(final_dir / "all_stocks.json")
    now = datetime.now(timezone.utc).isoformat()
    run_id = "current-sp500-2026-11-07-" + queue["baseline_sha256"][:16]
    base_log = {"run_id": run_id, "recorded_at": now, "as_of": "2026-11-07",
                "information_cutoff": max(observation_times),
                "actual_source_observations": "Exact per-source timestamps in archived manifests; no unobserved future data used.",
                "baseline_sha256": queue["baseline_sha256"],
                "inputs": [{"path": str(STUDY / "normalized/snapshots.json"), "sha256": sha(STUDY / "normalized/snapshots.json")}],
                "outputs": [{"path": str(final_dir / "adjusted.json"), "sha256": sha(final_dir / "adjusted.json")}],
                "already_in_consensus": None, "consensus_status": "unavailable",
                "annual_log_growth_delta": [0] * 6, "variance_multiplier": 1,
                "loss_probability_delta": 0, "confidence": None}
    for row in baseline["results"]:
        append_log({**base_log, "symbol": row["symbol"], "event_id": "no-eligible-ai-review-" + row["symbol"],
                    "type": "event_scenario_adjustment", "status": "not_selected", "applied": False,
                    "reason": "History-only forecast remains outside default AI queue; no news scenario was applied.",
                    "before": prediction_fields(row["prediction"]), "after": prediction_fields(row["prediction"]),
                    "evidence": [row["input"]["source"]["sec"], row["input"]["source"]["yahoo_actual_eps"]]})
    prior_path = STUDY / "normalized/snapshots_before_primary_quarter_preference.json"
    if prior_path.exists():
        prior = {s["symbol"]: s for s in read(prior_path)["snapshots"]}
        for row in baseline["results"]:
            after = row["input"]
            before = prior.get(row["symbol"])
            if not before:
                continue
            changed = {field: {"before": before.get(field), "after": after.get(field)} for field in
                       ("price", "price_date", "price_kind", "eps0", "eps_period_start", "eps_period_end")
                       if before.get(field) != after.get(field)}
            if not changed:
                continue
            bp = evaluate(build_baseline(before, read(LEGACY_CONFIG_PATH)))
            append_log({**base_log, "symbol": row["symbol"], "event_id": "primary-data-reconciliation-" + row["symbol"],
                        "baseline_sha256": None, "resulting_baseline_sha256": queue["baseline_sha256"],
                        "original_baseline_absence_reason": "Factual reconciliation preceded the first frozen baseline; before probability is a diagnostic recomputation from the frozen preliminary input.",
                        "type": "factual_source_reconciliation", "status": "applied_before_first_baseline", "applied": True,
                        "reason": "Use latest completed nominal close and actual SEC quarterly components when explicitly available; no forecast growth or event assumptions changed.",
                        "changed_fields": changed,
                        "before_input": {"path": str(prior_path), "sha256": sha(prior_path)},
                        "before": prediction_fields(bp), "after": prediction_fields(row["prediction"]),
                        "evidence": {"price": after["source"]["price"], "ttm": after["ttm_provenance"]}})
    ranked = sorted((r for r in final["stocks"] if r["export_status"] == "available"),
                    key=lambda r: r["prediction"]["p_below_6"], reverse=True)
    ranking = []
    for rank, row in enumerate(ranked, 1):
        x, p = row["input"], row["prediction"]
        ranking.append({"rank": rank, "symbol": row["symbol"], "company": x["company_name"],
                        "price": x["price"], "price_date": x["price_date"], "eps0": x["eps0"],
                        "eps_period_end": x["eps_period_end"], "annual_years": len(x["history"]),
                        "growth_observations": row["model"]["history_growth_observations"],
                        **prediction_fields(p), "p0_over_conditional_mean_eps6": x["price"] / p["positive_conditional_eps_mean"],
                        "probability_status": "uncalibrated_history_only_research_probability"})
    write(final_dir / "ranking.json", ranking)
    with (final_dir / "ranking.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ranking[0]) if ranking else ["rank", "symbol"])
        writer.writeheader()
        writer.writerows(ranking)
    summary = {"as_of": "2026-11-07", "target_date": "2032-11-07", "price_date": "2026-11-05",
               "definition": "Pr(EPS_2032 > current_price/6); not future market-price P/E",
               **norm, "exports": final["summary"], "ranking_count": len(ranking),
               "normalization_limitations": norm["limitations"],
               "limitations": ["Probabilities were computed only for compatible research inputs, using the unchanged model.",
                               "Compatible institutional consensus is absent; probabilities are history-only and uncalibrated.",
                               "Incomplete economic-basis and universe model coverage prevents a certified full-index ranking."],
               "all_candidate_count": len(queue["candidates"]), "news_scenario_adjustments_applied": 0,
               "probability_above_70pct_count": sum(r["p_below_6"] >= .7 for r in ranking),
               "config": {**DEFAULT_CONFIG, **read(LEGACY_CONFIG_PATH)}, "baseline_sha256": queue["baseline_sha256"],
               "raw_evidence_files_archived": len(source_paths), "mode": "research", "cleanup_deleted_count": 0,
               "headline_limit": "Ranking is only among available nonexcluded research inputs, not a certified highest-probability ranking of all503 securities."}
    write(final_dir / "study_summary.json", summary)
    print(json.dumps({"summary": summary, "top10": ranking[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
