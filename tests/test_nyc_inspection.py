"""Saved-publication fixtures exercise the notebook's NYC inspection path."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from data_factory.database.nyc_inspection import (
    inspect_nyc_delivery, read_nyc_batches, resolve_nyc_delivery, save_nyc_evidence,
)
from data_factory.exceptions import DataError
from data_factory.quality import _digest


class NYCInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proposal = json.loads((Path(__file__).resolve().parents[1] / "config/nyc_schema.json").read_text(encoding="utf-8"))
        self.case = {c["source"]: {"string": "value", "datetime": "2024-01-01T00:00:00.000000",
                     "date": "2019-07-01", "float": 1.0, "boolean": True, "array": []}[c["kind"]]
                     for c in self.proposal["case_columns"]}
        self.case.update(unique_key="000123", incident_zip="00123", population_area="00123",
                         population_geography="MODZCTA", reference_borough="TEST", reference_neighborhood="Area",
                         reference_population=100.25, closed_date="2024-01-01T01:00:00.000000",
                         closure_hours=1.0, closure_days=1/24, created_month="2024-01",
                         population_join_status="matched", borough_comparison="match")
        self.area = dict(population_area="00123", zip_codes=["00123", "00124"],
                         population_geography="MODZCTA", population_reference_date="2019-07-01",
                         reference_population=100.25, reference_borough="TEST", reference_neighborhood="Area",
                         matched_case_count=1)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def publish(self, cases=None, area=None):
        cases = cases if cases is not None else [self.case]
        area = area if area is not None else self.area
        self.write("config/setting.json", {"paths": {"curated_dir": "data/curated_data", "delivery_dir": "delivery", "population_dir": "population_join"}})
        self.write("config/nyc_schema.json", self.proposal)
        run = "a" * 64
        pop = self.write("data/curated_data/population_join/pop/population_areas.jsonl", area)
        pop_report = self.write("data/curated_data/population_join/pop/population_join_report.json", {
            "status": "passed", "files": {"population_areas.jsonl": {"sha256": _digest(pop)}}})
        self.write("data/curated_data/population_join/latest.json", {"report": "pop/population_join_report.json"})
        report = self.write(f"data/curated_data/delivery/{run}/delivery_report.json", {
            "status": "passed", "analytical_records": len(cases), "delivery_complete": False,
            "join_report_sha256": _digest(pop_report), "scope": {"start": "2024-01-01"}, "scope_note": "partial saved snapshot"})
        case_path = self.root / f"data/curated_data/delivery/{run}/analytical_cases.jsonl"
        case_path.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
        self.write("data/curated_data/delivery/executions/test/run_manifest.json", {
            "status": "passed", "artifact_run": run, "analytical_records": len(cases), "files": {
                "delivery_report.json": {"sha256": _digest(report)},
                "analytical_cases.jsonl": {"sha256": _digest(case_path), "bytes": case_path.stat().st_size,
                                            "path": f"{run}/analytical_cases.jsonl"}}})
        self.write("data/curated_data/delivery/latest.json", {"status": "passed", "run": run,
                   "report": f"{run}/delivery_report.json", "manifest": "executions/test/run_manifest.json"})
        return resolve_nyc_delivery(self.root)

    def test_jsonl_preview_preserves_zeros_and_complete_inspection_is_separate(self):
        second = {**self.case, "unique_key": "000124", "incident_zip": "00124"}
        area = {**self.area, "matched_case_count": 2}
        source = self.publish([self.case, second], area)
        with read_nyc_batches(source, 1) as reader:
            preview = next(reader)
        self.assertEqual(preview.iloc[0]["unique_key"], "000123")
        self.assertEqual(preview.iloc[0]["incident_zip"], "00123")
        profile = inspect_nyc_delivery(self.root, source, chunksize=1)
        self.assertEqual((profile["status"], profile["rows"]), ("passed", 2))
        self.assertFalse(profile["delivery_complete"])
        self.assertEqual(profile["alias_rows"], 2)
        self.assertTrue(save_nyc_evidence(self.root, profile).is_file())

    def test_duplicate_key_crosses_batch_boundary(self):
        source = self.publish([self.case, deepcopy(self.case)], {**self.area, "matched_case_count": 2})
        profile = inspect_nyc_delivery(self.root, source, chunksize=1)
        self.assertEqual(profile["status"], "failed")
        self.assertEqual(profile["duplicate_primary_key_extra_rows"], 1)

    def test_reference_and_duration_mismatches_are_visible(self):
        bad = {**self.case, "closure_hours": 2.0, "reference_population": 200.0}
        profile = inspect_nyc_delivery(self.root, self.publish([bad]))
        self.assertEqual(profile["status"], "failed")
        self.assertEqual(profile["consistency_checks"]["closure_hours_mismatch_rows"], 1)
        self.assertEqual(profile["consistency_checks"]["population_value_mismatch_rows"], 1)

    def test_unmatched_case_is_preserved_with_nullable_reference(self):
        case = {**self.case, "population_area": None, "population_join_status": "unmatched_zip",
                "population_rate_eligible": False, "incident_zip": "99999"}
        for field in ("population_geography", "population_reference_date", "reference_borough", "reference_neighborhood", "reference_population"):
            case[field] = None
        profile = inspect_nyc_delivery(self.root, self.publish([case], {**self.area, "matched_case_count": 0}))
        self.assertEqual((profile["status"], profile["rows"]), ("passed", 1))

    def test_changed_source_is_rejected_before_evidence_publication(self):
        source = self.publish()
        profile = inspect_nyc_delivery(self.root, source)
        source["path"].write_text("{}\n", encoding="utf-8")
        with self.assertRaises(DataError):
            save_nyc_evidence(self.root, profile)
        with self.assertRaises(DataError):
            resolve_nyc_delivery(self.root)

    def test_manifest_cannot_escape_publication_directory(self):
        self.publish()
        pointer_path = self.root / "data/curated_data/delivery/latest.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["manifest"] = "../../outside.json"
        pointer_path.write_text(json.dumps(pointer))
        with self.assertRaises(DataError):
            resolve_nyc_delivery(self.root)


if __name__ == "__main__":
    unittest.main()
