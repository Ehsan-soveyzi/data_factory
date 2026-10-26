"""Offline streaming validation, publication integrity and CLI failure behavior."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_factory.cli import main
from data_factory.exceptions import ConfigurationError, DataError, QuarantineThresholdError, SchemaDriftError
from data_factory.quality import validate_dataset
from data_factory.ingestion import download_batches, prepare_checkpoint
from test_ingestion import Response, Session
from test_validation import SCHEMA_PATH, SOURCE, case


class DatasetValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.config = {
            "source": deepcopy(SOURCE),
            "validation": {"schema_path": str(SCHEMA_PATH), "inspection_batches": 1,
                "schema_drift_min_records": 2, "profile_buffer_size": 2,
                "max_quarantine_fraction": 1.0},
            "paths": {"raw_dir": str(self.raw), "curated_dir": str(self.root / "curated"),
                "inspection_file": "inspection.json", "validation_dir": "validation"},
        }
        self.state = {"scope": deepcopy(SOURCE), "scope_fingerprint": "fixture",
                      "completed": False, "downloaded_records": 0, "batches": []}
        snapshot = patch("data_factory.quality.prepare_checkpoint", side_effect=lambda _: deepcopy(self.state))
        self.prepare = snapshot.start()
        self.addCleanup(snapshot.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Validation made an HTTP call"))
        network.start()
        self.addCleanup(network.stop)

    def batch(self, rows):
        filename = f"batch_{len(self.state['batches']) + 1:06d}.json"
        payload = json.dumps(rows).encode("utf-8")
        (self.raw / filename).write_bytes(payload)
        self.state["batches"].append({"file": filename, "records": len(rows),
                                    "sha256": hashlib.sha256(payload).hexdigest()})
        self.state["downloaded_records"] += len(rows)

    def run_validation(self):
        return validate_dataset(self.config, progress=None)

    @staticmethod
    def read_lines(directory, filename):
        return [json.loads(line) for line in (Path(directory) / filename).read_text(encoding="utf-8").splitlines()]

    def test_duplicates_across_batches_preserve_first_valid_case_and_full_evidence(self):
        first = case("1", extra="kept in raw")
        self.batch([case("1", agency=None), first, case("2")])
        self.batch([deepcopy(first), case("1", extra="changed"), case("3")])
        original = {p.name: p.read_bytes() for p in self.raw.iterdir()}
        result = self.run_validation()
        accepted = self.read_lines(result["output_dir"], "accepted.jsonl")
        rejected = self.read_lines(result["output_dir"], "quarantine.jsonl")
        self.assertEqual([r["unique_key"] for r in accepted], ["1", "2", "3"])
        self.assertEqual(result["raw_records"], 6)
        self.assertEqual(result["accepted_records"], 3)
        self.assertEqual(result["quarantined_records"], 3)
        self.assertEqual(result["reason_counts"], {
            "DUPLICATE_KEY_CONFLICT": 1, "DUPLICATE_RECORD": 1, "MISSING_REQUIRED_VALUE": 1})
        self.assertEqual(rejected[1]["record"], first)
        self.assertEqual(rejected[1]["source_file"], "batch_000002.json")
        self.assertEqual(rejected[1]["source_row"], 1)
        self.assertIn("extra", result["source_fields_outside_projection"])
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.raw.iterdir()})
        profile = json.loads((Path(result["output_dir"]) / "quality_profile.json").read_text())
        self.assertEqual(profile["rows"], 3)
        self.assertEqual(profile["columns"]["unique_key"]["unique_non_missing"], 3)

    def test_reason_counts_count_records_per_code_without_hiding_multiple_errors(self):
        self.batch([case(), case("2", agency=None, complaint_type=None)])
        result = self.run_validation()
        self.assertEqual(result["quarantined_records"], 1)
        self.assertEqual(result["reason_counts"], {"MISSING_REQUIRED_VALUE": 1})
        rejected = self.read_lines(result["output_dir"], "quarantine.jsonl")
        self.assertEqual(len(rejected[0]["errors"]), 2)

    def test_threshold_failure_publishes_diagnostics_and_failure_marker(self):
        self.batch([case(), case("2", closed_date="2023-01-01T00:00:00")])
        self.config["validation"]["max_quarantine_fraction"] = 0.49
        with self.assertRaises(QuarantineThresholdError) as raised:
            self.run_validation()
        report_path = Path(raised.exception.report_path)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["quarantine_fraction"], 0.5)
        self.assertEqual(len(self.read_lines(report_path.parent, "quarantine.jsonl")), 1)
        latest = json.loads((report_path.parent.parent / "latest.json").read_text())
        self.assertEqual(latest["status"], "failed")

    def test_threshold_equality_passes_and_partial_scope_is_explicit(self):
        self.batch([case(), case("2", agency=None)])
        self.config["validation"]["max_quarantine_fraction"] = 0.5
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["snapshot_completed"])
        self.assertIn("Partial", result["scope_note"])

    def test_all_quarantined_and_none_quarantined_have_correct_empty_outputs(self):
        for good in (True, False):
            with self.subTest(all_good=good):
                self.state["batches"] = []
                self.state["downloaded_records"] = 0
                self.batch([case(agency="NYPD" if good else None)])
                result = self.run_validation()
                self.assertEqual(result["accepted_records"], int(good))
                self.assertEqual(result["quarantined_records"], int(not good))
                empty = "quarantine.jsonl" if good else "accepted.jsonl"
                self.assertEqual((Path(result["output_dir"]) / empty).read_bytes(), b"")

    def test_repeated_and_independent_runs_produce_identical_bytes(self):
        self.batch([case(), case("2", incident_zip="bad")])
        first = self.run_validation()
        paths = list(Path(first["output_dir"]).iterdir())
        contents = {p.name: p.read_bytes() for p in paths}
        mtimes = {p.name: p.stat().st_mtime_ns for p in paths}
        again = self.run_validation()
        self.assertEqual(first, again)
        self.assertEqual(mtimes, {p.name: p.stat().st_mtime_ns for p in paths})
        self.config["paths"]["curated_dir"] = str(self.root / "independent")
        other = self.run_validation()
        self.assertEqual(contents, {p.name: p.read_bytes() for p in Path(other["output_dir"]).iterdir()})
        for name, metadata in other["files"].items():
            self.assertEqual(metadata["sha256"], hashlib.sha256(contents[name]).hexdigest())

    def test_corrupted_published_output_is_not_overwritten(self):
        self.batch([case()])
        result = self.run_validation()
        path = Path(result["output_dir"]) / "accepted.jsonl"
        path.write_text("corrupted", encoding="utf-8")
        with self.assertRaises(DataError):
            self.run_validation()
        self.assertEqual(path.read_text(), "corrupted")

    def test_changed_contract_creates_a_separate_result(self):
        self.batch([case(incident_zip="1234")])
        first = self.run_validation()
        schema = json.loads(SCHEMA_PATH.read_text())
        schema["fields"]["incident_zip"]["pattern"] = "^[0-9]{4,5}$"
        schema_path = self.root / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        self.config["validation"]["schema_path"] = str(schema_path)
        second = self.run_validation()
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertNotEqual(first["schema_hash"], second["schema_hash"])
        self.assertNotEqual(first["files"]["accepted.jsonl"], second["files"]["accepted.jsonl"])

    def test_drift_and_changed_raw_hash_stop_without_publishing(self):
        for rows, tamper, error in [([case(agency=123), case("2", agency=456)], False, SchemaDriftError),
                                    ([case()], True, DataError)]:
            with self.subTest(tamper=tamper):
                self.state["batches"] = []
                self.state["downloaded_records"] = 0
                self.batch(rows)
                if tamper:
                    (self.raw / "batch_000001.json").write_text("[]")
                with self.assertRaises(error):
                    self.run_validation()
                root = Path(self.config["paths"]["curated_dir"]) / "validation"
                self.assertEqual(list(root.iterdir()), [])

    def test_interruption_cleans_temporary_files_and_rerun_can_finish(self):
        self.batch([case()])
        def interrupt(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            validate_dataset(self.config, progress=interrupt)
        root = Path(self.config["paths"]["curated_dir"]) / "validation"
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_bad_settings_fail_before_snapshot_or_output_writes(self):
        changes = [("max_quarantine_fraction", float("nan")), ("max_quarantine_fraction", True),
                   ("max_quarantine_fraction", -0.01), ("schema_drift_min_records", 0)]
        for name, value in changes:
            with self.subTest(setting=name, value=value):
                config = deepcopy(self.config)
                config["validation"][name] = value
                with self.assertRaises(ConfigurationError):
                    validate_dataset(config, progress=None)
        config = deepcopy(self.config)
        config["source"]["complaint_types"] = []
        with self.assertRaises(ConfigurationError):
            validate_dataset(config, progress=None)
        self.prepare.assert_not_called()
        self.assertFalse(Path(config["paths"]["curated_dir"]).exists())

    def test_no_batches_is_an_actionable_data_error(self):
        with self.assertRaisesRegex(DataError, "ingest"):
            self.run_validation()

    def test_real_acquisition_checkpoint_feeds_offline_validation(self):
        config = deepcopy(self.config)
        config["source"]["endpoint"] = "https://example.test/resource/data.json"
        config["http"] = {"batch_size": 10, "timeout_seconds": 1, "max_retries": 0}
        config["paths"]["checkpoint_file"] = "checkpoint.json"
        session = Session(Response([case("1"), case("2", closed_date="2024-01-02T01:00:00")]), Response([]))
        downloaded = download_batches(config, session=session, progress=None)
        self.assertTrue(downloaded["completed"])
        with patch("data_factory.quality.prepare_checkpoint", wraps=prepare_checkpoint):
            result = validate_dataset(config, progress=None)
        self.assertTrue(result["snapshot_completed"])
        self.assertEqual(result["accepted_records"], 2)
        self.assertEqual(result["quarantined_records"], 0)
        self.assertEqual(result["scope"], downloaded["scope"])

    def test_cli_success_and_threshold_failure_use_distinct_exit_statuses(self):
        self.batch([case(), case("2", agency=None)])
        for threshold, expected_code in [(0.5, 0), (0.49, 4)]:
            self.config["validation"]["max_quarantine_fraction"] = threshold
            output, errors = StringIO(), StringIO()
            with patch("sys.argv", ["data-factory", "run", "--stage", "validate"]), \
                    patch("data_factory.cli.load_config", return_value=self.config), \
                    redirect_stdout(output), redirect_stderr(errors):
                code = main()
            self.assertEqual(code, expected_code)
            if code:
                self.assertIn("diagnostics:", errors.getvalue())
            else:
                self.assertIn('"accepted_records": 1', output.getvalue())


if __name__ == "__main__":
    unittest.main()
