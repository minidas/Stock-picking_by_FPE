"""TTM chronology, arithmetic, independent annual history, and API regression."""
from copy import deepcopy
from datetime import date, timedelta
import unittest

from stocking.data import DataError, validate_snapshot
from stocking.model import ModelError, _history, apply_events, build_baseline
from stocking.ttm import TTMError, prepare_ttm_snapshot, validate_ttm_snapshot


def snapshot():
    return {
        "symbol": "CLF", "cik": "0000764065", "synthetic": True,
        "as_of": "2012-12-31", "price": 38.56, "price_date": "2012-12-31", "currency": "USD",
        "eps0": 11.48, "eps_period_end": "2011-12-31", "eps_available_at": "2012-02-16",
        "eps_basis": "GAAP_diluted_as_reported_at_cutoff", "share_basis": "same_nominal_shares",
        "history": [{"period_end": f"{year}-12-31", "available_at": "2012-02-16", "eps": eps}
                    for year, eps in ((2009, 1.63), (2010, 7.49), (2011, 11.48))],
        "consensus": [],
    }


def anchor():
    source = snapshot()
    periods = [("2011-10-01", "2011-12-31", "2012-02-15", 1.30),
               ("2012-01-01", "2012-03-31", "2012-04-25", 2.63),
               ("2012-04-01", "2012-06-30", "2012-07-25", 1.81),
               ("2012-07-01", "2012-09-30", "2012-10-24", 0.59)]
    return {
        "symbol": "CLF", "cik": "0000764065", "eps": 6.33,
        "period_start": "2011-10-01", "period_end": "2012-09-30", "available_at": "2012-10-24",
        "eps_basis": source["eps_basis"], "share_basis": source["share_basis"],
        "method": "four_quarter_diluted_eps_sum", "latest_report_period_end": "2012-09-30",
        "components": [{"period_start": start, "period_end": end, "available_at": available,
                        "eps": eps, "source_url": "https://example.com/quarter",
                        "eps_basis": source["eps_basis"], "share_basis": source["share_basis"]}
                       for start, end, available, eps in periods],
    }


def annual_anchor():
    result = anchor()
    result.update(eps=11.48, period_start="2011-01-01", period_end="2011-12-31",
                  available_at="2012-02-16", method="annual_report_latest_ttm",
                  latest_report_period_end="2011-12-31")
    result["components"] = [{key: result[key] for key in
                             ("eps", "period_start", "period_end", "available_at", "eps_basis", "share_basis")}
                            | {"source_url": "https://example.com/annual"}]
    return result


