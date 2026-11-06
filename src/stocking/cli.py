"""Batch research CLI; never places orders or silently fills missing stocks."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

from .data import (DataError, load_json, load_snapshots, load_memberships, coverage_report,
                   fetch_alpha_vantage, fetch_sec_companyfacts)
from .model import ModelError, build_baseline, evaluate, apply_events, _config
from .export import create_exports, write_exports
from .retention import archive_run, finalize_run, cleanup_runs


WORKSPACE = Path(__file__).resolve().parents[2]


def managed_root(value):
    path = Path(value).absolute()
    resolved = path.resolve()
    if (resolved == WORKSPACE or not resolved.is_relative_to(WORKSPACE)
            or any(p in {".git", ".codex", ".agents"} for p in resolved.relative_to(WORKSPACE).parts)):
        raise DataError("Managed archive must stay inside a dedicated directory in this workspace")
    # Retention verifies each unresolved component too, rejecting links/junctions.
    return path


def stock_scope(snapshots):
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise DataError("Each stock snapshot must be an object")
        kind = str(snapshot.get("asset_type", "stock")).lower()
        if (kind not in {"stock", "equity", "common_stock"}
                or snapshot["symbol"].startswith("^")
                or snapshot["symbol"] == "SP_COMPOSITE_PROXY"
                or snapshot.get("share_basis") == "index_points"):
            raise DataError(f"{snapshot['symbol']}: this workflow accepts individual stocks only")


def run_archive(args, inputs):
    mode = getattr(args, "mode", "research")
    days = getattr(args, "retention_days", 7)
    if days < 0:
        raise DataError("retention-days cannot be negative")
    root = managed_root(getattr(args, "archive_root", WORKSPACE / "outputs/run_archive"))
    return archive_run(root, {k: v for k, v in inputs.items() if v}, mode=mode,
                       raw_paths=getattr(args, "raw_input", None))


def finish_archive(args, run_dir, out, export, metadata, working):
    if (export["summary"]["input_result_count"] > 0
            and export["summary"]["available_stock_count"] == 0
            and export["summary"]["excluded_stock_count"] == 0):
        write_json(out / "run_lifecycle.json", {
            "mode": getattr(args, "mode", "research"), "run_archive": str(run_dir),
            "status": "pending", "cleanup": None, "reason": "no_successful_stock_models"})
        raise DataError("All stock models failed; diagnostics saved, archive remains pending and no cleanup ran")
    # Compact forecasts survive raw cleanup for later six-year outcome scoring.
    audit_fields = ["symbol", "as_of", "target_date", "export_status", "risk_assessment",
                    "candidate", "exclusion_reasons", "error", "model_version", "price", "eps0",
                    "eps_basis", "eps_measure", "eps_period_start", "eps_period_end", "eps_available_at",
                    "share_basis", "buy_fpe_threshold", "sell_fpe_threshold",
                    "p_below_buy_threshold", "p_above_sell_threshold_positive", "p_below_6",
                    "p_above_6_5_positive", "p_loss", "event_review"]
    audit_path = out / "forecast_audit.csv"
    with audit_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=audit_fields, extrasaction="ignore")
        writer.writeheader()
        for row in export["stocks"] + export["excluded_stocks"]:
            flat = {**row.get("input", {}), **row.get("model", {}), **row,
                    **row.get("prediction", {})}
            flat["exclusion_reasons"] = ";".join(row["exclusion_reasons"])
            # Preserve numbers; escape spreadsheet formula prefixes in text.
            flat = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                    for k, v in flat.items()}
            writer.writerow(flat)
    configurations = {}
    event_audit = {}
    for row in export["stocks"] + export["excluded_stocks"]:
        model = row.get("model", {})
        if model.get("config"):
            digest = hashlib.sha256(json.dumps(model["config"], sort_keys=True).encode()).hexdigest()
            configurations[digest] = model["config"]
        if model.get("events"):
            event_audit[row["symbol"]] = model["events"]
    code_hashes = {name: file_hash(Path(__file__).parent / name)
                   for name in ("model.py", "data.py", "ttm.py", "export.py", "retention.py", "cli.py")}
    finalize_run(run_dir, artifacts={"forecast_audit": audit_path},
                 metadata={**metadata, "coverage": export["coverage"], "summary": export["summary"],
                           "exclusion_policy": export["policy"], "model_configurations": configurations,
                           "event_audit": event_audit, "code_sha256": code_hashes},
                 working_files=working)
    cleanup = None
    if getattr(args, "mode", "research") == "live":
        cleanup = cleanup_runs(run_dir.parent, retention_days=getattr(args, "retention_days", 7), dry_run=False)
    lifecycle = {"mode": getattr(args, "mode", "research"), "run_archive": str(run_dir),
                 "retention_days": getattr(args, "retention_days", 7), "cleanup": cleanup,
                 "note": "Only managed raw/working copies expire; original supplied files and compact forecasts are retained."}
    write_json(out / "run_lifecycle.json", lifecycle)
    return lifecycle


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def screen(args):
    snapshots = load_snapshots(args.input)
    stock_scope(snapshots)
    config = load_json(args.config) if args.config else None
    if config is not None and not isinstance(config, dict):
        raise DataError("config must be a JSON object")
    config = _config(config)  # Global mistakes must fail before creating a successful run.
    if len({s["as_of"] for s in snapshots}) != 1:
        raise DataError("screen requires a single as_of date; use score-history for historical panels")
    if len({s["symbol"] for s in snapshots}) != len(snapshots):
        raise DataError("duplicate symbols in a screen")
    coverage = {"status": "universe_unverified", "provided_snapshots": len(snapshots),
                "warning": "No audited constituent file; this run does not establish S&P 500 coverage."}
    if args.universe:
        coverage = coverage_report(snapshots, load_memberships(args.universe), snapshots[0]["as_of"])
        if coverage["extra_symbols"]:
            raise DataError("Snapshots outside supplied dated universe: " + str(coverage["extra_symbols"]))
    limit = getattr(args, "exclude_loss_probability", .25)
    # Validate policy before producing any artifacts.
    create_exports([], {}, max_loss_probability=limit)
    run_dir = run_archive(args, {"input": args.input, "universe": args.universe, "config": args.config})
    results, queue = [], []
    for snapshot in snapshots:
        try:
            model = build_baseline(snapshot, config)
            prediction = evaluate(model)
            row = {"symbol": snapshot["symbol"], "as_of": snapshot["as_of"],
                   "synthetic": snapshot.get("synthetic", False), "input": snapshot, "model": model,
                   "prediction": prediction}
            results.append(row)
        except (ValueError, KeyError, ModelError) as exc:
            results.append({"symbol": snapshot["symbol"], "as_of": snapshot["as_of"],
                            "input": snapshot, "synthetic": snapshot.get("synthetic", False),
                            "error": str(exc), "prediction": {"candidate": "abstain"}})
    export = create_exports(results, coverage, max_loss_probability=limit)
    included = {r["symbol"] for r in export["stocks"] if r["export_status"] == "available"}
    for row in results:
        if row["symbol"] in included and row["prediction"].get("candidate") in ("buy_review", "sell_review"):
            row["event_review"] = "pending"
            queue.append({"symbol": row["symbol"], "as_of": row["as_of"], "prediction": row["prediction"],
                          "source": row["input"].get("source"), "consensus": row["input"].get("consensus", []),
                          "instruction": "Review dated material events; see docs/AGENT_GUIDE.md."})
        else:
            row["event_review"] = "not_selected"
    export = create_exports(results, coverage, max_loss_probability=limit)
    out = Path(args.output)
    payload = {"schema_version": 2, "scope": "individual_stocks", "input_sha256": file_hash(args.input),
               "config_sha256": file_hash(args.config) if args.config else None,
               "coverage": coverage, "results": results, "export_policy": export["policy"],
               "run_archive": str(run_dir),
               "warning": "Model probabilities are uncalibrated research estimates, not confidence guarantees or orders."}
    write_json(out / "baseline.json", payload)
    write_json(out / "agent_queue.json", {"baseline_sha256": file_hash(out / "baseline.json"), "candidates": queue})
    summary = write_exports(out, results, coverage, max_loss_probability=limit)
    lifecycle = finish_archive(args, run_dir, out, export, {"stage": "screen", "input_sha256": payload["input_sha256"]},
                               {"baseline": out / "baseline.json", "agent_queue": out / "agent_queue.json",
                                "all_stocks": out / "all_stocks.json", "all_stocks_csv": out / "all_stocks.csv",
                                "excluded": out / "excluded_stocks.json", "excluded_csv": out / "excluded_stocks.csv"})
    print(json.dumps({**summary, "agent_candidates": len(queue), "output": str(out),
                      "run_archive": lifecycle["run_archive"]}, ensure_ascii=False))


def export_csv(path, results):
    fields = ["symbol", "as_of", "synthetic", "candidate", "buy_fpe_threshold", "sell_fpe_threshold",
              "p_below_buy_threshold", "p_above_sell_threshold_positive", "p_between_thresholds",
              "p_risk_or_above_sell_threshold", "p_below_6", "p_above_6_5_positive",
              "p_loss", "p_between_6_6_5", "p_risk_or_above_6_5", "error"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow({**row, **row.get("prediction", {})})


def adjust(args):
    original = load_json(args.baseline)
    event_file = load_json(args.events)
    if event_file.get("baseline_sha256") != file_hash(args.baseline):
        raise DataError("events must name the exact baseline_sha256 (prevents applying stale reviews)")
    event_map = event_file.get("events_by_symbol", {})
    if not isinstance(event_map, dict):
        raise DataError("events_by_symbol must be an object mapping symbol to event list")
    stock_scope([r.get("input", r.get("model", {"symbol": r["symbol"]})) for r in original["results"]])
    limit = getattr(args, "exclude_loss_probability", None)
    if limit is None:
        limit = original.get("export_policy", {}).get("max_loss_probability", .25)
    original_limit = original.get("export_policy", {}).get("max_loss_probability", .25)
    prior_export = create_exports(original["results"], original.get("coverage", {}), max_loss_probability=original_limit)
    eligible = {r["symbol"] for r in prior_export["stocks"] if r["export_status"] == "available"
                and r["prediction"].get("candidate") in ("buy_review", "sell_review")}
    if set(event_map) - eligible:
        raise DataError("AI adjustments only allowed for preliminary candidates: " + str(sorted(set(event_map) - eligible)))
    results = []
    for row in original["results"]:
        symbol = row["symbol"]
        if symbol in event_map:
            model = apply_events(row["model"], event_map[symbol])
            results.append({**row, "baseline_prediction": row["prediction"], "model": model,
                            "prediction": evaluate(model), "event_review": "reviewed"})
        else:
            results.append({**row, "event_review": "pending" if symbol in eligible else "not_selected"})
    export = create_exports(results, original.get("coverage", {}), max_loss_probability=limit)
    run_dir = run_archive(args, {"baseline": args.baseline, "events": args.events})
    payload = {**original, "schema_version": 2, "scope": "individual_stocks", "results": results,
               "export_policy": export["policy"], "run_archive": str(run_dir),
               "baseline_sha256": file_hash(args.baseline),
               "events_sha256": file_hash(args.events)}
    out = Path(args.output)
    write_json(out / "adjusted.json", payload)
    summary = write_exports(out, results, original.get("coverage", {}), max_loss_probability=limit)
    finish_archive(args, run_dir, out, export, {"stage": "adjust", "baseline_sha256": payload["baseline_sha256"],
                                               "events_sha256": payload["events_sha256"]},
                   {"adjusted": out / "adjusted.json", "all_stocks": out / "all_stocks.json",
                    "all_stocks_csv": out / "all_stocks.csv", "excluded": out / "excluded_stocks.json",
                    "excluded_csv": out / "excluded_stocks.csv"})
    print(json.dumps({**summary, "reviewed": len(event_map), "pending": len(eligible - set(event_map)), "output": str(out)}))


def lifecycle_options(parser):
    parser.add_argument("--mode", choices=["research", "live"], default="research")
    parser.add_argument("--archive-root", default=str(WORKSPACE / "outputs/run_archive"))
    parser.add_argument("--raw-input", action="append", help="Original provider file to preserve, repeatable")
    parser.add_argument("--retention-days", type=int, default=7, help="Old live raw/working cache age; research never expires")


def cleanup(args):
    result = cleanup_runs(managed_root(args.archive_root), retention_days=args.retention_days, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Six-year EPS probability research (no trading execution)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("screen", help="Export all eligible individual stocks and a separate AI candidate queue")
    p.add_argument("--input", required=True)
    p.add_argument("--universe", help="Audited dated membership CSV/JSON")
    p.add_argument("--config", help="JSON model parameter overrides")
    p.add_argument("--output", default="outputs/screen")
    p.add_argument("--exclude-loss-probability", type=float, default=.25)
    lifecycle_options(p)
    p.set_defaults(func=screen)
    p = sub.add_parser("adjust", help="Apply evidence records to preliminary candidates only")
    p.add_argument("--baseline", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--output", default="outputs/adjusted")
    p.add_argument("--exclude-loss-probability", type=float, default=None, help="Default: inherit baseline export policy")
    lifecycle_options(p)
    p.set_defaults(func=adjust)
    p = sub.add_parser("fetch-estimates", help="Archive vendor raw data; explicit normalization still required")
    p.add_argument("--symbol", required=True)
    p.add_argument("--output", default="data/private/alpha_vantage")
    p.set_defaults(func=lambda a: print(fetch_alpha_vantage(a.symbol, a.output)))
    p = sub.add_parser("fetch-sec", help="Archive SEC facts; not split-safe point-in-time EPS by itself")
    p.add_argument("--cik", required=True)
    p.add_argument("--output", default="data/private/sec")
    p.set_defaults(func=lambda a: print(fetch_sec_companyfacts(a.cik, a.output)))
    p = sub.add_parser("score-history", help="Replay dated stock snapshots and score matured six-year EPS labels")
    p.add_argument("--input", required=True)
    p.add_argument("--outcomes", required=True)
    p.add_argument("--universe", required=True)
    p.add_argument("--evaluation-date", required=True)
    p.add_argument("--output", default="outputs/stock_validation")
    p.add_argument("--archive-root", default=str(WORKSPACE / "outputs/run_archive"))
    p.add_argument("--raw-input", action="append", help="Original files to preserve with this research evaluation")
    p.set_defaults(func=score_history)
    p = sub.add_parser("cleanup", help="Preview or clean only expired managed live raw/working copies")
    p.add_argument("--archive-root", default=str(WORKSPACE / "outputs/run_archive"))
    p.add_argument("--retention-days", type=int, default=7)
    p.add_argument("--apply", action="store_true", help="Execute cleanup; default is preview")
    p.set_defaults(func=cleanup)
    args = parser.parse_args()
    try:
        args.func(args)
    except (DataError, ModelError, ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


def score_history(args):
    from .backtest import score_panel
    outcomes = load_json(args.outcomes)
    if isinstance(outcomes, dict):
        outcomes = outcomes["outcomes"]
    snapshots = load_snapshots(args.input)
    stock_scope(snapshots)
    result = score_panel(snapshots, outcomes, load_memberships(args.universe), args.evaluation_date)
    result["input_hashes"] = {name: file_hash(getattr(args, name)) for name in ("input", "outcomes", "universe")}
    run_dir = archive_run(managed_root(getattr(args, "archive_root", WORKSPACE / "outputs/run_archive")),
                          {key: getattr(args, key) for key in ("input", "outcomes", "universe")},
                          mode="research", raw_paths=getattr(args, "raw_input", None))
    result["run_archive"] = str(run_dir)
    write_json(Path(args.output) / "metrics.json", result)
    finalize_run(run_dir, artifacts={"metrics": Path(args.output) / "metrics.json"},
                 metadata={"stage": "score_history", "scope": "individual_stocks",
                           "evaluation_date": args.evaluation_date,
                           "model_sha256": file_hash(Path(__file__).parent / "model.py")})
    print(json.dumps({"scored": result["scored"], "excluded": len(result["excluded"]), "output": args.output}))
