"""History panels must be bound to exact inputs and leave TTM anchors intact."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ttm_universe_fixtures", ROOT / "tests/test_universe_ttm.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
analysis = fixtures.analysis
old = fixtures.fixtures


def panel(source=None, older=((2007, 1.0), (2008, 1.5)), member=None):
    source = source or old.company()
    member = member or old.member()
    base = analysis.assemble([member], [old.price()], [source], [])[0]["input"]
    return {"symbol": member["symbol"], "cik": member["cik"], "as_of": analysis.AS_OF,
            "base_history_sha256": analysis.history_sha256(base["history"]),
            "target_share_basis": base["share_basis"], "eps_basis": analysis.CANONICAL_EPS_BASIS,
            "context": {"segments": "", "coreg": ""}, "share_basis_verified": False,
            "history": [{"period_end": f"{year}-12-31", "available_at": "2010-02-01", "eps": eps}
                        for year, eps in older] + deepcopy(base["history"]),
            "audit": {"identity_join_allowed": member.get("2012_filing_identity_confirmation", {}).get("identity_join_allowed") is not False,
                      "additional_observations": len(older), "source": "synthetic test panel"}}


def assemble(source=None, panels=None, anchors=None, **kwargs):
    return analysis.assemble([old.member()], [old.price()], [source or old.company()], [],
                             eps_anchor="ttm", ttm_anchors=anchors if anchors is not None else [fixtures.anchor()],
                             history_panels=panels, **kwargs)[0]


class UniverseHistoryTests(unittest.TestCase):
    def test_extension_keeps_ttm_price_and_base_history_audit(self):
        source = old.company()
        history_panel = panel(source)
        saved_panel, saved_source = deepcopy(history_panel), deepcopy(source)
        original = assemble(source)
        extended = assemble(source, [history_panel])
        self.assertEqual(extended["test_status"], "evaluated")
        self.assertEqual(extended["input"]["history_before_extension"], original["input"]["history"])
        for key in ("eps0", "eps_period_start", "eps_period_end", "eps_available_at", "price", "ttm_provenance"):
            self.assertEqual(extended["input"][key], original["input"][key])
        self.assertEqual(extended["annual_history_count"], 5)
        self.assertEqual(extended["history_growth_observations"], 4)
        self.assertEqual(extended["original_annual_history_count"], 3)
        self.assertTrue(extended["history_expanded"])
        self.assertEqual(extended["history_extension_audit"]["application_status"], "applied")
        self.assertEqual(history_panel, saved_panel)
        self.assertEqual(source, saved_source)

    def test_panel_identity_hash_date_and_basis_mismatches_fail_explicitly(self):
        cases = [{"base_history_sha256": "0" * 64}, {"cik": "0000000002"},
                 {"as_of": "2013-01-01"}, {"target_share_basis": "another basis"},
                 {"eps_basis": "adjusted"}, {"context": {"segments": "ClassA", "coreg": ""}},
                 {"share_basis_verified": True}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                assemble(panels=[panel() | change])

    def test_future_added_observation_is_rejected_not_silently_filtered(self):
        future = panel()
        future["history"][0]["available_at"] = "2013-02-01"
        with self.assertRaisesRegex(ValueError, "future"):
            assemble(panels=[future])

    def test_panel_cannot_rewrite_current_years_or_add_ttm_history(self):
        changed = panel()
        changed["history"][-1]["eps"] = 99
        with self.assertRaisesRegex(ValueError, "protected"):
            assemble(panels=[changed])
        overlap = panel()
        overlap["history"][0]["measure"] = "ttm"
        with self.assertRaises(ValueError):
            assemble(panels=[overlap])

    def test_panel_extension_respects_calendar_window_and_latest_base_year(self):
        for observation in ({"period_end": "2000-12-31", "available_at": "2001-02-01", "eps": 1},
                            {"period_end": "2012-06-30", "available_at": "2012-07-31", "eps": 1}):
            changed = panel()
            changed["history"].append(observation)
            with self.subTest(observation=observation), self.assertRaisesRegex(ValueError, "calendar-year window"):
                assemble(panels=[changed])
        invalid_window = panel()
        invalid_window["audit"]["max_years"] = 12
        with self.assertRaisesRegex(ValueError, "max_years"):
            assemble(panels=[invalid_window])

    def test_only_selected_in_window_base_vintages_are_protected(self):
        source = old.company()
        source["history"].extend([
            {"period_end": "1999-12-31", "available_at": "2000-02-01", "eps": 1},
            {"period_end": "2009-12-31", "available_at": "2010-02-01", "eps": 99},
            {"period_end": "2009-12-31", "available_at": "2013-02-01", "eps": 199},
        ])
        clean = panel(source, older=())
        clean["history"] = deepcopy(old.company()["history"])
        clean["audit"]["max_years"] = 3
        row = assemble(source=source, panels=[clean])
        self.assertEqual(row["annual_history_count"], 3)
        self.assertEqual(len(row["input"]["history_before_extension"]), 6)

    def test_panel_cannot_create_history_without_an_original_annual_anchor(self):
        source = old.company()
        source["history"] = []
        with self.assertRaisesRegex(ValueError, "without a base annual anchor"):
            assemble(source=source, panels=[panel(source)])

    def test_added_losses_change_prior_but_only_25_percent_excludes(self):
        below = assemble(panels=[panel(older=((2008, -1),))])
        self.assertGreater(below["loss_probability"], .10)
        self.assertLess(below["loss_probability"], .25)
        self.assertFalse(below["risk_excluded"])
        self.assertEqual(below["computation_status"], "evaluated")
        high = assemble(panels=[panel(older=((2004, -1), (2005, -2), (2006, -3), (2007, -4), (2008, -5)))])
        self.assertGreaterEqual(high["loss_probability"], .25)
        self.assertTrue(high["risk_excluded"])
        self.assertEqual(high["computation_status"], "not_tested_high_loss")
        self.assertIsNone(high["model"])
        self.assertEqual(high["threshold_scenarios"], [])

    def test_missing_ttm_stays_missing_after_history_extension(self):
        row = assemble(panels=[panel()], anchors=[])
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertIn("ttm_anchor_unavailable", row["blockers"])
        self.assertIsNone(row["input"]["eps0"])
        self.assertEqual(row["annual_history_count"], 5)

    def test_denied_identity_records_only_an_unchanged_empty_panel(self):
        member = old.member(allowed=False)
        unchanged = panel(member=member, older=())
        row = analysis.assemble([member], [old.price()], [old.company()], [], history_panels=[unchanged])[0]
        self.assertEqual(row["test_status"], "data_unavailable")
        self.assertFalse(row["history_panel_applied"])
        self.assertEqual(row["input"]["history"], [])
        self.assertEqual(row["history_extension_audit"]["application_status"], "not_applied_identity_denied")
        with self.assertRaisesRegex(ValueError, "denied"):
            analysis.assemble([member], [old.price()], [old.company()], [], history_panels=[panel(member=member)])

    def test_summary_distributions_and_original_mode_remain_separate(self):
        original = assemble()
        self.assertEqual(original["history_mode"], "original")
        extended = assemble(panels=[panel()])
        summary = analysis.summarize([extended])
        self.assertEqual(summary["history_expanded_count"], 1)
        self.assertEqual(summary["annual_history_count_distribution"], {"5": 1})
        self.assertEqual(summary["growth_count_distribution"], {"4": 1})
        self.assertEqual(summary["original_annual_history_count_distribution"], {"3": 1})


if __name__ == "__main__":
    unittest.main()
