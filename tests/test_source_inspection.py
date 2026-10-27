"""Small offline fixtures for the module used by the stage 02 notebook."""

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from data_factory.database.source_inspection import (
    check_candidate_keys, check_dates, check_numeric, check_postal,
    check_relationships, save_evidence, sha256_file, summarize_sources,
)


class SourceInspectionTests(unittest.TestCase):
    def setUp(self):
        self.frames = {
            "orders": pd.DataFrame({"order_id": ["O1", "O2", "O3"], "customer_id": ["C1", "missing", None]}),
            "customers": pd.DataFrame({"customer_id": ["C1"], "customer_unique_id": ["U1"], "customer_zip_code_prefix": ["1234"]}),
            "order_items": pd.DataFrame({"order_id": ["O1", "orphan"], "order_item_id": [1, 1], "product_id": ["P1", "P1"], "seller_id": ["S1", "S1"]}),
            "order_payments": pd.DataFrame({"order_id": ["O1", "O1", "O1"], "payment_sequential": [1, 2, 2]}),
            "order_reviews": pd.DataFrame({"review_id": ["R1", "R1"], "order_id": ["O1", "O2"]}),
            "products": pd.DataFrame({"product_id": ["P1"], "product_category_name": ["untranslated"]}),
            "sellers": pd.DataFrame({"seller_id": ["S1"], "seller_zip_code_prefix": ["123"]}),
            "category_translation": pd.DataFrame({"product_category_name": ["known"]}),
            "geolocation": pd.DataFrame({"geolocation_zip_code_prefix": ["00123", "00123"]}),
        }

    def test_composite_key_keeps_distinct_orders_for_the_same_review(self):
        keys = check_candidate_keys(self.frames).set_index(["table", "key"])
        self.assertEqual(keys.loc[("order_reviews", "review_id"), "duplicate_extra_rows"], 1)
        self.assertTrue(keys.loc[("order_reviews", "review_id + order_id"), "valid_candidate"])
        self.assertFalse(keys.loc[("order_payments", "order_id + payment_sequential"), "valid_candidate"])

    def test_foreign_key_missing_and_unknown_values_are_separate(self):
        relations, categories = check_relationships(self.frames)
        orders = relations[relations["child"].eq("orders")].iloc[0]
        self.assertEqual(orders["missing_child_rows"], 1)
        self.assertEqual(orders["unmatched_rows"], 1)
        self.assertEqual(relations.iloc[1]["unmatched_rows"], 1)
        self.assertEqual(relations.iloc[-1]["unmatched_values"], ["untranslated"])
        self.assertEqual(categories, 2)

    def test_missing_profile_preserves_literal_na_and_distinguishes_blanks(self):
        frame = pd.DataFrame({"value": [None, "", "  ", "NA", "x"]})
        _, missing = summarize_sources({"fixture": frame})
        self.assertEqual(missing.iloc[0]["null_rows"], 1)
        self.assertEqual(missing.iloc[0]["blank_rows"], 2)
        self.assertEqual(missing.iloc[0]["effective_missing_rows"], 3)

    def test_money_precision_rejects_rounding_overflow_and_infinity(self):
        contract = {"tables": {"fixture": {"columns": [
            {"source": "amount", "kind": "money", "sql_type": "NUMERIC(12,2)", "minimum": 0},
        ]}}}
        values = pd.DataFrame({"amount": ["1.00", "1.001", "10000000000.00", "-1.00", None, "inf"]})
        result = check_numeric({"fixture": values}, contract).iloc[0]
        self.assertEqual(result["money_precision_errors"], 3)
        self.assertEqual(result["invalid_or_nonfinite_rows"], 1)
        self.assertEqual(result["below_rule_min_rows"], 1)

    def test_integer_rules_distinguish_zero_unknown_fraction_and_overflow(self):
        contract = {"tables": {"fixture": {"columns": [
            {"source": "count", "kind": "integer", "sql_type": "SMALLINT", "minimum": 1,
             "zero_policy": "null_with_quality_flag"},
        ]}}}
        values = pd.DataFrame({"count": ["0", "-1", "1.5", "32768", None, "2"]})
        result = check_numeric({"fixture": values}, contract).iloc[0]
        for field in ("zero_to_null_rows", "below_rule_min_rows", "fractional_integer_rows", "integer_storage_overflow_rows"):
            self.assertEqual(result[field], 1, field)

    def test_dates_reveal_invalid_calendar_values_and_reversed_events(self):
        columns = ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
                   "order_delivered_customer_date", "order_estimated_delivery_date"]
        contract = {"tables": {"orders": {"columns": [
            {"source": column, "kind": "date" if column == "order_estimated_delivery_date" else "timestamp"}
            for column in columns
        ]}}}
        orders = pd.DataFrame({
            "order_id": ["O1", "O2"], "order_status": ["delivered", "delivered"],
            "order_purchase_timestamp": ["2018-01-02 00:00:00", "bad"],
            "order_approved_at": ["2018-01-01 00:00:00", None],
            "order_delivered_carrier_date": ["2018-01-03 00:00:00", None],
            "order_delivered_customer_date": ["2018-01-02 00:00:00", None],
            "order_estimated_delivery_date": ["2018-01-04 12:00:00", "2018-02-30 00:00:00"],
        })
        dates, chronology, special = check_dates({"orders": orders, "order_payments": pd.DataFrame({"order_id": ["O1"]})}, contract)
        dates = dates.set_index("column")
        self.assertEqual(dates.loc["order_purchase_timestamp", "invalid_nonblank_rows"], 1)
        self.assertEqual(dates.loc["order_estimated_delivery_date", "invalid_nonblank_rows"], 1)
        self.assertEqual(dates.loc["order_estimated_delivery_date", "non_midnight_rows"], 1)
        self.assertEqual(chronology.iloc[0]["negative_duration_rows"], 1)
        self.assertEqual(special["delivered_without_actual_date"], 1)

    def test_postal_coverage_does_not_depend_on_point_key_uniqueness(self):
        postal, count = check_postal(self.frames)
        postal = postal.set_index("table")
        self.assertEqual(postal.loc["customers", "missing_geolocation_rows"], 1)
        self.assertEqual(postal.loc["sellers", "missing_geolocation_rows"], 0)
        self.assertEqual(count, 2)

    def test_changed_raw_file_prevents_evidence_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "orders.csv"
            source.write_text("id\nfirst\n", encoding="utf-8")
            inspection = {"raw_paths": {"orders": "orders.csv"},
                          "input_hashes": {"orders": sha256_file(source)}}
            source.write_text("id\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Raw CSV changed"):
                save_evidence(root, inspection)
            self.assertFalse((root / "stages").exists())

    def test_checks_do_not_mutate_input_dataframes(self):
        originals = {name: frame.copy(deep=True) for name, frame in self.frames.items()}
        summarize_sources(self.frames)
        check_candidate_keys(self.frames)
        check_relationships(self.frames)
        check_postal(self.frames)
        for name, frame in self.frames.items():
            pd.testing.assert_frame_equal(frame, originals[name])


if __name__ == "__main__":
    unittest.main()
