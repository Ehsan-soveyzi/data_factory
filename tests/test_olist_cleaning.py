"""Boundary, provenance and repeatability checks for pre-database preparation."""

from copy import deepcopy
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from data_factory.exceptions import DataError
from data_factory.cli import main
from data_factory.olist_cleaning import prepare_olist, run_olist_cleaning
from data_factory.quality import _digest


PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    contract = json.loads((PROJECT / "config/olist_schema.json").read_text(encoding="utf-8"))
    frames = {}
    ids = {"order_id": "1" * 32, "customer_id": "2" * 32, "customer_unique_id": "3" * 32,
           "product_id": "4" * 32, "seller_id": "5" * 32, "review_id": "6" * 32}
    for name, definition in contract["tables"].items():
        row = {}
        for column in definition["columns"]:
            field, kind = column["source"], column["kind"]
            if field in ids:
                value = ids[field]
            elif kind == "postal":
                value = "123"
            elif kind in ("timestamp", "date"):
                value = "2018-01-02 00:00:00"
            elif kind in ("integer", "money"):
                value = "1" if kind == "integer" else "10.00"
            elif kind == "real":
                value = "-23.50" if field.endswith("lat") else "-46.50"
            elif field.endswith("state"):
                value = "SP"
            elif field == "product_category_name":
                value = "known"
            else:
                value = "value"
            row[field] = value
        frames[name] = pd.DataFrame([row], dtype="string")
    frames["orders"].loc[0, ["order_status", "order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date", "order_delivered_customer_date"]] = [
        "delivered", "2018-01-01 10:00:00", "2018-01-03 10:00:00", "2018-01-02 12:00:00", "2018-01-02 09:00:00",
    ]
    frames["products"].loc[0, "product_category_name"] = "untranslated"
    frames["products"].loc[0, "product_weight_g"] = "0"
    frames["order_payments"].loc[0, "payment_installments"] = "0"
    frames["order_reviews"].loc[0, "review_comment_title"] = "  "
    frames["order_reviews"].loc[0, "review_comment_message"] = " keep wording "
    frames["sellers"].loc[0, "seller_zip_code_prefix"] = "99999"
    frames["order_items"].loc[0, "freight_value"] = "0.00"
    geo = frames["geolocation"]
    different = geo.copy()
    different.loc[0, "geolocation_lat"] = "-24.50"
    frames["geolocation"] = pd.concat([geo, geo, different], ignore_index=True)
    return frames, contract


