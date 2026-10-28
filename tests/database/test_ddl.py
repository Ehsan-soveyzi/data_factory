"""Initial DDL checks; live fixtures require an explicitly isolated test database."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from data_factory.database.connection import DatabaseSettings, create_database_engine
from data_factory.database.ddl import DDL_FILES, EXPECTED_TABLES, inspect_initial_schema, run_initial_ddl
from data_factory.exceptions import ConfigurationError


DDL_DIR = Path(__file__).resolve().parents[2] / "sql/ddl"
TEST_DATABASE_URL = os.environ.get("DATA_FACTORY_TEST_DATABASE_URL")


class DDLOfflineTests(unittest.TestCase):
    def test_missing_second_file_fails_before_opening_transaction(self):
        engine = Mock(spec=Engine)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, DDL_FILES[0]).write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "002_nyc311.sql"):
                run_initial_ddl(engine, directory)
        engine.begin.assert_not_called()

    def test_empty_file_fails_before_opening_transaction(self):
        engine = Mock(spec=Engine)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, DDL_FILES[0]).write_text("  \n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "empty"):
                run_initial_ddl(engine, directory)
        engine.begin.assert_not_called()

    def test_other_database_dialects_are_rejected(self):
        engine = create_engine("sqlite://")
        try:
            with self.assertRaisesRegex(ConfigurationError, "PostgreSQL psycopg"):
                run_initial_ddl(engine, DDL_DIR)
        finally:
            engine.dispose()


@unittest.skipUnless(TEST_DATABASE_URL, "Set DATA_FACTORY_TEST_DATABASE_URL to an isolated df_ddl_test_* database")
class PostgreSQLDDLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_DATABASE_URL)
        if not url.database or not url.database.startswith("df_ddl_test_"):
            raise RuntimeError("DDL fixtures require a dedicated df_ddl_test_* database.")
        cls.engine = create_database_engine(DatabaseSettings(url=url))
        with cls.engine.connect() as connection:
            actual = connection.scalar(text("SELECT current_database()"))
        if actual != url.database:
            cls.engine.dispose()
            raise RuntimeError("The actual database does not match the explicitly configured test database.")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # These fixtures are confined to the dedicated database checked above.
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS olist CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS nyc311 CASCADE")

    def test_creates_twelve_empty_tables_and_constraints_visible_after_commit(self):
        report = run_initial_ddl(self.engine, DDL_DIR)
        committed = inspect_initial_schema(self.engine)
        self.assertEqual(report["status"], "created")
        self.assertTrue(report["transaction_committed"])
        self.assertEqual(committed["table_count"], 12)
        self.assertEqual(committed["constraint_counts"]["PRIMARY KEY"], 12)
        self.assertEqual(committed["constraint_counts"]["FOREIGN KEY"], 9)
        self.assertTrue(all(row["row_count"] == 0 for row in committed["tables"]))
        self.assertEqual([row["file"] for row in report["files"]], list(DDL_FILES))
        self.assertTrue(all(row["executed"] for row in report["files"]))

    def test_repeat_leaves_existing_data_and_tables_untouched(self):
        run_initial_ddl(self.engine, DDL_DIR)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO olist.product_categories (product_category_name) VALUES (:name)"), {"name": "retained_test_category"})
        report = run_initial_ddl(self.engine, DDL_DIR)
        self.assertEqual(report["status"], "already_present")
        self.assertFalse(report["ddl_executed"])
        self.assertFalse(report["existing_definitions_compared_to_sql"])
        self.assertTrue(all(not row["executed"] for row in report["files"]))
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT product_category_name FROM olist.product_categories")), "retained_test_category")

    def test_failure_in_second_file_rolls_back_both_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copyfile(DDL_DIR / DDL_FILES[0], Path(directory, DDL_FILES[0]))
            Path(directory, DDL_FILES[1]).write_text(
                "CREATE SCHEMA nyc311; CREATE TABLE nyc311.population_areas (id INTEGER PRIMARY KEY); SELECT nonexistent_ddl_test_function();",
                encoding="utf-8",
            )
            with self.assertRaises(SQLAlchemyError):
                run_initial_ddl(self.engine, directory)
        with self.engine.connect() as connection:
            self.assertFalse(inspect(connection).has_schema("olist"))
            self.assertFalse(inspect(connection).has_schema("nyc311"))

    def test_partial_existing_schema_is_rejected_without_changes(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA olist")
            connection.exec_driver_sql("CREATE TABLE olist.product_categories (original_column INTEGER)")
        with self.assertRaisesRegex(ConfigurationError, "Partial or conflicting"):
            run_initial_ddl(self.engine, DDL_DIR)
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            self.assertEqual(inspector.get_table_names(schema="olist"), ["product_categories"])
            self.assertEqual([c["name"] for c in inspector.get_columns("product_categories", schema="olist")], ["original_column"])
            self.assertFalse(inspector.has_schema("nyc311"))

    def test_whole_script_preserves_semicolons_and_percent_signs_in_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in DDL_FILES:
                shutil.copyfile(DDL_DIR / name, Path(directory, name))
            with Path(directory, DDL_FILES[0]).open("a", encoding="utf-8") as handle:
                handle.write("\nCOMMENT ON TABLE olist.product_categories IS 'keeps ; semicolon and 100% text';\n")
            run_initial_ddl(self.engine, directory)
        with self.engine.connect() as connection:
            comment = connection.scalar(text("SELECT obj_description('olist.product_categories'::regclass, 'pg_class')"))
        self.assertEqual(comment, "keeps ; semicolon and 100% text")
