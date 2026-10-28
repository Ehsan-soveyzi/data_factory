"""Q2 correctness, date boundaries, missingness and compiled EXPLAIN binds."""

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.delivery_analysis import QUERY_FILE, verify_delivery_results
from data_factory.database.query_benchmark import Explain


ROOT = Path(__file__).resolve().parents[2]
QUERY = text((ROOT / "sql/queries" / QUERY_FILE).read_text(encoding="utf-8"))
TEST_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


class ExplainCompilationTests(unittest.TestCase):
    def test_explain_preserves_bound_values_without_interpolation(self):
        value = "delivered'; SELECT 'must stay a parameter'"
        statement = Explain(QUERY.bindparams(order_status=value))
        compiled = statement.compile(dialect=postgresql.dialect())
        self.assertTrue(str(compiled).startswith("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)"))
        self.assertNotIn(value, str(compiled))
        self.assertEqual(compiled.params["order_status"], value)
        self.assertIn("start_date", compiled.params)
        self.assertIn("end_date", compiled.params)


@unittest.skipUnless(TEST_URL, "Use an isolated df_ddl_test_* PostgreSQL database")
class PostgreSQLDeliveryAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("Q2 fixtures require an isolated df_ddl_test_* database.")
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
            -- The target was verified as the isolated fixture database above.
            -- Rollback also restores any schema left by another test class.
            DROP SCHEMA IF EXISTS olist CASCADE;
            CREATE SCHEMA olist;
            CREATE TABLE olist.customers (customer_id TEXT PRIMARY KEY, customer_state CHAR(2) NOT NULL);
            CREATE TABLE olist.orders (
                order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                order_status TEXT NOT NULL, order_purchase_timestamp TIMESTAMP NOT NULL,
                order_delivered_customer_date TIMESTAMP,
                order_estimated_delivery_date DATE, delivery_delay_eligible BOOLEAN NOT NULL
            );
        """)
        self.conn.execute(text("INSERT INTO olist.customers VALUES (:id, :state)"),
                          [{"id": "sp", "state": "SP"}, {"id": "rj", "state": "RJ"}])
        values = [
            ("same_day", "sp", "delivered", "2017-01-01", "2017-01-02T23:59:59", "2017-01-02", True),
            ("next_day", "sp", "delivered", "2017-01-01", "2017-01-03", "2017-01-02", True),
            ("early", "sp", "delivered", "2017-01-01", "2017-01-01", "2017-01-02", True),
            ("missing", "sp", "delivered", "2017-01-01", None, "2017-01-02", True),
            ("flagged", "sp", "delivered", "2017-01-01", "2017-01-03", "2017-01-02", False),
            ("reversed", "sp", "delivered", "2017-01-10", "2017-01-09", "2017-01-08", True),
            ("rj_missing_actual", "rj", "delivered", "2017-01-01", None, "2017-01-02", False),
            ("rj_missing_estimate", "rj", "delivered", "2017-01-01", "2017-01-02", None, True),
            ("feb_purchase", "sp", "delivered", "2017-02-01", "2017-03-02", "2017-03-01", True),
            ("canceled", "sp", "canceled", "2017-01-01", "2017-01-03", "2017-01-02", True),
        ]
        self.conn.execute(text("""
            INSERT INTO olist.orders VALUES (:id, :customer, :status, :purchase, :actual, :estimate, :eligible)
        """), [{"id": i, "customer": c, "status": s, "purchase": datetime.fromisoformat(p),
               "actual": datetime.fromisoformat(a) if a else None,
               "estimate": date.fromisoformat(e) if e else None, "eligible": f}
              for i, c, s, p, a, e, f in values])

    def params(self, start=date(2017, 1, 1), end=date(2017, 3, 1)):
        return {"start_date": start, "end_date": end, "order_status": "delivered"}

    def results(self, params=None):
        return [dict(row) for row in self.conn.execute(QUERY, params or self.params()).mappings()]

    def january_sp(self):
        return next(row for row in self.results() if row["customer_state"] == "SP"
                    and row["purchase_month"] == date(2017, 1, 1))

    def test_same_calendar_date_is_on_time_and_next_midnight_is_late(self):
        row = self.january_sp()
        self.assertEqual(row["late_order_count"], 1)
        self.assertEqual(row["on_time_order_count"], 2)

    def test_unknown_invalid_and_false_flag_do_not_enter_denominator(self):
        row = self.january_sp()
        self.assertEqual(row["delivered_order_count"], 6)
        self.assertEqual(row["eligible_order_count"], 3)
        self.assertEqual(row["excluded_order_count"], 3)
        self.assertEqual(row["late_delivery_pct"], Decimal("33.33"))

    def test_zero_denominator_retains_group_and_null_percentage(self):
        row = next(row for row in self.results() if row["customer_state"] == "RJ")
        self.assertEqual(row["delivered_order_count"], 2)
        self.assertEqual(row["excluded_order_count"], 2)
        self.assertEqual(row["eligible_order_count"], 0)
        self.assertIsNone(row["late_delivery_pct"])

    def test_purchase_month_and_start_inclusive_end_exclusive(self):
        january = self.results(self.params(end=date(2017, 2, 1)))
        self.assertTrue(all(row["purchase_month"] == date(2017, 1, 1) for row in january))
        self.assertEqual(sum(row["delivered_order_count"] for row in january), 8)
        february = self.results(self.params(start=date(2017, 2, 1)))
        self.assertEqual(len(february), 1)
        self.assertEqual(february[0]["purchase_month"], date(2017, 2, 1))
        self.assertEqual(february[0]["late_delivery_pct"], Decimal("100.00"))

    def test_non_delivered_orders_excluded_and_state_groups_do_not_overlap(self):
        rows = self.results()
        self.assertEqual(sum(row["delivered_order_count"] for row in rows), 9)
        self.assertEqual(sum(row["late_order_count"] for row in rows), 2)
        self.assertEqual({row["customer_state"] for row in rows}, {"SP", "RJ"})

    def test_overall_percentage_is_weighted_and_python_checks_all_groups(self):
        checks = verify_delivery_results(self.conn, self.params(), self.results())
        self.assertTrue(checks["passed"])
        self.assertEqual(checks["totals"]["late_delivery_pct"], Decimal("50.00"))
        self.assertEqual(checks["totals"]["eligible_order_count"], 4)
        self.assertEqual(checks["exclusion_reasons"], {"missing_dates": 3, "other_ineligible": 2})
        self.assertEqual(checks["delivered_on_estimated_date"], 1)

    def test_independent_control_detects_wrong_percentage(self):
        rows = self.results()
        rows[0]["late_delivery_pct"] = Decimal("99.99")
        with self.assertRaisesRegex(AssertionError, "percentage"):
            verify_delivery_results(self.conn, self.params(), rows)

    def test_empty_interval_has_no_synthetic_rows(self):
        params = self.params(start=date(2018, 1, 1), end=date(2018, 2, 1))
        rows = self.results(params)
        self.assertEqual(rows, [])
        checks = verify_delivery_results(self.conn, params, rows)
        self.assertEqual(checks["totals"]["delivered_order_count"], 0)
        self.assertIsNone(checks["totals"]["late_delivery_pct"])
