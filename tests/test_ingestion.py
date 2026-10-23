"""Offline tests for pagination, crash recovery and acquisition failures."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from data_factory import http_client, ingestion
from data_factory.exceptions import CheckpointError, ConfigurationError, DataError, DownloadError


def record(key, date="2024-01-01T00:00:00.000", **extra):
    return {"created_date": date, "unique_key": key, "complaint_type": "Noise - Residential", "borough": "BROOKLYN", **extra}


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **deepcopy(kwargs)})
        if not self.responses:
            raise AssertionError("Unexpected HTTP request.")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.raw = Path(self.directory.name) / "raw"
        self.config = {
            "source": {
                "endpoint": "https://example.test/resource/data.json",
                "start_date": "2024-01-01T00:00:00",
                "end_date_exclusive": "2025-07-01T00:00:00",
                "complaint_types": ["Noise - Residential"],
                "boroughs": [],
            },
            "http": {"batch_size": 2, "timeout_seconds": 5, "max_retries": 0},
            "paths": {"raw_dir": str(self.raw), "checkpoint_file": "checkpoint.json"},
        }

    def run_download(self, *responses, **kwargs):
        session = Session(*responses)
        state = ingestion.download_batches(self.config, session=session, progress=None, **kwargs)
        return state, session

    def saved_state(self):
        return json.loads((self.raw / "checkpoint.json").read_text(encoding="utf-8"))

    def test_initialize_without_http(self):
        state = ingestion.prepare_checkpoint(self.config)
        self.assertEqual(state["downloaded_records"], 0)
        self.assertFalse(state["completed"])
        self.assertEqual(self.saved_state(), state)

    def test_automatic_loop_keeps_short_page_and_checks_empty_page(self):
        state, session = self.run_download(Response([record("10"), record("2")]), Response([record("9")]), Response([]))
        self.assertTrue(state["completed"])
        self.assertEqual(state["downloaded_records"], 3)
        self.assertEqual(state["last_batch"], 2)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0]["params"]["$order"], ingestion.ORDER)
        self.assertEqual(session.calls[0]["timeout"], 5)
        self.assertTrue(all(call["method"] == "GET" for call in session.calls))
        self.assertIn("unique_key > '2'", session.calls[1]["params"]["$where"])
        self.assertIn("created_date = '2024-01-01T00:00:00.000'", session.calls[1]["params"]["$where"])

    def test_resume_and_completed_rerun_make_no_duplicate_files(self):
        first, _ = self.run_download(Response([record("1"), record("2")]), max_batches=1)
        self.assertFalse(first["completed"])
        original = (self.raw / "batch_000001.json").read_bytes()
        final, session = self.run_download(Response([record("3")]), Response([]))
        self.assertEqual(final["downloaded_records"], 3)
        self.assertIn("unique_key > '2'", session.calls[0]["params"]["$where"])
        self.assertEqual((self.raw / "batch_000001.json").read_bytes(), original)
        again, no_requests = self.run_download()
        self.assertEqual(final, again)
        self.assertEqual(no_requests.calls, [])

    def test_crash_after_batch_save_recovers_without_redownload(self):
        ingestion.prepare_checkpoint(self.config)
        original_write = ingestion._atomic_write

        def interrupt_checkpoint(path, value):
            if path.name == "checkpoint.json":
                raise KeyboardInterrupt("Simulated kernel interruption")
            return original_write(path, value)

        with patch.object(ingestion, "_atomic_write", side_effect=interrupt_checkpoint):
            with self.assertRaises(KeyboardInterrupt):
                self.run_download(Response([record("1"), record("2")]))
        self.assertEqual(self.saved_state()["last_batch"], 0)
        self.assertTrue((self.raw / "batch_000001.json").exists())
        state, session = self.run_download(Response([]))
        self.assertEqual(state["downloaded_records"], 2)
        self.assertEqual(state["last_batch"], 1)
        self.assertIn("unique_key > '2'", session.calls[0]["params"]["$where"])

    def test_changed_scope_rejected_before_http(self):
        ingestion.prepare_checkpoint(self.config)
        for key, value in [("boroughs", ["BROOKLYN"]), ("complaint_types", ["Noise - Park"]), ("end_date_exclusive", "2025-06-01T00:00:00")]:
            with self.subTest(field=key):
                changed = deepcopy(self.config)
                changed["source"][key] = value
                with self.assertRaises(CheckpointError):
                    ingestion.download_batches(changed, session=Session(), progress=None)

    def test_batch_size_and_seed_can_change_on_resume(self):
        self.run_download(Response([record("1")]), max_batches=1)
        self.config["http"]["batch_size"] = 1
        self.config["source"]["seed"] = 42
        state, _ = self.run_download(Response([record("2")]), Response([]))
        self.assertEqual(state["downloaded_records"], 2)

    def test_filter_list_order_does_not_change_scope_identity(self):
        self.config["source"]["complaint_types"].append("Noise - Park")
        first = ingestion.prepare_checkpoint(self.config)
        self.config["source"]["complaint_types"].reverse()
        second = ingestion.prepare_checkpoint(self.config)
        self.assertEqual(first["scope_fingerprint"], second["scope_fingerprint"])

    def test_tampered_or_missing_raw_file_is_rejected(self):
        self.run_download(Response([record("1")]), max_batches=1)
        path = self.raw / "batch_000001.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            ingestion.prepare_checkpoint(self.config)
        path.unlink()
        with self.assertRaises(CheckpointError):
            ingestion.prepare_checkpoint(self.config)

    def test_corrupt_checkpoint_is_not_silently_reset(self):
        ingestion.prepare_checkpoint(self.config)
        (self.raw / "checkpoint.json").write_text("{truncated", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            ingestion.prepare_checkpoint(self.config)

    def test_invalid_checkpoint_schema_and_counts_are_rejected(self):
        original = ingestion.prepare_checkpoint(self.config)
        for field, value in [("batches", {}), ("version", True), ("downloaded_records", 1), ("last_unique_key", "1")]:
            with self.subTest(field=field):
                state = deepcopy(original)
                state[field] = value
                (self.raw / "checkpoint.json").write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(CheckpointError):
                    ingestion.prepare_checkpoint(self.config)

    def test_batches_without_checkpoint_are_preserved_and_rejected(self):
        self.raw.mkdir()
        path = self.raw / "batch_000001.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            ingestion.prepare_checkpoint(self.config)
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_unexpected_extra_batch_is_not_overwritten(self):
        ingestion.prepare_checkpoint(self.config)
        path = self.raw / "batch_000003.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.run_download()
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_invalid_pages_never_advance_checkpoint(self):
        cases = [
            {"error": "not an array"}, [None], [record("1", unique_key=1)],
            [record("2"), record("1")], [record("1"), record("1")],
            [record("1", date="2025-07-01T00:00:00")],
            [record("1", date="2023-12-31T23:59:59")],
            [record("1", complaint_type="Noise - Park")],
            [record("1"), record("2"), record("3")],
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(DataError):
                    self.run_download(Response(payload))
                self.assertEqual(self.saved_state()["downloaded_records"], 0)
                self.assertEqual(list(self.raw.glob("batch_*.json")), [])

    def test_borough_filter_is_enforced_and_quotes_are_escaped(self):
        self.config["source"]["boroughs"] = ["O'Brien"]
        self.assertIn("borough IN ('O''Brien')", ingestion.build_scope_filter(self.config))
        with self.assertRaises(DataError):
            self.run_download(Response([record("1")]))

    def test_page_repeating_previous_cursor_is_rejected(self):
        self.run_download(Response([record("1")]), max_batches=1)
        with self.assertRaises(DataError):
            self.run_download(Response([record("1")]))
        self.assertEqual(self.saved_state()["downloaded_records"], 1)

    def test_timeout_preserves_saved_progress_and_resume_works(self):
        with self.assertRaises(DownloadError):
            self.run_download(Response([record("1")]), requests.Timeout("timeout"))
        self.assertEqual(self.saved_state()["downloaded_records"], 1)
        state, _ = self.run_download(Response([record("2")]), Response([]))
        self.assertTrue(state["completed"])
        self.assertEqual(state["downloaded_records"], 2)

    @patch.object(http_client.time, "sleep")
    def test_transient_http_errors_retry_with_the_same_cursor(self, sleep):
        self.config["http"]["max_retries"] = 2
        state, session = self.run_download(Response(None, 429, {"Retry-After": "0"}), Response(None, 503), Response([]))
        self.assertTrue(state["completed"])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0]["params"], session.calls[2]["params"])
        self.assertEqual(sleep.call_count, 2)

    @patch.object(http_client.time, "sleep")
    def test_retry_budget_exhausted(self, sleep):
        self.config["http"]["max_retries"] = 1
        with self.assertRaises(DownloadError):
            self.run_download(requests.ConnectionError("offline"), requests.ConnectionError("offline"))
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(self.saved_state()["last_batch"], 0)

    def test_nonretryable_http_error_fails_immediately(self):
        session = Session(Response(None, 400))
        with self.assertRaises(DownloadError):
            ingestion.download_batches(self.config, session=session, progress=None)
        self.assertEqual(len(session.calls), 1)

    @patch.object(http_client.time, "sleep")
    def test_truncated_json_is_retried_but_not_committed(self, sleep):
        self.config["http"]["max_retries"] = 1
        with self.assertRaises(DataError):
            self.run_download(Response(ValueError("truncated")), Response(ValueError("truncated")))
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(self.saved_state()["downloaded_records"], 0)

    def test_max_batches_bounds_one_run_without_completing_it(self):
        state, session = self.run_download(Response([record("1")]), max_batches=1)
        self.assertFalse(state["completed"])
        self.assertEqual(len(session.calls), 1)

    def test_count_uses_the_same_filter(self):
        session = Session(Response([{"n_records": "300001"}]))
        self.assertEqual(ingestion.count_records(self.config, session=session), 300001)
        self.assertEqual(session.calls[0]["params"]["$where"], ingestion.build_scope_filter(self.config))
        with self.assertRaises(DataError):
            ingestion.count_records(self.config, session=Session(Response([{"n_records": "-1"}])))

    def test_invalid_settings_create_no_checkpoint(self):
        for key, value in [("batch_size", 0), ("max_retries", -1), ("timeout_seconds", 0)]:
            with self.subTest(field=key):
                changed = deepcopy(self.config)
                changed["http"][key] = value
                with self.assertRaises(ConfigurationError):
                    ingestion.prepare_checkpoint(changed)
        self.config["paths"]["checkpoint_file"] = "../checkpoint.json"
        with self.assertRaises(ConfigurationError):
            ingestion.prepare_checkpoint(self.config)
        self.assertFalse(self.raw.exists())

    def test_atomic_write_failure_does_not_replace_a_good_checkpoint(self):
        original = ingestion.prepare_checkpoint(self.config)
        with patch.object(ingestion.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                ingestion._atomic_write(self.raw / "checkpoint.json", {"bad": True})
        self.assertEqual(self.saved_state(), original)
        self.assertEqual(list(self.raw.glob("*.tmp")), [])

    def test_simultaneous_run_is_rejected_and_lock_is_reusable(self):
        ingestion.prepare_checkpoint(self.config)
        with ingestion._lock(self.raw):
            with self.assertRaises(CheckpointError):
                ingestion.prepare_checkpoint(self.config)
        self.assertEqual(ingestion.prepare_checkpoint(self.config)["last_batch"], 0)


if __name__ == "__main__":
    unittest.main()
