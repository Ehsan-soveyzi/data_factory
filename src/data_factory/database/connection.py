"""Load PostgreSQL settings and build a lazy SQLAlchemy Core engine."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import ArgumentError

from data_factory.exceptions import ConfigurationError


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


@dataclass(frozen=True)
class DatabaseSettings:
    """Validated connection settings; repr deliberately omits the URL."""

    url: URL = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.url, URL):
            raise ConfigurationError("DatabaseSettings.url must be a SQLAlchemy URL.")
        if self.url.drivername != "postgresql+psycopg":
            raise ConfigurationError("DATABASE_URL must use postgresql+psycopg://.")
        if not self.url.database or not self.url.database.strip():
            raise ConfigurationError("DATABASE_URL must include a database name.")
        if self.url.port is not None and not 1 <= self.url.port <= 65535:
            raise ConfigurationError("DATABASE_URL port must be between 1 and 65535.")

    @property
    def safe_url(self) -> str:
        """Describe the target without username, password or query values."""
        return URL.create(
            drivername=self.url.drivername,
            host=self.url.host,
            port=self.url.port,
            database=self.url.database,
        ).render_as_string()


def load_database_settings(
    *, environ: Mapping[str, str] | None = None, env_file: str | Path | None = None,
) -> DatabaseSettings:
    """Read process settings with project .env fallback, without server access.

    An explicit environ mapping bypasses .env for isolated callers and tests.
    dotenv values are not expanded and do not mutate the process environment.
    """
    if environ is None:
        try:
            file_values = dotenv_values(
                DEFAULT_ENV_FILE if env_file is None else env_file,
                encoding="utf-8-sig", interpolate=False,
            )
        except (OSError, UnicodeError):
            raise ConfigurationError("Unable to read the database .env settings file.") from None
        environment = {**file_values, **os.environ}
    else:
        environment = environ
    raw_url = environment.get("DATABASE_URL")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ConfigurationError("DATABASE_URL is required in the process environment or .env file.")
    try:
        url = make_url(raw_url.strip())
    except (ArgumentError, TypeError, ValueError):
        # SQLAlchemy parse errors can contain the original connection string.
        raise ConfigurationError(
            "DATABASE_URL is not a valid SQLAlchemy connection URL; "
            "check its format and numeric port."
        ) from None
    return DatabaseSettings(url=url)


def create_database_engine(settings: DatabaseSettings | None = None) -> Engine:
    """Build an engine without connecting; the caller owns engine.dispose()."""
    if settings is None:
        settings = load_database_settings()
    return create_engine(
        settings.url,
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
