import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stocking.data import (DataError, coverage_report, fetch_alpha_vantage, load_json,
                           load_memberships, load_snapshots, normalize_consensus_rows,
                           universe_at, validate_snapshot)


@contextmanager
def workspace_temp():
    root = Path(__file__).resolve().parents[1]
    directory = tempfile.TemporaryDirectory(dir=root)
    if not Path(directory.name).resolve().is_relative_to(root):
        raise RuntimeError("Temporary path outside workspace")
    try:
        yield directory.name
    finally:
        directory.cleanup()


def snapshot():
    return {"symbol": "DEMO", "synthetic": True, "as_of": "2026-11-06",
            "price": 100.0, "price_date": "2026-11-05", "currency": "USD",
            "eps0": 10.0, "eps_period_end": "2025-12-31", "eps_available_at": "2026-02-15",
            "eps_basis": "GAAP diluted annual", "share_basis": "as_of split basis",
            "history": [], "consensus": []}


class DataTests(unittest.TestCase):
    def test_finite_price_and_eps_chronology(self):
        row = snapshot()
        row["price"] = float("nan")
        with self.assertRaises(DataError):
            validate_snapshot(row)
        row = snapshot()
        row["eps_available_at"] = "2026-11-07"
        with self.assertRaises(DataError):
            validate_snapshot(row)

    def test_negative_eps_is_preserved_for_abstention(self):
        row = snapshot()
        row["eps0"] = -1.0
        self.assertEqual(validate_snapshot(row)["eps0"], -1.0)

    def test_mixed_basis_rejected(self):
        row = snapshot()
        row["consensus"] = [{"target_date": "2027-12-31", "available_at": "2026-10-31",
                             "eps_mean": 12, "analysts": 10, "eps_basis": "adjusted",
                             "share_basis": row["share_basis"]}]
        with self.assertRaisesRegex(DataError, "Mixed eps_basis"):
            validate_snapshot(row)

    def test_future_forecast_vintage_preserved_for_model_filter(self):
        row = snapshot()
        row["consensus"] = [{"target_date": "2027-12-31", "available_at": "2026-11-09",
                             "eps_mean": 12, "analysts": 10, "eps_basis": row["eps_basis"],
                             "share_basis": row["share_basis"]}]
        self.assertEqual(len(validate_snapshot(row)["consensus"]), 1)

    def test_no_mutation(self):
        row = snapshot()
        before = copy.deepcopy(row)
        validate_snapshot(row)["history"].append({})
        self.assertEqual(row, before)

    def test_provenance_required_for_real_data(self):
        row = snapshot()
        del row["synthetic"]
        with self.assertRaisesRegex(DataError, "source provenance"):
            validate_snapshot(row)

    def test_duplicate_and_nonfinite_json_rejected(self):
        with workspace_temp() as tmp:
            path = Path(tmp) / "input.json"
            for raw in ('{"x": 1, "x": 2}', '{"x": NaN}'):
                path.write_text(raw)
                with self.assertRaises(DataError):
                    load_json(path)
            path.write_text(json.dumps([snapshot(), snapshot()]))
            with self.assertRaisesRegex(DataError, "Duplicate symbol"):
                load_snapshots(path)

    def test_membership_effective_dates_and_publication(self):
        rows = [dict(symbol="LEFT", effective_from="2000-01-01", effective_to="2010-01-01",
                     available_at="2000-01-01", source="test"),
                dict(symbol="NEW", effective_from="2010-01-01", effective_to=None,
                     available_at="2009-12-20", source="test"),
                dict(symbol="LATE", effective_from="2000-01-01", effective_to=None,
                     available_at="2020-01-01", source="test")]
        self.assertEqual(universe_at(rows, "2009-12-31"), ["LEFT"])
        self.assertEqual(universe_at(rows, "2010-01-01"), ["NEW"])
        report = coverage_report([], rows, "2010-01-01")
        self.assertEqual(report["missing_symbols"], ["NEW"])
        self.assertFalse(report["complete"])

    def test_membership_csv_and_overlap(self):
        with workspace_temp() as tmp:
            path = Path(tmp) / "members.csv"
            path.write_text("symbol,effective_from,effective_to,available_at,source\nA,2000-01-01,,2000-01-01,test\n")
            rows = load_memberships(path)
            self.assertEqual(universe_at(rows, "2020-01-01"), ["A"])
            with self.assertRaisesRegex(DataError, "Overlapping"):
                universe_at(rows + rows, "2020-01-01")

    def test_current_mapping_cannot_backdate_availability(self):
        rows = [{"end": "2027-12-31", "mean": "14.2", "n": "20"}]
        args = dict(field_map={"target_date": "end", "eps_mean": "mean", "analysts": "n"},
                    retrieved_at="2026-11-06T08:00:00Z", eps_basis="adjusted diluted",
                    share_basis="2026-11-06 split basis", source="archive.raw.json")
        with self.assertRaises(DataError):
            normalize_consensus_rows(rows, **args)
        mapped = normalize_consensus_rows(rows, **args, annual_only_confirmed=True)
        self.assertEqual(mapped[0]["available_at"], "2026-11-07")
        self.assertNotIn("eps_std", mapped[0])  # Missing dispersion is not manufactured.
        rows[0]["n"] = "3.5"
        with self.assertRaises(DataError):
            normalize_consensus_rows(rows, **args, annual_only_confirmed=True)

    def test_credentials_not_logged_or_archived_on_provider_error(self):
        with workspace_temp() as tmp:
            with patch("stocking.data._download_json", return_value=({"Information": "quota"}, b'{}')):
                with self.assertRaises(DataError) as ctx:
                    fetch_alpha_vantage("IBM", tmp, api_key="super-secret-test")
                self.assertNotIn("super-secret-test", str(ctx.exception))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_raw_archive_hash_and_key_exclusion(self):
        with workspace_temp() as tmp:
            raw = b'{"symbol":"IBM","estimates":[]}'
            with patch("stocking.data._download_json", return_value=(json.loads(raw), raw)):
                path = fetch_alpha_vantage("IBM", tmp, api_key="super-secret-test")
            archive = load_json(path)
            self.assertNotIn("super-secret-test", path.read_text())
            self.assertEqual(path.with_suffix(".raw.json").read_bytes(), raw)
            self.assertTrue(archive["metadata"]["normalization_required"])


if __name__ == "__main__":
    unittest.main()
