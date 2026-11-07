"""New observed-information research admission preserves old inputs and gaps."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_consensus_rerank import ROOT, admit_snapshot, apply_reviewed_price, build, calendar_fiscal_evidence, latest_actual_guard, load_ttm_updates, sha
from normalize_current_consensus import parse_collected, verify_sources
from test_fiscal_calendar import issuer_fixture


CUTOFF = "2026-11-07T02:30:00+00:00"


def snapshot():
    return {"symbol": "TEST", "cik": "0000000001", "as_of": "2026-11-07", "currency": "USD",
            "eps_basis": "GAAP_diluted", "share_basis": "current_shares", "consensus": [],
            "price": 100, "price_date": "2026-11-05", "eps0": 2.5, "eps_measure": "ttm",
            "history": [{"period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "eps": 2,
                         "available_at": "2026-02-02", "accession": "SEC-2026",
                         "source_url": "https://www.sec.gov/Archives/edgar/data/1/annual.htm"}
                        for year in (2023, 2024, 2025)]}


def security(actual):
    raw = {"symbol": "TEST", "retrieval_finished_at": "2026-11-07T01:00:01+00:00",
           "response": {"quoteSummary": {"result": [{"price": {"symbol": "TEST"}, "earningsTrendGaap": {
               "defaultMethodology": "nongaap", "trend": [
                   {"period": period, "endDate": end, "earningsEstimate": {"avg": {"raw": mean}, "numberOfAnalysts": {"raw": 10},
                       "yearAgoEps": {"raw": previous}, "earningsCurrency": "USD"}}
                   for period, end, mean, previous in (("0y", "2026-12-31", 3, 2), ("+1y", "2027-12-31", 4, 3))]}}]}}}
    return parse_collected(raw, {"symbol": "TEST", "cik": "0000000001"}, as_of="2026-11-07", snapshot=actual,
                           source_audit={"verified_matching_response": True, "sources": [], "response_retrieved_at": "2026-11-07T01:00:00+00:00"})


class ConsensusAdmissionTests(unittest.TestCase):
    def test_new_intraday_policy_keeps_original_staging_and_actuals(self):
        actual = snapshot()
        observed = security(actual)
        originals = deepcopy((actual, observed))
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertEqual(audit["admitted_annual_count"], 2)
        self.assertEqual(updated["consensus_rerank_group"], "consensus_research_join")
        for row in updated["consensus"]:
            self.assertEqual(row["available_at"], "2026-11-07")
            self.assertEqual(row["original_staged_available_at"], "2026-11-08")
            self.assertIsNone(row["eps_std"])
            self.assertFalse(row["full_economic_basis_verified"])
        for field in ("history", "eps0", "eps_measure", "price", "price_date"):
            self.assertEqual(updated[field], actual[field])
        self.assertEqual((actual, observed), originals)

    def test_week_based_calendar_and_prior_eps_mismatch_stay_history_only(self):
        actual = snapshot()
        observed = security(actual)
        for year, end, start in ((2023, "2023-12-30", "2023-01-01"), (2024, "2024-12-28", "2023-12-31"), (2025, "2025-12-27", "2024-12-29")):
            row = actual["history"][year - 2023]
            row.update(period_start=start, period_end=end)
        self.assertFalse(calendar_fiscal_evidence(actual, "2026-11-07")["supported"])
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertFalse(updated["consensus"])
        self.assertIn("fiscal_date_requires_independent_review", audit["observations"][0]["reasons"])
        actual = snapshot()
        observed = security(actual)
        observed["observations"][0]["year_ago_eps_as_reported"] = 1.5
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertEqual(updated["consensus_rerank_group"], "history_only_pending_consensus_review")
        self.assertEqual(audit["year_ago_eps_support"]["status"], "mismatch_requires_review")

    def test_ended_current_year_keeps_only_forward_next_year(self):
        actual = snapshot()
        for row in actual["history"]:
            year = int(row["period_end"][:4])
            row.update(period_start=f"{year-1}-11-01", period_end=f"{year}-10-31")
        observed = security(actual)
        for row, target in zip(observed["observations"], ("2026-10-31", "2027-10-31")):
            row["as_reported_target_date"] = target
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertEqual([r["target_date"] for r in updated["consensus"]], ["2027-10-31"])
        self.assertIn("reported_annual_target_already_ended", audit["observations"][0]["reasons"])

    def test_future_observation_cannot_enter_even_with_matching_day(self):
        actual = snapshot()
        observed = security(actual)
        for row in observed["observations"]:
            row["retrieved_at"] = "2026-11-07T03:00:00+00:00"
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertFalse(updated["consensus"])
        self.assertIn("retrieved_after_information_cutoff", audit["observations"][0]["reasons"])

    def test_nonpositive_forecast_is_admitted_for_risk_exclusion_not_erased(self):
        actual = snapshot()
        observed = security(actual)
        observed["observations"][0]["eps_mean"] = -3
        updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07", information_cutoff=CUTOFF)
        self.assertEqual(updated["consensus"][0]["eps_mean"], -3)
        self.assertEqual(audit["negative_or_zero_admitted_eps_count"], 1)

    def test_price_replacement_checks_source_delay_and_keeps_original_price(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            raw_file = Path(directory) / "quote.json"
            collected = Path(directory) / "collected.json"
            market = "2026-11-06T21:00:00+00:00"
            raw = {"quoteSummary": {"result": [{"price": {"symbol": "TEST", "currency": "USD",
                "regularMarketPrice": {"raw": 101}, "regularMarketTime": {"raw": int(datetime.fromisoformat(market).timestamp())}}}]}}
            raw_file.write_text(json.dumps(raw), encoding="utf-8")
            collected.write_text(json.dumps({"response": raw}), encoding="utf-8")
            review = {"symbol": "TEST", "price_observation_valid": True, "currency": "USD", "validation_errors": [],
                      "old_price": 100, "old_price_date": "2026-11-05", "new_price": 101, "new_price_date": "2026-11-06",
                      "new_price_kind": "vendor_regular_session_quote", "exchange_data_delayed_by_minutes": 15,
                      "regular_market_timestamp": market, "session_completed_plus15min_at": "2026-11-06T21:15:00+00:00",
                      "retrieved_at": "2026-11-07T01:00:00+00:00",
                      "new_source": {"raw_file": str(raw_file), "sha256": sha(raw_file),
                                     "collected_file": str(collected), "collected_sha256": sha(collected)}}
            actual = snapshot()
            updated, evidence = apply_reviewed_price(actual, review, information_cutoff=CUTOFF)
            self.assertEqual(updated["price"], 101)
            self.assertEqual(updated["prior_run_price_comparison"]["price"], 100)
            self.assertEqual(updated["history"], actual["history"])
            self.assertEqual(len(evidence), 2)
            with self.assertRaisesRegex(ValueError, "delay"):
                apply_reviewed_price(actual, review | {"retrieved_at": "2026-11-06T21:10:00+00:00"}, information_cutoff=CUTOFF)
            with self.assertRaisesRegex(ValueError, "archived"):
                apply_reviewed_price(actual, review | {"new_price": 102}, information_cutoff=CUTOFF)

    def test_evidenced_new_ttm_replaces_only_existing_compatible_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            base_file, update_file, source_file = (Path(directory) / name for name in ("base.json", "updates.json", "actual.json"))
            old = snapshot()
            old.update(eps_period_start="2025-07-01", eps_period_end="2026-06-30", eps_available_at="2026-08-01",
                       source={"actual": "https://example.org/actual"})
            new = deepcopy(old)
            new.update(eps0=3.1, eps_period_start="2025-10-01", eps_period_end="2026-09-30", eps_available_at="2026-11-07")
            new["ttm_provenance"] = {"method": "direct_reported_ttm", "components": [{
                "period_start": new["eps_period_start"], "period_end": new["eps_period_end"], "available_at": new["eps_available_at"],
                "eps": 3.1, "eps_basis": new["eps_basis"], "share_basis": new["share_basis"], "source_url": "https://example.org/actual"}]}
            base_file.write_text(json.dumps({"snapshots": [old]}), encoding="utf-8")
            source_file.write_text("reviewed actual data fixture", encoding="utf-8")
            new["ttm_refresh_source_observations"] = {"source": {"symbol": "TEST", "cik": "0000000001",
                "status": 200, "retrieved_at": "2026-11-07T01:30:00+00:00", "file": str(source_file), "sha256": sha(source_file)}}
            payload = {"base_snapshots_sha256": sha(base_file), "snapshots": [new],
                       "validation": {"status": "verified_research_inputs", "rationale": "Newly reported full TTM verified.",
                                      "evidence_files": [{"path": str(source_file), "sha256": sha(source_file)}]}}
            update_file.write_text(json.dumps(payload), encoding="utf-8")
            updates, files, audit, unavailable = load_ttm_updates(update_file, base_file, [old], information_cutoff=CUTOFF)
            self.assertEqual(updates["TEST"]["eps_period_end"], "2026-09-30")
            self.assertEqual(audit[0]["before"], old)
            self.assertEqual(len(files), 3)
            self.assertFalse(unavailable)
            payload["snapshots"][0]["share_basis"] = "other_shares"
            update_file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identity/basis"):
                load_ttm_updates(update_file, base_file, [old], information_cutoff=CUTOFF)
            payload["snapshots"][0]["share_basis"] = old["share_basis"]
            update_file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cutoff"):
                load_ttm_updates(update_file, base_file, [old], information_cutoff="2026-11-07T01:00:00+00:00")
            payload["snapshots"] = []
            payload["unavailable_symbols"] = ["TEST"]
            update_file.write_text(json.dumps(payload), encoding="utf-8")
            updates, _, _, unavailable = load_ttm_updates(update_file, base_file, [old], information_cutoff=CUTOFF)
            self.assertFalse(updates)
            self.assertEqual(unavailable, {"TEST"})

    def test_newer_actual_blocks_old_ttm_without_summing_quarters(self):
        actual = snapshot() | {"eps_period_end": "2026-06-30"}
        observed = security(actual)
        observed["raw_modules"]["earningsHistoryGaap"] = {"history": [{
            "quarter": {"raw": datetime.fromisoformat("2026-09-30T00:00:00+00:00").timestamp()},
            "epsActual": {"raw": 99}, "currency": "USD"}]}
        guard = latest_actual_guard(actual, observed, CUTOFF)
        self.assertTrue(guard["blocks_old_ttm"])
        self.assertEqual(guard["period_gap_days"], 92)
        self.assertFalse(guard["quarterly_eps_summed_or_substituted"])
        self.assertFalse(latest_actual_guard(actual | {"eps_period_end": "2026-09-30"}, observed, CUTOFF)["blocks_old_ttm"])

    def test_reviewed_week_calendar_joins_prior_fiscal_year_and_never_relabels_ttm(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            weekly, config = issuer_fixture(directory, symbol="TEST")
            actual = snapshot() | weekly | {"eps_period_start": "2025-07-28", "eps_period_end": "2026-07-26"}
            observed = security(actual)
            for row, target in zip(observed["observations"], ("2027-01-31", "2028-01-31")):
                row["as_reported_target_date"] = target
            original = deepcopy((actual, observed))
            updated, audit = admit_snapshot(actual, observed, as_of="2026-11-07",
                information_cutoff="2026-11-07T13:00:00+00:00", fiscal_calendar=config)
            self.assertEqual([row["target_date"] for row in updated["consensus"]], ["2027-01-31", "2028-01-30"])
            self.assertEqual(updated["consensus"][1]["as_reported_target_date"], "2028-01-31")
            self.assertEqual(audit["year_ago_eps_support"]["actual_period_end"], "2026-01-25")
            self.assertEqual(audit["year_ago_eps_support"]["status"], "matching_supporting_evidence")
            self.assertFalse(updated["consensus"][0]["fiscal_calendar_mapping"]["mapping_applied"])
            for field in ("history", "eps0", "eps_period_start", "eps_period_end"):
                self.assertEqual(updated[field], actual[field])
            self.assertEqual((actual, observed), original)
            for bad_config, bad_observed in ((config | {"evidence": []}, observed),
                    (config, observed | {"module_used": "earningsTrend"})):
                rejected, _ = admit_snapshot(actual, bad_observed, as_of="2026-11-07",
                    information_cutoff="2026-11-07T13:00:00+00:00", fiscal_calendar=bad_config)
                self.assertFalse(rejected["consensus"])

    def test_later_same_day_actual_evidence_is_not_used(self):
        actual = snapshot() | {"eps_period_end": "2026-06-30",
                               "source": {"actual": {"retrieved_at": "2026-11-07T03:00:00+00:00"}}}
        with self.assertRaisesRegex(ValueError, "observation follows information cutoff"):
            admit_snapshot(actual, security(actual), as_of="2026-11-07", information_cutoff=CUTOFF)
        observed = security(actual)
        observed["source_audit"]["response_retrieved_at"] = "2026-11-07T03:00:00+00:00"
        observed["raw_modules"]["earningsHistoryGaap"] = {"history": [{
            "quarter": {"raw": datetime.fromisoformat("2026-09-30T00:00:00+00:00").timestamp()},
            "epsActual": {"raw": 99}, "currency": "USD"}]}
        guard = latest_actual_guard(actual, observed, CUTOFF)
        self.assertFalse(guard["blocks_old_ttm"])
        self.assertTrue(guard["source_observation_after_cutoff_excluded"])

    def test_explicit_actuals_and_collected_paths_build_fresh_output_with_audit(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            study = Path(directory)
            def save(relative, value):
                path = study / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
                return path
            weekly, config = issuer_fixture(directory, symbol="TEST")
            actual = snapshot() | weekly | {"eps_period_start": "2025-07-01", "eps_period_end": "2026-06-30",
                                   "eps_available_at": "2026-08-01", "source": {"actual": "https://example.org/actual"}}
            actual["ttm_provenance"] = {"method": "direct_reported_ttm", "components": [{
                "period_start": actual["eps_period_start"], "period_end": actual["eps_period_end"],
                "available_at": actual["eps_available_at"], "eps": actual["eps0"],
                "eps_basis": actual["eps_basis"], "share_basis": actual["share_basis"],
                "source_url": "https://example.org/actual"}]}
            actual_file = save("new_actuals.json", {"snapshots": [actual]})
            save("normalized_latest_available_reviewed/snapshots.json", {"snapshots": [actual]})
            member = {"symbol": "TEST", "cik": "0000000001"}
            missing = {"symbol": "MISSING", "cik": "0000000002"}
            universe = save("input/universe.json", {"members": [member, missing]})
            market = "2026-11-06T21:00:00+00:00"
            modules = security(actual)["raw_modules"]
            for row, target in zip(modules["earningsTrendGaap"]["trend"], ("2027-01-31", "2028-01-31")):
                row["endDate"] = target
            modules["price"] = {"symbol": "TEST", "currency": "USD", "regularMarketPrice": {"raw": 101},
                "regularMarketTime": {"raw": int(datetime.fromisoformat(market).timestamp())}}
            response = {"quoteSummary": {"result": [modules]}}
            raw_file = save("raw/response.json", response)
            record = {"symbol": "TEST", "response": response, "sources": [{
                "file": str(raw_file), "sha256": sha(raw_file), "status": 200,
                "retrieved_at": "2026-11-07T01:00:00+00:00"}]}
            collected = save("custom_collected/TEST_collected.json", record)
            staged_security = parse_collected(record, member, as_of="2026-11-07", snapshot=None,
                                             source_audit=verify_sources(record))
            # Stage the earlier actual cohort and an old storage location. The
            # new explicit inputs reparse identical raw data and audit both hashes.
            staged_security["collected_archive"] = {"file": "unused_old_location.json", "sha256": sha(collected)}
            stage = save("custom_staged.json", {"actual_snapshots_sha256": "1" * 64,
                "universe_sha256": sha(universe), "securities": [staged_security,
                    parse_collected(None, missing, as_of="2026-11-07")]})
            price = save("price_review.json", {"prices": {"TEST": {
                "symbol": "TEST", "price_observation_valid": True, "currency": "USD", "validation_errors": [],
                "old_price": 100, "old_price_date": "2026-11-05", "new_price": 101, "new_price_date": "2026-11-06",
                "new_price_kind": "vendor_regular_session_quote", "exchange_data_delayed_by_minutes": 15,
                "regular_market_timestamp": market, "session_completed_plus15min_at": "2026-11-06T21:15:00+00:00",
                "retrieved_at": "2026-11-07T01:00:00+00:00", "new_source": {
                    "raw_file": str(raw_file), "sha256": sha(raw_file), "collected_file": str(collected),
                    "collected_sha256": sha(collected)}}}})
            frozen = save("consensus_rerank_v5/input/sentinel.json", {"frozen": True})
            calendars = save("reviewed_rules.json", {"schema_version": 1, "calendars": {"TEST": config}})
            before = {str(path): sha(path) for path in (frozen, actual_file, stage, universe, collected, raw_file)}
            output = study / "new_reconciliation/input"
            options = dict(information_cutoff="2026-11-07T13:00:00+00:00", as_of="2026-11-07", output_dir=output,
                price_review=price, consensus_observations=stage, collected_dir=collected.parent, fiscal_calendars=calendars)
            with self.assertRaisesRegex(ValueError, "cohort hashes"):
                build(study, **options)
            result = build(study, actuals=actual_file, **options)
            self.assertEqual(result["assembled_model_input_count"], 1)
            self.assertEqual(result["universe_security_count"], 2)
            self.assertEqual(result["missing_actual_snapshot_count"], 1)
            self.assertEqual(result["admitted_annual_observation_count"], 2)
            self.assertEqual(result["changed_forecast_target_date_count"], 1)
            self.assertEqual(result["reviewed_fiscal_calendar_admitted_observation_count"], 2)
            policy = json.loads((output / "policy.json").read_text())
            self.assertTrue(policy["explicit_actual_input_override"])
            self.assertEqual(policy["original_staged_actuals_sha256"], "1" * 64)
            self.assertEqual(policy["actuals_sha256"], sha(actual_file))
            self.assertEqual(before, {path: sha(path) for path in before})
            with self.assertRaisesRegex(ValueError, "overwrite"):
                build(study, actuals=actual_file, **options)
            with self.assertRaisesRegex(ValueError, "dedicated study"):
                build(study, actuals=actual_file, **(options | {"output_dir": study / "input"}))


if __name__ == "__main__":
    unittest.main()
