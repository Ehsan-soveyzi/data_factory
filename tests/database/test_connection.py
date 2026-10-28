"""Offline configuration checks: no PostgreSQL server or real credentials."""

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

import psycopg
from sqlalchemy.engine import URL

from data_factory.database.connection import (
    DatabaseSettings,
    create_database_engine,
    load_database_settings,
)
from data_factory.exceptions import ConfigurationError


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        # Isolate existing offline tests from the user's real local credentials.
        self.guards.enter_context(patch(
            "data_factory.database.connection.DEFAULT_ENV_FILE",
            Path(__file__).parent / "missing-test-settings.env",
        ))
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo", "psycopg.connect",
        ):
            self.guards.enter_context(patch(
                target, side_effect=AssertionError("Server access is forbidden in step 2")
            ))

    def test_missing_or_blank_environment_reports_required_setting(self):
        for environ in ({}, {"DATABASE_URL": ""}, {"DATABASE_URL": "  "}):
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(ConfigurationError, "DATABASE_URL is required"):
                    load_database_settings(environ=environ)

    def test_environment_is_read_on_each_call(self):
        for database in ("first_db", "second_db"):
            with patch.dict(os.environ, {
                "DATABASE_URL": f"postgresql+psycopg://localhost/{database}",
            }):
                self.assertEqual(load_database_settings().url.database, database)

    def test_explicit_environment_does_not_fall_back_to_process_settings(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://localhost/real_db"}):
            with self.assertRaises(ConfigurationError):
                load_database_settings(environ={})

    def test_parse_errors_do_not_expose_credentials_in_tracebacks(self):
        secret = "private_password_123"
        for raw in (f"not-a-url-{secret}", f"postgresql+psycopg://user:{secret}@host:notaport/db"):
            with self.subTest(case="malformed URL"):
                try:
                    load_database_settings(environ={"DATABASE_URL": raw})
                except ConfigurationError:
                    formatted = traceback.format_exc()
                    self.assertNotIn(secret, formatted)
                    self.assertIn("DATABASE_URL is not a valid", formatted)
                else:
                    self.fail("Malformed URL accepted")

    def test_only_the_selected_postgresql_driver_is_accepted(self):
        for driver in ("postgresql", "postgresql+psycopg2", "postgresql+psycopg_async", "sqlite"):
            with self.subTest(driver=driver):
                with self.assertRaisesRegex(ConfigurationError, "must use postgresql\\+psycopg"):
                    load_database_settings(environ={"DATABASE_URL": f"{driver}://localhost/db"})

    def test_database_name_is_required(self):
        for suffix in ("", "/", "/   "):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(ConfigurationError, "database name"):
                    load_database_settings(environ={
                        "DATABASE_URL": f"postgresql+psycopg://localhost{suffix}",
                    })

    def test_port_range_is_validated(self):
        for port in (0, 65536, -1):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ConfigurationError, "port must be between"):
                    load_database_settings(environ={
                        "DATABASE_URL": f"postgresql+psycopg://localhost:{port}/db",
                    })

    def test_encoded_password_and_connection_options_are_preserved(self):
        settings = load_database_settings(environ={
            "DATABASE_URL": "postgresql+psycopg://reader:p%40ss%2Fword@localhost:5433/db?sslmode=require",
        })
        self.assertEqual(settings.url.password, "p@ss/word")
        self.assertEqual(settings.url.query["sslmode"], "require")
        self.assertEqual(settings.url.port, 5433)

    def test_display_omits_credentials_and_query_values(self):
        settings = load_database_settings(environ={
            "DATABASE_URL": "postgresql+psycopg://private_user:private_pass@localhost:5432/db?password=private_query",
        })
        self.assertEqual(settings.safe_url, "postgresql+psycopg://localhost:5432/db")
        for secret in ("private_user", "private_pass", "private_query"):
            self.assertNotIn(secret, repr(settings))
            self.assertNotIn(secret, settings.safe_url)

    def test_programmatic_settings_are_also_validated(self):
        with self.assertRaises(ConfigurationError):
            DatabaseSettings(url=URL.create("sqlite", database="example.db"))
        with self.assertRaises(ConfigurationError):
            DatabaseSettings(url="postgresql+psycopg://localhost/db")

    def test_engine_creation_and_disposal_do_not_contact_server(self):
        # The actual engine and dialect are used; only outbound transport is guarded.
        settings = load_database_settings(environ={
            "DATABASE_URL": "postgresql+psycopg://demo_user:demo_pass@localhost:5432/db",
        })
        engine = create_database_engine(settings)
        try:
            self.assertEqual(engine.dialect.name, "postgresql")
            self.assertEqual(engine.dialect.driver, "psycopg")
            self.assertIs(engine.dialect.dbapi, psycopg)
            self.assertEqual(engine.pool.checkedout(), 0)
            self.assertEqual(engine.pool.checkedin(), 0)
            self.assertTrue(engine.hide_parameters)
            self.assertFalse(engine.echo)
        finally:
            engine.dispose()

    def test_engine_factory_loads_environment_and_rejects_missing_settings(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://localhost/env_db"}):
            engine = create_database_engine()
            try:
                self.assertEqual(engine.url.database, "env_db")
            finally:
                engine.dispose()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "DATABASE_URL is required"):
                create_database_engine()

    def test_env_file_is_loaded_without_changing_process_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DATABASE_URL='postgresql+psycopg://test_user:literal${PASSWORD}@localhost:5423/file_db'\n",
                encoding="utf-8",
            )
            settings = load_database_settings(env_file=env_file)
            self.assertEqual(settings.url.port, 5423)
            self.assertEqual(settings.url.database, "file_db")
            self.assertEqual(settings.url.password, "literal${PASSWORD}")
            self.assertNotIn("DATABASE_URL", os.environ)

    def test_process_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("DATABASE_URL=postgresql+psycopg://localhost:5423/file_db\n", encoding="utf-8")
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://localhost:5444/process_db"}):
                settings = load_database_settings(env_file=env_file)
            self.assertEqual(settings.url.port, 5444)
            self.assertEqual(settings.url.database, "process_db")

    def test_explicit_mapping_bypasses_env_file(self):
        with patch("data_factory.database.connection.dotenv_values", side_effect=AssertionError("Must not read .env")):
            settings = load_database_settings(environ={"DATABASE_URL": "postgresql+psycopg://localhost/isolated_db"})
        self.assertEqual(settings.url.database, "isolated_db")

    def test_default_env_path_is_used_by_engine_factory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_text("DATABASE_URL=postgresql+psycopg://localhost:5423/file_db\n", encoding="utf-8")
            with patch("data_factory.database.connection.DEFAULT_ENV_FILE", env_file):
                engine = create_database_engine()
            try:
                self.assertEqual(engine.url.port, 5423)
                self.assertEqual(engine.url.database, "file_db")
            finally:
                engine.dispose()

    def test_unreadable_env_file_has_a_safe_configuration_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_bytes(b"DATABASE_URL=private_password\xff\n")
            with self.assertRaises(ConfigurationError) as raised:
                load_database_settings(env_file=env_file)
            self.assertNotIn("private_password", str(raised.exception))