class TTMTests(unittest.TestCase):
    def test_clf_four_quarters_update_anchor_without_inventing_growth_observation(self):
        original, component_anchor = snapshot(), anchor()
        before, anchor_before = deepcopy(original), deepcopy(component_anchor)
        updated = prepare_ttm_snapshot(original, component_anchor)
        self.assertEqual(original, before)
        self.assertEqual(component_anchor, anchor_before)
        self.assertEqual(updated["history"], original["history"])
        self.assertIsNot(updated["history"], original["history"])
        self.assertEqual(updated["eps0"], 6.33)
        self.assertEqual(updated["eps_period_end"], "2012-09-30")
        self.assertEqual(updated["eps_available_at"], "2012-10-24")
        old_model, model = build_baseline(original), build_baseline(updated)
        self.assertEqual(model["history_growth_observations"], 2)
        self.assertEqual(model["historical_log_growth_center"], old_model["historical_log_growth_center"])
        self.assertEqual(model["loss_probability"], old_model["loss_probability"])
        self.assertLess(model["interval_years"][0], old_model["interval_years"][0])
        self.assertEqual(model["target_date"], "2018-12-31")
        self.assertAlmostEqual(model["interval_years"][0], 1.251223502193748)
        self.assertEqual(model["ttm_provenance"], updated["ttm_provenance"])
        self.assertIn("ttm_quarterly_eps_sum_not_recomputed_annual_gaap", model["flags"])
        self.assertEqual(validate_snapshot(updated), updated)

    def test_future_component_cannot_be_hidden_by_old_anchor_availability(self):
        changed = anchor()
        changed["components"][-1]["available_at"] = "2013-01-02"
        with self.assertRaises(TTMError):
            prepare_ttm_snapshot(snapshot(), changed)
        changed["available_at"] = "2013-01-02"
        with self.assertRaises(TTMError):
            prepare_ttm_snapshot(snapshot(), changed)

    def test_gap_overlap_missing_quarter_and_duplicate_vintage_are_rejected(self):
        cases = []
        gap = anchor()
        gap["components"][1]["period_start"] = "2012-01-02"
        cases.append(gap)
        overlap = anchor()
        overlap["components"][1]["period_start"] = "2011-12-31"
        cases.append(overlap)
        missing = anchor()
        missing["components"].pop()
        cases.append(missing)
        conflict = anchor()
        conflict["components"][1] = deepcopy(conflict["components"][0])
        conflict["components"][1]["eps"] = 9.0
        cases.append(conflict)
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(TTMError):
                prepare_ttm_snapshot(snapshot(), case)

    def test_ytd_or_nonannual_span_cannot_be_called_direct_ttm(self):
        partial = annual_anchor()
        partial.update(period_start="2011-04-01", method="direct_reported_ttm")
        partial["components"][0]["period_start"] = "2011-04-01"
        with self.assertRaises(TTMError):
            prepare_ttm_snapshot(snapshot(), partial)
        partial = anchor()
        partial["components"][0]["period_start"] = "2011-11-01"
        partial["period_start"] = "2011-11-01"
        with self.assertRaises(TTMError):
            prepare_ttm_snapshot(snapshot(), partial)

    def test_component_sum_and_mixed_bases_are_rejected(self):
        cases = []
        wrong_sum = anchor()
        wrong_sum["eps"] = 6.30
        cases.append(wrong_sum)
        for field, value in (("eps_basis", "adjusted_EPS"), ("share_basis", "post_split_shares"),
                             ("currency", "EUR"), ("source_url", "not-a-source-url")):
            case = anchor()
            case["components"][0][field] = value
            cases.append(case)
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(TTMError):
                prepare_ttm_snapshot(snapshot(), case)

    def test_availability_is_latest_component_and_cannot_be_relabeled(self):
        for value in ("2012-10-23", "2012-10-25"):
            case = anchor()
            case["available_at"] = value
            with self.subTest(value=value), self.assertRaises(TTMError):
                prepare_ttm_snapshot(snapshot(), case)

    def test_metadata_is_required_by_both_model_and_data_entry_points(self):
        updated = prepare_ttm_snapshot(snapshot(), anchor())
        for key in ("ttm_provenance", "eps_period_start"):
            broken = deepcopy(updated)
            del broken[key]
            with self.subTest(key=key), self.assertRaises(ModelError):
                build_baseline(broken)
            with self.subTest(key=key), self.assertRaises(DataError):
                validate_snapshot(broken)
        del updated["eps_measure"]
        with self.assertRaises(ModelError):
            build_baseline(updated)

    def test_ttm_history_append_is_rejected_even_at_old_075_year_boundary(self):
        updated = prepare_ttm_snapshot(snapshot(), anchor())
        updated["history"].append({"period_start": "2011-10-01", "period_end": "2012-09-30",
                                   "available_at": "2012-10-24", "eps": 6.33, "measure": "ttm"})
        with self.assertRaises(ModelError):
            build_baseline(updated)
        with self.assertRaises(DataError):
            validate_snapshot(updated)

    def test_explicit_annual_history_overlap_rejected_without_breaking_legacy(self):
        original = snapshot()
        build_baseline(original)
        original["history"][-1].update(measure="annual", period_start="2010-10-01", period_end="2011-09-30")
        with self.assertRaises(ModelError):
            build_baseline(original)

    def test_separate_risk_history_path_rejects_conflicting_or_malformed_measure(self):
        for labels in ({"measure": "annual", "eps_measure": "ttm"}, {"measure": ["annual"]},
                       {"measure": {"kind": "annual"}}):
            original = snapshot()
            original["history"][-1].update(labels)
            with self.subTest(labels=labels), self.assertRaises(ModelError):
                _history(original, date(2012, 12, 31), set())

    def test_ttm_200_day_boundary_does_not_shorten_legacy_annual_limit(self):
        for days, valid in ((200, True), (201, False)):
            original = snapshot()
            original["as_of"] = (date(2012, 9, 30) + timedelta(days=days)).isoformat()
            original["price_date"] = original["as_of"]
            updated = prepare_ttm_snapshot(original, anchor())
            if valid:
                build_baseline(updated)
            else:
                with self.assertRaises(ModelError):
                    build_baseline(updated)
            build_baseline(original)  # Its 2011 annual EPS remains within 550 days.

    def test_annual_report_ttm_requires_latest_period_and_same_period_value(self):
        original = snapshot()
        original.update(as_of="2012-03-01", price_date="2012-03-01")
        valid = annual_anchor()
        model = build_baseline(prepare_ttm_snapshot(original, valid))
        self.assertFalse(model["ttm_provenance"]["external_latest_period_completeness_verified"])
        for latest in (None, "2012-03-31"):
            invalid = deepcopy(valid)
            if latest is None:
                del invalid["latest_report_period_end"]
            else:
                invalid["latest_report_period_end"] = latest
            with self.subTest(latest=latest), self.assertRaises(TTMError):
                prepare_ttm_snapshot(original, invalid)
        invalid = deepcopy(valid)
        invalid["eps"] = invalid["components"][0]["eps"] = 10.0
        with self.assertRaises(TTMError):
            prepare_ttm_snapshot(original, invalid)

    def test_direct_reported_ttm_and_fiscal_53_week_quarters_are_supported(self):
        direct = annual_anchor()
        direct["method"] = "direct_reported_ttm"
        validate_ttm_snapshot(prepare_ttm_snapshot(snapshot(), direct))
        fiscal = anchor()
        # Extend the first quarter to fourteen weeks and keep the remaining
        # three fiscal quarters at thirteen weeks: a contiguous 371-day year.
        start = date(2011, 9, 25)
        lengths = (98, 91, 91, 91)
        for row, length in zip(fiscal["components"], lengths):
            end = start + timedelta(days=length - 1)
            row.update(period_start=start.isoformat(), period_end=end.isoformat(),
                       available_at=(end + timedelta(days=25)).isoformat())
            start = end + timedelta(days=1)
        fiscal.update(period_start=fiscal["components"][0]["period_start"],
                      period_end=fiscal["components"][-1]["period_end"],
                      available_at=fiscal["components"][-1]["available_at"])
        fiscal["latest_report_period_end"] = fiscal["period_end"]
        build_baseline(prepare_ttm_snapshot(snapshot(), fiscal))

    def test_nonpositive_ttm_abstains_and_keeps_audit_metadata(self):
        case = anchor()
        for row in case["components"]:
            row["eps"] = -row["eps"]
        case["eps"] = -6.33
        model = build_baseline(prepare_ttm_snapshot(snapshot(), case))
        self.assertEqual(model["status"], "abstain")
        self.assertEqual(model["eps_measure"], "ttm")
        self.assertIn("ttm_quarterly_eps_sum_not_recomputed_annual_gaap", model["flags"])

    def test_empty_ai_review_preserves_ttm_audit_as_a_deep_copy(self):
        model = build_baseline(prepare_ttm_snapshot(snapshot(), anchor()))
        adjusted = apply_events(model, [])
        self.assertEqual(adjusted["ttm_provenance"], model["ttm_provenance"])
        adjusted["ttm_provenance"]["components"][0]["eps"] = 100
        self.assertEqual(model["ttm_provenance"]["components"][0]["eps"], 1.3)


if __name__ == "__main__":
    unittest.main()
