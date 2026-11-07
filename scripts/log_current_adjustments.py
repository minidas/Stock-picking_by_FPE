"""Append evidence-bound decisions for the incomplete 2026-11-06 study.

This records no applied adjustments, not a completed review of candidates.
Only current-study files are read; legacy data/ fixtures are never consulted.
Identical inputs and evidence produce the same run/record IDs on every run.
New evidence creates a new append-only run; old pending records stay unresolved.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "outputs/current_sp500_2026-11-06"
LOG = STUDY / "ai_adjustment_log.jsonl"
AS_OF = "2026-11-06"
PROBABILITIES = ("p_below_6", "p_above_6_5_positive", "p_loss")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def file_ref(path: Path) -> dict:
    return {"path": relative(path), "sha256": digest(path.read_bytes())}


def read_jsonl(raw: bytes, label: str) -> list[dict]:
    rows = []
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Incomplete/invalid JSONL: {label}:{number}; retry after acquisition completes") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Expected object: {label}:{number}")
            rows.append(row)
    return rows


def source_symbol(row: dict) -> str | None:
    if row.get("symbol"):
        return row["symbol"]
    query = parse_qs(urlsplit(row.get("url", "")).query)
    if query.get("symbol"):
        return query["symbol"][0].replace("-", ".")
    match = re.search(r"/(?:quoteSummary|quote|timeseries)/([^/?]+)", row.get("url", ""))
    if match:
        return match.group(1).replace("-", ".")
    name = row.get("name", "")
    if name.startswith("mu_"):
        return "MU"
    if name.startswith("c_"):
        return "C"
    return None


def load_evidence() -> tuple[list[dict], list[dict]]:
    sources, manifests = [], []
    paths = sorted((STUDY / "evidence").rglob("*manifest.jsonl"))
    for manifest in paths:
        # One immutable in-memory snapshot, even if a collector appends later.
        raw = manifest.read_bytes()
        manifest_ref = {"path": relative(manifest), "sha256": digest(raw)}
        manifests.append(manifest_ref)
        for number, row in enumerate(read_jsonl(raw, relative(manifest)), 1):
            archive = None
            if row.get("file"):
                path = (ROOT / Path(row["file"].replace("\\", "/"))).resolve()
                if not path.is_relative_to(STUDY.resolve()):
                    raise ValueError("Evidence archive must stay inside the current study")
                if not path.is_file():
                    raise ValueError(f"Missing archived evidence: {relative(path)}")
                archive = file_ref(path)
                if row.get("sha256") and archive["sha256"] != row["sha256"]:
                    raise ValueError(f"Evidence hash mismatch: {relative(path)}")
            sources.append({
                "symbol": source_symbol(row), "source_name": row.get("name"),
                "source_url": row.get("url"), "published_at": None,
                "publication_date_status": "not_verified_in_download_manifest",
                "retrieved_at": row.get("retrieved_at"),
                "http_status": row.get("status"),
                "acquisition_success": row.get("success", row.get("status") == 200),
                "error_type": row.get("error", row.get("error_type")),
                "raw_archive": archive,
                "raw_archive_status": "retained_hash_verified" if archive else "not_available_download_failed",
                "manifest_snapshot": manifest_ref, "manifest_record_number": number,
            })
    if not sources:
        raise ValueError("No actual current-study source manifests found")
    return sources, manifests


def main() -> None:
    for name in ("baseline.json", "agent_queue.json", "adjusted.json"):
        if list(STUDY.rglob(name)):
            raise ValueError(f"{name} now exists: reassess this no-baseline log before appending")
    inputs = [file_ref(path) for path in sorted((STUDY / "input").iterdir()) if path.is_file()]
    if not inputs:
        raise ValueError("Current-study universe/input artifacts are required")
    rules = [file_ref(ROOT / "docs/AGENT_GUIDE.md"), file_ref(ROOT / "src/stocking/data.py"),
             file_ref(Path(__file__).resolve())]
    sources, manifests = load_evidence()
    identity = {"as_of": AS_OF, "inputs": inputs, "rules": rules, "evidence_manifests": manifests}
    run_id = "current-sp500-2026-11-06-" + digest(canonical(identity))[:20]
    observed = [datetime.fromisoformat(s["retrieved_at"].replace("Z", "+00:00"))
                for s in sources if s["retrieved_at"]]
    if any(t.tzinfo is None for t in observed):
        raise ValueError("Evidence retrieval time requires an explicit timezone")
    cutoff = max(observed).astimezone(timezone.utc).isoformat()
    recorded_at = datetime.now(timezone.utc).isoformat()
    common = {
        "schema_version": "1.0", "run_id": run_id, "recorded_at": recorded_at,
        "run_identity": identity,
        "decision_date": AS_OF, "decision_timezone": "Asia/Shanghai",
        "information_cutoff": cutoff,
        "cutoff_definition": "Latest observed retrieval in this frozen manifest snapshot; not a historical publication date or end-of-day completeness claim.",
        "baseline_sha256": None,
        "baseline_hash_reason": "No validated current-study baseline was computed; no baseline artifact exists.",
        "input_artifacts": inputs, "rule_artifacts": rules,
        "model_output_artifacts": [],
        "model_output_hash_reason": "No baseline or adjusted model was produced; no model-output hash can be supplied.",
        "applied": False, "applied_event_ids": [],
        "already_in_consensus": None,
        "consensus_absorption_rationale": "Not assessed: no valid same-basis institutional-consensus baseline or candidate queue.",
        "eps_impact_derivation": None,
        "eps_impact_derivation_reason": "No event impact was estimated or applied.",
        "parameter_changes": {
            "annual_log_growth_delta": None, "variance_multiplier": None,
            "loss_probability_delta": None, "confidence": None,
        },
        "before": {**{p: None for p in PROBABILITIES}, "candidate_status": None, "exclusion_status": None},
        "after": {**{p: None for p in PROBABILITIES}, "candidate_status": None, "exclusion_status": None},
        "null_value_reason": "Not computed or not assessed; null does not mean zero probability or no risk.",
        "no_compounded_event_applied": True,
        "supersedes_record_id": None, "resolves_record_id": None,
    }
    entries = []

    def add(event_id: str, symbol: str | None, kind: str, status: str,
            reason: str, evidence: list[dict], changes=None, **extra) -> None:
        row = {**common, "event_id": event_id, "economic_event_deduplication_key": event_id,
               "symbol": symbol, "processing_type": kind, "status": status,
               "rationale": reason, "evidence": evidence,
               "field_changes": changes or [], **extra}
        row["record_id"] = digest(canonical({"run_id": run_id, "event_id": event_id,
                                              "symbol": symbol, "processing_type": kind, "status": status}))
        entries.append(row)

    failed = [s for s in sources if not s["acquisition_success"]]
    add("run-no-events-applied", None, "event_scenario_review", "no_adjustment",
        "No AI event adjustment was applied because no validated baseline/candidate queue was produced. This is a run-level no-op, not a completed no-change review of 503 stocks.",
        failed, candidate_review_status="not_started_no_baseline", candidate_count=None)

    forecast = [s for s in sources if s["source_name"] == "mu_forecast" and s["acquisition_success"]]
    actuals = [s for s in sources if s["source_name"] in {"mu_income", "mu_quarterly"} and s["acquisition_success"]]
    disclosure = "EPS and Forward PE are based on non-GAAP adjusted numbers."
    if forecast:
        source_file = ROOT / forecast[-1]["raw_archive"]["path"]
        text = re.sub(r"<[^>]*>", " ", source_file.read_text(encoding="utf-8"))
        if disclosure not in text:
            raise ValueError("Archived forecast no longer contains the verified non-GAAP disclosure")
        add("reject-gaap-nongaap-forecast-mix", "MU", "normalization_decision", "rejected",
            "The public forecast explicitly declares adjusted non-GAAP EPS. Relabelling it as GAAP diluted or combining it with GAAP actual EPS without a verified reconciliation was rejected. Provider values were not changed.",
            forecast + actuals,
            [{"field": "consensus[].eps_basis", "source_value": "non-GAAP adjusted",
              "proposed_normalized_value": "GAAP_diluted", "accepted_value": None,
              "before": None, "after": None, "applied": False}],
            verified_source_disclosure=disclosure)

    add("reject-current-consensus-vintage-backdating", None, "normalization_decision", "rejected",
        "The existing normalize_consensus_rows rule assigns a current download to the following UTC date. No retrieval timestamp or financial period-end was relabelled as an earlier consensus publication date to make it usable on 2026-11-06. No valid forecast vintage was available for normalization.",
        forecast + [s for s in sources if "earningsTrendGaap" in (s["source_url"] or "")],
        [{"field": "consensus[].available_at", "before": None, "after": None,
          "applied": False, "rule": "UTC retrieval date + one calendar day"}])

    for symbol in sorted({s["symbol"] for s in failed if s["symbol"]}):
        add("source-access-pending-" + symbol, symbol, "input_availability", "pending",
            "Archived acquisition attempts failed. Required input verification remains unresolved; access failures are not evidence of adverse company fundamentals, exclusion, or a completed event review.",
            [s for s in failed if s["symbol"] == symbol], candidate_review_status="not_started_no_baseline")

    previous = read_jsonl(LOG.read_bytes(), relative(LOG)) if LOG.exists() else []
    ids = [row.get("record_id") for row in previous]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("Existing adjustment log has missing or duplicate record IDs")
    new = [row for row in entries if row["record_id"] not in set(ids)]
    if new:
        with LOG.open("a", encoding="utf-8", newline="\n") as handle:
            for row in new:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"run_id": run_id, "appended_records": len(new),
                      "existing_records": len(previous), "applied_adjustments": 0,
                      "log": relative(LOG), "sha256": digest(LOG.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
