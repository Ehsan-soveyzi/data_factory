"""CSV contracts on unrelated datasets, quarantine evidence and safe publication."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_factory.cli import main
from data_factory.csv_contract import load_contract, normalize_row, validate_contract
from data_factory.csv_quality import validate_csv
from data_factory.exceptions import ConfigurationError, DataError, QuarantineThresholdError, SchemaDriftError


PROJECT = Path(__file__).resolve().parents[1]


def contract():
    return {"version": 1, "dataset": "Unrelated local inventory", "primary_key": ["id"],
            "max_quarantine_fraction": 1, "fields": [
                {"source": "sku", "name": "id", "type": "string", "nullable": False, "pattern": "[0-9]+", "role": "identifier"},
                {"source": "count", "name": "count", "type": "integer", "nullable": False, "min": 0},
                {"source": "cost", "name": "cost", "type": "number", "nullable": True,
                 "min": 0, "warn_max": 100, "missing_tokens": ["", "NA"]},
            ]}


class CSVContractTests(unittest.TestCase):
    def test_nullable_fields_and_normalized_values_keep_warnings(self):
        schema = validate_contract(contract())
        raw = {"sku": " 001 ", "count": "2.0", "cost": " na "}
        clean, errors, flags = normalize_row(raw, schema)
        self.assertEqual(clean, {"id": "001", "count": 2, "cost": None})
        self.assertEqual(errors, [])
        self.assertEqual(flags, ["cost:MISSING_VALUE", "cost:TRIMMED", "id:TRIMMED"])
        self.assertEqual(raw["sku"], " 001 ")

    def test_every_error_is_retained_and_integer_conversion_is_exact(self):
        schema = validate_contract(contract())
        _, errors, _ = normalize_row({"sku": "bad", "count": "1.5", "cost": "inf"}, schema)
        self.assertEqual({e["code"] for e in errors}, {"INVALID_FORMAT", "INVALID_INTEGER", "INVALID_NUMBER"})
        clean, errors, _ = normalize_row({"sku": "1", "count": "9007199254740993", "cost": "1"}, schema)
        self.assertFalse(errors)
        self.assertEqual(clean["count"], 9007199254740993)

    def test_invalid_optional_value_can_be_null_but_required_values_cannot(self):
        schema = contract()
        schema["fields"][2]["on_invalid"] = "set_null"
        clean, errors, flags = normalize_row({"sku": "1", "count": "0", "cost": "bad"}, validate_contract(schema))
        self.assertFalse(errors)
        self.assertIsNone(clean["cost"])
        self.assertIn("cost:INVALID_NUMBER_SET_NULL", flags)
        schema["fields"][0]["on_invalid"] = "set_null"
        with self.assertRaises(ConfigurationError):
            validate_contract(schema)

    def test_supported_scalar_types_have_real_validation(self):
        for kind, text, expected, bad in [
            ("boolean", "TRUE", True, "yes"),
            ("date", "2024-02-29", "2024-02-29", "2023-02-29"),
            ("datetime", "2024-02-29T01:02:03", "2024-02-29T01:02:03", "2024-02-29"),
        ]:
            with self.subTest(kind=kind):
                schema = validate_contract({"version": 1, "dataset": "other", "primary_key": [],
                    "fields": [{"source": "v", "name": "v", "type": kind, "nullable": False}]})
                clean, errors, _ = normalize_row({"v": text}, schema)
                self.assertEqual(clean["v"], expected)
                self.assertFalse(errors)
                self.assertTrue(normalize_row({"v": bad}, schema)[1])

    def test_contract_typos_and_inconsistent_rules_fail_before_execution(self):
        mutations = [
            lambda s: s.update(unrecognised=True),
            lambda s: s.update(version=True),
            lambda s: s.update(max_quarantine_fraction=float("nan")),
            lambda s: s.update(primary_key=["absent"]),
            lambda s: s.update(primary_key=["id", "id"]),
            lambda s: s.update(extra_columns="silently"),
            lambda s: s["fields"][0].update(nullable=True),
            lambda s: s["fields"][1].update(type="guess"),
            lambda s: s["fields"][1].update(min=10, max=1),
            lambda s: s["fields"][1].update(allowed=[True]),
            lambda s: s["fields"][1].update(warn_values=["96"]),
            lambda s: s["fields"][1].update(required_presence=False),
            lambda s: s["fields"][0].update(pattern="["),
            lambda s: s["fields"][0].update(name="quality_flags"),
            lambda s: s["fields"][1].update(source="sku"),
            lambda s: s["fields"][2].update(missing_tokens=["", "NA", "na"]),
        ]
        for mutate in mutations:
            schema = contract()
            mutate(schema)
            with self.subTest(schema=schema), self.assertRaises(ConfigurationError):
                validate_contract(schema)

    def test_actual_credit_contract_retains_missing_values_and_warns_on_unusual_counts(self):
        schema = load_contract(PROJECT / "config/credit_schema.json")
        raw = {f["source"]: "0" for f in schema["fields"]}
        raw.update({"": "12", "age": "45", "MonthlyIncome": "NA", "NumberOfDependents": "",
                    "RevolvingUtilizationOfUnsecuredLines": "2", "NumberOfTimes90DaysLate": "98"})
        clean, errors, flags = normalize_row(raw, schema)
        self.assertFalse(errors)
        self.assertIsNone(clean["MonthlyIncome"])
        self.assertIsNone(clean["NumberOfDependents"])
        self.assertEqual(clean["NumberOfTimes90DaysLate"], 98)
        self.assertIn("NumberOfTimes90DaysLate:UNUSUAL_VALUE", flags)
        self.assertIn("RevolvingUtilizationOfUnsecuredLines:UNUSUAL_VALUE", flags)
        raw["age"] = "0"
        self.assertIn({"field": "age", "code": "OUT_OF_RANGE"}, normalize_row(raw, schema)[1])
        raw["age"] = "45"
        raw["SeriousDlqin2yrs"] = "2"
        self.assertIn({"field": "SeriousDlqin2yrs", "code": "INVALID_CATEGORY"}, normalize_row(raw, schema)[1])
        raw["SeriousDlqin2yrs"] = ""
        self.assertIn({"field": "SeriousDlqin2yrs", "code": "MISSING_REQUIRED_VALUE"}, normalize_row(raw, schema)[1])


class CSVQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw.csv"
        self.schema_path = self.root / "contract.json"
        self.config_path = self.root / "config.json"
        self.output = self.root / "curated"
        self.schema = contract()
        self.config = {"source": {"path": "raw.csv"}, "contract": "contract.json", "output_dir": "curated"}
        self.save()
        self.rows([['001', '2', ''], ['002', '0', '102']])
        network = patch("socket.socket.connect", side_effect=AssertionError("CSV validation must be offline"))
        network.start()
        self.addCleanup(network.stop)

    def save(self):
        self.schema_path.write_text(json.dumps(self.schema), encoding="utf-8")
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def rows(self, rows, header=("sku", "count", "cost"), delimiter=","):
        with self.raw.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file, delimiter=delimiter)
            writer.writerow(header)
            writer.writerows(rows)

    def run_csv(self):
        return validate_csv(self.config_path, progress=None)

    def lines(self, result, name):
        return [json.loads(line) for line in (Path(result["output_dir"]) / name).read_text(encoding="utf-8").splitlines()]

    def test_unrelated_csv_is_processed_with_no_nyc_settings_and_raw_is_immutable(self):
        original = self.raw.read_bytes()
        result = self.run_csv()
        accepted = self.lines(result, "accepted.jsonl")
        self.assertEqual((result["raw_records"], result["accepted_records"], result["quarantined_records"]), (2, 2, 0))
        self.assertEqual(accepted[0]["id"], "001")
        self.assertIsNone(accepted[0]["cost"])
        self.assertEqual(accepted[1]["count"], 0)
        self.assertEqual(accepted[1]["source_row"], 2)
        self.assertEqual(accepted[1]["quality_flags"], ["cost:UNUSUAL_VALUE"])
        self.assertEqual(result["warning_counts"], {"cost:MISSING_VALUE": 1, "cost:UNUSUAL_VALUE": 1})
        self.assertEqual(result["feature_columns"], ["count", "cost"])
        self.assertEqual(original, self.raw.read_bytes())
        directory = Path(result["output_dir"])
        for name, meta in result["files"].items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), meta["sha256"])
        profile = json.loads((directory / "quality_profile.json").read_text())
        self.assertEqual(profile["columns"]["cost"]["missing_count"], 1)
        self.assertEqual(profile["columns"]["count"]["ranges_by_type"]["number"], {"min": 0, "max": 2})
        with (directory / "accepted.csv").open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            self.assertEqual(reader.fieldnames, [f["name"] for f in self.schema["fields"]])
            csv_rows = list(reader)
        self.assertEqual(len(csv_rows), len(accepted))
        self.assertEqual(csv_rows[0]["cost"], "")
        self.assertEqual(csv_rows[1], {"id": "002", "count": "0", "cost": "102.0"})

    def test_invalid_rows_duplicate_records_and_conflicting_keys_keep_evidence(self):
        self.rows([['1', '-1', 'bad'], ['1', '1', '2'], ['1', '1', '2'], ['1', '2', '2'], ['2', '3'], ['3', '0', '4']])
        result = self.run_csv()
        self.assertEqual(result["accepted_records"], 2)
        rejected = self.lines(result, "quarantine.jsonl")
        self.assertEqual([r["source_row"] for r in rejected], [1, 3, 4, 5])
        self.assertEqual(len(rejected[0]["errors"]), 2)
        self.assertEqual(rejected[1]["reason_codes"], ["DUPLICATE_RECORD"])
        self.assertEqual(rejected[2]["reason_codes"], ["DUPLICATE_KEY_CONFLICT"])
        self.assertEqual(rejected[3]["record"]["values"], ['2', '3'])
        self.assertEqual(rejected[3]["source_line_end"], 6)

    def test_composite_keys_and_disabled_deduplication(self):
        self.rows([['1', '1', '2'], ['1', '2', '2'], ['1', '1', '2']])
        self.schema["primary_key"] = ["id", "count"]
        self.save()
        self.assertEqual(self.run_csv()["accepted_records"], 2)
        self.schema["primary_key"] = []
        self.save()
        self.assertEqual(self.run_csv()["accepted_records"], 3)

    def test_threshold_equality_passes_and_exceeding_it_publishes_failed_diagnostics(self):
        self.rows([['1', '0', ''], ['2', '-1', '2']])
        self.schema["max_quarantine_fraction"] = 0.5
        self.save()
        self.assertEqual(self.run_csv()["status"], "passed")
        self.schema["max_quarantine_fraction"] = 0.49
        self.save()
        with self.assertRaises(QuarantineThresholdError) as error:
            self.run_csv()
        report = json.loads(Path(error.exception.report_path).read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["quarantine_fraction"], 0.5)
        latest = json.loads((self.output / "latest.json").read_text())
        self.assertEqual(latest["status"], "failed")
        self.assertTrue((self.output / latest["run"] / "quarantine.jsonl").is_file())

    def test_schema_drift_duplicate_headers_and_empty_inputs_publish_nothing(self):
        for text in ('', 'sku,count,cost\n', 'sku,sku,cost\n1,2,3\n', 'sku,count\n1,2\n', 'sku,count,cost,new\n1,2,3,4\n'):
            with self.subTest(text=text):
                self.raw.write_text(text, encoding="utf-8")
                with self.assertRaises(DataError):
                    self.run_csv()
                self.assertFalse((self.output / "latest.json").exists())
                self.assertEqual(list(self.output.iterdir()), [])

    def test_optional_headers_and_explicit_extra_column_policy(self):
        self.schema["fields"][2]["required_presence"] = False
        self.schema["extra_columns"] = "ignore"
        self.save()
        self.rows([['1', '2', 'kept only in raw']], header=['sku', 'count', 'other'])
        result = self.run_csv()
        self.assertEqual(result["ignored_source_columns"], ['other'])
        self.assertIsNone(self.lines(result, "accepted.jsonl")[0]["cost"])

    def test_multiline_quoted_values_and_alternate_delimiter_have_correct_provenance(self):
        self.schema["fields"][0].pop("pattern")
        self.config["source"]["delimiter"] = ";"
        self.save()
        self.rows([['a;\nb', '2', '1'], ['next', '-1', '2']], delimiter=";")
        result = self.run_csv()
        self.assertEqual(self.lines(result, "accepted.jsonl")[0]["id"], 'a;\nb')
        rejected = self.lines(result, "quarantine.jsonl")[0]
        self.assertEqual(rejected["source_row"], 2)
        self.assertEqual(rejected["source_line_end"], 4)

    def test_malformed_csv_and_nonfinite_values_do_not_slip_through(self):
        self.raw.write_text('sku,count,cost\n"unterminated,2,3', encoding="utf-8")
        with self.assertRaisesRegex(DataError, "Malformed CSV"):
            self.run_csv()
        self.rows([['1', 'NaN', 'Infinity']])
        result = self.run_csv()
        self.assertEqual(result["accepted_records"], 0)
        self.assertEqual(result["quarantined_records"], 1)
        with (Path(result["output_dir"]) / "accepted.csv").open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            self.assertEqual(reader.fieldnames, [f["name"] for f in self.schema["fields"]])
            self.assertEqual(list(reader), [])

    def test_source_pin_is_checked_and_changes_during_processing_are_not_published(self):
        self.config["source"]["expected_sha256"] = "0" * 64
        self.save()
        with self.assertRaisesRegex(DataError, "checksum"):
            self.run_csv()
        self.assertFalse(self.output.exists())
        self.config["source"].pop("expected_sha256")
        self.save()
        def mutate(raw, schema):
            result = normalize_row(raw, schema)
            with self.raw.open("ab") as file:
                file.write(b"\n")
            return result
        with patch("data_factory.csv_quality.normalize_row", side_effect=mutate):
            with self.assertRaisesRegex(DataError, "changed during validation"):
                self.run_csv()
        self.assertFalse((self.output / "latest.json").exists())

    def test_repeated_execution_is_deterministic_and_refuses_tampered_artifacts(self):
        first = self.run_csv()
        directory = Path(first["output_dir"])
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        self.assertEqual(self.run_csv()["output_dir"], first["output_dir"])
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})
        (directory / "accepted.jsonl").write_text('tampered\n')
        with self.assertRaisesRegex(DataError, "refusing to overwrite"):
            self.run_csv()

    def test_config_paths_are_relative_to_config_not_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.output.parent.parent)
            self.assertEqual(self.run_csv()["accepted_records"], 2)
        finally:
            os.chdir(previous)

    def test_duplicate_json_keys_and_bad_configs_are_rejected(self):
        for text in ('{"version":1,"version":2}', '{"version":NaN}'):
            self.schema_path.write_text(text)
            with self.assertRaises(ConfigurationError):
                self.run_csv()
        for mutate in (
            lambda c: c.update(output_dir="."),
            lambda c: c["source"].update(delimiter="xx"),
            lambda c: c["source"].update(encoding="not-a-codec"),
            lambda c: c["source"].update(expected_sha256="invalid"),
            lambda c: c.update(unrecognised=True),
        ):
            self.config = {"source": {"path": "raw.csv"}, "contract": "contract.json", "output_dir": "curated"}
            mutate(self.config)
            self.save()
            with self.assertRaises(ConfigurationError):
                self.run_csv()

    def test_cli_reports_success_configuration_and_threshold_exit_codes(self):
        def call():
            out, error = StringIO(), StringIO()
            with patch("sys.argv", ['data-factory', 'validate-csv', '--config', str(self.config_path)]), redirect_stdout(out), redirect_stderr(error):
                code = main()
            return code, out.getvalue(), error.getvalue()
        code, out, _ = call()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["accepted_records"], 2)
        self.rows([['1', '-1', '2']])
        self.schema["max_quarantine_fraction"] = 0
        self.save()
        code, _, error = call()
        self.assertEqual(code, 4)
        self.assertIn("diagnostics:", error)
        self.schema_path.write_text('{"version":1,"version":1}')
        self.assertEqual(call()[0], 2)


if __name__ == "__main__":
    unittest.main()
