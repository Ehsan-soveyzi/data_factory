"""Offline evidence for population reference validation and join cardinality."""

import base64
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
from data_factory.exceptions import ConfigurationError, DataError
from data_factory.population import enrich_case, join_population, parse_reference, prepare_population_reference
from data_factory.profiling import canonical_json
from test_ingestion import Response, Session


HEADER = "MODIFIED_ZCTA,NEIGHBORHOOD_NAME,BOROUGH_GROUP,label,POP_DENOMINATOR\n"
CSV = HEADER + '10001,Example,Manhattan,"10001, 10118",100.25\n11211,Example,Brooklyn,"11211, 11249",200\n10002,Example,Manhattan,10002,0\n'
PROJECT = Path(__file__).resolve().parents[1]


class PopulationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((PROJECT / "config/setting.json").read_text())
        self.config["paths"]["created_dir"] = str(self.root / "created")
        self.config["paths"]["reference_dir"] = str(self.root / "reference")
        self.settings = self.config["population"]
        self.settings["expected_sha256"] = hashlib.sha256(CSV.encode()).hexdigest()
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network access"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def reference(self):
        return prepare_population_reference(self.config, csv_text=CSV, acquired_via="offline fixture")

    def validated_input(self, records, *, status="passed"):
        run = "a" * 64
        root = Path(self.config["paths"]["created_dir"]) / "validation"
        directory = root / run
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "accepted.jsonl").write_text("".join(canonical_json(r) + "\n" for r in records), encoding="utf-8")
        (directory / "quarantine.jsonl").write_text("", encoding="utf-8")
        (directory / "quality_profile.json").write_text("{}\n", encoding="utf-8")
        report = {"status": status, "accepted_records": len(records), "snapshot_completed": False,
            "scope": {"fixture": True}, "scope_note": "Partial acquisition",
            "files": {name: {"sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
                      for name in ("accepted.jsonl", "quarantine.jsonl", "quality_profile.json")}}
        (directory / "validation_report.json").write_text(json.dumps(report), encoding="utf-8")
        (root / "latest.json").write_text(json.dumps({"run": run, "report": f"{run}/validation_report.json", "status": status}), encoding="utf-8")
        return directory

    def test_reference_aliases_share_one_area_population_without_double_counting(self):
        ref = self.reference()
        self.assertEqual(len(ref["areas"]), 3)
        self.assertEqual(len(ref["aliases"]), 5)
        self.assertEqual(ref["aliases"]["11249"], "11211")
        self.assertEqual(sum(r["reference_population"] for r in ref["areas"].values()), 300.25)

    def test_invalid_populations_columns_boroughs_and_aliases_fail(self):
        samples = [CSV.replace("100.25", x) for x in ("-1", "nan", "inf", "missing")]
        samples += [CSV.replace("POP_DENOMINATOR", "deleted"), CSV.replace("Manhattan", "unknown"),
                    CSV.replace('"11211, 11249"', '"11211, 10118"'),
                    CSV.replace('"11211, 11249"', '"11211, 11211"'),
                    CSV.replace('"11211, 11249"', '"11211, invalid"'),
                    CSV + "10001,Other,Manhattan,10001,100\n", HEADER,
                    CSV.replace('"11211, 11249"', '"11249"'),
                    CSV.replace("10002,Example,Manhattan,10002,0", "10002,Example,Manhattan,10002"),
                    CSV.replace("POP_DENOMINATOR\n", "POP_DENOMINATOR,label\n")]
        for value in samples:
            with self.subTest(source=value):
                with self.assertRaises(DataError):
                    parse_reference(value, self.settings)

    def test_reference_size_and_row_limits_are_enforced(self):
        for key, value in [("max_reference_bytes", 10), ("max_reference_rows", 2)]:
            settings = {**self.settings, key: value}
            with self.subTest(key=key), self.assertRaises(DataError):
                parse_reference(CSV, settings)

    def test_leading_zero_identifiers_are_preserved(self):
        text = CSV.replace("10001", "01234").replace("10118", "01235")
        areas, aliases = parse_reference(text, self.settings)
        self.assertEqual(aliases["01235"], "01234")
        self.assertEqual(areas["01234"]["population_area"], "01234")

    def test_github_http_download_verifies_checksum_and_cache_avoids_second_request(self):
        payload = {"encoding": "base64", "size": len(CSV.encode()),
                   "content": base64.encodebytes(CSV.encode()).decode()}
        session = Session(Response(payload))
        first = prepare_population_reference(self.config, session=session)
        second = prepare_population_reference(self.config, session=Session())
        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["params"], {"ref": self.settings["commit"]})
        self.assertIn("retrieved_at_utc", first["provenance"])

    def test_corrupted_or_wrong_downloads_never_create_a_cache(self):
        payloads = [[], {"encoding": "base64", "size": len(CSV), "content": "!bad"},
                    {"encoding": "base64", "size": 1, "content": base64.b64encode(CSV.encode()).decode()},
                    {"encoding": "base64", "size": 5, "content": base64.b64encode(b"other").decode()}]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(DataError):
                prepare_population_reference(self.config, session=Session(Response(payload)))
        self.assertFalse(Path(self.config["paths"]["reference_dir"]).exists())
        with self.assertRaises(DataError):
            prepare_population_reference(self.config, csv_text=CSV + "wrong")

    def test_changed_cached_source_is_rejected_without_network_or_overwrite(self):
        ref = self.reference()
        source = Path(ref["cache_dir"]) / "source.csv"
        source.write_text("changed", encoding="utf-8")
        with self.assertRaises(DataError):
            prepare_population_reference(self.config)
        self.assertEqual(source.read_text(), "changed")

    def test_enrichment_preserves_case_and_distinguishes_missing_unmatched_and_mismatch(self):
        ref = self.reference()
        cases = [({"incident_zip": "10118", "borough": "MANHATTAN"}, "matched", "match", True),
                 ({"incident_zip": "10001", "borough": "QUEENS"}, "matched", "mismatch", False),
                 ({"incident_zip": "10001", "borough": None}, "matched", "unavailable", True),
                 ({"incident_zip": "99999", "borough": "QUEENS"}, "unmatched_zip", "unavailable", False),
                 ({"incident_zip": None, "borough": "QUEENS"}, "missing_zip", "unavailable", False),
                 ({"incident_zip": "10002", "borough": "MANHATTAN"}, "matched", "match", False)]
        for case, status, comparison, eligible in cases:
            before = deepcopy(case)
            clean = enrich_case(case, ref["areas"], ref["aliases"], self.settings)
            self.assertEqual(clean["population_join_status"], status)
            self.assertEqual(clean["borough_comparison"], comparison)
            self.assertEqual(clean["population_rate_eligible"], eligible)
            self.assertEqual(case, before)
            for key, value in case.items():
                self.assertEqual(clean[key], value)

    def test_numeric_zip_and_already_enriched_input_are_data_errors(self):
        ref = self.reference()
        for record in ({"incident_zip": 10001}, {"incident_zip": "10001", "population_area": "10001"}, []):
            with self.subTest(record=record), self.assertRaises(DataError):
                enrich_case(record, ref["areas"], ref["aliases"], self.settings)

    def test_join_keeps_all_rows_in_order_and_reports_diagnostics(self):
        self.reference()
        rows = [{"unique_key": str(i), "incident_zip": z, "borough": b, "quality_flags": []}
                for i, (z, b) in enumerate([("10001", "MANHATTAN"), ("10118", "QUEENS"),
                                           ("99999", "QUEENS"), (None, None), ("11249", None)])]
        source = self.validated_input(rows)
        original = (source / "accepted.jsonl").read_bytes()
        result = join_population(self.config, progress=None)
        target = Path(result["output_dir"])
        enriched = [json.loads(line) for line in (target / "enriched_cases.jsonl").read_text().splitlines()]
        self.assertEqual(result["input_records"], 5)
        self.assertEqual(result["output_records"], 5)
        self.assertEqual(result["join_status_counts"], {"matched": 3, "missing_zip": 1, "unmatched_zip": 1})
        self.assertEqual(result["borough_comparison_counts"], {"match": 1, "mismatch": 1, "unavailable": 3})
        self.assertEqual(result["rate_eligible_records"], 2)
        self.assertEqual(result["unmatched_zip_counts"], {"99999": 1})
        self.assertEqual([r["unique_key"] for r in enriched], [r["unique_key"] for r in rows])
        for raw, clean in zip(rows, enriched):
            for key in raw:
                self.assertEqual(raw[key], clean[key])
        self.assertFalse(result["snapshot_completed"])
        self.assertEqual(original, (source / "accepted.jsonl").read_bytes())
        areas = [json.loads(line) for line in (target / "population_areas.jsonl").read_text().splitlines()]
        self.assertEqual(sum(r["reference_population"] for r in areas), 300.25)
        self.assertEqual(len((target / "join_issues.jsonl").read_text().splitlines()), 4)
        for name, metadata in result["files"].items():
            self.assertEqual(metadata["sha256"], hashlib.sha256((target / name).read_bytes()).hexdigest())

    def test_failed_validation_stops_before_fetching_reference(self):
        self.validated_input([], status="failed")
        with self.assertRaisesRegex(DataError, "passed"):
            join_population(self.config, progress=None)
        self.assertFalse(Path(self.config["paths"]["reference_dir"]).exists())

    def test_missing_changed_or_miscounted_validated_input_does_not_publish(self):
        with self.assertRaisesRegex(DataError, "validate"):
            join_population(self.config, progress=None)
        self.reference()
        directory = self.validated_input([{"incident_zip": "10001", "borough": "MANHATTAN"}])
        (directory / "accepted.jsonl").write_text("{}")
        with self.assertRaisesRegex(DataError, "checksum"):
            join_population(self.config, progress=None)
        directory = self.validated_input([])
        report = json.loads((directory / "validation_report.json").read_text())
        report["accepted_records"] = 1
        (directory / "validation_report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(DataError, "accounting"):
            join_population(self.config, progress=None)
        self.assertFalse((Path(self.config["paths"]["created_dir"]) / "population_join/latest.json").exists())

    def test_repeated_and_independent_runs_produce_identical_output_bytes(self):
        self.reference()
        rows = [{"incident_zip": "10001", "borough": "MANHATTAN"}, {"incident_zip": None, "borough": None}]
        self.validated_input(rows)
        first = join_population(self.config, progress=None)
        target = Path(first["output_dir"])
        contents = {p.name: p.read_bytes() for p in target.iterdir()}
        mtimes = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
        second = join_population(self.config, progress=None)
        self.assertEqual(first, second)
        self.assertEqual(mtimes, {p.name: p.stat().st_mtime_ns for p in target.iterdir()})
        self.config["paths"]["created_dir"] = str(self.root / "independent")
        self.validated_input(rows)
        other = join_population(self.config, progress=None)
        self.assertEqual(contents, {p.name: p.read_bytes() for p in Path(other["output_dir"]).iterdir()})

    def test_existing_output_corruption_is_not_overwritten(self):
        self.reference()
        self.validated_input([])
        first = join_population(self.config, progress=None)
        path = Path(first["output_dir"]) / "enriched_cases.jsonl"
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(DataError, "overwrite"):
            join_population(self.config, progress=None)
        self.assertEqual(path.read_text(), "changed")

    def test_interrupted_join_cleans_unpublished_files_and_can_be_rerun(self):
        self.reference()
        self.validated_input([{"incident_zip": "10001", "borough": "MANHATTAN"}])
        with patch("data_factory.population.enrich_case", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                join_population(self.config, progress=None)
        root = Path(self.config["paths"]["created_dir"]) / "population_join"
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(join_population(self.config, progress=None)["output_records"], 1)

    def test_invalid_validation_pointer_shape_and_traversal_are_rejected(self):
        directory = self.validated_input([])
        for value in ([], {"run": "../escape", "report": "../escape/validation_report.json"}):
            (directory.parent / "latest.json").write_text(json.dumps(value))
            with self.subTest(pointer=value), self.assertRaises(DataError):
                join_population(self.config, progress=None)
        self.assertFalse(Path(self.config["paths"]["reference_dir"]).exists())

    def test_bad_population_settings_fail_without_files(self):
        for field, value in [("expected_sha256", "bad"), ("commit", "master"), ("reference_date", "yesterday"),
                             ("max_reference_rows", False), ("columns", {}), ("geography", "ZIP")]:
            with self.subTest(field=field):
                config = deepcopy(self.config)
                config["population"][field] = value
                with self.assertRaises(ConfigurationError):
                    prepare_population_reference(config)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_population_stage_runs_offline_and_missing_validation_is_exit_four(self):
        self.reference()
        for ready, expected in [(False, 4), (True, 0)]:
            if ready:
                self.validated_input([])
            output, error = StringIO(), StringIO()
            with patch("sys.argv", ["data-factory", "run", "--stage", "population"]), \
                    patch("data_factory.cli.load_config", return_value=self.config), \
                    redirect_stdout(output), redirect_stderr(error):
                code = main()
            self.assertEqual(code, expected)
            if not expected:
                self.assertIn('"output_records": 0', output.getvalue())


if __name__ == "__main__":
    unittest.main()
