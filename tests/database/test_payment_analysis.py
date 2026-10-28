"""Q5: split payments, mixed plans, exclusive order profiles and exact values."""

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.engine import make_url

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.payment_analysis import QUERY_FILE, verify_payment_results


ROOT = Path(__file__).resolve().parents[2]
QUERY = text((ROOT / "sql/queries" / QUERY_FILE).read_text(encoding="utf-8"))
TEST_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Use an isolated df_ddl_test_* PostgreSQL database")
class PostgreSQLPaymentAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("Q5 fixtures require an isolated df_ddl_test_* database.")
        cls.engine = create_database_engine(DatabaseSettings(url=url))
        with cls.engine.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != url.database:
                cls.engine.dispose()
                raise RuntimeError("Actual database differs from fixture target.")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.transaction = self.conn.begin()
        self.addCleanup(self.conn.close)
        self.addCleanup(self.transaction.rollback)
        self.conn.exec_driver_sql("""
            -- Only the verified isolated fixture database is modified.
            DROP SCHEMA IF EXISTS olist CASCADE;
            CREATE SCHEMA olist;
            CREATE TABLE olist.orders (
                order_id TEXT PRIMARY KEY, order_status TEXT NOT NULL,
                order_purchase_timestamp TIMESTAMP NOT NULL
            );
            CREATE TABLE olist.order_items (
                order_id TEXT, order_item_id INTEGER, price NUMERIC(12,2),
                freight_value NUMERIC(12,2), PRIMARY KEY(order_id, order_item_id)
            );
            CREATE TABLE olist.order_payments (
                order_id TEXT, payment_sequential INTEGER, payment_type TEXT NOT NULL,
                payment_installments SMALLINT, payment_value NUMERIC(12,2),
                PRIMARY KEY(order_id, payment_sequential)
            );
        """)
        orders = [{"id": f"o{i}", "status": "delivered", "purchased": datetime(2017, 1, i)} for i in range(1,9)]
        orders.extend([{"id": "canceled", "status": "canceled", "purchased": datetime(2017, 1, 1)},
                       {"id": "boundary", "status": "delivered", "purchased": datetime(2017, 2, 1)}])
        self.conn.execute(text("INSERT INTO olist.orders VALUES (:id, :status, :purchased)"), orders)
        self.conn.execute(text("INSERT INTO olist.order_items VALUES (:order, :item, :price, :freight)"), [
            {"order": o, "item": i, "price": Decimal(p), "freight": Decimal("10.00")}
            for o,i,p in [("o1",1,"30.00"),("o1",2,"70.00"),("o2",1,"200.00"),
                          ("o3",1,"50.00"),("o4",1,"80.00"),("o5",1,"120.00"),
                          ("o6",1,"40.00"),("o8",1,"60.00"),
                          ("canceled",1,"999.00"),("boundary",1,"999.00")]])
        payments = [("o1",1,"credit_card",3,"60.00"),("o1",2,"credit_card",3,"50.00"),
                    ("o2",1,"credit_card",3,"220.00"),
                    ("o3",1,"credit_card",1,"30.00"),("o3",2,"voucher",1,"25.00"),
                    ("o4",1,"credit_card",None,"88.00"),
                    ("o5",1,"credit_card",1,"60.00"),("o5",2,"credit_card",2,"72.00"),
                    ("o7",1,"boleto",1,"0.00"),
                    ("o8",1,"credit_card",1,"33.00"),("o8",2,"credit_card",None,"33.00"),
                    ("canceled",1,"credit_card",6,"999.00"),("boundary",1,"credit_card",6,"999.00")]
        self.conn.execute(text("INSERT INTO olist.order_payments VALUES (:order, :seq, :method, :plan, :value)"), [
            {"order": o, "seq": s, "method": m, "plan": p, "value": Decimal(v)} for o,s,m,p,v in payments])

    def params(self, start=date(2017, 1, 1), end=date(2017, 2, 1)):
        return {"start_date": start, "end_date": end, "order_status": "delivered"}

    def results(self, params=None):
        return [dict(row) for row in self.conn.execute(QUERY, params or self.params()).mappings()]

    def group(self, level, method, installments, kind=None):
        return next(r for r in self.results() if r["analysis_level"] == level
                    and r["payment_type"] == method and r["payment_installments"] == installments
                    and (kind is None or r["installment_kind"] == kind))

    def test_repeated_same_plan_rows_do_not_overweight_order_value(self):
        row = self.group("payment_plan", "credit_card", 3)
        self.assertEqual(row["order_count"], 2)
        self.assertEqual(row["payment_row_count"], 3)
        self.assertEqual(row["avg_order_value_brl"], Decimal("150.00"))
        self.assertEqual(row["merchandise_value_brl"], Decimal("300.00"))

    def test_recorded_amount_is_not_multiplied_by_installments_or_item_rows(self):
        row = self.group("payment_plan", "credit_card", 3)
        self.assertEqual(row["recorded_payment_value_brl"], Decimal("330.00"))
        self.assertEqual(row["avg_payment_row_value_brl"], Decimal("110.00"))
        self.assertNotEqual(row["recorded_payment_value_brl"], row["merchandise_value_brl"])

    def test_order_profiles_are_exclusive_and_mixed_methods_are_separate(self):
        rows = [r for r in self.results() if r["analysis_level"] == "order_profile"]
        self.assertEqual(sum(r["order_count"] for r in rows), 8)
        mixed = next(r for r in rows if r["payment_method_kind"] == "mixed_methods")
        self.assertIsNone(mixed["payment_type"])
        self.assertEqual(mixed["order_count"], 1)
        self.assertEqual(mixed["merchandise_value_brl"], Decimal("50.00"))

    def test_mixed_unknown_and_partly_unknown_plans_are_not_imputed(self):
        rows = [r for r in self.results() if r["analysis_level"] == "order_profile"]
        for kind in ["mixed_plans", "unknown", "partly_unknown"]:
            row = next(r for r in rows if r["installment_kind"] == kind)
            self.assertIsNone(row["payment_installments"])
            self.assertEqual(row["order_count"], 1)
        unknown = self.group("payment_plan", "credit_card", None)
        self.assertEqual(unknown["payment_row_count"], 2)

    def test_order_without_payment_retained_without_invented_paid_zero(self):
        row = next(r for r in self.results() if r["payment_method_kind"] == "no_payment")
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["merchandise_value_brl"], Decimal("40.00"))
        self.assertEqual(row["payment_row_count"], 0)
        self.assertIsNone(row["recorded_payment_value_brl"])
        self.assertIsNone(row["avg_payment_row_value_brl"])

    def test_missing_items_keep_unknown_value_while_zero_payment_is_valid(self):
        row = self.group("payment_plan", "boleto", 1)
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["valued_order_count"], 0)
        self.assertIsNone(row["avg_order_value_brl"])
        self.assertIsNone(row["merchandise_value_brl"])
        self.assertEqual(row["recorded_payment_value_brl"], Decimal("0.00"))

    def test_plan_memberships_overlap_but_recorded_payment_totals_do_not(self):
        rows = self.results()
        plans = [r for r in rows if r["analysis_level"] == "payment_plan"]
        profiles = [r for r in rows if r["analysis_level"] == "order_profile"]
        self.assertEqual(sum(r["order_count"] for r in plans), 10)
        self.assertEqual(sum(r["merchandise_value_brl"] or 0 for r in plans), Decimal("840.00"))
        self.assertEqual(sum(r["merchandise_value_brl"] or 0 for r in profiles), Decimal("650.00"))
        for subset in [plans, profiles]:
            self.assertEqual(sum(r["recorded_payment_value_brl"] or 0 for r in subset), Decimal("671.00"))

    def test_independent_control_matches_hand_counts_and_coverage(self):
        checks = verify_payment_results(self.conn, self.params(), self.results())
        self.assertTrue(checks["passed"])
        self.assertEqual(checks["scoped_order_count"], 8)
        self.assertEqual(checks["valued_order_count"], 7)
        self.assertEqual(checks["payment_row_count"], 11)
        self.assertEqual(checks["avg_order_value_brl"], Decimal("92.86"))
        self.assertEqual(checks["coverage"], {"orders_without_payment": 1, "orders_without_items": 1,
            "orders_with_multiple_payment_rows": 4, "orders_with_mixed_methods": 1,
            "orders_with_unknown_installments": 2, "unknown_installment_rows": 2})

    def test_scope_status_and_inclusive_exclusive_boundaries(self):
        params = self.params(start=date(2017, 2, 1), end=date(2017, 3, 1))
        rows = self.results(params)
        profiles = [r for r in rows if r["analysis_level"] == "order_profile"]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["order_count"], 1)
        self.assertEqual(profiles[0]["avg_order_value_brl"], Decimal("999.00"))

    def test_all_orders_without_payments_have_undefined_payment_distribution(self):
        self.conn.execute(text("DELETE FROM olist.order_payments"))
        rows = self.results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_count"], 8)
        self.assertIsNone(rows[0]["payment_row_share_pct"])
        self.assertIsNone(rows[0]["recorded_payment_value_brl"])
        self.assertTrue(verify_payment_results(self.conn, self.params(), rows)["passed"])

    def test_observed_zero_merchandise_value_is_not_unknown(self):
        self.conn.execute(text("UPDATE olist.order_items SET price=0 WHERE order_id=:id"), {"id": "o1"})
        row = self.group("payment_plan", "credit_card", 3)
        self.assertEqual(row["valued_order_count"], 2)
        self.assertEqual(row["avg_order_value_brl"], Decimal("100.00"))

    def test_independent_control_rejects_wrong_average(self):
        rows = self.results()
        rows[0]["avg_order_value_brl"] = Decimal("9999.00")
        with self.assertRaisesRegex(AssertionError, "independent Python"):
            verify_payment_results(self.conn, self.params(), rows)

    def test_empty_interval_has_no_synthetic_distributions(self):
        params = self.params(start=date(2018, 1, 1), end=date(2018, 2, 1))
        rows = self.results(params)
        self.assertEqual(rows, [])
        checks = verify_payment_results(self.conn, params, rows)
        self.assertEqual(checks["scoped_order_count"], 0)
        self.assertIsNone(checks["avg_order_value_brl"])
