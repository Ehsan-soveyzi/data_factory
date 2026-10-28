"""Hand-calculated Q3 fixtures: delay boundaries and latest-review selection."""

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.engine import make_url

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.satisfaction_analysis import QUERY_FILE, verify_satisfaction_results


ROOT = Path(__file__).resolve().parents[2]
QUERY = text((ROOT / "sql/queries" / QUERY_FILE).read_text(encoding="utf-8"))
TEST_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Use an isolated df_ddl_test_* PostgreSQL database")
class PostgreSQLSatisfactionAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("Q3 fixtures require an isolated df_ddl_test_* database.")
        cls.engine = create_database_engine(DatabaseSettings(url=url))
        with cls.engine.connect() as conn:
            if conn.scalar(text("SELECT current_database()")) != url.database:
                cls.engine.dispose()
                raise RuntimeError("Actual database differs from the fixture target.")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.transaction = self.conn.begin()
        self.addCleanup(self.conn.close)
        self.addCleanup(self.transaction.rollback)
        self.conn.exec_driver_sql("""
            -- Only the isolated fixture database verified above is modified.
            DROP SCHEMA IF EXISTS olist CASCADE;
            CREATE SCHEMA olist;
            CREATE TABLE olist.orders (
                order_id TEXT PRIMARY KEY, order_status TEXT NOT NULL,
                order_purchase_timestamp TIMESTAMP NOT NULL,
                order_delivered_customer_date TIMESTAMP,
                order_estimated_delivery_date DATE, delivery_delay_eligible BOOLEAN NOT NULL
            );
            CREATE TABLE olist.order_reviews (
                review_id TEXT, order_id TEXT, review_score SMALLINT NOT NULL,
                review_creation_date TIMESTAMP NOT NULL,
                review_answer_timestamp TIMESTAMP NOT NULL,
                PRIMARY KEY(review_id, order_id)
            );
        """)
        orders = [(name, "delivered", "2017-01-01", actual, "2017-01-10", True)
                  for name, actual in [
                      ("early", "2017-01-09"), ("early_unreviewed", "2017-01-08"),
                      ("same_day", "2017-01-10T23:59:59"),
                      ("late1", "2017-01-11"), ("late3", "2017-01-13"),
                      ("late4", "2017-01-14"), ("late7", "2017-01-17"),
                      ("late8", "2017-01-18"), ("late14", "2017-01-24"),
                      ("late15", "2017-01-25"), ("late30", "2017-02-09")]]
        orders.extend([
            ("unknown", "delivered", "2017-01-01", None, "2017-01-10", False),
            ("flagged", "delivered", "2017-01-01", "2017-01-15", "2017-01-10", False),
            ("reversed", "delivered", "2017-01-02", "2017-01-01", "2017-01-01", True),
            ("canceled", "canceled", "2017-01-01", "2017-01-11", "2017-01-10", True),
            ("end_boundary", "delivered", "2017-02-01", "2017-02-10", "2017-02-05", True),
        ])
        self.conn.execute(text("""
            INSERT INTO olist.orders VALUES (:id, :status, :purchase, :actual, :estimate, :flag)
        """), [{"id": i, "status": s, "purchase": datetime.fromisoformat(p),
               "actual": datetime.fromisoformat(a) if a else None,
               "estimate": date.fromisoformat(e), "flag": f} for i,s,p,a,e,f in orders])
        reviews = [
            ("early_old", "early", 1, "2017-01-20", "2017-01-11"),
            ("early_new", "early", 5, "2017-01-11", "2017-01-12"),
            ("same", "same_day", 4, "2017-01-09", "2017-01-10T12:00:00"),
            ("neutral", "late1", 3, "2017-01-14", "2017-01-15"),
            ("a", "late4", 1, "2017-01-19", "2017-01-20"),
            ("z", "late4", 2, "2017-01-19", "2017-01-20"),
            ("z", "late7", 5, "2017-01-18", "2017-01-20"),
            ("a", "late7", 1, "2017-01-19", "2017-01-20"),
            ("long1", "late15", 1, "2017-01-25", "2017-01-26"),
            ("long2", "late30", 2, "2017-02-09", "2017-02-10"),
        ]
        self.conn.execute(text("""
            INSERT INTO olist.order_reviews VALUES (:review, :order, :score, :created, :answered)
        """), [{"review": r, "order": o, "score": s,
               "created": datetime.fromisoformat(c), "answered": datetime.fromisoformat(a)}
              for r,o,s,c,a in reviews])

    def params(self, start=date(2017, 1, 1), end=date(2017, 2, 1)):
        return {"start_date": start, "end_date": end, "order_status": "delivered"}

    def results(self, params=None):
        return [dict(row) for row in self.conn.execute(QUERY, params or self.params()).mappings()]

    def bucket(self, number):
        return next(row for row in self.results() if row["delay_bucket_order"] == number)

    def test_all_delay_boundaries_and_calendar_day_semantics(self):
        rows = self.results()
        self.assertEqual([r["delay_bucket_order"] for r in rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual([r["eligible_order_count"] for r in rows], [2, 1, 2, 2, 2, 2])
        self.assertEqual(self.bucket(2)["avg_delay_days"], Decimal("0.00"))
        self.assertEqual(self.bucket(5)["avg_delay_days"], Decimal("11.00"))

    def test_latest_answer_has_priority_and_each_order_has_one_weight(self):
        row = self.bucket(1)
        self.assertEqual(row["reviewed_order_count"], 1)
        self.assertEqual(row["avg_review_score"], Decimal("5.00"))
        self.assertEqual(row["eligible_order_count"], 2)

    def test_creation_timestamp_breaks_equal_answer_ties(self):
        self.conn.execute(text("DELETE FROM olist.orders WHERE order_id=:id"), {"id": "late4"})
        self.assertEqual(self.bucket(4)["avg_review_score"], Decimal("1.00"))

    def test_review_id_breaks_equal_timestamp_ties_deterministically(self):
        self.conn.execute(text("DELETE FROM olist.orders WHERE order_id=:id"), {"id": "late7"})
        self.assertEqual(self.bucket(4)["avg_review_score"], Decimal("2.00"))

    def test_unreviewed_orders_retained_and_score_statistics_are_null(self):
        row = self.bucket(5)
        self.assertEqual(row["missing_review_count"], 2)
        self.assertEqual(row["review_coverage_pct"], Decimal("0.00"))
        self.assertIsNone(row["avg_review_score"])
        self.assertIsNone(row["low_review_pct"])
        self.assertIsNone(row["high_review_pct"])

    def test_score_percentages_use_reviewed_orders_and_preserve_neutral_scores(self):
        self.assertEqual(self.bucket(1)["review_coverage_pct"], Decimal("50.00"))
        self.assertEqual(self.bucket(1)["high_review_pct"], Decimal("100.00"))
        self.assertEqual(self.bucket(3)["avg_review_score"], Decimal("3.00"))
        self.assertEqual(self.bucket(3)["low_review_pct"], Decimal("0.00"))
        self.assertEqual(self.bucket(3)["high_review_pct"], Decimal("0.00"))
        self.assertEqual(self.bucket(4)["low_review_pct"], Decimal("100.00"))

    def test_pre_delivery_response_is_retained_and_flagged(self):
        row = self.bucket(2)
        self.assertEqual(row["selected_review_before_delivery_count"], 1)
        self.assertEqual(row["avg_review_score"], Decimal("4.00"))

    def test_scope_exclusions_and_independent_control_match_hand_counts(self):
        checks = verify_satisfaction_results(self.conn, self.params(), self.results())
        self.assertTrue(checks["passed"])
        self.assertEqual(checks["delivered_orders_in_scope"], 14)
        self.assertEqual(checks["totals"]["eligible_order_count"], 11)
        self.assertEqual(checks["totals"]["reviewed_order_count"], 7)
        self.assertEqual(checks["totals"]["missing_review_count"], 4)
        self.assertEqual(checks["exclusion_reasons"], {"excluded_missing_dates": 1, "excluded_other_ineligible": 2})
        self.assertEqual(checks["eligible_orders_with_multiple_reviews"], 3)
        self.assertEqual(checks["review_rows_not_selected"], 3)
        feb = self.results(self.params(start=date(2017, 2, 1), end=date(2017, 3, 1)))
        self.assertEqual(sum(row["eligible_order_count"] for row in feb), 1)

    def test_independent_control_rejects_wrong_average(self):
        rows = self.results()
        rows[0]["avg_review_score"] = Decimal("1.00")
        with self.assertRaisesRegex(AssertionError, "independent Python"):
            verify_satisfaction_results(self.conn, self.params(), rows)

    def test_empty_interval_has_no_synthetic_zero_score(self):
        params = self.params(start=date(2018, 1, 1), end=date(2018, 2, 1))
        rows = self.results(params)
        self.assertEqual(rows, [])
        checks = verify_satisfaction_results(self.conn, params, rows)
        self.assertEqual(checks["totals"]["reviewed_order_count"], 0)
        self.assertIsNone(checks["totals"]["avg_review_score"])
