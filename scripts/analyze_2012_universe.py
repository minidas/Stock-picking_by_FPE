"""Run explicitly unverified, stock-only 2012 history probability scenarios.

This separate research entry point deliberately keeps missing consensus and
nonpositive current EPS in its coverage. Only the historical endpoint-loss
prior can cause a risk exclusion. It does not change the production model,
certify share bases, create AI candidates, or use realized six-year outcomes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from stocking.data import load_json, load_snapshots
from stocking.model import DEFAULT_CONFIG, ModelError, _history, build_baseline, evaluate
from stocking.ttm import TTMError, prepare_ttm_snapshot, validate_annual_history
from stocking.retention import archive_run, finalize_run
from analyze_stock_thresholds import THRESHOLDS, minimum_threshold, threshold_diagnostics

# Frozen 2012 reproduction; new tasks use the public screen defaults.
LEGACY_CONFIG_PATH = ROOT / "examples/thresholds_legacy.json"
LEGACY_MODEL_CONFIG = {**DEFAULT_CONFIG, **load_json(LEGACY_CONFIG_PATH)}
AS_OF = "2012-12-31"
SCENARIO = "as_reported_unverified_research_scenario"
LIMITATIONS = [
    "Bulk probabilities assume filing EPS and year-end nominal shares are comparable; most share bases remain unverified.",
    "Bulk fiscal dates may be rounded to the nearest month end; exact fiscal dates remain unverified.",
    "Prices from fund holding values are not independently verified exchange closing prices.",
    "Annual history coverage varies by issuer; missing years remain missing and sparse growth uses the default volatility floor.",
    "Historical institutional consensus is absent; history-only probabilities do not enable AI candidates.",
    "No realized six-year EPS labels, calibration, return backtest, or optimal-threshold claim.",
    "Only endpoint loss prior at or above the configured cutoff causes risk exclusion; missing data is retained.",
    "The endpoint loss prior is uncalibrated, not bankruptcy risk or probability of any loss during six years.",
    "A split multiplier does not reconcile spin-offs, divestitures or mergers; historical corporate scope may differ from the year-end issuer.",
]
CANONICAL_EPS_BASIS = "GAAP_diluted_as_reported_at_cutoff"
KNOWN_EPS_LABELS = {"annual_diluted_EPS_coherent_filing_research_only",
                    "GAAP_diluted_annual_as_reported_at_cutoff", CANONICAL_EPS_BASIS}


def unique(rows, key):
    result = {}
    for row in rows:
        value = row.get(key)
        if not value or value in result:
            raise ValueError(f"Missing or duplicate {key}: {value}")
        result[value] = row
    return result


def fresh_anchor(company):
    eligible = [r for r in company.get("history", []) if r["period_end"] <= r["available_at"] <= AS_OF]
    if not eligible:
        return None
    latest = max(eligible, key=lambda r: (r["period_end"], r["available_at"]))
    if company.get("period_end") and latest["period_end"] != company["period_end"]:
        return None
    age = (date.fromisoformat(AS_OF) - date.fromisoformat(latest["period_end"])).days
    return latest if age <= LEGACY_MODEL_CONFIG["max_eps_age_days"] else None


def history_loss_prior(snapshot):
    """Use the model's exact history selection even if its EPS branch abstains."""
    flags = set()
    history, _ = _history(snapshot, date.fromisoformat(AS_OF), flags)
    if not history:
        return None, flags
    losses = sum(eps <= 0 for _, eps in history)
    cfg = LEGACY_MODEL_CONFIG
    value = (losses + cfg["loss_prior_probability"] * cfg["loss_prior_strength"]) / (len(history) + cfg["loss_prior_strength"])
    return value, flags


def apply_adjustment(company, adjustment):
    """Apply an evidenced, accession-specific EPS multiplier to a copied filing."""
    required = ("symbol", "expected_accession", "effective_date", "eps_multiplier", "source_url", "rationale")
    if any(key not in adjustment for key in required):
        raise ValueError(f"Adjustment requires {required}")
    factor = adjustment["eps_multiplier"]
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
        raise ValueError("Adjustment EPS multiplier must be finite and positive")
    if adjustment["expected_accession"] != company.get("accession"):
        raise ValueError("Adjustment accession differs from the selected coherent filing")
    if date.fromisoformat(adjustment["effective_date"]) > date.fromisoformat(AS_OF):
        raise ValueError("Adjustment corporate action occurs after the research cutoff")
    url = urlparse(str(adjustment["source_url"]))
    if url.scheme not in ("http", "https") or not url.netloc or not str(adjustment["rationale"]).strip():
        raise ValueError("Adjustment needs an evidence URL and a rationale")
    result = deepcopy(company)
    for row in result.get("history", []):
        row["eps"] *= factor
    return result


