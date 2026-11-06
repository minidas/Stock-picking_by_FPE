"""Annual-history joins retain uncertainty and never invent splits or years."""
from copy import deepcopy
import unittest

from stocking.history import HistoryError, extend_annual_history


def snapshot():
    return {"symbol": "TEST", "cik": "0000000001", "as_of": "2012-12-31", "currency": "USD",
            "eps_basis": "GAAP_diluted", "share_basis": "shares_2012", "context": {"segments": "", "coreg": ""},
            "eps0": 4.5, "eps_measure": "ttm", "eps_period_end": "2012-09-30",
            "history": [{"period_end": f"{year}-12-31", "available_at": "2012-02-15", "eps": eps}
                        for year, eps in ((2009, 2.0), (2010, 3.0), (2011, 4.0))]}


def filing(accession="OLD", values=((2007, 1.0), (2008, 1.5), (2009, 2.0)), available="2010-02-15"):
    return {"cik": "0000000001", "accession": accession, "available_at": available,
            "source_url": "https://example.com/filing", "identity_join_allowed": True,
            "eps_basis": "GAAP_diluted", "share_basis": "shares_2012",
            "context": {"segments": "", "coreg": ""},
            "history": [{"period_end": f"{year}-12-31", "eps": eps, "available_at": available,
                         "tag": "EarningsPerShareDiluted", "unit_as_reported": "USD", "qtrs": 4}
                        for year, eps in values]}


def mapping(factor=0.5):
    return {"target_share_basis": "shares_2012", "eps_multiplier": factor,
            "available_at": "2011-08-01", "source_url": "https://example.com/split"}


