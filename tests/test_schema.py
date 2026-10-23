"""Contract configuration checks; do not execute dataset validation/quarantine."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from data_factory.exceptions import ConfigurationError, SchemaDriftError
from data_factory.schema import check_batch_schema, contract_rows, load_schema, parse_timestamp


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "schema.json"
        self.schema = json.loads((PROJECT_ROOT / "config/schema.json").read_text(encoding="utf-8"))
        self.source = {"complaint_types": ["Noise - Residential", "Noise - Park"]}

    def load(self, schema=None, *, source=None):
        self.path.write_text(json.dumps(self.schema if schema is None else schema), encoding="utf-8")
        return load_schema(self.path, source=source)

    def test_project_contract_preserves_missing_closure_and_identifier_semantics(self):
        schema = self.load(source=self.source)
        self.assertEqual(len(schema["fields"]), 17)
        self.assertEqual(len(schema["derived_fields"]), 4)
        self.assertEqual(schema["fields"]["unique_key"]["type"], "string")
        self.assertFalse(schema["fields"]["unique_key"]["nullable"])
        self.assertTrue(schema["fields"]["closed_date"]["nullable"])
        self.assertTrue(schema["derived_fields"]["closure_hours"]["nullable"])
        self.assertNotIn("UNKNOWN", schema["fields"]["open_data_channel_type"]["missing_tokens"])
        self.assertFalse(schema["fields"]["status"]["required_presence"])

    def test_scope_categories_are_resolved_from_settings_without_an_agency_filter(self):
        schema = self.load(source=self.source)
        rows = {row["field"]: row for row in contract_rows(schema, source=self.source)}
        self.assertEqual(rows["complaint_type"]["allowed"], self.source["complaint_types"])
        self.assertIsNone(rows["agency"]["allowed"])
        self.assertEqual(rows["incident_zip"]["pattern"], "^[0-9]{5}$")
        unresolved = {row["field"]: row for row in contract_rows(schema)}
        self.assertEqual(unresolved["complaint_type"]["allowed"], "source.complaint_types")

    def test_missing_or_empty_source_scope_binding_is_rejected(self):
        for source in [{}, {"complaint_types": []}, {"complaint_types": "Noise"}, {"complaint_types": [1]}]:
            with self.subTest(source=source):
                with self.assertRaises(ConfigurationError):
                    self.load(source=source)

    def test_duplicate_json_keys_are_not_silently_overwritten(self):
        self.path.write_text('{"version":1,"version":2}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "Duplicate JSON key"):
            load_schema(self.path)

    def test_nonfinite_json_constant_is_rejected(self):
        self.path.write_text('{"version":NaN}', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_schema(self.path)

    def test_wrong_version_or_top_level_shape_has_a_configuration_error(self):
        for version in [True, 2, "1", None]:
            with self.subTest(version=version):
                schema = deepcopy(self.schema)
                schema["version"] = version
                with self.assertRaises(ConfigurationError):
                    self.load(schema)
        with self.assertRaises(ConfigurationError):
            self.load([])

    def test_nonnullable_field_cannot_be_repaired_to_null(self):
        self.schema["fields"]["unique_key"]["on_invalid"] = "set_null"
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_nullability_and_presence_require_real_booleans(self):
        for field in ["nullable", "required_presence"]:
            with self.subTest(field=field):
                schema = deepcopy(self.schema)
                schema["fields"]["descriptor"][field] = "false"
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_source_types_must_be_convertible_to_the_declared_target(self):
        for value in ["string", [], ["object"], ["string", "string"]]:
            with self.subTest(value=value):
                schema = deepcopy(self.schema)
                schema["fields"]["latitude"]["source_types"] = value
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_invalid_numeric_bounds_are_rejected(self):
        for minimum, maximum in [(90, -90), (True, 90), ("-90", 90)]:
            with self.subTest(minimum=minimum, maximum=maximum):
                schema = deepcopy(self.schema)
                schema["fields"]["latitude"].update(min=minimum, max=maximum)
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_bad_patterns_and_contradictory_category_domains_are_rejected(self):
        schema = deepcopy(self.schema)
        schema["fields"]["incident_zip"]["pattern"] = "["
        with self.assertRaises(ConfigurationError):
            self.load(schema)
        for values in [["BRONX", "BRONX"], ["UNKNOWN"], []]:
            with self.subTest(values=values):
                schema = deepcopy(self.schema)
                schema["fields"]["borough"]["allowed"] = values
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_unknown_rule_keys_and_unexplained_policies_are_rejected(self):
        for key, value in [("nullabble", True), ("missing_policy", "")]:
            with self.subTest(key=key):
                schema = deepcopy(self.schema)
                schema["fields"]["descriptor"][key] = value
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_invalid_relations_are_rejected_before_record_processing(self):
        for relation, value in [
            ("primary_key", "not_a_field"),
            ("start_date_field", "agency"),
            ("closure_date_field", "created_date"),
            ("coordinate_fields", ["latitude", "latitude"]),
            ("coordinate_fields", ["latitude", "city"]),
        ]:
            with self.subTest(relation=relation):
                schema = deepcopy(self.schema)
                schema["relations"][relation] = value
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_derived_field_type_and_nullability_are_part_of_the_contract(self):
        for key, value in [("type", "string"), ("nullable", False)]:
            with self.subTest(key=key):
                schema = deepcopy(self.schema)
                schema["derived_fields"]["closure_hours"][key] = value
                with self.assertRaises(ConfigurationError):
                    self.load(schema)

    def test_missing_tokens_and_dataset_policies_must_be_unambiguous(self):
        for tokens in [["N/A"], ["", "UNKNOWN", "unknown"], ["", " N/A"]]:
            with self.subTest(tokens=tokens):
                schema = deepcopy(self.schema)
                schema["fields"]["descriptor"]["missing_tokens"] = tokens
                with self.assertRaises(ConfigurationError):
                    self.load(schema)
        del self.schema["dataset_rules"]["duplicates"]
        with self.assertRaises(ConfigurationError):
            self.load()


class TimestampContractTests(unittest.TestCase):
    def test_floating_timestamp_and_microsecond_precision(self):
        self.assertEqual(parse_timestamp("2024-02-29T00:00:00"), datetime(2024, 2, 29))
        self.assertEqual(parse_timestamp("2024-01-01T00:00:00.123456").microsecond, 123456)

    def test_date_only_timezone_and_invalid_calendar_dates_are_rejected(self):
        for value in ["2024-01-01", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00+03:00", "2024-02-30T00:00:00", "2024-01-01T00:00:00.1234567", None]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_timestamp(value)


class SourceStructureContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema(PROJECT_ROOT / "config/schema.json")
        self.record = {"unique_key": "1", "created_date": "2024-01-01T00:00:00", "agency": "NYPD", "complaint_type": "Noise - Residential"}

    def test_optional_keys_can_be_absent_in_a_whole_batch(self):
        check_batch_schema([self.record], self.schema, minimum_records=100)

    def test_disappeared_core_field_is_a_structural_error(self):
        del self.record["created_date"]
        with self.assertRaisesRegex(SchemaDriftError, "created_date"):
            check_batch_schema([self.record], self.schema, minimum_records=100)

    def test_systematic_type_change_differs_from_an_isolated_row_error(self):
        changed = {**self.record, "unique_key": 1}
        check_batch_schema([changed], self.schema, minimum_records=2)
        with self.assertRaisesRegex(SchemaDriftError, "unique_key"):
            check_batch_schema([changed, changed], self.schema, minimum_records=2)
        check_batch_schema([changed, self.record], self.schema, minimum_records=2)

    def test_invalid_structural_threshold_has_a_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            check_batch_schema([self.record], self.schema, minimum_records=0)


if __name__ == "__main__":
    unittest.main()
