"""Function semantics and atomic, repeatable, month-scoped Procedure behavior."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

import test_delivery_analysis as fixtures
from data_factory.database.routines import CALL_MONTH, install_routines, read_month


ROOT = Path(__file__).resolve().parents[2]
JANUARY = date(2017, 1, 1)
FEBRUARY = date(2017, 2, 1)


@unittest.skipUnless(fixtures.TEST_URL, "Use the verified disposable database runner")
class PostgreSQLRoutineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PostgreSQLDeliveryAnalysisTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # Reuse the independently hand-checked Q2 date/missingness fixture.
        fixtures.PostgreSQLDeliveryAnalysisTests.setUp(self)
        install_routines(self.conn, ROOT)

    def call(self, month=JANUARY):
        self.conn.execute(CALL_MONTH, {"month": month})

    def metadata(self):
        return self.conn.execute(text("SELECT * FROM olist.monthly_delivery_snapshot_runs ORDER BY purchase_month")).all()

    def test_function_rounds_fractional_percentages_in_select(self):
        values = self.conn.execute(text("""
            SELECT olist.safe_percentage(1, 3), olist.safe_percentage(2, 3),
                   olist.safe_percentage(0, 10), olist.safe_percentage(3, 3)
        """)).one()
        self.assertEqual(tuple(values), (Decimal("33.33"), Decimal("66.67"), Decimal("0.00"), Decimal("100.00")))

    def test_function_zero_denominator_and_null_inputs_are_undefined(self):
        values = self.conn.execute(text("""
            SELECT olist.safe_percentage(1, 0), olist.safe_percentage(0, 0),
                   olist.safe_percentage(NULL, 3), olist.safe_percentage(1, NULL)
        """)).one()
        self.assertEqual(tuple(values), (None, None, None, None))

    def test_procedure_matches_q2_and_hand_checked_null_date_groups(self):
        self.call()
        expected = [dict(r) for r in self.conn.execute(fixtures.QUERY, {
            "start_date": JANUARY, "end_date": FEBRUARY, "order_status": "delivered"}).mappings()]
        rows = read_month(self.conn, JANUARY)
        self.assertEqual(rows, expected)
        sp = next(r for r in rows if r["customer_state"] == "SP")
        rj = next(r for r in rows if r["customer_state"] == "RJ")
        self.assertEqual((sp["delivered_order_count"], sp["eligible_order_count"], sp["late_order_count"]), (6, 3, 1))
        self.assertEqual(sp["late_delivery_pct"], Decimal("33.33"))
        self.assertIsNone(rj["late_delivery_pct"])

    def test_repeat_call_and_reinstallation_do_not_duplicate_results_or_metadata(self):
        self.call()
        original = read_month(self.conn, JANUARY)
        install_routines(self.conn, ROOT)
        self.assertEqual(read_month(self.conn, JANUARY), original)
        self.call()
        self.assertEqual(read_month(self.conn, JANUARY), original)
        self.assertEqual(len(self.metadata()), 1)

    def test_normalizes_input_date_to_purchase_month_and_excludes_end_boundary(self):
        self.call(date(2017, 1, 31))
        rows = read_month(self.conn, JANUARY)
        self.assertEqual(sum(r["delivered_order_count"] for r in rows), 8)
        self.assertEqual(len(self.metadata()), 1)
        self.assertEqual(self.metadata()[0].purchase_month, JANUARY)

    def test_refresh_replaces_changed_month_and_preserves_other_month_rows_and_metadata(self):
        self.call(JANUARY)
        self.call(FEBRUARY)
        feb_rows = read_month(self.conn, FEBRUARY)
        feb_meta = next(r for r in self.metadata() if r.purchase_month == FEBRUARY)
        jan_rows = read_month(self.conn, JANUARY)
        self.conn.execute(text("UPDATE olist.orders SET order_status='canceled' WHERE order_id='next_day'"))
        self.call(JANUARY)
        refreshed = read_month(self.conn, JANUARY)
        self.assertNotEqual(refreshed, jan_rows)
        self.assertEqual(sum(r["late_order_count"] for r in refreshed), 0)
        self.assertEqual(read_month(self.conn, FEBRUARY), feb_rows)
        self.assertEqual(next(r for r in self.metadata() if r.purchase_month == FEBRUARY), feb_meta)

    def test_empty_month_removes_stale_rows_and_records_zero_coverage(self):
        self.call()
        self.conn.execute(text("UPDATE olist.orders SET order_status='canceled' WHERE order_purchase_timestamp < '2017-02-01'"))
        self.call()
        self.assertEqual(read_month(self.conn, JANUARY), [])
        metadata = self.metadata()[0]
        self.assertEqual((metadata.state_row_count, metadata.delivered_order_count), (0, 0))

    def test_null_and_infinite_month_are_rejected_without_changing_cache(self):
        self.call()
        cached, metadata = read_month(self.conn, JANUARY), self.metadata()
        for value in (None, "infinity", "-infinity"):
            with self.subTest(value=value), self.assertRaises(DBAPIError) as raised:
                with self.conn.begin_nested():
                    self.call(value)
            self.assertEqual(raised.exception.orig.sqlstate, "22023")
        self.assertEqual(read_month(self.conn, JANUARY), cached)
        self.assertEqual(self.metadata(), metadata)

    def test_insert_failure_after_delete_restores_previous_snapshot(self):
        self.call()
        cached, metadata = read_month(self.conn, JANUARY), self.metadata()
        self.conn.exec_driver_sql("""
            ALTER TABLE olist.monthly_delivery_snapshot ADD CONSTRAINT reject_new_rows
            CHECK (delivered_order_count < 0) NOT VALID
        """)
        with self.assertRaises(IntegrityError):
            with self.conn.begin_nested():
                self.call()
        self.assertEqual(read_month(self.conn, JANUARY), cached)
        self.assertEqual(self.metadata(), metadata)

    def test_metadata_failure_after_insert_rolls_back_all_monthly_changes(self):
        self.call()
        cached, metadata = read_month(self.conn, JANUARY), self.metadata()
        self.conn.execute(text("UPDATE olist.orders SET order_status='canceled' WHERE order_id='next_day'"))
        self.conn.exec_driver_sql("""
            ALTER TABLE olist.monthly_delivery_snapshot_runs ADD CONSTRAINT reject_new_metadata
            CHECK (state_row_count < 0) NOT VALID
        """)
        with self.assertRaises(IntegrityError):
            with self.conn.begin_nested():
                self.call()
        self.assertEqual(read_month(self.conn, JANUARY), cached)
        self.assertEqual(self.metadata(), metadata)

    def test_caller_can_roll_back_a_successful_call(self):
        self.call()
        cached, metadata = read_month(self.conn, JANUARY), self.metadata()
        self.conn.execute(text("UPDATE olist.orders SET order_status='canceled' WHERE order_id='next_day'"))
        with self.assertRaisesRegex(RuntimeError, "caller abort"):
            with self.conn.begin_nested():
                self.call()
                self.assertNotEqual(read_month(self.conn, JANUARY), cached)
                raise RuntimeError("caller abort")
        self.assertEqual(read_month(self.conn, JANUARY), cached)
        self.assertEqual(self.metadata(), metadata)
