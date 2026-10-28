"""Hand-calculated seller ranking, weighting, missingness and multi-seller tests."""

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.engine import make_url

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.seller_analysis import QUERY_FILE, verify_seller_results


ROOT = Path(__file__).resolve().parents[2]
QUERY = text((ROOT / "sql/queries" / QUERY_FILE).read_text(encoding="utf-8"))
TEST_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Use an isolated df_ddl_test_* PostgreSQL database")
class PostgreSQLSellerAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("Q4 fixtures require an isolated df_ddl_test_* database.")
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
            -- The actual target was verified as the isolated test database.
            DROP SCHEMA IF EXISTS olist CASCADE;
            CREATE SCHEMA olist;
            CREATE TABLE olist.sellers (
                seller_id TEXT PRIMARY KEY, seller_city TEXT, seller_state CHAR(2)
            );
            CREATE TABLE olist.orders (
                order_id TEXT PRIMARY KEY, order_status TEXT NOT NULL,
                order_purchase_timestamp TIMESTAMP NOT NULL,
                order_approved_at TIMESTAMP, order_delivered_carrier_date TIMESTAMP,
                approval_to_carrier_duration_valid BOOLEAN NOT NULL
            );
            CREATE TABLE olist.order_items (
                order_id TEXT, order_item_id INTEGER, seller_id TEXT,
                price NUMERIC(12,2), freight_value NUMERIC(12,2),
                PRIMARY KEY(order_id, order_item_id)
            );
            CREATE TABLE olist.order_reviews (
                review_id TEXT, order_id TEXT, review_score SMALLINT NOT NULL,
                review_creation_date TIMESTAMP NOT NULL,
                review_answer_timestamp TIMESTAMP NOT NULL,
                PRIMARY KEY(review_id, order_id)
            );
        """)
        self.conn.execute(text("INSERT INTO olist.sellers VALUES (:id, :city, :state)"), [
            {"id": i, "city": i + " city", "state": s}
            for i,s in [("a", "SP"), ("b", "RJ"), ("c", "MG"), ("d", "PR")]])
        orders = [
            ("o1", "delivered", "2017-01-01", "2017-01-01T10:00:00", "2017-01-02T10:00:00", True),
            ("o2", "delivered", "2017-01-02", "2017-01-02T10:00:00", "2017-01-05T10:00:00", True),
            ("o3", "delivered", "2017-01-03", "2017-01-03T10:00:00", "2017-01-03T10:00:00", True),
            ("o4", "delivered", "2017-01-04", "2017-01-04T10:00:00", "2017-01-04T09:00:00", True),
            ("o5", "delivered", "2017-01-05", None, "2017-01-06", False),
            ("o6", "delivered", "2017-01-06", "2017-01-06", "2017-01-06T01:00:00", False),
            ("canceled", "canceled", "2017-01-01", "2017-01-01", "2017-01-02", True),
            ("boundary", "delivered", "2017-02-01", "2017-02-01", "2017-02-02", True),
        ]
        self.conn.execute(text("""
            INSERT INTO olist.orders VALUES (:id, :status, :purchased, :approved, :carrier, :valid)
        """), [{"id": i, "status": s, "purchased": datetime.fromisoformat(p),
               "approved": datetime.fromisoformat(a) if a else None,
               "carrier": datetime.fromisoformat(c), "valid": f} for i,s,p,a,c,f in orders])
        items = [("o1", 1, "a", "30.00"), ("o1", 2, "a", "20.00"),
                 ("o1", 3, "b", "40.00"), ("o2", 1, "a", "50.00"),
                 ("o3", 1, "b", "40.00"), ("o4", 1, "c", "40.00"),
                 ("o5", 1, "c", "40.00"), ("o6", 1, "d", "0.00"),
                 ("canceled", 1, "a", "999.00"), ("boundary", 1, "a", "999.00")]
        self.conn.execute(text("""
            INSERT INTO olist.order_items VALUES (:order, :item, :seller, :price, :freight)
        """), [{"order": o, "item": i, "seller": s, "price": Decimal(p),
               "freight": Decimal("999.00")} for o,i,s,p in items])
        self.conn.execute(text("""
            INSERT INTO olist.order_reviews VALUES (:id, :order, :score, :created, :answered)
        """), [{"id": i, "order": o, "score": s,
               "created": datetime.fromisoformat(c), "answered": datetime.fromisoformat(a)}
              for i,o,s,c,a in [
                  ("old", "o1", 1, "2017-01-04", "2017-01-05"),
                  ("new", "o1", 5, "2017-01-03", "2017-01-06"),
                  ("second", "o2", 1, "2017-01-06", "2017-01-07"),
                  ("other", "o4", 2, "2017-01-05", "2017-01-06")]])

    def params(self, top_n=20, start=date(2017, 1, 1), end=date(2017, 2, 1)):
        return {"start_date": start, "end_date": end, "order_status": "delivered", "top_n": top_n}

    def results(self, params=None):
        return [dict(row) for row in self.conn.execute(QUERY, params or self.params()).mappings()]

    def seller(self, key):
        return next(row for row in self.results() if row["seller_id"] == key)

    def test_revenue_is_own_item_share_without_freight_or_review_multiplication(self):
        self.assertEqual(self.seller("a")["gross_product_sales_brl"], Decimal("100.00"))
        self.assertEqual(self.seller("b")["gross_product_sales_brl"], Decimal("80.00"))
        self.assertEqual(sum(r["gross_product_sales_brl"] for r in self.results()), Decimal("260.00"))

    def test_averages_weight_seller_orders_once_not_items(self):
        row = self.seller("a")
        self.assertEqual(row["sold_item_count"], 3)
        self.assertEqual(row["sold_order_count"], 2)
        self.assertEqual(row["avg_preparation_days"], Decimal("2.00"))
        self.assertEqual(row["avg_review_score"], Decimal("3.00"))

    def test_multi_seller_order_gets_each_share_and_same_order_level_proxy(self):
        self.assertEqual(self.seller("a")["reviewed_order_count"], 2)
        self.assertEqual(self.seller("b")["reviewed_order_count"], 1)
        self.assertEqual(self.seller("b")["avg_review_score"], Decimal("5.00"))
        self.assertEqual(sum(r["sold_order_count"] for r in self.results()), 7)

    def test_top_limit_and_revenue_ties_are_deterministic(self):
        rows = self.results(self.params(top_n=2))
        self.assertEqual([r["seller_id"] for r in rows], ["a", "b"])
        self.assertEqual([r["revenue_rank"] for r in rows], [1, 2])
        self.assertEqual([r["seller_id"] for r in self.results()], ["a", "b", "c", "d"])

    def test_invalid_missing_or_false_flag_durations_do_not_remove_revenue(self):
        c, d = self.seller("c"), self.seller("d")
        self.assertEqual(c["gross_product_sales_brl"], Decimal("80.00"))
        self.assertEqual(c["preparation_excluded_count"], 2)
        self.assertIsNone(c["avg_preparation_days"])
        self.assertIsNone(d["avg_preparation_days"])

    def test_zero_duration_and_zero_revenue_are_retained(self):
        row = self.seller("b")
        self.assertEqual(row["preparation_eligible_count"], 2)
        self.assertEqual(row["avg_preparation_days"], Decimal("0.50"))
        self.assertEqual(self.seller("d")["gross_product_sales_brl"], Decimal("0.00"))
        self.assertEqual(self.seller("d")["sold_order_count"], 1)

    def test_subday_duration_is_not_truncated_to_calendar_days(self):
        self.conn.execute(text("""
            UPDATE olist.orders SET order_delivered_carrier_date=:carrier WHERE order_id=:id
        """), {"carrier": datetime(2017, 1, 3, 22), "id": "o3"})
        self.assertEqual(self.seller("b")["avg_preparation_days"], Decimal("0.75"))

    def test_missing_reviews_remain_null_and_latest_review_has_one_weight(self):
        row = self.seller("b")
        self.assertEqual(row["missing_review_count"], 1)
        self.assertEqual(row["avg_review_score"], Decimal("5.00"))
        self.assertIsNone(self.seller("d")["avg_review_score"])
        self.assertEqual(self.seller("d")["missing_review_count"], 1)

    def test_scope_boundaries_and_independent_control_match_hand_counts(self):
        checks = verify_seller_results(self.conn, self.params(), self.results())
        self.assertTrue(checks["passed"])
        self.assertEqual(checks["all_product_sales_brl"], Decimal("260.00"))
        self.assertEqual(checks["all_item_count"], 8)
        self.assertEqual(checks["distinct_scoped_order_count"], 6)
        self.assertEqual(checks["multi_seller_order_count"], 1)
        self.assertEqual(checks["top_raw_review_rows"], 4)
        self.assertEqual(checks["top_review_rows_not_selected"], 1)
        feb = self.results(self.params(start=date(2017, 2, 1), end=date(2017, 3, 1)))
        self.assertEqual(len(feb), 1)
        self.assertEqual(feb[0]["gross_product_sales_brl"], Decimal("999.00"))

    def test_independent_control_rejects_wrong_ranking_metrics(self):
        rows = self.results()
        rows[0]["gross_product_sales_brl"] = Decimal("999.00")
        with self.assertRaisesRegex(AssertionError, "independent Python"):
            verify_seller_results(self.conn, self.params(), rows)

    def test_empty_interval_does_not_invent_sellers_or_scores(self):
        params = self.params(start=date(2018, 1, 1), end=date(2018, 2, 1))
        rows = self.results(params)
        self.assertEqual(rows, [])
        checks = verify_seller_results(self.conn, params, rows)
        self.assertEqual(checks["all_seller_count"], 0)
        self.assertEqual(checks["all_product_sales_brl"], Decimal("0"))
