"""Offline checks for observational profiles; no validation policy is applied."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from data_factory.exceptions import ConfigurationError, DataError
from data_factory.profiling import ColumnProfiler
from data_factory.quality import inspect_raw_data


class ProfilerTests(unittest.TestCase):
    def test_missing_kinds_and_distinct_values_across_buffer_flushes(self):
        with tempfile.TemporaryDirectory() as folder:
            connection = sqlite3.connect(Path(folder) / "profile.sqlite")
            try:
                profiler = ColumnProfiler(connection, buffer_size=1, sentinel_tokens=["UNKNOWN"])
                rows = [{"a": "x"}, {"a": "x"}, {"a": None}, {"a": " "}, {}, {"a": " UNKNOWN "}]
                before = deepcopy(rows)
                for row in rows:
                    profiler.add(row)
                stats = profiler.result()["columns"]["a"]
                self.assertEqual(stats["absent_key_count"], 1)
                self.assertEqual(stats["null_count"], 1)
                self.assertEqual(stats["blank_count"], 1)
                self.assertEqual(stats["missing_count"], 3)
                self.assertEqual(stats["unique_non_missing"], 2)
                self.assertEqual(stats["sentinel_counts"], {"unknown": 1})
                self.assertEqual(rows, before)
            finally:
                connection.close()

    def test_ranges_preserve_raw_types_and_empty_profile_is_defined(self):
        with tempfile.TemporaryDirectory() as folder:
            connection = sqlite3.connect(Path(folder) / "profile.sqlite")
            try:
                profiler = ColumnProfiler(connection, fields=["empty"])
                self.assertIsNone(profiler.result()["columns"]["empty"]["missing_percent"])
                profiler.add({"s": "9", "n": 9})
                profiler.add({"s": "10", "n": 10})
                result = profiler.result()["columns"]
                self.assertEqual(result["s"]["ranges_by_type"]["string"], {"min": "10", "max": "9"})
                self.assertEqual(result["n"]["ranges_by_type"]["number"], {"min": 9, "max": 10})
                self.assertEqual(result["empty"]["missing_count"], 2)
            finally:
                connection.close()


class InspectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.config = {
            "validation": {
                "inspection_batches": 6, "profile_buffer_size": 2,
                "inspection_category_limit": 2,
                "inspection_sentinel_tokens": ["Unspecified", "UNKNOWN"],
                "inspection_categorical_fields": ["status", "open_data_channel_type"],
                # Inspection must not open this draft contract.
                "schema_path": str(self.root / "does-not-exist.json"),
            },
            "paths": {"raw_dir": str(self.raw), "curated_dir": str(self.root / "curated"), "inspection_file": "raw_inspection.json"},
        }
        self.state = {"scope_fingerprint": "test", "completed": False, "downloaded_records": 0, "batches": []}
        prepare = patch("data_factory.quality.prepare_checkpoint", return_value=self.state)
        self.prepare = prepare.start()
        self.addCleanup(prepare.stop)

    def batch(self, rows):
        name = f"batch_{len(self.state['batches']) + 1:06d}.json"
        payload = json.dumps(rows).encode("utf-8")
        (self.raw / name).write_bytes(payload)
        self.state["batches"].append({"file": name, "records": len(rows), "sha256": hashlib.sha256(payload).hexdigest()})
        self.state["downloaded_records"] += len(rows)

    def test_sample_covers_first_last_and_is_explicitly_partial(self):
        for index in range(10):
            self.batch([{"unique_key": str(index)}])
        report = inspect_raw_data(self.config, progress=None)
        self.assertEqual(len(report["selected_files"]), 6)
        self.assertEqual(report["selected_files"][0], "batch_000001.json")
        self.assertEqual(report["selected_files"][-1], "batch_000010.json")
        self.assertEqual(report["snapshot_records"], 10)
        self.assertEqual(report["selected_records"], 6)
        self.assertFalse(report["snapshot_completed"])
        self.assertEqual(report["profile"]["rows"], 6)

    def test_dates_sentinels_and_bad_records_are_observed_without_rejection(self):
        self.batch([
            {"created_date": "2024-01-02T00:00:00", "closed_date": "2024-01-01T00:00:00", "status": "Unspecified", "open_data_channel_type": "UNKNOWN"},
            {"created_date": "invalid", "closed_date": None, "status": "Closed"},
            None,
        ])
        original = (self.raw / "batch_000001.json").read_bytes()
        report = inspect_raw_data(self.config, progress=None)
        self.assertEqual(report["non_object_records"], 1)
        self.assertEqual(report["selected_records"], 3)
        self.assertEqual(report["profile"]["rows"], 2)
        self.assertEqual(report["date_observations"]["negative_closure_intervals"], 1)
        self.assertEqual(report["date_observations"]["created_date_unparseable"], 1)
        self.assertEqual(report["date_observations"]["closed_date_missing"], 1)
        channel = report["profile"]["columns"]["open_data_channel_type"]
        self.assertEqual(channel["missing_count"], 1)
        self.assertEqual(channel["sentinel_counts"], {"unknown": 1})
        self.assertEqual((self.raw / "batch_000001.json").read_bytes(), original)
        self.assertEqual(list((self.root / "curated").iterdir()), [self.root / "curated" / "raw_inspection.json"])

    def test_category_tracking_is_bounded_and_overflow_is_reported(self):
        self.batch([{"status": value} for value in ["A", "B", "C", "A", "C"]])
        report = inspect_raw_data(self.config, progress=None)
        categories = report["categorical_counts"]["status"]
        self.assertEqual(len(categories["values"]), 2)
        self.assertEqual(categories["other_count"], 2)
        self.assertEqual(sum(row["count"] for row in categories["values"]) + categories["other_count"], 5)

    def test_repeat_produces_identical_report_and_temporary_db_is_removed(self):
        self.batch([{"status": "Closed"}, {"status": "Unspecified"}])
        first = inspect_raw_data(self.config, progress=None)
        path = self.root / "curated" / "raw_inspection.json"
        original = path.read_bytes()
        second = inspect_raw_data(self.config, progress=None)
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list((self.root / "curated").glob(".inspection-*")))

    def test_changed_raw_file_fails_before_report_is_published(self):
        self.batch([{"status": "Closed"}])
        (self.raw / "batch_000001.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(DataError):
            inspect_raw_data(self.config, progress=None)
        self.assertFalse((self.root / "curated" / "raw_inspection.json").exists())

    def test_empty_snapshot_and_invalid_settings_have_clear_errors(self):
        with self.assertRaises(DataError):
            inspect_raw_data(self.config, progress=None)
        self.config["validation"]["inspection_batches"] = 0
        self.prepare.reset_mock()
        with self.assertRaises(ConfigurationError):
            inspect_raw_data(self.config, progress=None)
        self.prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
