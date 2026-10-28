"""Q1 analytical correctness against hand-calculated PostgreSQL fixtures."""

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.engine import make_url

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.sales import EXPLAIN_PREFIX, PLAN_FILE, QUERY_FILE


ROOT = Path(__file__).resolve().parents[2]
QUERY_SQL = (ROOT / "sql/queries" / QUERY_FILE).read_text(encoding="utf-8")
TEST_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


class SalesSQLFileTests(unittest.TestCase):
    def test_static_explain_matches_exact_query(self):
        plan = (ROOT / "sql/queries" / PLAN_FILE).read_text(encoding="utf-8")
        self.assertEqual(plan, EXPLAIN_PREFIX + QUERY_SQL)


@unittest.skipUnless(TEST_URL, "Set DATA_FACTORY_TEST_DATABASE_URL to an isolated df_ddl_test_* database")
class PostgreSQLSalesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("Q1 fixtures require an isolated df_ddl_test_* database.")
        cls.engine = create_database_engine(DatabaseSettings(url=url))
        with cls.engine.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != url.database:
                cls.engine.dispose()
                raise RuntimeError("Actual database does not match the fixture database.")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.transaction = self.conn.begin()
        # This database is isolated; rollback removes every fixture object.
        self.conn.exec_driver_sql("""
            -- Restore any prior test class's schema on transaction rollback.
            DROP SCHEMA IF EXISTS olist CASCADE;
            CREATE SCHEMA olist;
            CREATE TABLE olist.orders (
                order_id TEXT PRIMARY KEY, order_status TEXT NOT NULL,
                order_purchase_timestamp TIMESTAMP NOT NULL
            );
            CREATE TABLE olist.products (
                product_id TEXT PRIMARY KEY, product_category_name TEXT
            );
            CREATE TABLE olist.product_categories (
                product_category_name TEXT PRIMARY KEY,
                product_category_name_english TEXT
            );
            CREATE TABLE olist.order_items (
                order_id TEXT, order_item_id INTEGER, product_id TEXT,
                price NUMERIC(12,2), freight_value NUMERIC(12,2),
                PRIMARY KEY (order_id, order_item_id)
            );
        """)
        self.conn.execute(text("""
            INSERT INTO olist.product_categories VALUES (:key, :label)
        """), [{"key": "alpha", "label": "beta"}, {"key": "beta", "label": None}])
        self.conn.execute(text("INSERT INTO olist.products VALUES (:product, :category)"), [
            {"product": "p1", "category": "alpha"},
            {"product": "p2", "category": "alpha"},
            {"product": "p3", "category": "beta"},
            {"product": "p4", "category": None},
        ])
        self.conn.execute(text("INSERT INTO olist.orders VALUES (:order, :status, :purchased)"), [
            {"order": "o1", "status": "delivered", "purchased": datetime(2017, 1, 31, 23, 59, 59)},
            {"order": "o2", "status": "delivered", "purchased": datetime(2017, 1, 1)},
            {"order": "o3", "status": "delivered", "purchased": datetime(2017, 2, 1)},
            {"order": "o4", "status": "canceled", "purchased": datetime(2017, 1, 10)},
            {"order": "o5", "status": "shipped", "purchased": datetime(2017, 1, 10)},
        ])
        self.conn.execute(text("""
            INSERT INTO olist.order_items VALUES (:order, :item, :product, :price, :freight)
        """), [
            {"order": o, "item": i, "product": p, "price": Decimal(v), "freight": Decimal("99.99")}
            for o, i, p, v in [
                ("o1", 1, "p1", "10.10"), ("o1", 2, "p2", "20.20"),
                ("o1", 3, "p3", "7.70"), ("o2", 1, "p1", "9.70"),
                ("o2", 2, "p4", "0.00"), ("o3", 1, "p3", "100.00"),
                ("o4", 1, "p1", "500.00"), ("o5", 1, "p1", "600.00"),
            ]
        ])

    def tearDown(self):
        self.transaction.rollback()
        self.conn.close()

    def results(self, start=date(2017, 1, 1), end=date(2017, 2, 1)):
        return [dict(row) for row in self.conn.execute(text(QUERY_SQL), {
            "start_date": start, "end_date": end, "order_status": "delivered",
        }).mappings()]

    def test_multiple_items_count_one_order_and_average_category_share(self):
        alpha = next(row for row in self.results() if row["product_category_name"] == "alpha")
        self.assertEqual(alpha["gross_product_sales_brl"], Decimal("40.00"))
        self.assertEqual(alpha["unique_order_count"], 2)
        self.assertEqual(alpha["avg_order_category_value_brl"], Decimal("20.00"))

    def test_multicategory_order_does_not_repeat_whole_basket_or_freight(self):
        rows = self.results()
        beta = next(row for row in rows if row["product_category_name"] == "beta")
        self.assertEqual(beta["gross_product_sales_brl"], Decimal("7.70"))
        self.assertEqual(sum(row["gross_product_sales_brl"] for row in rows), Decimal("47.70"))
        self.assertEqual(sum(row["unique_order_count"] for row in rows), 4)

    def test_missing_category_zero_price_and_missing_translation_are_retained(self):
        rows = self.results()
        missing = next(row for row in rows if row["product_category_name"] is None)
        self.assertEqual(missing["category_label"], "Uncategorized")
        self.assertEqual(missing["unique_order_count"], 1)
        self.assertEqual(missing["gross_product_sales_brl"], Decimal("0.00"))
        self.assertEqual(next(row for row in rows if row["product_category_name"] == "beta")["category_label"], "beta")

    def test_identical_display_labels_do_not_merge_distinct_category_keys(self):
        rows = [row for row in self.results() if row["category_label"] == "beta"]
        self.assertEqual({row["product_category_name"] for row in rows}, {"alpha", "beta"})
        self.assertEqual(len(rows), 2)

    def test_month_and_inclusive_exclusive_boundaries(self):
        january = self.results()
        self.assertTrue(all(row["purchase_month"] == date(2017, 1, 1) for row in january))
        february = self.results(date(2017, 2, 1), date(2017, 3, 1))
        self.assertEqual(len(february), 1)
        self.assertEqual(february[0]["gross_product_sales_brl"], Decimal("100.00"))
        self.assertEqual(february[0]["purchase_month"], date(2017, 2, 1))

    def test_other_statuses_excluded_and_empty_interval_returns_no_synthetic_zeros(self):
        self.assertEqual(sum(row["gross_product_sales_brl"] for row in self.results()), Decimal("47.70"))
        self.assertEqual(self.results(date(2017, 3, 1), date(2017, 4, 1)), [])