class OlistCleaningTests(unittest.TestCase):
    def test_cli_dispatches_to_shared_cleaning_function(self):
        result = {"report": {"status": "passed", "deduplicated_rows": 1, "quarantined_rows": 0},
                  "output_dir": "data/curated/run", "manifest_path": "data/curated/execution.json"}
        with patch("sys.argv", ["data-factory", "clean-olist", "--project-root", "example"]), \
                patch("data_factory.olist_cleaning.run_olist_cleaning", return_value=result) as run, \
                redirect_stdout(StringIO()) as stream:
            self.assertEqual(main(), 0)
        run.assert_called_once_with("example")
        self.assertEqual(json.loads(stream.getvalue())["deduplicated_rows"], 1)

    def test_exact_duplicates_removed_but_distinct_same_prefix_points_survive(self):
        frames, contract = fixture()
        before = {name: value.copy(deep=True) for name, value in frames.items()}
        clean, audit = prepare_olist(frames, contract)
        self.assertEqual(len(clean["geolocation"]), 2)
        self.assertEqual(clean["geolocation"]["source_row"].tolist(), [1, 3])
        self.assertEqual(audit["deduplicated"][0]["source_row"], 2)
        self.assertEqual(audit["deduplicated"][0]["kept_source_row"], 1)
        for name in frames:
            pd.testing.assert_frame_equal(frames[name], before[name])

    def test_unknown_zeros_become_null_but_money_and_free_freight_survive(self):
        frames, contract = fixture()
        clean, _ = prepare_olist(frames, contract)
        self.assertTrue(pd.isna(clean["products"].iloc[0]["product_weight_g"]))
        self.assertTrue(pd.isna(clean["order_payments"].iloc[0]["payment_installments"]))
        self.assertEqual(clean["order_payments"].iloc[0]["payment_value"], Decimal("10.00"))
        self.assertEqual(clean["order_items"].iloc[0]["freight_value"], Decimal("0.00"))
        self.assertIn("UNKNOWN_ZERO:payment_installments", clean["order_payments"].iloc[0]["quality_flags"])

    def test_whitespace_only_comment_becomes_null_and_other_wording_is_preserved(self):
        frames, contract = fixture()
        clean, audit = prepare_olist(frames, contract)
        self.assertTrue(pd.isna(clean["order_reviews"].iloc[0]["review_comment_title"]))
        self.assertEqual(clean["order_reviews"].iloc[0]["review_comment_message"], " keep wording ")
        self.assertTrue(any(row["column"] == "review_comment_title" and row["whitespace_only_to_null_rows"] == 1 for row in audit["column_changes"]))

    def test_category_completion_and_missing_geography_do_not_drop_entities(self):
        frames, contract = fixture()
        clean, audit = prepare_olist(frames, contract)
        self.assertEqual(set(clean["category_translation"]["product_category_name"]), {"known", "untranslated"})
        extra = clean["category_translation"].set_index("product_category_name").loc["untranslated"]
        self.assertTrue(pd.isna(extra["product_category_name_english"]))
        self.assertEqual(len(clean["products"]), 1)
        self.assertEqual(len(clean["sellers"]), 1)
        self.assertIn("NO_GEOLOCATION_MATCH", clean["sellers"].iloc[0]["quality_flags"])
        self.assertEqual(audit["post_checks"]["unmatched_foreign_key_rows"], 0)

    def test_reversed_carrier_dates_flag_only_the_affected_duration(self):
        frames, contract = fixture()
        clean, _ = prepare_olist(frames, contract)
        order = clean["orders"].iloc[0]
        self.assertFalse(order["approval_to_carrier_duration_valid"])
        self.assertFalse(order["carrier_to_customer_duration_valid"])
        self.assertTrue(order["delivery_delay_eligible"])
        self.assertIn("CUSTOMER_BEFORE_CARRIER", order["quality_flags"])

    def test_customer_before_purchase_is_not_eligible_for_delivery_metrics(self):
        frames, contract = fixture()
        frames["orders"].loc[0, "order_delivered_customer_date"] = "2017-12-31 00:00:00"
        clean, _ = prepare_olist(frames, contract)
        self.assertFalse(clean["orders"].iloc[0]["delivery_delay_eligible"])
        self.assertIn("CUSTOMER_BEFORE_PURCHASE", clean["orders"].iloc[0]["quality_flags"])

    def test_missing_parent_rejections_cascade_without_leaving_orphans(self):
        frames, contract = fixture()
        frames["customers"] = frames["customers"].iloc[:0].copy()
        clean, audit = prepare_olist(frames, contract)
        for name in ("orders", "order_items", "order_payments", "order_reviews"):
            self.assertEqual(len(clean[name]), 0, name)
        self.assertEqual(len(audit["quarantine"]), 4)
        self.assertEqual(audit["quarantine"][0]["source_row"], 1)
        self.assertIn("MISSING_PARENT", audit["quarantine"][0]["reason_codes"])

    def test_conflicting_primary_keys_are_all_quarantined(self):
        frames, contract = fixture()
        frames["customers"] = pd.concat([frames["customers"], frames["customers"]], ignore_index=True)
        clean, audit = prepare_olist(frames, contract)
        rejected = [row for row in audit["quarantine"] if row["table"] == "customers"]
        self.assertEqual(len(clean["customers"]), 0)
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("DUPLICATE_PRIMARY_KEY" in row["reason_codes"] for row in rejected))

    def test_required_money_precision_error_retains_original_record_in_quarantine(self):
        frames, contract = fixture()
        frames["order_items"].loc[0, "price"] = "10.001"
        clean, audit = prepare_olist(frames, contract)
        rejected = next(row for row in audit["quarantine"] if row["table"] == "order_items")
        self.assertEqual(len(clean["order_items"]), 0)
        self.assertEqual(rejected["record"]["price"], "10.001")
        self.assertEqual(rejected["source_file"], "olist_order_items_dataset.csv")

    def test_invalid_optional_measurement_is_flagged_without_rejecting_product(self):
        frames, contract = fixture()
        frames["products"].loc[0, "product_weight_g"] = "-1"
        clean, _ = prepare_olist(frames, contract)
        self.assertEqual(len(clean["products"]), 1)
        self.assertTrue(pd.isna(clean["products"].iloc[0]["product_weight_g"]))
        self.assertIn("INVALID_OPTIONAL:product_weight_g", clean["products"].iloc[0]["quality_flags"])

    def test_publication_is_repeatable_with_separate_execution_manifests(self):
        frames, contract = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            raw = root / "data/raw"
            raw.mkdir(parents=True)
            (root / "config/olist_schema.json").write_text(json.dumps(contract), encoding="utf-8")
            settings = {"paths": {"olist_raw_dir": "data/raw", "curated_dir": "data/curated"}}
            (root / "config/database.example.json").write_text(json.dumps(settings), encoding="utf-8")
            for name, frame in frames.items():
                frame.to_csv(raw / contract["tables"][name]["file"], index=False)
            hashes = {path.name: _digest(path) for path in raw.glob("*.csv")}
            first = run_olist_cleaning(root)
            second = run_olist_cleaning(root)
            self.assertEqual(first["output_dir"], second["output_dir"])
            self.assertEqual(first["report"], second["report"])
            self.assertNotEqual(first["manifest_path"], second["manifest_path"])
            self.assertEqual(first["report"]["deduplicated_rows"], 1)
            for path in raw.glob("*.csv"):
                self.assertEqual(_digest(path), hashes[path.name])
            self.assertTrue(Path(first["output_dir"], "product_categories.csv").is_file())


if __name__ == "__main__":
    unittest.main()
