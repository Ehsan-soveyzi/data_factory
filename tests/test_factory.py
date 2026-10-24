"""Section six: complete offline pipeline, interval boundaries and retry budget."""

from copy import deepcopy
import base64
from datetime import datetime
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from data_factory.exceptions import ConfigurationError, DownloadError
from data_factory.http_client import HttpClient
from data_factory.incremental import incremental_config
from data_factory.ingestion import download_batches
from data_factory.pipeline import run_pipeline, track_http_requests
from data_factory.population import prepare_population_reference
from data_factory.schema import load_schema
from data_factory.testing import run_tests
from data_factory.validation import normalize_record
from test_ingestion import Response, Session
from test_population import CSV, PROJECT
from test_validation import case


class FactoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((PROJECT / "config/setting.json").read_text())
        self.config["validation"]["schema_path"] = str(PROJECT / "config/schema.json")
        for name, folder in (("raw_dir", "raw"), ("created_dir", "created"), ("reference_dir", "reference")):
            self.config["paths"][name] = str(self.root / folder)
        self.config["population"]["expected_sha256"] = hashlib.sha256(CSV.encode()).hexdigest()
        self.config["http"].update(batch_size=2, max_retries=0)

    def completed(self):
        return download_batches(self.config, session=Session(Response([])), progress=None)

    def test_entire_pipeline_from_empty_directories_and_repeat_have_identical_artifact_hashes(self):
        reference = {"encoding": "base64", "size": len(CSV.encode()),
                     "content": base64.b64encode(CSV.encode()).decode()}
        transport = Session(Response([case("1", incident_zip="10001", borough="MANHATTAN",
                                        closed_date="2024-01-02T01:00:00"), case("2")]), Response([]), Response(reference))
        transport.close = Mock()
        with patch("requests.Session", return_value=transport):
            first = run_pipeline(self.config, progress=None)
            second = run_pipeline(self.config, progress=None)
        self.assertTrue(first["delivery_complete"])
        self.assertEqual(first["analytical_records"], 2)
        self.assertEqual(first["files"], second["files"])
        first_manifest = json.loads(Path(first["manifest_path"]).read_text())
        second_manifest = json.loads(Path(second["manifest_path"]).read_text())
        self.assertEqual(first_manifest["request_budget"]["http_attempts"], 3)
        self.assertEqual(second_manifest["request_budget"]["http_attempts"], 0)
        self.assertEqual(first_manifest["request_budget"]["ideal_fresh_acquisition_minimum"], 2)
        self.assertEqual(len(transport.calls), 3)

    def test_retry_budget_counts_failed_attempts_and_scopes_are_isolated(self):
        settings = {**self.config["http"], "max_retries": 1}
        transport = Session(Response({}, status=503), Response([]))
        with patch("data_factory.http_client.time.sleep"), track_http_requests() as budget:
            HttpClient(settings, session=transport).request_json("GET", "https://example.test")
        self.assertEqual(budget["http_attempts"], 2)
        with track_http_requests() as next_budget:
            self.assertEqual(next_budget["http_attempts"], 0)

    def test_failed_transport_is_counted(self):
        with track_http_requests() as budget, self.assertRaises(DownloadError):
            HttpClient(self.config["http"], session=Session(Response({}, status=403))).request_json("GET", "https://example.test")
        self.assertEqual(budget["http_attempts"], 1)

    def test_incremental_refuses_incomplete_base_without_making_a_gap(self):
        with self.assertRaisesRegex(ConfigurationError, "Complete the base snapshot"):
            incremental_config(self.config, now=datetime(2025, 8, 1))
        self.assertFalse((Path(self.config["paths"]["raw_dir"]) / "incremental").exists())

    def test_incremental_starts_at_exclusive_end_and_keeps_base_unchanged(self):
        self.completed()
        base = Path(self.config["paths"]["raw_dir"]) / "checkpoint.json"
        original = base.read_bytes()
        delta = incremental_config(self.config, now=datetime(2025, 8, 1))
        self.assertEqual(datetime.fromisoformat(delta["source"]["start_date"]), datetime(2025, 7, 1))
        self.assertEqual(datetime.fromisoformat(delta["source"]["end_date_exclusive"]), datetime(2025, 8, 1))
        self.assertNotEqual(delta["paths"]["raw_dir"], self.config["paths"]["raw_dir"])
        self.assertEqual(base.read_bytes(), original)

    def test_interrupted_incremental_reuses_frozen_end_instead_of_now(self):
        self.completed()
        first = incremental_config(self.config, now=datetime(2025, 8, 1))
        second = incremental_config(self.config, now=datetime(2025, 9, 1))
        self.assertEqual(first, second)

    def test_next_completed_incremental_continues_without_overlap(self):
        self.completed()
        first = incremental_config(self.config, now=datetime(2025, 8, 1))
        download_batches(first, session=Session(Response([])), progress=None)
        second = incremental_config(self.config, now=datetime(2025, 9, 1))
        self.assertEqual(second["source"]["start_date"], first["source"]["end_date_exclusive"])
        self.assertNotEqual(first["paths"]["raw_dir"], second["paths"]["raw_dir"])

    def test_incremental_rejects_nonincreasing_end_and_changed_settings(self):
        self.completed()
        with self.assertRaises(ConfigurationError):
            incremental_config(self.config, now=datetime(2024, 1, 1))
        incremental_config(self.config, now=datetime(2025, 8, 1))
        changed = deepcopy(self.config)
        changed["validation"]["max_quarantine_fraction"] = 0.02
        with self.assertRaises(ConfigurationError):
            incremental_config(changed, now=datetime(2025, 9, 1))

    def test_incremental_rejects_corrupt_pointer_and_redirected_paths(self):
        self.completed()
        incremental_config(self.config, now=datetime(2025, 8, 1))
        pointer = Path(self.config["paths"]["raw_dir"]) / "incremental/latest.json"
        value = json.loads(pointer.read_text())
        value["config"]["paths"]["raw_dir"] = str(self.root / "unexpected")
        pointer.write_text(json.dumps(value))
        with self.assertRaises(ConfigurationError):
            incremental_config(self.config, now=datetime(2025, 9, 1))
        self.assertFalse((self.root / "unexpected").exists())

    def test_leap_day_and_subsecond_closure_cross_month_correctly(self):
        schema = load_schema(PROJECT / "config/schema.json", source=self.config["source"])
        clean, errors = normalize_record(case(created_date="2024-02-29T23:59:59.500000",
                                             closed_date="2024-03-01T00:00:00.500000"), schema, self.config["source"])
        self.assertEqual(errors, [])
        self.assertAlmostEqual(clean["closure_hours"], 1 / 3600)
        self.assertEqual(clean["created_month"], "2024-02")

    def test_scope_start_inclusive_and_end_exclusive(self):
        schema = load_schema(PROJECT / "config/schema.json", source=self.config["source"])
        for date, valid in (("2024-01-01T00:00:00", True), ("2025-06-30T23:59:59.999999", True),
                            ("2025-07-01T00:00:00", False)):
            with self.subTest(date=date):
                clean, errors = normalize_record(case(created_date=date), schema, self.config["source"])
                self.assertEqual(clean is not None, valid)

    def test_guarded_discovery_denies_real_connections_and_dns(self):
        project = self.root / "guarded_project"
        tests = project / "tests"
        tests.mkdir(parents=True)
        (tests / "test_network_guard_fixture.py").write_text(
            "import socket, unittest\nclass Guard(unittest.TestCase):\n"
            " def test_guard(self):\n"
            "  with self.assertRaises(AssertionError): socket.create_connection(('example.test', 443))\n"
            "  with self.assertRaises(AssertionError): socket.getaddrinfo('example.test', 443)\n")
        result = run_tests(project, stream=StringIO())
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)


if __name__ == "__main__":
    unittest.main()
