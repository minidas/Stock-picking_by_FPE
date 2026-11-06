"""Regression checks for the dated 2012 data assembly, not financial calibration."""
from copy import deepcopy
from datetime import date
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("history_builder", ROOT / "scripts/build_2012_history.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def read(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


class HistoryBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members = {r["symbol"]: r for r in read("universe_2012_members.json")["members"]}
        cls.prices = {r["symbol"]: r for r in read("research_2012_prices.json")["prices"]}
        cls.companies = {r["cik"]: r for r in read("research_2012_sec_eps.json")["companies"]}

    def baseline(self, symbol, adjustment=None):
        member = self.members[symbol]
        return builder.assemble([member], [self.prices[symbol]], [self.companies[member["cik"]]], [],
                                adjustments=[adjustment] if adjustment else [])[0]

    def test_clf_eleven_years_preserve_losses_and_current_eps(self):
        base = self.baseline("CLF")
        original = deepcopy(base)
        blocks = read("research_2012_history_supplements.json")["source_blocks"]
        panel = builder.build_panels([base], [], blocks)[0]
        self.assertEqual(base, original)
        self.assertEqual([r["period_end"][:4] for r in panel["history"]], [str(y) for y in range(2001, 2012)])
        expected = [-.285, -2.325, -.4, 2.95, 2.50, 2.60, 2.57, 4.76, 1.63, 7.49, 11.48]
        for row, eps in zip(panel["history"], expected):
            self.assertAlmostEqual(row["eps"], eps)
            self.assertLessEqual(row["available_at"], builder.AS_OF)
        probe = {**base["input"], "history": panel["history"], "eps_basis": builder.BASIS}
        history, growth = builder._history(probe, date.fromisoformat(builder.AS_OF), set())
        self.assertEqual(len(growth), 7)
        self.assertEqual(sum(eps <= 0 for _, eps in history), 3)
        self.assertFalse(panel["share_basis_verified"])
        self.assertEqual(panel["base_history_sha256"], builder.history_digest(original["input"]["history"]))

    def test_exact_legacy_identity_is_required(self):
        base = self.baseline("AA")
        evidence = base["membership_evidence"]["historical_filing_name_confirmation"]["evidence"][0]
        filing = {"accession": evidence["accession"], "name": evidence["name"]}
        self.assertTrue(builder.approve_legacy_identity(base["membership_evidence"], filing))
        filing["accession"] = "an-unreviewed-new-filing"
        self.assertFalse(builder.approve_legacy_identity(base["membership_evidence"], filing))
        filing["accession"] = evidence["accession"]
        base["membership_evidence"]["2012_filing_identity_confirmation"]["identity_join_allowed"] = False
        self.assertFalse(builder.approve_legacy_identity(base["membership_evidence"], filing))

    def test_2012_bridge_maps_old_values_once_and_keeps_raw_evidence(self):
        adjustment = next(a for a in read("research_2012_share_adjustments.json")["adjustments"] if a["symbol"] == "KO")
        base = self.baseline("KO", adjustment)
        evidence = base["membership_evidence"]["historical_filing_name_confirmation"]["evidence"][0]
        filing = {"accession": evidence["accession"], "name": evidence["name"], "available_at": "2010-02-26",
                  "share_basis": "old_native_basis", "history": [{"eps": 2.93, "available_at": "2010-02-26"}]}
        before = deepcopy(filing)
        candidate = builder.prepare_candidate(filing, base)
        self.assertEqual(filing, before)
        self.assertEqual(candidate["history"][0]["eps"], 1.465)
        self.assertEqual(candidate["history"][0]["eps_as_reported_before_2012_split_bridge"], 2.93)
        self.assertEqual(candidate["already_mapped"]["eps_multiplier"], .5)
        self.assertEqual(candidate["native_share_basis"], "old_native_basis")
        filing["available_at"] = "2012-10-01"
        self.assertNotIn("already_mapped", builder.prepare_candidate(filing, base))

    def test_special_share_classes_remain_separate(self):
        company = next(c for c in read("research_2012_supplemental_eps.json")["companies"] if c["symbol"] == "HSY")
        self.assertEqual(builder.context_of(company), {"segments": "ClassOfStock=CommonStock;", "coreg": ""})
        self.assertEqual(builder.context_of(None), {"segments": "", "coreg": ""})

    def test_unconfirmed_roster_identity_cannot_be_rescued_by_reviewed_block(self):
        base = self.baseline("CLF")
        base["membership_evidence"].pop("2012_filing_identity_confirmation")
        blocks = read("research_2012_history_supplements.json")["source_blocks"]
        panel = builder.build_panels([base], [], blocks)[0]
        self.assertEqual(len(panel["history"]), 3)
        self.assertEqual(panel["audit"]["additional_observations"], 0)

    def test_conflicting_duplicate_filing_is_not_last_record_wins(self):
        base = self.baseline("CLF")
        filing = {"cik": base["cik"], "accession": "same-accession", "name": "CLF",
                  "available_at": "2010-02-26", "share_basis": "native", "history": [{"eps": 1}]}
        conflicting = deepcopy(filing)
        conflicting["history"][0]["eps"] = 2
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            builder.build_panels([base], [filing, conflicting], [])


if __name__ == "__main__":
    unittest.main()
