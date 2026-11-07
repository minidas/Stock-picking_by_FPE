"""Latest financial vintages retain revision, entity and share-basis evidence."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from stocking.history import HistoryError, extend_annual_history

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_current_snapshots as current


def annual(year, eps, publication="2026-02-02", accession="CURRENT"):
    return {"period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "eps": eps,
            "available_at": publication, "measure": "annual", "tag": "EarningsPerShareDiluted", "unit": "USD/shares",
            "eps_basis": "GAAP_diluted", "share_basis": "current_shares", "currency": "USD",
            "accession": accession, "source_url": "https://example.org/filing/" + accession}


def snapshot():
    return {"symbol": "TEST", "cik": "0000000001", "as_of": "2026-11-07", "currency": "USD",
            "eps_basis": "GAAP_diluted", "share_basis": "current_shares", "context": {}, "eps0": 5.6,
            "eps_measure": "ttm", "eps_period_end": "2026-09-30",
            "history": [annual(2023, 3.1), annual(2024, 4), annual(2025, 5)]}


def filing(accession="OLD", values=((2021, 1), (2022, 2), (2023, 3)), publication="2024-02-02"):
    return {"cik": "0000000001", "accession": accession, "available_at": publication,
            "source_url": "https://example.org/filing/" + accession, "identity_join_allowed": True,
            "eps_basis": "GAAP_diluted", "share_basis": "current_shares", "context": {},
            "history": [annual(y, e, publication, accession) for y, e in values]}


def boundary_evidence():
    return {"source_url": "https://example.org/common-control-merger", "available_at": "2024-02-02",
            "rationale": "Combined entity was retrospectively presented from the 2021 annual period."}


def sec_annual(year, eps, accession, filed):
    return {"start": f"{year}-01-01", "end": f"{year}-12-31", "filed": filed,
            "accn": accession, "val": eps, "form": "10-K", "tag": "EarningsPerShareDiluted",
            "duration_days": (date(year, 12, 31) - date(year, 1, 1)).days + 1}


class LatestHistoryTests(unittest.TestCase):
    def test_default_latest_extends_through_restatement_and_records_old_values(self):
        s, older = snapshot(), filing()
        originals = deepcopy((s, older))
        history, audit = extend_annual_history(s, [older])
        self.assertEqual([r["eps"] for r in history], [1, 2, 3.1, 4, 5])
        self.assertEqual(audit["conflict_policy"], "latest_available")
        correction = next(r for r in audit["revision_audit"] if r["value_changed"])
        self.assertEqual((correction["before"]["eps"], correction["after"]["eps"]), (3, 3.1))
        self.assertEqual(correction["before"]["source_accession"], "OLD")
        self.assertEqual((s, older), originals)
        strict, strict_audit = extend_annual_history(s, [older], conflict_policy="strict_overlap")
        self.assertEqual(strict, s["history"])
        self.assertTrue(strict_audit["pending_filings"])

    def test_newer_filing_can_replace_seed_including_negative_eps(self):
        revised = filing("REVISION", ((2023, -2),), "2026-10-02")
        history, audit = extend_annual_history(snapshot(), [revised])
        self.assertEqual(history[0]["eps"], -2)
        self.assertEqual(history[0]["accession"], "REVISION")
        self.assertEqual(audit["historical_nonpositive_observations"], 1)
        self.assertEqual(audit["history_growth_observations"], 1)

    def test_future_filing_and_row_do_not_override_current(self):
        future_filing = filing("FUTURE", ((2023, 99),), "2027-02-02")
        future_row = filing("ROW", ((2023, 99),), "2026-10-02")
        future_row["history"][0]["available_at"] = "2027-02-02"
        for f in (future_filing, future_row):
            with self.subTest(accession=f["accession"]):
                history, audit = extend_annual_history(snapshot(), [f])
                self.assertEqual([r["eps"] for r in history], [3.1, 4, 5])
                self.assertTrue(audit["pending_filings"])

    def test_mapping_or_retrieval_date_cannot_win_financial_version_order(self):
        s = snapshot()
        for r in s["history"]:
            r["financial_filed_date"] = "2026-02-01"
            r["available_at"] = "2026-11-07"  # Observed now, financial disclosure was February.
        newer = filing("REVISED", ((2023, 3.2),), "2026-10-02")
        history, _ = extend_annual_history(s, [newer])
        self.assertEqual(history[0]["eps"], 3.2)
        older = filing("OLDER", ((2023, 6),), "2025-02-02")
        bridge = {"source_accession": "OLDER", "target_share_basis": "current_shares", "eps_multiplier": .5,
                  "source_url": "https://example.org/split", "available_at": "2026-10-02"}
        history, audit = extend_annual_history(snapshot(), [older], reviewed_bridges=[bridge])
        self.assertEqual(history[0]["eps"], 3.1)
        self.assertEqual(audit["revision_audit"][0]["before"]["source_financial_available_at"], "2025-02-02")
        # Candidate retrieval availability is not its financial publication.
        older["available_at"] = "2026-11-07"
        older["history"][0].update(available_at="2026-11-07", financial_filed_date="2025-02-01")
        history, audit = extend_annual_history(snapshot(), [older])
        self.assertEqual(history[0]["eps"], 3.1)
        self.assertEqual(audit["revision_audit"][0]["before"]["source_financial_available_at"], "2025-02-02")

    def test_latest_same_day_conflict_is_pending_without_older_fallback(self):
        a = filing("A", ((2025, 5.2),), "2026-10-02")
        b = filing("B", ((2025, 5.3),), "2026-10-02")
        results = []
        for files in ([a, b], [b, a]):
            history, audit = extend_annual_history(snapshot(), files)
            results.append(history)
            self.assertEqual([r["period_end"] for r in history], ["2023-12-31", "2024-12-31"])
            self.assertEqual(audit["pending_periods"][0]["reason"], "same_day_latest_annual_eps_conflict")
            self.assertEqual(len(audit["pending_periods"][0]["observations"]), 3)
        self.assertEqual(results[0], results[1])

    def test_latest_same_day_conflict_inside_filing_is_not_hidden_by_old_seed(self):
        f = filing("BAD", ((2025, 5.2), (2025, 5.3)), "2026-10-02")
        history, audit = extend_annual_history(snapshot(), [f])
        self.assertNotIn("2025-12-31", {r["period_end"] for r in history})
        self.assertTrue(audit["pending_periods"])

    def test_newer_clear_version_resolves_older_same_day_ambiguity(self):
        files = [filing("A", ((2023, 2),), "2025-02-02"), filing("B", ((2023, 4),), "2025-02-02")]
        history, audit = extend_annual_history(snapshot(), files)
        self.assertEqual(history[0]["eps"], 3.1)
        self.assertFalse(audit["pending_periods"])

    def test_different_start_for_same_end_is_pending_not_same_fiscal_year(self):
        f = filing("DIFFERENT_PERIOD", ((2024, 4.1),), "2026-10-02")
        f["history"][0]["period_start"] = "2024-01-02"  # Still 365 days, different complete period.
        history, audit = extend_annual_history(snapshot(), [f])
        self.assertNotIn("2024-12-31", {r["period_end"] for r in history})
        self.assertEqual(audit["pending_periods"][0]["reason"], "annual_period_definition_conflict")

    def test_identity_accounting_units_context_still_restrict_latest(self):
        changes = ({"cik": "0000000002"}, {"identity_join_allowed": False}, {"eps_basis": "adjusted"},
                   {"share_basis": "different_shares"}, {"context": {"segments": "ClassB", "coreg": ""}})
        files = [filing("WRONG", ((2023, 99),), "2026-10-02") | change for change in changes]
        wrong_unit = filing("UNIT", ((2023, 99),), "2026-10-02")
        wrong_unit["history"][0]["unit"] = "EUR/shares"
        files.append(wrong_unit)
        for f in files:
            with self.subTest(f=f):
                history, audit = extend_annual_history(snapshot(), [f])
                self.assertEqual([r["eps"] for r in history], [3.1, 4, 5])
                self.assertTrue(audit["pending_filings"])

    def test_entity_boundary_excludes_standalone_years_and_retains_recast_years(self):
        f = filing("STANDALONE", ((2019, 10), (2020, 11), (2021, .81), (2022, 2.1)), "2023-02-02")
        combined = filing("COMBINED", ((2021, 7.94), (2022, 8.05), (2023, 3.1)), "2024-02-02")
        history, audit = extend_annual_history(snapshot(), [f, combined], entity_basis_start="2021-01-01",
                                               entity_basis_evidence=boundary_evidence())
        self.assertEqual([r["eps"] for r in history], [7.94, 8.05, 3.1, 4, 5])
        self.assertEqual({r["period_end"] for r in audit["entity_boundary_exclusions"]}, {"2019-12-31", "2020-12-31"})
        with self.assertRaises(HistoryError):
            extend_annual_history(snapshot(), [f], entity_basis_start="2021-01-01")
        with self.assertRaises(HistoryError):
            extend_annual_history(snapshot(), [f], entity_basis_start="2021-01-01",
                                   entity_basis_evidence=boundary_evidence() | {"available_at": "2027-01-01"})

    def test_disconnected_negative_and_zero_history_remains_real_with_gaps(self):
        f = filing("OLD", ((2015, -1), (2017, 0), (2020, 2)), "2021-02-02")
        history, audit = extend_annual_history(snapshot(), [f])
        self.assertEqual([r["eps"] for r in history], [-1, 0, 2, 3.1, 4, 5])
        self.assertIn(2016, audit["missing_calendar_years_in_window"])
        self.assertEqual(audit["history_growth_observations"], 2)

    def test_observation_cap_audit_excludes_twelfth_annual_period(self):
        s = snapshot()
        # Twelve complete 52-week years can end within eleven calendar years.
        first_end = date(2015, 1, 3)
        s["history"] = []
        for index in range(12):
            end = first_end + timedelta(days=364 * index)
            row = annual(end.year, index + 1)
            row.update(period_start=(end - timedelta(days=363)).isoformat(), period_end=end.isoformat())
            s["history"].append(row)
        history, audit = extend_annual_history(s, [])
        self.assertEqual(history, s["history"][1:])
        self.assertEqual(audit["history_count"], 11)
        selections = audit["period_selection_audit"]
        self.assertEqual(len(selections), 12)
        self.assertEqual(selections[0]["status"], "excluded_by_observation_cap")
        self.assertFalse(selections[0]["kept_in_history"])
        self.assertEqual(selections[0]["selected"]["eps"], 1)
        self.assertEqual({r["period_end"] for r in selections if r["status"] == "selected"},
                         {r["period_end"] for r in history})
        self.assertTrue(all(r["kept_in_history"] for r in selections[1:]))


class LatestNormalizerTests(unittest.TestCase):
    def rows(self):
        return [sec_annual(y, e, "CURRENT", "2026-02-01") for y, e in ((2023, 3.1), (2024, 4), (2025, 5))] + [
            sec_annual(y, e, "OLD", "2024-02-01") for y, e in ((2021, 1), (2022, 2), (2023, 3))]

    def test_normalizer_default_uses_latest_and_preserves_revision_audit(self):
        history, audit, _ = current.annual_chain(self.rows(), {"symbol": "TEST", "cik": "0000000001"},
                                                 "current_shares", "2026-11-07")
        self.assertEqual([r["eps"] for r in history], [1, 2, 3.1, 4, 5])
        self.assertEqual(audit["conflict_policy"], "latest_available")

    def test_split_disclosure_is_only_guard_and_never_inferred_multiplier(self):
        rows = self.rows()
        rows[-1]["val"] = 12.4
        disclosure = {"accn": "QUARTERLY_ONLY", "val": 4, "end": "2020-12-31", "filed": "2024-06-01",
                      "form": "10-Q", "source_url": "https://example.org/split"}
        history, audit, _ = current.annual_chain(rows, {"symbol": "TEST", "cik": "0000000001"},
                                                 "current_shares", "2026-11-07", split_disclosures=[disclosure, disclosure])
        self.assertEqual([r["eps"] for r in history], [3.1, 4, 5])
        self.assertEqual(audit["share_basis_extension_guard"]["guarded_accessions"], ["OLD"])
        self.assertEqual(len(audit["share_basis_exclusions"]), 2)
        self.assertTrue(all(r["history_extension_provenance"]["multiplier_applied_by_this_module"] == 1
                            for r in audit["share_basis_exclusions"]))

    def test_unrelated_old_split_disclosure_does_not_block_newer_revisions(self):
        disclosure = {"accn": "UNRELATED_OLD_REPORT", "val": 7, "end": "2010-12-31", "filed": "2011-02-01"}
        history, audit, _ = current.annual_chain(self.rows(), {"symbol": "TEST", "cik": "0000000001"},
                                                 "current_shares", "2026-11-07", split_disclosures=[disclosure])
        self.assertEqual(len(history), 5)
        self.assertFalse(audit["share_basis_extension_guard"]["guarded_accessions"])

    def test_disconnected_old_units_require_review_even_without_overlap_conflict(self):
        rows = self.rows()[:3] + [sec_annual(y, e, "DISCONNECTED", "2018-02-01") for y, e in ((2016, 8), (2017, 9))]
        signal = {"accn": "QUARTERLY_SPLIT", "val": .125, "filed": "2021-07-30", "form": "10-Q",
                  "source_url": "https://example.org/reverse-split"}
        history, audit, _ = current.annual_chain(rows, {"symbol": "TEST", "cik": "0000000001"},
                                                 "current_shares", "2026-11-07", split_disclosures=[signal])
        self.assertEqual([r["eps"] for r in history], [3.1, 4, 5])
        self.assertEqual(audit["share_basis_extension_guard"]["guarded_accessions"], ["DISCONNECTED"])
        self.assertTrue(all(r["history_extension_provenance"]["multiplier_applied_by_this_module"] == 1
                            for r in audit["share_basis_exclusions"]))

    def test_reviewed_split_bridge_is_applied_once_with_evidence(self):
        rows = self.rows()
        rows[-3]["val"], rows[-2]["val"], rows[-1]["val"] = 4, 8, 12.4
        bridge = {"source_accession": "OLD", "target_share_basis": "current_shares", "eps_multiplier": .25,
                  "source_url": "https://example.org/reviewed-split", "available_at": "2025-01-01",
                  "mapping_reason": "stock_split", "event_id": "REVIEWED-4-FOR-1", "effective_date": "2024-06-01",
                  "rationale": "Source text confirms the old filing's native units and the four-for-one action."}
        history, audit, _ = current.annual_chain(rows, {"symbol": "TEST", "cik": "0000000001"}, "current_shares", "2026-11-07",
                                                 split_disclosures=[{"accn": "CURRENT", "val": 4, "filed": "2024-07-01"}],
                                                 entity_override={"reviewed_bridges": [bridge]})
        self.assertEqual([r["eps"] for r in history], [1, 2, 3.1, 4, 5])
        self.assertEqual(history[0]["history_extension_provenance"]["multiplier_applied_by_this_module"], .25)
        self.assertFalse(audit["share_basis_extension_guard"]["guarded_accessions"])

    def annual_supplement(self, directory):
        source = Path(directory) / "annual-report.html"
        source.write_text("<table>GAAP diluted annual EPS: 2016 1.85; 2017 1.86.</table>", encoding="utf-8")
        accession = "0000000001-21-000020"
        return {"source_accession": accession, "cik": "0000000001", "eps_basis": "GAAP_diluted",
                "share_basis": "current_shares", "currency": "USD", "unit": "USD/shares",
                "tag": "EarningsPerShareDiluted", "form": "10-K", "filed": "2021-01-28",
                "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000121000020/report.htm",
                "source_file": str(source.relative_to(current.ROOT)),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "evidence_locator": "Five-year GAAP diluted EPS table, 2016 and 2017 columns.",
                "rationale": "Complete annual EPS in the filing's current share units, missing from Companyfacts.",
                "history": [{"start": "2016-01-01", "end": "2016-12-31", "eps": 1.85},
                            {"start": "2017-01-01", "end": "2017-12-31", "eps": 1.86}]}

    def test_evidenced_annual_supplement_wins_by_financial_filing_date(self):
        with tempfile.TemporaryDirectory(dir=current.ROOT) as directory:
            supplement = self.annual_supplement(directory)
            rows = self.rows()[:3] + [
                sec_annual(y, e, supplement["source_accession"], supplement["filed"])
                for y, e in ((2018, 2.1), (2019, 2.2), (2020, 2.3))] + [
                sec_annual(y, e, "BEFORE_SPLIT", "2020-02-01") for y, e in ((2016, 3.69), (2017, 3.72))]
            originals = deepcopy(rows)
            bridge = {"source_accession": "BEFORE_SPLIT", "target_share_basis": "current_shares",
                      "eps_multiplier": .5, "source_url": "https://example.org/split",
                      "available_at": "2026-11-07"}
            history, audit, _ = current.annual_chain(rows, {"symbol": "TEST", "cik": "0000000001"},
                "current_shares", "2026-11-07", entity_override={"annual_supplements": [supplement], "reviewed_bridges": [bridge]})
            self.assertEqual([r["eps"] for r in history[:2]], [1.85, 1.86])
            self.assertEqual(history[0]["financial_filed_date"], "2021-01-28")
            self.assertEqual(history[0]["available_at"], "2021-01-29")
            self.assertEqual(history[0]["observation_source"], "reviewed_annual_filing_supplement_not_companyfacts")
            self.assertTrue(history[0]["annual_supplement_evidence"]["source_file_sha256_verified"])
            correction = next(r for r in audit["revision_audit"] if r["period_end"] == "2016-12-31")
            self.assertEqual((correction["before"]["eps"], correction["after"]["eps"]), (1.845, 1.85))
            self.assertEqual(audit["annual_supplements"][0]["source_financial_available_at"], "2021-01-29")
            self.assertEqual(rows, originals)

    def test_annual_supplement_rejects_existing_period_and_invalid_evidence(self):
        with tempfile.TemporaryDirectory(dir=current.ROOT) as directory:
            supplement = self.annual_supplement(directory)
            rows = self.rows()[:3] + [sec_annual(2018, 2.1, supplement["source_accession"], supplement["filed"])]
            conflicting = deepcopy(supplement)
            conflicting["history"] = [{"start": "2018-01-01", "end": "2018-12-31", "eps": 99}]
            cases = [(conflicting, "cannot_replace_existing_annual_period"),
                     (supplement | {"source_sha256": "0" * 64}, "source_hash_mismatch"),
                     (supplement | {"filed": "2022-01-28"}, "filing_metadata_mismatch"),
                     (supplement | {"cik": "0000000002"}, "basis_mismatch"),
                     (supplement | {"share_basis": "old_shares"}, "basis_mismatch"),
                     (supplement | {"unit": "USD"}, "basis_mismatch")]
            for invalid, reason in cases:
                with self.subTest(reason=reason), self.assertRaisesRegex(current.Unavailable, reason):
                    current.annual_chain(rows, {"symbol": "TEST", "cik": "0000000001"}, "current_shares", "2026-11-07",
                                         entity_override={"annual_supplements": [invalid]})

    def test_new_normalizer_refuses_frozen_or_existing_output(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            study = Path(temporary).resolve()
            old = study / "normalized"
            old.mkdir()
            (old / "snapshots.json").write_text("frozen", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "never the frozen"):
                current.build(study, "2026-11-07", output_dir=old)
            target = study / "normalized_latest_available"
            target.mkdir()
            (target / "sentinel").write_text("preserved", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                current.build(study, "2026-11-07")
            self.assertEqual((old / "snapshots.json").read_text(encoding="utf-8"), "frozen")


if __name__ == "__main__":
    unittest.main()