class AnnualHistoryTests(unittest.TestCase):
    def test_chain_extends_earlier_years_without_mutating_snapshot_or_ttm(self):
        current = snapshot()
        old = filing()
        older = filing("OLDER", ((2005, 0.5), (2006, 0.8), (2007, 1.0)), "2008-02-15")
        originals = deepcopy((current, old, older))
        history, audit = extend_annual_history(current, [older, old])
        self.assertEqual((current, old, older), originals)
        self.assertEqual([row["period_end"][:4] for row in history], [str(year) for year in range(2005, 2012)])
        self.assertEqual(history[-3:], current["history"])
        self.assertEqual(current["eps0"], 4.5)
        self.assertFalse(audit["share_basis_verified"])
        self.assertEqual(audit["method"], "overlap_consistent_unverified_assumption")
        self.assertEqual(audit["history_growth_observations"], 6)
        self.assertEqual(audit["pending_filings"], [])

    def test_current_history_wins_and_conflicts_do_not_infer_a_split(self):
        candidate = filing(values=((2008, 3.0), (2009, 4.0)))
        history, audit = extend_annual_history(snapshot(), [candidate])
        self.assertEqual(history, snapshot()["history"])
        self.assertIn("overlapping_annual_eps_conflict_no_ratio_inferred", audit["pending_filings"][0]["reasons"])
        reviewed = mapping() | {"source_accession": "OLD"}
        history, audit = extend_annual_history(snapshot(), [candidate], reviewed_bridges=[reviewed])
        self.assertEqual(history[0]["eps"], 1.5)
        self.assertEqual(history[1]["eps"], 2.0)
        self.assertEqual(history[0]["history_extension_provenance"]["multiplier_applied_by_this_module"], 0.5)
        self.assertFalse(audit["share_basis_verified"])

    def test_already_mapped_values_are_not_multiplied_twice(self):
        candidate = filing()
        candidate["share_basis"] = "old_native_shares"
        candidate["already_mapped"] = mapping()
        candidate["source_raw_values"] = {"2007": 2.0, "2008": 3.0, "2009": 4.0}
        history, audit = extend_annual_history(snapshot(), [candidate])
        self.assertEqual(history[0]["eps"], 1.0)
        self.assertEqual(history[0]["history_extension_provenance"]["multiplier_applied_by_this_module"], 1.0)
        self.assertEqual(audit["filing_audit"][0]["source_filing"]["source_raw_values"], candidate["source_raw_values"])

    def test_explicit_rounding_bound_applies_only_with_mapping_evidence(self):
        candidate = filing(values=((2008, 1.5), (2009, 2.005)))
        _, default_audit = extend_annual_history(snapshot(), [candidate])
        self.assertTrue(default_audit["pending_filings"])
        candidate["already_mapped"] = mapping(1.0) | {
            "reviewed_overlap_tolerance": 0.0075,
            "reviewed_overlap_tolerance_rationale": "Documented two-place reporting after a known split; this is a bound, not verification."}
        history, audit = extend_annual_history(snapshot(), [candidate])
        self.assertEqual(history[1]["eps"], 2.0)  # Keep current value, not 2.005.
        comparison = audit["filing_audit"][0]["overlap_comparisons"][0]
        self.assertTrue(comparison["requires_reviewed_rounding_tolerance"])
        self.assertAlmostEqual(comparison["absolute_difference"], 0.005)
        del candidate["already_mapped"]["reviewed_overlap_tolerance_rationale"]
        _, audit = extend_annual_history(snapshot(), [candidate])
        self.assertTrue(audit["pending_filings"])

    def test_future_filing_row_and_mapping_evidence_cannot_enter_history(self):
        future_filing = filing(available="2013-02-15")
        future_row = filing()
        future_row["history"][0]["available_at"] = "2013-01-01"
        future_mapping = filing()
        future_mapping["already_mapped"] = mapping() | {"available_at": "2013-01-01"}
        for index, candidate in enumerate((future_filing, future_row, future_mapping)):
            with self.subTest(case=index):
                history, audit = extend_annual_history(snapshot(), [candidate])
                self.assertEqual(history, snapshot()["history"])
                self.assertTrue(audit["pending_filings"])

    def test_identity_accounting_units_and_context_must_match(self):
        changes = [{"identity_join_allowed": False}, {"cik": "0000000002"},
                   {"eps_basis": "adjusted_diluted"}, {"share_basis": "unmapped_native"},
                   {"context": {"segments": "ClassB", "coreg": ""}}]
        candidates = [filing() | change for change in changes]
        bad_unit = filing()
        bad_unit["history"][0]["unit_as_reported"] = "shares"
        candidates.append(bad_unit)
        basic = filing()
        basic["history"][0]["tag"] = "EarningsPerShareBasic"
        candidates.append(basic)
        for index, candidate in enumerate(candidates):
            with self.subTest(case=index):
                history, audit = extend_annual_history(snapshot(), [candidate])
                self.assertEqual(history, snapshot()["history"])
                self.assertTrue(audit["pending_filings"])

    def test_no_overlap_and_same_period_source_conflict_remain_pending(self):
        disconnected = filing(values=((2003, 0.2), (2004, 0.3)))
        conflicting = filing()
        conflicting["history"].append(deepcopy(conflicting["history"][-1]) | {"eps": 5.0})
        for candidate in (disconnected, conflicting):
            history, audit = extend_annual_history(snapshot(), [candidate])
            self.assertEqual(history, snapshot()["history"])
            self.assertEqual(len(audit["pending_filings"]), 1)

    def test_negative_years_survive_and_do_not_create_log_growth(self):
        history, audit = extend_annual_history(snapshot(), [filing(values=((2007, 1.0), (2008, -1.5), (2009, 2.0)))])
        self.assertEqual(history[1]["eps"], -1.5)
        self.assertEqual(audit["historical_nonpositive_observations"], 1)
        self.assertEqual(audit["history_growth_observations"], 2)

    def test_window_uses_latest_annual_year_not_ttm_and_caps_eleven_real_years(self):
        values = [(year, 1.0) for year in range(1990, 2009)] + [(2009, 2.0)]
        history, audit = extend_annual_history(snapshot(), [filing(values=values)])
        self.assertEqual(len(history), 11)
        self.assertEqual(history[0]["period_end"], "2001-12-31")
        self.assertEqual(audit["latest_annual_period_end"], "2011-12-31")
        self.assertEqual(audit["first_calendar_year_in_window"], 2001)
        with self.assertRaises(HistoryError):
            extend_annual_history(snapshot(), [], max_years=12)

    def test_long_gaps_remain_missing_without_synthetic_growth(self):
        history, audit = extend_annual_history(snapshot(), [filing(values=((2002, 0.5), (2009, 2.0)))])
        self.assertEqual(len(history), 4)
        self.assertEqual(audit["history_growth_observations"], 2)
        self.assertIn(2005, audit["missing_calendar_years_in_window"])

    def test_newer_source_vintage_wins_for_added_years_and_raw_provenance_survives(self):
        older = filing("OLDER", ((2007, 1.0), (2008, 1.5), (2009, 2.0)), "2010-02-01")
        newer = filing("NEWER", ((2007, 1.0), (2008, 1.5), (2009, 2.0)), "2011-02-01")
        history, audit = extend_annual_history(snapshot(), [older, newer])
        self.assertEqual(history[0]["accession"], "NEWER")
        self.assertEqual(history[0]["history_extension_provenance"]["source_financial_available_at"], "2011-02-01")
        self.assertEqual(len(audit["filing_audit"]), 2)

    def test_conflicting_same_day_filings_are_both_pending_regardless_of_input_order(self):
        first = filing("FIRST", ((2008, 1.5), (2009, 2.0)), "2010-02-15")
        second = filing("SECOND", ((2008, 1.7), (2009, 2.0)), "2010-02-15")
        outputs = []
        for candidates in ([first, second], [second, first]):
            history, audit = extend_annual_history(snapshot(), candidates)
            outputs.append(history)
            self.assertEqual(history, snapshot()["history"])
            self.assertEqual({row["source_accession"] for row in audit["pending_filings"]}, {"FIRST", "SECOND"})
            self.assertTrue(all(row["reasons"] == ["same_day_cross_filing_eps_conflict"] for row in audit["pending_filings"]))
        self.assertEqual(outputs[0], outputs[1])

    def test_equal_same_day_filings_select_stable_provenance(self):
        first, second = filing("FIRST"), filing("SECOND")
        left, _ = extend_annual_history(snapshot(), [first, second])
        right, _ = extend_annual_history(snapshot(), [second, first])
        self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
