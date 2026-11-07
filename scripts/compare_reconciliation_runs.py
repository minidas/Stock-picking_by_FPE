"""Compare arbitrary immutable research runs and record every security transition."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def index(rows):
    result = {row["symbol"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate securities in comparison input")
    return result


def load_run(actuals, output, admission):
    output = Path(output)
    files = [Path(actuals), output / "all_stocks.json", output / "excluded_stocks.json", Path(admission)]
    actual, main, excluded, joined = map(read, files)
    if main["as_of"] != excluded["as_of"] or main["policy"] != excluded["policy"]:
        raise ValueError("Output cohort dates or policies disagree")
    stocks = index(main["stocks"] + excluded["excluded_stocks"])
    snapshots, audits = index(actual["snapshots"]), index(joined["securities"])
    if not set(snapshots) <= set(stocks) or not set(audits) <= set(stocks):
        raise ValueError("Actual/admission securities absent from complete output")
    return {"stocks": stocks, "actuals": snapshots, "admission": audits, "files": files,
            "as_of": main["as_of"], "policy": main["policy"], "information_cutoff": joined["policy"]["information_cutoff"]}


def compact(run, symbol):
    stock, actual, admission = run["stocks"][symbol], run["actuals"].get(symbol), run["admission"].get(symbol)
    prediction = stock.get("prediction", {})
    return {"actual_input_present": actual is not None,
            "eps0": actual["eps0"] if actual else None,
            "eps_period_start": actual["eps_period_start"] if actual else None,
            "eps_period_end": actual["eps_period_end"] if actual else None,
            "ttm_method": actual["ttm_provenance"]["method"] if actual else None,
            "annual_history_count": len(actual["history"]) if actual else None,
            "consensus_admitted_count": admission["admitted_annual_count"] if admission else None,
            "consensus_group": admission["group"] if admission else None,
            "export_status": stock["export_status"], "risk_assessment": stock["risk_assessment"],
            "exclusion_reasons": stock.get("exclusion_reasons", []),
            "buy_fpe_threshold": prediction.get("buy_fpe_threshold"),
            "sell_fpe_threshold": prediction.get("sell_fpe_threshold"),
            "p_below_buy_threshold": prediction.get("p_below_buy_threshold"),
            "p_above_sell_threshold_positive": prediction.get("p_above_sell_threshold_positive"),
            "p_between_thresholds": prediction.get("p_between_thresholds"),
            "p_risk_or_above_sell_threshold": prediction.get("p_risk_or_above_sell_threshold"),
            "p_below_6": prediction.get("p_below_6"), "p_loss": prediction.get("p_loss"),
            "event_review": stock.get("event_review"), "candidate": prediction.get("candidate")}


def compare(before, after, output):
    destination = Path(output)
    if destination.exists():
        raise ValueError("Comparison output already exists; preserve immutable runs")
    if set(before["stocks"]) != set(after["stocks"]):
        raise ValueError("Comparison requires the same complete security universe")
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for symbol in sorted(before["stocks"]):
        old, new = compact(before, symbol), compact(after, symbol)
        rows.append({"symbol": symbol, "recorded_at": now, "type": "data_reconciliation_run_comparison",
            "before": old, "after": new, "changed_fields": [key for key in old if old[key] != new[key]],
            "new_actual_period_evidence": (after["actuals"].get(symbol) or {}).get("ttm_provenance", {}).get("actual_period_reconciliation", []),
            "new_forecast_period_evidence": [{"before": x["as_reported_target_date"],
                "after": (x.get("fiscal_calendar_mapping") or {}).get("target_date", x["as_reported_target_date"])}
                for x in (after["admission"].get(symbol) or {}).get("observations", []) if x.get("admitted")],
            "news_scenario_adjustment_in_this_comparison": False,
            "note": "A difference records data/model output, not a discretionary AI event or completed trade review."})
    financial_keys = ("eps0", "eps_period_start", "eps_period_end", "history", "price", "price_date")
    common = before["actuals"].keys() & after["actuals"].keys()
    summary = {"created_at": now, "security_count": len(rows),
        "before_as_of": before["as_of"], "after_as_of": after["as_of"],
        "before_information_cutoff": before["information_cutoff"], "after_information_cutoff": after["information_cutoff"],
        "before_actual_count": len(before["actuals"]), "after_actual_count": len(after["actuals"]),
        "added_actual_symbols": sorted(after["actuals"].keys() - before["actuals"].keys()),
        "removed_actual_symbols": sorted(before["actuals"].keys() - after["actuals"].keys()),
        "changed_existing_financial_values": sorted(s for s in common if any(before["actuals"][s].get(k) != after["actuals"][s].get(k) for k in financial_keys)),
        "before_status_counts": dict(Counter(r["before"]["export_status"] for r in rows)),
        "after_status_counts": dict(Counter(r["after"]["export_status"] for r in rows)),
        "before_consensus_rankable_count": sum(r["before"]["export_status"] == "available" and r["before"]["consensus_group"] == "consensus_research_join" for r in rows),
        "after_consensus_rankable_count": sum(r["after"]["export_status"] == "available" and r["after"]["consensus_group"] == "consensus_research_join" for r in rows),
        "risk_policy_unchanged": before["policy"] == after["policy"],
        "changed_security_count": sum(bool(r["changed_fields"]) for r in rows),
        "scope": "Input and workflow regression comparison; no completed buy/sell event review or replacement recommendation ranking is claimed."}
    files = list(dict.fromkeys(before["files"] + after["files"] + [Path(__file__).resolve()]))
    manifest = {"files": [{"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    destination.mkdir(parents=True)
    for name, value in (("summary.json", summary), ("source_manifest.json", manifest)):
        with (destination / name).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    with (destination / "change_log.jsonl").open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("before", "after"):
        for kind in ("actuals", "output", "admission"):
            parser.add_argument(f"--{side}-{kind}", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    before = load_run(args.before_actuals, args.before_output, args.before_admission)
    after = load_run(args.after_actuals, args.after_output, args.after_admission)
    print(json.dumps(compare(before, after, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
