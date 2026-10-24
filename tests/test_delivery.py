"""Offline final-delivery tests: correct denominators, coverage and audit trails."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_factory.cli import main
from data_factory.delivery import ARTIFACTS, build_delivery
from data_factory.exceptions import ConfigurationError, DataError
from data_factory.population import join_population, prepare_population_reference
from data_factory.profiling import canonical_json
from data_factory.schema import load_schema
from data_factory.validation import normalize_record
from test_population import CSV, PROJECT
from test_validation import case


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((PROJECT / "config/setting.json").read_text())
        self.config["paths"]["created_dir"] = str(self.root / "created")
        self.config["paths"]["reference_dir"] = str(self.root / "reference")
        self.config["source"]["end_date_exclusive"] = "2024-04-01T00:00:00"
        self.config["source"]["complaint_types"] = ["Noise - Residential"]
        self.config["population"]["expected_sha256"] = hashlib.sha256(CSV.encode()).hexdigest()
        prepare_population_reference(self.config, csv_text=CSV, acquired_via="offline fixture")
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network access"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def inputs(self, rows=None, *, completed=True, rejected=0):
        if rows is None:
            rows = [case("1", incident_zip="10001", borough="MANHATTAN",
                         closed_date="2024-01-02T02:00:00")]
        schema = load_schema(PROJECT / "config/schema.json", source=self.config["source"])
        clean = []
        for row in rows:
            normalized, errors = normalize_record(row, schema, self.config["source"])
            self.assertEqual(errors, [])
            clean.append(normalized)
        directory = Path(self.config["paths"]["created_dir"]) / "validation" / ("a" * 64)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "accepted.jsonl").write_text("".join(canonical_json(r) + "\n" for r in clean), encoding="utf-8")
        (directory / "quarantine.jsonl").write_text("{}\n" * rejected, encoding="utf-8")
        (directory / "quality_profile.json").write_text("{}\n", encoding="utf-8")
        scope = {**deepcopy(self.config["source"]), "order": "created_date ASC, unique_key ASC"}
        report = {"status": "passed", "accepted_records": len(rows), "raw_records": len(rows) + rejected,
            "quarantined_records": rejected, "reason_counts": {"MISSING_REQUIRED_VALUE": rejected} if rejected else {},
            "snapshot_completed": completed, "scope": scope,
            "scope_note": "Full acquisition" if completed else "Partial acquisition",
            "files": {name: {"sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
                for name in ("accepted.jsonl", "quarantine.jsonl", "quality_profile.json")}}
        (directory / "validation_report.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
        (directory.parent / "latest.json").write_text(canonical_json({"run": "a" * 64,
            "report": f"{'a' * 64}/validation_report.json", "status": "passed"}), encoding="utf-8")
        return join_population(self.config, progress=None), directory

    def deliver(self):
        return build_delivery(self.config, progress=None)

    def wide(self, result, area="MODZCTA:10001"):
        with (Path(result["output_dir"]) / "monthly_summary_wide.csv").open(encoding="utf-8", newline="") as file:
            return {row["metric"]: row for row in csv.DictReader(file) if row["analysis_area_key"] == area}

    def test_shared_zip_population_counted_once_and_null_closure_not_zero(self):
        rows = [case("1", incident_zip="10001", borough="MANHATTAN", closed_date="2024-01-02T00:00:00"),
                case("2", incident_zip="10118", borough="MANHATTAN", closed_date="2024-01-02T04:00:00"),
                case("3", incident_zip="10001", borough="MANHATTAN")]
        self.inputs(rows, rejected=1)
        result = self.deliver()
        wide = self.wide(result)
        self.assertEqual(wide["case_count"]["2024-01"], "3")
        self.assertEqual(wide["recorded_closure_count"]["2024-01"], "2")
        self.assertEqual(wide["missing_closure_count"]["2024-01"], "1")
        self.assertEqual(float(wide["mean_closure_hours"]["2024-01"]), 2)
        self.assertEqual(float(wide["median_closure_hours"]["2024-01"]), 2)
        self.assertAlmostEqual(float(wide["complaints_per_population_scale"]["2024-01"]), 3 / 100.25 * 100000, places=5)
        final = [json.loads(line) for line in (Path(result["output_dir"]) / "analytical_cases.jsonl").read_text().splitlines()]
        self.assertEqual([r["unique_key"] for r in final], ["1", "2", "3"])
        self.assertEqual([r["closure_days"] for r in final], [0, 4 / 24, None])
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        self.assertEqual(manifest["raw_records"], 4)
        self.assertEqual(manifest["quarantine_reason_counts"], {"MISSING_REQUIRED_VALUE": 1})
        self.assertLessEqual(datetime.fromisoformat(manifest["started_at_utc"]), datetime.fromisoformat(manifest["finished_at_utc"]))
        for name, metadata in manifest["files"].items():
            self.assertEqual(metadata["sha256"], hashlib.sha256((Path(result["output_dir"]) / name).read_bytes()).hexdigest())

    def test_median_odd_count_and_mean_have_expected_values(self):
        self.inputs([case(str(i), incident_zip="10001", closed_date=f"2024-01-02T{hour:02d}:00:00")
                     for i, hour in enumerate([0, 3, 9])])
        wide = self.wide(self.deliver())
        self.assertEqual(float(wide["median_closure_hours"]["2024-01"]), 3)
        self.assertEqual(float(wide["mean_closure_hours"]["2024-01"]), 4)

    def test_mismatch_unmatched_missing_and_zero_population_are_preserved(self):
        rows = [case("1", incident_zip="10001", borough="QUEENS"),
                case("2", incident_zip="99999", borough="QUEENS"),
                case("3", incident_zip=None), case("4", incident_zip="10002", borough="MANHATTAN")]
        self.inputs(rows)
        result = self.deliver()
        self.assertEqual(result["analytical_records"], 4)
        self.assertEqual(result["rate_eligible_records"], 0)
        self.assertEqual(self.wide(result)["rate_excluded_case_count"]["2024-01"], "1")
        self.assertEqual(self.wide(result, "UNMATCHED_ZIP:99999")["case_count"]["2024-01"], "1")
        self.assertEqual(self.wide(result, "MISSING_ZIP")["complaints_per_population_scale"]["2024-01"], "")
        self.assertEqual(self.wide(result, "MODZCTA:10002")["complaints_per_population_scale"]["2024-01"], "")

    def test_partial_months_have_counts_but_no_rates_and_future_is_blank(self):
        self.inputs([case("1", created_date="2024-01-02T00:00:00", incident_zip="10001"),
                     case("2", created_date="2024-02-10T00:00:00", incident_zip="10001")], completed=False)
        result = self.deliver()
        self.assertFalse(result["delivery_complete"])
        self.assertEqual(result["month_coverage"], {"2024-01": "complete", "2024-02": "partial_acquisition", "2024-03": "not_acquired"})
        wide = self.wide(result)
        self.assertEqual(wide["case_count"]["2024-02"], "1")
        self.assertEqual(wide["case_count"]["2024-03"], "")
        self.assertNotEqual(wide["complaints_per_population_scale"]["2024-01"], "")
        self.assertEqual(wide["complaints_per_population_scale"]["2024-02"], "")

    def test_empty_completed_months_have_zero_counts_but_null_durations(self):
        self.inputs([])
        wide = self.wide(self.deliver())
        self.assertEqual(wide["case_count"]["2024-01"], "0")
        self.assertEqual(wide["mean_closure_hours"]["2024-01"], "")
        self.assertEqual(float(wide["complaints_per_population_scale"]["2024-01"]), 0)

    def test_empty_incomplete_snapshot_does_not_invent_zeros(self):
        self.inputs([], completed=False)
        result = self.deliver()
        self.assertEqual(set(result["month_coverage"].values()), {"not_acquired"})
        self.assertEqual(self.wide(result)["case_count"]["2024-01"], "")

    def test_partial_scope_months_and_borough_filters_do_not_get_full_area_rates(self):
        self.config["source"]["start_date"] = "2024-01-02T00:00:00"
        self.config["source"]["boroughs"] = ["MANHATTAN"]
        self.inputs()
        result = self.deliver()
        self.assertEqual(result["month_coverage"]["2024-01"], "partial_scope")
        wide = self.wide(result)
        self.assertEqual(wide["case_count"]["2024-01"], "1")
        self.assertEqual(wide["complaints_per_population_scale"]["2024-02"], "")

    def test_independent_analytics_identical_while_execution_manifests_differ(self):
        self.inputs()
        first = self.deliver()
        contents = {name: (Path(first["output_dir"]) / name).read_bytes() for name in ARTIFACTS}
        mtimes = {name: (Path(first["output_dir"]) / name).stat().st_mtime_ns for name in ARTIFACTS}
        again = self.deliver()
        self.assertNotEqual(first["manifest_path"], again["manifest_path"])
        self.assertEqual(mtimes, {name: (Path(first["output_dir"]) / name).stat().st_mtime_ns for name in ARTIFACTS})
        self.config["paths"]["created_dir"] = str(self.root / "independent")
        self.inputs()
        other = self.deliver()
        self.assertEqual(contents, {name: (Path(other["output_dir"]) / name).read_bytes() for name in ARTIFACTS})

    def test_changed_population_file_and_stale_validation_cannot_publish(self):
        joined, validation = self.inputs()
        enriched = Path(joined["output_dir"]) / "enriched_cases.jsonl"
        original = enriched.read_bytes()
        enriched.write_text("changed")
        with self.assertRaisesRegex(DataError, "checksum"):
            self.deliver()
        enriched.write_bytes(original)
        report = validation / "validation_report.json"
        report.write_text(report.read_text() + " ")
        with self.assertRaisesRegex(DataError, "stale"):
            self.deliver()
        self.assertFalse((Path(self.config["paths"]["created_dir"]) / "delivery/latest.json").exists())

    def test_changed_source_selection_and_reference_pin_require_new_upstream_outputs(self):
        self.inputs()
        for section, field, value in [("source", "end_date_exclusive", "2024-05-01T00:00:00"),
                                      ("population", "reference_date", "2020-01-01")]:
            with self.subTest(field=field):
                config = deepcopy(self.config)
                config[section][field] = value
                with self.assertRaises(DataError):
                    build_delivery(config, progress=None)

    def test_existing_corrupted_analytics_are_not_overwritten(self):
        self.inputs()
        first = self.deliver()
        path = Path(first["output_dir"]) / "monthly_summary_wide.csv"
        path.write_text("changed")
        with self.assertRaisesRegex(DataError, "overwrite"):
            self.deliver()
        self.assertEqual(path.read_text(), "changed")

    def test_interruption_cleans_work_and_rerun_succeeds(self):
        self.inputs()
        with patch("data_factory.delivery._summary", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.deliver()
        directory = Path(self.config["paths"]["created_dir"]) / "delivery"
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(self.deliver()["analytical_records"], 1)

    def test_invalid_settings_fail_before_publication(self):
        for key, value in [("population_scale", 0), ("population_scale", float("nan")),
                           ("decimal_places", True), ("max_months", 0)]:
            config = deepcopy(self.config)
            config["delivery"][key] = value
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                build_delivery(config, progress=None)

    def test_semantically_invalid_duration_and_duplicate_ids_are_rejected(self):
        joined, _ = self.inputs()
        path = Path(joined["output_dir"]) / "enriched_cases.jsonl"
        report_path = path.parent / "population_join_report.json"
        original = json.loads(path.read_text())
        for changes in ({"closure_hours": -2}, {"closure_hours": None}, {"created_month": "2024-03"}):
            with self.subTest(changes=changes):
                path.write_text(canonical_json({**original, **changes}) + "\n", encoding="utf-8")
                report = json.loads(report_path.read_text())
                report["files"][path.name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(DataError, "Invalid analytical case"):
                    self.deliver()
        path.write_text((canonical_json(original) + "\n") * 2, encoding="utf-8")
        report["files"][path.name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(DataError, "Duplicate"):
            self.deliver()

    def test_input_modified_during_aggregation_prevents_publication(self):
        joined, _ = self.inputs()
        path = Path(joined["output_dir"]) / "enriched_cases.jsonl"
        from data_factory.delivery import _summary

        def change_after_summary(*args):
            result = _summary(*args)
            path.write_text(path.read_text() + " ", encoding="utf-8")
            return result

        with patch("data_factory.delivery._summary", side_effect=change_after_summary):
            with self.assertRaisesRegex(DataError, "changed while processing"):
                self.deliver()
        root = Path(self.config["paths"]["created_dir"]) / "delivery"
        self.assertFalse((root / "latest.json").exists())
        self.assertEqual(list(root.iterdir()), [])

    def test_cli_finalize_and_all_dispatch_and_propagate_failures(self):
        self.inputs()
        with patch("sys.argv", ["data-factory", "run", "--stage", "finalize"]), \
                patch("data_factory.cli.load_config", return_value=self.config), redirect_stdout(StringIO()):
            self.assertEqual(main(), 0)
        with patch("sys.argv", ["data-factory", "run", "--stage", "all"]), \
                patch("data_factory.cli.load_config", return_value=self.config), \
                patch("data_factory.cli.download_batches") as download, \
                patch("data_factory.cli.validate_dataset") as validate, \
                patch("data_factory.cli.join_population") as population, redirect_stdout(StringIO()):
            self.assertEqual(main(), 0)
            download.assert_called_once()
            validate.assert_called_once()
            population.assert_called_once()
        with patch("sys.argv", ["data-factory", "run", "--stage", "all"]), \
                patch("data_factory.cli.load_config", return_value=self.config), \
                patch("data_factory.cli.download_batches", side_effect=DataError("broken")), \
                patch("data_factory.cli.build_delivery") as final, redirect_stderr(StringIO()):
            self.assertEqual(main(), 4)
            final.assert_not_called()


if __name__ == "__main__":
    unittest.main()
