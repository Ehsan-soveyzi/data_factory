"""Record-level contract execution: preserve good cases, explain every rejection."""

from copy import deepcopy
from pathlib import Path
import unittest

from data_factory.schema import load_schema
from data_factory.validation import normalize_record


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "config/schema.json"
SOURCE = {
    "start_date": "2024-01-01T00:00:00",
    "end_date_exclusive": "2025-07-01T00:00:00",
    "complaint_types": ["Noise - Residential"], "boroughs": [],
}


def case(key="123", **changes):
    return {
        "unique_key": key, "created_date": "2024-01-02T00:00:00",
        "agency": "NYPD", "complaint_type": "Noise - Residential", **changes,
    }


class RecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema(SCHEMA_PATH, source=SOURCE)

    def normalize(self, record, source=SOURCE):
        return normalize_record(record, self.schema, source)

    def test_projection_types_and_derived_fields_do_not_mutate_raw_record(self):
        raw = case(incident_zip="01234", latitude="40.7", longitude="-73.9",
                   closed_date="2024-01-02T01:30:00", status="Closed", extra="raw only")
        before = deepcopy(raw)
        clean, errors = self.normalize(raw)
        self.assertEqual(errors, [])
        self.assertEqual(set(clean), set(self.schema["fields"]) | set(self.schema["derived_fields"]))
        self.assertEqual(clean["incident_zip"], "01234")
        self.assertIsInstance(clean["latitude"], float)
        self.assertEqual(clean["created_date"], "2024-01-02T00:00:00.000000")
        self.assertEqual(clean["closure_hours"], 1.5)
        self.assertEqual(clean["created_month"], "2024-01")
        self.assertTrue(clean["has_recorded_closure"])
        self.assertEqual(raw, before)

    def test_missing_closure_stays_null_even_with_a_resolution_narrative(self):
        clean, errors = self.normalize(case(resolution_description="Action taken", status="Closed"))
        self.assertEqual(errors, [])
        self.assertIsNone(clean["closed_date"])
        self.assertIsNone(clean["closure_hours"])
        self.assertFalse(clean["has_recorded_closure"])
        self.assertIn("status:CLOSED_WITHOUT_CLOSED_DATE", clean["quality_flags"])

    def test_zero_and_long_positive_intervals_are_retained(self):
        for closed, hours in [("2024-01-02T00:00:00", 0), ("2026-01-02T00:00:00", 731 * 24)]:
            with self.subTest(closed=closed):
                clean, errors = self.normalize(case(closed_date=closed, status="Open"))
                self.assertEqual(errors, [])
                self.assertEqual(clean["closure_hours"], hours)
                self.assertIn("status:NONCLOSED_WITH_CLOSED_DATE", clean["quality_flags"])

    def test_missing_and_invalid_required_fields_are_all_reported(self):
        clean, errors = self.normalize(case(unique_key="bad", agency=None, complaint_type="other"))
        self.assertIsNone(clean)
        self.assertEqual({(e["field"], e["code"]) for e in errors}, {
            ("unique_key", "INVALID_FORMAT"), ("agency", "MISSING_REQUIRED_VALUE"),
            ("complaint_type", "OUTSIDE_SCOPE"),
        })

    def test_bad_calendar_date_timezone_and_date_only_are_quarantined(self):
        for value in ("2024-02-30T01:00:00", "2024-01-02", "2024-01-02T00:00:00Z", 123):
            with self.subTest(value=value):
                clean, errors = self.normalize(case(closed_date=value))
                self.assertIsNone(clean)
                self.assertEqual(errors[0]["field"], "closed_date")

    def test_negative_closure_is_rejected(self):
        clean, errors = self.normalize(case(closed_date="2024-01-01T23:59:59"))
        self.assertIsNone(clean)
        self.assertIn({"field": "closed_date", "code": "NEGATIVE_CLOSURE_INTERVAL"}, errors)

    def test_creation_scope_is_start_inclusive_end_exclusive(self):
        for value, accepted in [(SOURCE["start_date"], True),
                                ("2023-12-31T23:59:59", False),
                                (SOURCE["end_date_exclusive"], False)]:
            with self.subTest(value=value):
                clean, errors = self.normalize(case(created_date=value))
                self.assertEqual(clean is not None, accepted)
                if not accepted:
                    self.assertIn({"field": "created_date", "code": "OUTSIDE_SCOPE"}, errors)

    def test_optional_repairs_and_unknown_channel_are_visible(self):
        clean, errors = self.normalize(case(incident_zip=" N/A ", borough="invalid",
            descriptor=[], agency=" nypd ", open_data_channel_type=" unknown ",
            resolution_action_updated_date="yesterday"))
        self.assertEqual(errors, [])
        self.assertEqual(clean["agency"], "NYPD")
        self.assertEqual(clean["open_data_channel_type"], "UNKNOWN")
        for name in ("incident_zip", "borough", "descriptor", "resolution_action_updated_date"):
            self.assertIsNone(clean[name])
        self.assertIn("descriptor:SOURCE_TYPE_MISMATCH_SET_NULL", clean["quality_flags"])
        self.assertEqual(clean["quality_flags"], sorted(set(clean["quality_flags"])))

    def test_bad_coordinates_null_the_pair_without_rejecting_the_case(self):
        for value in ("NaN", "Infinity", "91", True, {}, None):
            with self.subTest(latitude=value):
                clean, errors = self.normalize(case(latitude=value, longitude="-74"))
                self.assertEqual(errors, [])
                self.assertIsNone(clean["latitude"])
                self.assertIsNone(clean["longitude"])
                self.assertIn("coordinates:INCOMPLETE_PAIR_SET_NULL", clean["quality_flags"])

    def test_borough_scope_is_hard_even_for_an_optional_field(self):
        source = {**SOURCE, "boroughs": ["QUEENS"]}
        for value, accepted in [(" queens ", True), (None, False), ("BROOKLYN", False)]:
            with self.subTest(borough=value):
                clean, errors = self.normalize(case(borough=value), source)
                self.assertEqual(clean is not None, accepted)

    def test_non_object_input_is_explained(self):
        for value in (None, [], "record", 1):
            with self.subTest(value=value):
                self.assertEqual(self.normalize(value), (None, [{"field": None, "code": "NOT_AN_OBJECT"}]))


if __name__ == "__main__":
    unittest.main()