def bulk_snapshot(member, price, company, origin):
    anchor = fresh_anchor(company)
    return {
        "symbol": member["symbol"], "as_of": AS_OF, "asset_type": "stock", "currency": "USD",
        "price": price["price"] if price else None, "price_date": price["date"] if price else None,
        "price_kind": price.get("price_kind") if price else None,
        "eps0": anchor["eps"] if anchor else None,
        "eps_period_end": anchor["period_end"] if anchor else None,
        "eps_available_at": anchor["available_at"] if anchor else None,
        "eps_basis": "annual_diluted_EPS_coherent_filing_research_only",
        "share_basis": "ASSUMED_filing_EPS_comparable_to_nominal_shares_2012_12_31",
        "share_basis_verified": False,
        "research_scenario": SCENARIO,
        "history": deepcopy(company.get("history", [])), "consensus": [],
        "source": {"financial_statement": company.get("source_url"), "financial_input_kind": origin,
                   "financial_accession": company.get("accession"),
                   "price": price.get("source") if price else None,
                   "assumptions": LIMITATIONS[:2]},
    }


def history_sha256(history):
    """Bind an extension to the exact, already-adjusted input history."""
    return hashlib.sha256(json.dumps(history, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _context(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("History context must be an object")
    return {key: value.get(key) or "" for key in ("segments", "coreg")}


def apply_history_panel(snapshot, panel, member):
    """Apply an independently audited panel, without replacing the EPS anchor."""
    identity_denied = member.get("2012_filing_identity_confirmation", {}).get("identity_join_allowed") is False
    if panel.get("symbol") != snapshot["symbol"] or panel.get("symbol") != member["symbol"]:
        raise ValueError("History panel symbol mismatch")
    if panel.get("cik") != member["cik"] or (snapshot.get("cik") and snapshot["cik"] != member["cik"]):
        raise ValueError("History panel CIK mismatch")
    if panel.get("as_of") != AS_OF or snapshot.get("as_of") != AS_OF:
        raise ValueError("History panel as_of mismatch or future vintage")
    if panel.get("base_history_sha256") != history_sha256(snapshot.get("history", [])):
        raise ValueError("History panel base_history_sha256 mismatch; rebuild against current inputs")
    if panel.get("target_share_basis") != snapshot.get("share_basis"):
        raise ValueError("History panel target_share_basis mismatch")
    if panel.get("eps_basis") != CANONICAL_EPS_BASIS or snapshot.get("eps_basis") not in KNOWN_EPS_LABELS:
        raise ValueError("History panel accounting EPS basis mismatch")
    if _context(panel.get("context")) != _context(snapshot.get("context")):
        raise ValueError("History panel share-class/coregistrant context mismatch")
    if panel.get("share_basis_verified") is not False:
        raise ValueError("History extension must retain share_basis_verified=false")
    if not isinstance(panel.get("history"), list) or not isinstance(panel.get("audit"), dict):
        raise ValueError("History panel requires history and audit")
    if identity_denied:
        if (panel["history"] != snapshot.get("history", [])
                or panel["audit"].get("identity_join_allowed") is not False
                or panel["audit"].get("additional_observations") != 0):
            raise ValueError("History panel cannot override denied issuer identity")
        result = deepcopy(snapshot)
        result["history_extension_audit"] = deepcopy(panel["audit"])
        result["history_extension_audit"].update(application_status="not_applied_identity_denied",
                                                 base_history_sha256=panel["base_history_sha256"])
        return result
    replacement = deepcopy(panel["history"])
    for observation in replacement:
        if not isinstance(observation, dict):
            raise ValueError("History panel observation must be an object")
        available = date.fromisoformat(observation.get("available_at", ""))
        period = date.fromisoformat(observation.get("period_end", ""))
        if period > available or available > date.fromisoformat(AS_OF):
            raise ValueError("History panel contains future or invalid observation dates")
        if observation.get("eps_basis", CANONICAL_EPS_BASIS) != CANONICAL_EPS_BASIS:
            raise ValueError("History panel observation accounting basis mismatch")
        if observation.get("share_basis", snapshot["share_basis"]) != snapshot["share_basis"]:
            raise ValueError("History panel observation share basis mismatch")
        if "context" in observation and _context(observation["context"]) != _context(panel.get("context")):
            raise ValueError("History panel observation context mismatch")
    result = deepcopy(snapshot)
    result["history_before_extension"] = deepcopy(snapshot.get("history", []))
    result["history"] = replacement
    result["eps_basis"] = CANONICAL_EPS_BASIS
    result["cik"] = member["cik"]
    result["history_extension_audit"] = deepcopy(panel["audit"])
    result["history_extension_audit"].update(base_history_sha256=panel["base_history_sha256"],
                                             target_share_basis=panel["target_share_basis"],
                                             share_basis_verified=False, application_status="applied")
    validate_annual_history(result, date.fromisoformat(AS_OF))
    selected, _ = _history(result, date.fromisoformat(AS_OF), set())
    max_years = panel["audit"].get("max_years", 11)
    if isinstance(max_years, bool) or not isinstance(max_years, int) or not 1 <= max_years <= 11:
        raise ValueError("History panel max_years must be an integer from one to eleven")
    if len(replacement) != len(selected) or len(replacement) > max_years:
        raise ValueError("History panel must contain distinct selected annual periods within max_years")
    # Select the latest available vintage just as the model does. Older
    # restatements and years outside the builder's calendar window are not
    # protected inputs, but extending history may never advance its end date.
    base_selected, _ = _history(snapshot, date.fromisoformat(AS_OF), set())
    if not base_selected and selected:
        raise ValueError("History panel cannot add observations without a base annual anchor")
    if base_selected:
        latest_period = base_selected[-1][0]
        first_year = latest_period.year - max_years + 1
        if any(period > latest_period or period.year < first_year for period, _ in selected):
            raise ValueError("History panel extends beyond the base annual end or calendar-year window")
        base_selected = [(period, eps) for period, eps in base_selected if period.year >= first_year]
    selected_values = {period.isoformat(): eps for period, eps in selected}
    for period, eps in base_selected:
        if period.isoformat() not in selected_values or not math.isclose(
                selected_values[period.isoformat()], eps, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("History panel changed or dropped a protected base-history observation")
    return result


def history_counts(snapshot):
    try:
        history, rates = _history(snapshot, date.fromisoformat(AS_OF), set())
        return len(history), len(rates)
    except (ModelError, ValueError, TypeError):
        return None, None  # The established model-error path retains diagnostics.


def assemble(members, prices, companies, manual, *, adjustments=None, supplemental=None, loss_cutoff=0.25,
             eps_anchor="annual", ttm_anchors=None, history_panels=None):
    """The API preserves legacy annual research; the CLI defaults to explicit TTM.

    Missing TTM is a coverage gap, never permission to silently reuse annual EPS.
    Annual history still supplies growth and the endpoint loss prior.
    """
    if eps_anchor not in {"annual", "ttm"}:
        raise ValueError("eps_anchor must be annual or ttm")
    if not math.isfinite(loss_cutoff) or not 0 <= loss_cutoff <= 1:
        raise ValueError("loss_cutoff must lie in [0, 1]")
    member_map, price_map = unique(members, "symbol"), unique(prices, "symbol")
    company_map, manual_map = unique(companies, "cik"), unique(manual, "symbol")
    adjustment_map, supplement_map = unique(adjustments or [], "symbol"), unique(supplemental or [], "symbol")
    ttm_map = unique(ttm_anchors or [], "symbol")
    panel_map = unique(history_panels or [], "symbol")
    if any(snapshot.get("as_of") != AS_OF for snapshot in manual):
        raise ValueError("Manual snapshots must use the 2012-12-31 research cutoff")
    for mapping in (manual_map, adjustment_map, supplement_map, ttm_map, panel_map):
        if set(mapping) - set(member_map):
            raise ValueError("Manual, supplemental, TTM, history-panel or adjusted symbols must belong to the historical roster")
    for symbol, company in supplement_map.items():
        url = urlparse(str(company.get("source_url", "")))
        if (company.get("cik") != member_map[symbol]["cik"] or not company.get("source_url")
                or url.scheme not in ("http", "https") or not url.netloc
                or not company.get("accession") or not company.get("history")):
            raise ValueError("Supplemental company requires exact symbol/CIK, filing accession, history and evidence URL")
        if company["cik"] in company_map:
            raise ValueError("Supplemental companies cannot override a bulk coherent filing")
    if set(adjustment_map) & set(manual_map):
        raise ValueError("Do not re-adjust the manually checked snapshots")
    results, used_adjustments = [], set()
    for member in members:
        symbol, cik = member["symbol"], member["cik"]
        price = price_map.get(symbol)
        original_company = company_map.get(cik)
        origin = "coherent_bulk_filing"
        if original_company is None and symbol in supplement_map:
            original_company, origin = supplement_map[symbol], "explicitly_selected_supplemental_filing"
        identity_allowed = member.get("2012_filing_identity_confirmation", {}).get("identity_join_allowed") is not False
        company = original_company if identity_allowed else None
        flags, blockers = set(), []
        applied = None
        if symbol in adjustment_map:
            if company is None:
                raise ValueError(f"Cannot apply adjustment without an identity-approved filing: {symbol}")
            applied = deepcopy(adjustment_map[symbol])
            company = apply_adjustment(company, applied)
            used_adjustments.add(symbol)
            flags.add("evidenced_eps_multiplier_applied")
        if not identity_allowed:
            blockers.append("issuer_identity_join_disallowed")
        if symbol in manual_map:
            snapshot = deepcopy(manual_map[symbol])
            input_status = "manually_checked_snapshot"
            # The independently checked snapshot keeps its own price and dates.
        else:
            snapshot = bulk_snapshot(member, price, company or {}, origin)
            input_status = SCENARIO
            flags.update(("unverified_share_basis_assumption", "fiscal_dates_may_be_month_end_approximations"))
            if company and company.get("split_facts"):
                flags.add("filing_split_facts_present_review_not_implied")
        original_history_count, _ = history_counts(snapshot)
        if symbol in panel_map:
            snapshot["context"] = _context((original_company or {}).get("context_selection"))
            snapshot = apply_history_panel(snapshot, panel_map[symbol], member)
            flags.add("audited_history_panel_applied_unverified" if identity_allowed else "history_panel_not_applied_identity_denied")
        annual_history_count, growth_count = history_counts(snapshot)
        history_expanded = (annual_history_count is not None and original_history_count is not None
                            and annual_history_count > original_history_count)
        annual_anchor = None
        ttm_evidence = None
        annual_input_status = input_status if eps_anchor == "ttm" else None
        if eps_anchor == "ttm":
            input_status = "ttm_anchor_unavailable"
            annual_anchor = {key: deepcopy(snapshot.get(key)) for key in
                             ("eps0", "eps_period_end", "eps_available_at", "eps_basis")}
            ttm_evidence = deepcopy(ttm_map.get(symbol))
            # These two legacy labels describe the same GAAP diluted accounting
            # basis while also embedding the old measurement period in the name.
            # Normalize only those known labels; arbitrary bases must not merge.
            if snapshot["eps_basis"] in KNOWN_EPS_LABELS:
                snapshot["eps_basis"] = CANONICAL_EPS_BASIS
            snapshot["cik"] = cik
            snapshot.update(eps0=None, eps_period_end=None, eps_available_at=None,
                            eps_measure="ttm")
            if ttm_evidence is None:
                blockers.append("ttm_anchor_unavailable")
            elif identity_allowed:
                try:
                    snapshot = prepare_ttm_snapshot(snapshot, ttm_evidence)
                    input_status = "ttm_anchor_with_annual_history_research"
                    flags.add("ttm_anchor_with_annual_history")
                    if ttm_evidence["method"] == "four_quarter_diluted_eps_sum":
                        flags.add("ttm_quarter_sum_not_recomputed_annual_gaap_eps")
                    # Annual accession-specific split factors above apply only
                    # to annual history. TTM components must be reconciled by
                    # their own evidence; never apply the same factor again.
                except (TTMError, ValueError, TypeError) as exc:
                    input_status = "ttm_anchor_invalid"
                    blockers.append("invalid_ttm_anchor: " + str(exc))
        if snapshot.get("price") is None:
            blockers.append("price_unavailable")
        if snapshot.get("eps0") is None and eps_anchor == "annual":
            blockers.append("fresh_annual_eps_anchor_unavailable")
        row = {
            "symbol": symbol, "company_name": member.get("name"), "cik": cik, "as_of": AS_OF,
            "input_status": input_status, "input": snapshot,
            "membership_evidence": deepcopy(member), "price_evidence": deepcopy(price),
            "annual_eps_evidence_before_adjustment": deepcopy(original_company),
            "eps_anchor_mode": eps_anchor,
            "history_mode": "expanded" if history_panels is not None else "original",
            "history_panel_applied": symbol in panel_map and identity_allowed, "history_expanded": history_expanded,
            "original_annual_history_count": original_history_count,
            "annual_history_count": annual_history_count, "history_growth_observations": growth_count,
            "history_extension_audit": deepcopy(snapshot.get("history_extension_audit")),
            "annual_anchor_before_ttm": annual_anchor, "ttm_anchor_evidence": ttm_evidence,
            "annual_input_status_before_ttm": annual_input_status,
            "adjustment": applied, "blockers": blockers,
            "model": None, "prediction": None, "threshold_scenarios": [],
            "loss_probability": None, "loss_probability_origin": None,
            "computation_status": "data_unavailable", "ai_queue_eligible": False,
        }
        # An explicit denied identity cannot be rescued by a numerical match.
        if identity_allowed:
            try:
                prior, prior_flags = history_loss_prior(snapshot)
                row["loss_probability"] = prior
                flags.update(prior_flags)
                if prior is not None:
                    row["loss_probability_origin"] = "default_history_endpoint_loss_prior"
                elif not blockers:
                    # A complete snapshot with no history uses the model's
                    # prior-only risk state; genuinely missing inputs stay unknown.
                    prior = LEGACY_MODEL_CONFIG["loss_prior_probability"]
                    row["loss_probability"] = prior
                    row["loss_probability_origin"] = "default_endpoint_loss_prior_only"
                    flags.add("loss_probability_prior_only")
                if prior is not None and prior >= loss_cutoff:
                    row["computation_status"] = "not_tested_high_loss"
                elif not blockers:
                    model = build_baseline(snapshot, LEGACY_MODEL_CONFIG)
                    prediction = evaluate(model)
                    row.update(model=model, prediction=prediction)
                    flags.update(model.get("flags", []))
                    if model["status"] == "ready":
                        row["loss_probability"] = model["loss_probability"]
                        row["loss_probability_origin"] = "default_model_endpoint_loss_prior"
                        row["computation_status"] = "evaluated"
                        row["threshold_scenarios"] = [threshold_diagnostics(model, cutoff) for cutoff in THRESHOLDS]
                        row["minimum_threshold_for_70_percent"] = minimum_threshold(model)
                    else:
                        row["computation_status"] = "model_abstained"
                        row["abstention_reason"] = model.get("reason")
            except (ModelError, ValueError, TypeError) as exc:
                row["computation_status"] = "model_error"
                row["error"] = str(exc)
        risk = row["loss_probability"] is not None and row["loss_probability"] >= loss_cutoff
        row["risk_excluded"] = risk
        row["exclusion_reasons"] = ["endpoint_loss_probability_at_or_above_cutoff"] if risk else []
        row["test_status"] = "risk_excluded" if risk else row["computation_status"]
        row["flags"] = sorted(flags)
        results.append(row)
    if used_adjustments != set(adjustment_map):
        raise ValueError("Some adjustments did not apply")
    return results


def summarize(rows, loss_cutoff=0.25):
    retained = [r for r in rows if not r["risk_excluded"]]
    evaluated = [r for r in retained if r["computation_status"] == "evaluated"]
    counts = []
    for cutoff in THRESHOLDS:
        scans = [s for row in evaluated for s in row["threshold_scenarios"] if s["buy_fpe_threshold"] == cutoff]
        counts.append({"threshold": cutoff, "evaluated_nonexcluded_count": len(scans),
                       "probability_at_least_70_count": sum(s["probability_condition_passes"] for s in scans),
                       "probability_at_least_70_and_loss_at_most_10_count": sum(s["probability_and_loss_conditions_pass"] for s in scans),
                       "formal_model_buy_eligible_count": sum(s["formal_buy_eligible"] for s in scans)})
    return {
        "as_of": AS_OF, "target_date": "2018-12-31", "mode": "research",
        "roster_count": len(rows), "retained_main_table_count": len(retained),
        "risk_excluded_count": len(rows) - len(retained),
        "all_computation_status_counts": dict(Counter(r["computation_status"] for r in rows)),
        "retained_test_status_counts": dict(Counter(r["test_status"] for r in retained)),
        "input_status_counts": dict(Counter(r["input_status"] for r in rows)),
        "input_ready_manual_count": sum(r["input_status"] == "manually_checked_snapshot" and r["model"] is not None for r in rows),
        "supplemental_filing_evidence_count": sum(r["input"].get("source", {}).get("financial_input_kind") == "explicitly_selected_supplemental_filing" for r in rows),
        "supplemental_filing_symbols": [r["symbol"] for r in rows if r["input"].get("source", {}).get("financial_input_kind") == "explicitly_selected_supplemental_filing"],
        "valid_consensus_count": sum(bool((r["model"] or {}).get("consensus_anchors")) for r in rows),
        "sparse_history_count": sum("sparse_earnings_history" in r["flags"] for r in rows),
        "adjusted_symbols": [r["symbol"] for r in rows if r["adjustment"]],
        "risk_excluded_symbols": [r["symbol"] for r in rows if r["risk_excluded"]],
        "data_unavailable_symbols": [r["symbol"] for r in rows if r["computation_status"] == "data_unavailable"],
        "model_abstained_retained_symbols": [r["symbol"] for r in retained if r["computation_status"] == "model_abstained"],
        "model_error_symbols": [r["symbol"] for r in rows if r["computation_status"] == "model_error"],
        "outcome_labels_used": 0, "ai_queue_created": False, "risk_loss_cutoff": loss_cutoff,
        "eps_anchor_mode_counts": dict(Counter(r.get("eps_anchor_mode", "annual") for r in rows)),
        "history_mode_counts": dict(Counter(r.get("history_mode", "original") for r in rows)),
        "history_panel_applied_count": sum(r.get("history_panel_applied", False) for r in rows),
        "history_expanded_count": sum(r.get("history_expanded", False) for r in rows),
        "history_expanded_symbols": [r["symbol"] for r in rows if r.get("history_expanded")],
        "original_annual_history_count_distribution": dict(sorted(Counter(str(r.get("original_annual_history_count")) for r in rows).items())),
        "annual_history_count_distribution": dict(sorted(Counter(str(r.get("annual_history_count")) for r in rows).items())),
        "growth_count_distribution": dict(sorted(Counter(str(r.get("history_growth_observations")) for r in rows).items())),
        "full_eleven_year_history_count": sum(r.get("annual_history_count") == 11 for r in rows),
        "fewer_than_three_growth_observations_count": sum(r.get("history_growth_observations") is not None and r["history_growth_observations"] < 3 for r in rows),
        "ttm_anchor_count": sum(r["input"].get("eps_measure") == "ttm" and r["input"].get("eps0") is not None for r in rows),
        "ttm_missing_or_invalid_count": sum(r.get("eps_anchor_mode") == "ttm" and r["input"].get("eps0") is None for r in rows),
        "threshold_counts": counts, "limitations": LIMITATIONS,
    }


def write_csv(path, rows):
    fields = ["symbol", "company_name", "cik", "test_status", "computation_status", "input_status",
              "price", "eps0", "eps_measure", "eps_period_start", "eps_period_end", "eps_available_at", "loss_probability", "loss_probability_origin",
              "risk_excluded", "p_above_6_5_positive", "minimum_threshold_for_70_percent",
              "positive_conditional_eps_mean", "positive_conditional_eps_median", "flags", "blockers",
              "corporate_action_review_notes", "history_mode", "history_panel_applied", "history_expanded",
              "original_annual_history_count", "annual_history_count", "history_growth_observations"]
    for cutoff in THRESHOLDS:
        fields += [f"p_below_{cutoff:g}", f"probability_pass_{cutoff:g}", f"probability_and_10pct_loss_pass_{cutoff:g}"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            pred = row["prediction"] or {}
            flat = {key: row.get(key) for key in fields}
            flat.update({key: row["input"].get(key) for key in
                         ("price", "eps0", "eps_measure", "eps_period_start", "eps_period_end", "eps_available_at")})
            flat.update({key: pred.get(key) for key in ("p_above_6_5_positive", "positive_conditional_eps_mean", "positive_conditional_eps_median")})
            flat["minimum_threshold_for_70_percent"] = row.get("minimum_threshold_for_70_percent", {}).get("threshold")
            for scan in row["threshold_scenarios"]:
                suffix = f"{scan['buy_fpe_threshold']:g}"
                flat[f"p_below_{suffix}"] = scan["p_below_threshold"]
                flat[f"probability_pass_{suffix}"] = scan["probability_condition_passes"]
                flat[f"probability_and_10pct_loss_pass_{suffix}"] = scan["probability_and_loss_conditions_pass"]
            for key, value in flat.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                flat[key] = value
            writer.writerow(flat)


def write_outputs(out, rows, summary):
    out.mkdir(parents=True, exist_ok=True)
    metadata = {"schema_version": 1, "scope": "unverified_2012_stock_probability_research",
                "as_of": AS_OF, "limitations": LIMITATIONS, "summary": summary}
    for stem, excluded in (("all_stocks", False), ("excluded_stocks", True)):
        selected = [r for r in rows if r["risk_excluded"] is excluded]
        (out / f"{stem}.json").write_text(json.dumps({**metadata, "stocks": selected}, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        write_csv(out / f"{stem}.csv", selected)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# 2012 年全名单：历史回退概率研究", "",
             "**多数输入的股份口径尚未核验。本轮按用户要求，暂假设同份申报中的年度 EPS 与年末名义股价可比，"
             "并接受 SEC 财政日期月末近似，计算研究情景概率。它们不是已验证预测、置信率或收益回测。**", "",
             "当前盈利基准与年度历史分开处理。原始年度历史及扩展面板审计均保留；"
             "扩展只接受与当前输入hash、证券身份、会计及股份口径匹配的面板，缺失或冲突年份不补零。"
             "证据支持的拆股变换单独记录，扩展不把股份口径升级为已核验；其他公司行动仍可能影响结果。", "",
             f"名单 {summary['roster_count']} 只；主表保留 {summary['retained_main_table_count']} 只；"
             f"仅因终点亏损先验≥{summary['risk_loss_cutoff']:.0%}排除 {summary['risk_excluded_count']} 只。", "",
             "高亏损先验股票在运行正盈利模型和阈值测试之前排除，标为 `not_tested_high_loss`；"
             "排除表保留原始数据、单独亏损先验和原因。", "",
             "缺机构共识不排除：仍计算历史回退概率，但不自动启用 AI 候选。当前 EPS 非正、且亏损先验未达排除线的股票"
             "留在主表，标为 `model_abstained`；它们只报告单独计算的亏损状态先验，正盈利分支概率为空。"
             "缺价、缺有效锚点及身份禁止连接的股票以 `data_unavailable` 保留，不填零、不冒充低风险。", "",
             "| 状态（排除前） | 数量 |", "|---|---:|"]
    lines += [f"| {status} | {count} |" for status, count in summary["all_computation_status_counts"].items()]
    lines += ["", "## 阈值敏感性", "",
              "分母仅为主表中成功计算概率的股票；排除、缺数据与弃权另行计数。"
              "‘仅概率’统计 P(E6>P0/K)≥70%；‘概率+10%亏损条件’另外要求终点亏损先验≤10%。"
              "10%只是原买入研究条件，不是本轮主表排除线；主表仅按25%（或显式配置值）排除。", "",
              "| K | 可计算且未排除 | 仅概率≥70% | 概率≥70%且亏损≤10% | 正式模型买入合格 |",
              "|---:|---:|---:|---:|---:|"]
    for row in summary["threshold_counts"]:
        lines.append(f"| {row['threshold']:g} | {row['evaluated_nonexcluded_count']} | {row['probability_at_least_70_count']}"
                     f" | {row['probability_at_least_70_and_loss_at_most_10_count']} | {row['formal_model_buy_eligible_count']} |")
    lines += ["", "固定6.5的正盈利高估概率、各阈值概率及亏损先验见逐股 CSV/JSON。"
              "JSON保留输入、未调整原始证据、变换证据、完整模型、原始评估与每阈值评估。"
              "阈值评估的辅助卖出参数只满足 buy<sell，不构成卖出策略研究。", "",
              "增大 K 必然增加达标数，不能据此选出‘合理’或最优策略。按用户选择的10%年化价格收益情景，"
              "若六年终点交易 PE 假设为15或18，则 K=M/(1.1)^6 约为8.47或10.16。"
              "这是研究假设；缺终点 EPS 标签和回报数据，本轮不能检验它们优于6。", "",
              f"稀疏盈利历史标记共 {summary['sparse_history_count']} 只。模型不足3条正盈利增长率时采用0.20波动下限，"
              "不代表这些公司的实际预测误差已被可靠估计；亏损频率也向6%先验收缩，概率尚未校准。", "",
              "## 留存", "", "本轮使用 research 归档保存源 JSON、配置、程序代码、完整结果和报告；没有清理源数据。"
              "未额外传入原始供应商文件时，归档只含归一化输入及程序证据，不能称为完整原始资料归档。", ""]
    lines += ["## 年度历史覆盖", "",
              f"历史模式：{summary.get('history_mode_counts', {})}；面板应用 {summary.get('history_panel_applied_count', 0)} 只，"
              f"其中新增年度观察 {summary.get('history_expanded_count', 0)} 只；"
              f"拥有11个年度观察 {summary.get('full_eleven_year_history_count', 0)} 只。", "",
              "| 实际年度历史条数 | 个股数 |", "|---:|---:|"]
    lines += [f"| {count} | {stocks} |" for count, stocks in summary.get("annual_history_count_distribution", {}).items()]
    lines += ["", "| 有效年度增长率条数 | 个股数 |", "|---:|---:|"]
    lines += [f"| {count} | {stocks} |" for count, stocks in summary.get("growth_count_distribution", {}).items()]
    lines += ["", "年度条数与有效增长率条数不同：亏损、缺年与非相邻期间不会制造增长样本。"
              "扩展仍是明确标记的研究假设，匹配重叠EPS不等于完整核验了股份或会计可比性。", ""]
    notes = summary.get("share_adjustment_review", {}).get("review_notes", [])
    if notes:
        lines += ["## 已知企业范围变化", "", "以下股票仍按本轮规则保留测试，但原申报总EPS与年末保留业务的可比性不足，不能把结果作为持续经营预测。逐股CSV/JSON也保留此提示。", ""]
        lines += [f"- [{note['symbol']}]({note['source_url']}): {note['note']}" for note in notes]
    if summary.get("eps_anchor_mode_counts", {}).get("ttm"):
        lines[0] = "# 2012 年全名单：TTM盈利基准与年度历史研究"
        lines[2:6] = [
            "本轮当前EPS必须来自显式TTM证据；缺TTM的股票保留为数据缺失，不静默退回旧年度EPS。多数股份口径仍未完整核验。", "",
            "历史增长、波动与亏损频率继续使用不重叠的年度历史。年报拆股倍率只作用于该年报历史，TTM各期须另行核对。", "",
        ]
        lines += ["## TTM覆盖与口径", "",
                  f"已接入TTM基准 {summary['ttm_anchor_count']} 只；缺失或无效 {summary['ttm_missing_or_invalid_count']} 只。", "",
                  "四个单季稀释EPS之和是本轮TTM研究口径；由于分母及稀释规则可能变化，不等于重新计算的完整年度GAAP稀释EPS。"
                  "TTM与原年度结果使用不同输出目录，原研究保留。", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--members", type=Path, default=ROOT / "data/universe_2012_members.json")
    parser.add_argument("--prices", type=Path, default=ROOT / "data/research_2012_prices.json")
    parser.add_argument("--companies", type=Path, default=ROOT / "data/research_2012_sec_eps.json")
    parser.add_argument("--manual", type=Path, default=ROOT / "data/research_2012_snapshots.json")
    parser.add_argument("--adjustments", type=Path, default=ROOT / "data/research_2012_share_adjustments.json")
    parser.add_argument("--supplemental-companies", type=Path, default=ROOT / "data/research_2012_supplemental_eps.json")
    parser.add_argument("--eps-anchor", choices=("ttm", "annual"), default="ttm",
                        help="Use evidenced TTM by default; annual explicitly reproduces legacy research.")
    parser.add_argument("--ttm-anchors", type=Path, default=ROOT / "data/research_2012_ttm_anchors.json")
    parser.add_argument("--history-mode", choices=("expanded", "original"), default="expanded",
                        help="Apply evidenced history panels by default; original preserves prior annual histories.")
    parser.add_argument("--history-panels", type=Path, default=ROOT / "data/research_2012_history_panels.json")
    parser.add_argument("--exclude-loss-probability", type=float, default=0.25)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive-root", type=Path, default=ROOT / "outputs/run_archive")
    parser.add_argument("--raw-input", action="append", type=Path, default=[])
    args = parser.parse_args()
    if args.output is None:
        if args.history_mode == "expanded":
            directory = "stocks_2012_ttm_history_expanded" if args.eps_anchor == "ttm" else "stocks_2012_annual_history_expanded"
        else:
            directory = "stocks_2012_ttm_probability_test" if args.eps_anchor == "ttm" else "stocks_2012_probability_test"
        args.output = ROOT / "outputs" / directory
    inputs = {key: getattr(args, key) for key in ("members", "prices", "companies", "manual")}
    for label in ("adjustments", "supplemental_companies"):
        if getattr(args, label) is not None:
            inputs[label] = getattr(args, label)
    ttm_anchors = []
    history_panels = None
    if args.history_mode == "expanded":
        if not args.history_panels.exists():
            parser.error("History panel file missing; run build_2012_history.py or explicitly select --history-mode original.")
        inputs["history_panels"] = args.history_panels
        history_payload = load_json(args.history_panels)
        if history_payload.get("as_of") != AS_OF:
            parser.error("History panel file must state as_of=2012-12-31")
        history_panels = history_payload["panels"]
    if args.eps_anchor == "ttm":
        if not args.ttm_anchors.exists():
            parser.error("TTM anchor file missing; supply --ttm-anchors. Annual fallback requires explicit --eps-anchor annual.")
        inputs["ttm_anchors"] = args.ttm_anchors
        ttm_payload = load_json(args.ttm_anchors)
        if ttm_payload.get("as_of") != AS_OF:
            parser.error("TTM anchor file must state as_of=2012-12-31")
        ttm_anchors = ttm_payload["anchors"]
    rows = assemble(load_json(args.members)["members"], load_json(args.prices)["prices"],
                    load_json(args.companies)["companies"], load_snapshots(args.manual),
                    adjustments=load_json(args.adjustments)["adjustments"] if args.adjustments else [],
                    supplemental=load_json(args.supplemental_companies)["companies"] if args.supplemental_companies else [],
                    loss_cutoff=args.exclude_loss_probability, eps_anchor=args.eps_anchor, ttm_anchors=ttm_anchors,
                    history_panels=history_panels)
    summary = summarize(rows, args.exclude_loss_probability)
    if args.adjustments:
        summary["share_adjustment_review"] = load_json(args.adjustments)
        by_symbol = {row["symbol"]: row for row in rows}
        for note in summary["share_adjustment_review"].get("review_notes", []):
            row = by_symbol[note["symbol"]]
            row.setdefault("corporate_action_review_notes", []).append(deepcopy(note))
            row["flags"] = sorted(set(row["flags"]) | {"corporate_scope_change_not_reconciled"})
    args.output.mkdir(parents=True, exist_ok=True)
    config_path = args.output / "research_config.json"
    config_path.write_text(json.dumps({"model_defaults": LEGACY_MODEL_CONFIG, "thresholds": THRESHOLDS,
                                      "loss_exclusion_cutoff": args.exclude_loss_probability,
                                      "eps_anchor_mode": args.eps_anchor,
                                      "history_mode": args.history_mode,
                                      "research_scenario": SCENARIO, "limitations": LIMITATIONS}, indent=2) + "\n", encoding="utf-8")
    inputs.update(config=config_path, legacy_thresholds=LEGACY_CONFIG_PATH, analysis_script=Path(__file__),
                  threshold_script=ROOT / "scripts/analyze_stock_thresholds.py", model_source=ROOT / "src/stocking/model.py",
                  ttm_source=ROOT / "src/stocking/ttm.py", history_source=ROOT / "src/stocking/history.py")
    if args.history_mode == "expanded":
        inputs.update(history_builder_source=ROOT / "scripts/build_2012_history.py",
                      history_download_source=ROOT / "scripts/download_sec_history.py")
    summary["input_sha256"] = {label: hashlib.sha256(path.read_bytes()).hexdigest() for label, path in inputs.items()}
    write_outputs(args.output, rows, summary)
    run = archive_run(args.archive_root, inputs, mode="research", raw_paths=args.raw_input)
    finalize_run(run, artifacts={"summary": args.output / "summary.json", "report": args.output / "report.md"},
                 working_files={stem: args.output / f"{stem}.json" for stem in ("all_stocks", "excluded_stocks")}
                 | {f"{stem}_csv": args.output / f"{stem}.csv" for stem in ("all_stocks", "excluded_stocks")},
                 metadata={"scope": SCENARIO, "summary": summary})
    (args.output / "run_lifecycle.json").write_text(json.dumps({"mode": "research", "run_archive": str(run),
                                                               "raw_originals_retained": True, "cleaned_files": 0}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key.endswith("count") or key.endswith("counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
