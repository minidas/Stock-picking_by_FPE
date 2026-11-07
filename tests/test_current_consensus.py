"""Consensus observation parsing never silently produces model-ready inputs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import normalize_current_consensus as consensus


MEMBER = {"symbol": "TEST", "cik": "0000000001"}


def annual(period, end, mean, analysts=8, prior=2):
    return {"period": period, "endDate": end, "growth": {"raw": 999},
            "earningsEstimate": {"avg": {"raw": mean}, "numberOfAnalysts": {"raw": analysts},
                                 "earningsCurrency": "USD", "yearAgoEps": {"raw": prior},
                                 "low": {"raw": mean - .5}, "high": {"raw": mean + .5}}}


def record():
    return {"symbol": "TEST", "retrieval_started_at": "2026-11-07T01:00:00+00:00",
            "retrieval_finished_at": "2026-11-07T01:00:01+00:00", "modules_requested": ["earningsTrendGaap", "price"],
            "response": {"quoteSummary": {"result": [{
                "price": {"symbol": "TEST", "currency": "USD"},
                "earningsTrendGaap": {"defaultMethodology": "nongaap", "trend": [
                    annual("0q", "2026-12-31", 1), annual("0y", "2026-12-31", 3), annual("+1y", "2027-12-31", 4, prior=3)]},
                "earningsTrend": {"trend": [annual("0y", "2026-12-31", 999)]}}]}}}


class ConsensusParserTests(unittest.TestCase):
    def parse(self, value, as_of="2026-11-07", snapshot=None):
        return consensus.parse_collected(value, MEMBER, as_of=as_of, snapshot=snapshot)

    def test_annual_gaap_slots_ignore_default_methodology_and_top_level_growth(self):
        parsed = self.parse(record())
        self.assertEqual([row["period"] for row in parsed["observations"]], ["0y", "+1y"])
        self.assertEqual([row["eps_mean"] for row in parsed["observations"]], [3, 4])
        self.assertEqual(parsed["vendor_default_methodology_metadata"], "nongaap")
        self.assertEqual(parsed["raw_complete_annual_count"], 2)
        self.assertFalse(parsed["top_level_trend_growth_used"])
        for row in parsed["observations"]:
            self.assertIsNone(row["eps_std"])
            self.assertNotIn("target_date", row)
            self.assertIsNone(row["eps_basis"])
            self.assertFalse(row["model_admitted"])

    def test_empty_missing_and_default_only_never_fabricate_gaap_forecasts(self):
        default_only = record()
        default_only["response"]["quoteSummary"]["result"][0].pop("earningsTrendGaap")
        for value in (None, {}, {"symbol": "TEST", "response": {"quoteSummary": None}}, default_only):
            with self.subTest(value=value):
                parsed = self.parse(value)
                self.assertEqual(len(parsed["observations"]), 2)
                self.assertEqual(parsed["raw_complete_annual_count"], 0)
                self.assertTrue(all(r["eps_mean"] is None for r in parsed["observations"]))

    def test_missing_or_noninteger_analyst_count_stays_incomplete(self):
        for count in (None, 0, -1, 2.5, True):
            value = record()
            value["response"]["quoteSummary"]["result"][0]["earningsTrendGaap"]["trend"][1]["earningsEstimate"]["numberOfAnalysts"] = {"raw": count}
            parsed = self.parse(value)["observations"][0]
            with self.subTest(count=count):
                self.assertEqual(parsed["eps_mean"], 3)
                self.assertIsNone(parsed["analysts"])
                self.assertFalse(parsed["raw_complete"])
                self.assertIn("analyst_count_missing_or_invalid", parsed["raw_incomplete_reasons"])

    def test_availability_is_next_utc_day_and_never_backfilled(self):
        value = record()
        value["retrieval_finished_at"] = "2026-11-07T23:30:00-05:00"
        row = self.parse(value)["observations"][0]
        self.assertEqual(row["available_at"], "2026-11-09")
        self.assertIn("observation_not_available_at_model_as_of", row["model_admission_reasons"])
        row = self.parse(record())["observations"][0]
        self.assertEqual(row["available_at"], "2026-11-08")
        later = self.parse(record(), as_of="2026-11-08")["observations"][0]
        self.assertNotIn("observation_not_available_at_model_as_of", later["model_admission_reasons"])
        self.assertFalse(later["model_admitted"])

    def test_ended_reported_year_is_not_forward_or_relabelled(self):
        value = record()
        value["response"]["quoteSummary"]["result"][0]["earningsTrendGaap"]["trend"][1]["endDate"] = "2026-09-30"
        row = self.parse(value)["observations"][0]
        self.assertEqual(row["as_reported_target_date"], "2026-09-30")
        self.assertFalse(row["as_reported_target_after_model_as_of"])
        self.assertTrue(row["raw_complete"])
        self.assertIn("reported_target_not_forward_of_model_as_of", row["model_admission_reasons"])

    def test_year_ago_match_is_supporting_only_and_next_year_is_not_actual(self):
        snapshot = {"history": [{"period_end": "2025-12-31", "eps": 2, "available_at": "2026-02-02"}]}
        parsed = self.parse(record(), snapshot=snapshot)
        evidence = parsed["observations"][0]["year_ago_eps_comparison"]
        self.assertEqual(evidence["status"], "matching_supporting_evidence")
        self.assertFalse(evidence["full_basis_verified"])
        self.assertFalse(parsed["observations"][0]["model_admitted"])
        self.assertEqual(parsed["observations"][1]["year_ago_eps_comparison"]["reason"], "next_year_yearAgoEps_may_itself_be_a_forecast")

    def test_duplicate_annual_period_requires_review_instead_of_arbitrary_choice(self):
        value = record()
        value["response"]["quoteSummary"]["result"][0]["earningsTrendGaap"]["trend"].append(annual("0y", "2026-12-31", 5))
        row = self.parse(value)["observations"][0]
        self.assertFalse(row["raw_complete"])
        self.assertIsNone(row["eps_mean"])
        self.assertEqual(len(row["raw_rows"]), 2)

    def test_build_keeps_all_members_verifies_sources_and_preserves_outputs(self):
        with tempfile.TemporaryDirectory(dir=consensus.ROOT) as directory:
            study = Path(directory)
            (study / "input").mkdir()
            (study / "input/universe.json").write_text(json.dumps({"members": [MEMBER, {"symbol": "MISSING"}]}), encoding="utf-8")
            retry = study / consensus.RETRY_NAME
            source_dir = retry / "host_network/yahoo"
            source_dir.mkdir(parents=True)
            value = record()
            raw = json.dumps(value["response"]).encode()
            source = source_dir / "raw.json"
            source.write_bytes(raw)
            value["sources"] = [{"file": str(source.relative_to(consensus.ROOT)), "sha256": hashlib.sha256(raw).hexdigest(),
                                 "status": 200, "retrieved_at": "2026-11-07T01:00:00+00:00"}]
            (source_dir / "TEST_collected.json").write_text(json.dumps(value), encoding="utf-8")
            summary = consensus.build(study)
            self.assertEqual(summary["universe_security_count"], 2)
            self.assertEqual(summary["collected_security_count"], 1)
            self.assertEqual(summary["verified_response_security_count"], 1)
            self.assertEqual(summary["raw_complete_annual_observation_count"], 2)
            self.assertEqual(summary["model_admitted_security_count"], 0)
            normalized = consensus.read_json(retry / consensus.OUTPUT_NAMES[0])
            self.assertEqual(len(normalized["securities"]), 2)
            self.assertFalse(normalized["securities"][1]["collection_present"])
            with self.assertRaisesRegex(ValueError, "overwrite"):
                consensus.build(study)
            value["sources"][0]["sha256"] = "0" * 64
            self.assertFalse(consensus.verify_sources(value)["verified_matching_response"])


if __name__ == "__main__":
    unittest.main()
