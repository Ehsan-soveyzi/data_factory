"""Isolated PostgreSQL checks for ordinary views, stale caches and atomic refresh."""

from pathlib import Path
import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import test_payment_analysis as fixtures
from data_factory.database.views import (
    PARAMETERS, SNAPSHOT, VIEW, _rows, install_views, refresh_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(fixtures.TEST_URL, "Use the verified isolated database runner")
class PostgreSQLViewSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.PostgreSQLPaymentAnalysisTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # Existing hand-checked split-payment fixtures; no analysis DB mutations.
        fixtures.PostgreSQLPaymentAnalysisTests.setUp(self)
        install_views(self.conn, ROOT)

    def test_simple_view_preserves_all_order_rows_and_statuses(self):
        actual = self.conn.execute(text("SELECT * FROM olist.v_orders ORDER BY order_id")).all()
        expected = self.conn.execute(text("SELECT * FROM olist.orders ORDER BY order_id")).all()
        self.assertEqual(actual, expected)

    def test_multitable_view_equals_original_q5_with_mixed_and_unknown_plans(self):
        expected = [dict(r) for r in self.conn.execute(fixtures.QUERY, PARAMETERS).mappings()]
        self.assertEqual(_rows(self.conn, VIEW), expected)
        # The upper time boundary remains exclusive even in a nonparameterized view.
        self.conn.execute(text("INSERT INTO olist.orders VALUES ('end', 'delivered', '2019-01-01')"))
        self.assertEqual(_rows(self.conn, VIEW), expected)

    def test_snapshot_is_empty_until_explicit_refresh(self):
        self.assertEqual(_rows(self.conn, SNAPSHOT), [])
        metadata = refresh_snapshot(self.conn)
        self.assertEqual(_rows(self.conn, SNAPSHOT), _rows(self.conn, VIEW))
        self.assertEqual(metadata["row_count"], len(_rows(self.conn, VIEW)))
        self.assertLessEqual(metadata["source_snapshot_at"], metadata["refreshed_at"])

    def test_repeat_install_and_refresh_preserve_results_without_duplicates(self):
        refresh_snapshot(self.conn)
        expected = _rows(self.conn, SNAPSHOT)
        install_views(self.conn, ROOT)
        self.assertEqual(_rows(self.conn, SNAPSHOT), expected)
        refresh_snapshot(self.conn)
        self.assertEqual(_rows(self.conn, SNAPSHOT), expected)
        self.assertEqual(self.conn.scalar(text("SELECT COUNT(*) FROM olist.analysis_snapshot_metadata")), 1)

    def test_live_view_updates_while_snapshot_stays_stale_until_refresh(self):
        refresh_snapshot(self.conn)
        cached = _rows(self.conn, SNAPSHOT)
        self.conn.execute(text("UPDATE olist.order_items SET price = price + 10 WHERE order_id = 'o1'"))
        self.assertNotEqual(_rows(self.conn, VIEW), cached)
        self.assertEqual(_rows(self.conn, SNAPSHOT), cached)
        refresh_snapshot(self.conn)
        self.assertEqual(_rows(self.conn, SNAPSHOT), _rows(self.conn, VIEW))

    def test_failed_refresh_rolls_back_rows_and_metadata_together(self):
        refresh_snapshot(self.conn)
        cached = _rows(self.conn, SNAPSHOT)
        metadata = self.conn.execute(text("SELECT * FROM olist.analysis_snapshot_metadata")).all()
        self.conn.execute(text("UPDATE olist.order_items SET price = price + 10 WHERE order_id = 'o1'"))
        self.conn.exec_driver_sql("""
            ALTER TABLE olist.analysis_snapshot_metadata ADD CONSTRAINT fail_new_metadata
            CHECK (row_count < 0) NOT VALID
        """)
        with self.assertRaises(IntegrityError):
            with self.conn.begin_nested():
                refresh_snapshot(self.conn)
        self.assertEqual(_rows(self.conn, SNAPSHOT), cached)
        self.assertEqual(self.conn.execute(text("SELECT * FROM olist.analysis_snapshot_metadata")).all(), metadata)

    def test_empty_source_refresh_removes_obsolete_cached_rows(self):
        refresh_snapshot(self.conn)
        self.conn.execute(text("DELETE FROM olist.order_payments"))
        self.conn.execute(text("DELETE FROM olist.order_items"))
        self.conn.execute(text("DELETE FROM olist.orders"))
        metadata = refresh_snapshot(self.conn)
        self.assertEqual(_rows(self.conn, SNAPSHOT), [])
        self.assertEqual(metadata["row_count"], 0)


class ViewDefinitionTests(unittest.TestCase):
    def test_view_body_is_original_q5_with_only_fixed_scope_and_order_view_substituted(self):
        source = (ROOT / "sql/queries/05_payment_methods_installments.sql").read_text(encoding="utf-8")
        expected = source[source.index("WITH scoped_orders"):]
        for old, new in (("FROM olist.orders", "FROM olist.v_orders"),
                         (":order_status", "'delivered'"),
                         (":start_date", "DATE '2016-01-01'"),
                         (":end_date", "DATE '2019-01-01'")):
            expected = expected.replace(old, new)
        view = (ROOT / "sql/views/001_payment_analysis.sql").read_text(encoding="utf-8")
        actual = view.split("CREATE OR REPLACE VIEW olist.v_payment_analysis AS\n", 1)[1]
        self.assertEqual(actual, expected)
