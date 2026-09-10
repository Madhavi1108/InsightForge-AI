"""InsightForge AI - runtime configuration (Phase 7 / spec Phase 14).

Single place where PostgreSQL connection settings are read from the environment
and assembled into a connection URL. Per ``docs/security.md`` the database URL is
**assembled at runtime from ``.env`` parts** and is never hard-coded.

This module does **not** open a database connection - that is the job of
``src/database.py`` (Phase 9). It only parses configuration, so it is safe to
import anywhere (including from tests with no database available).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import URL, make_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The placeholder shipped in ``.env.example``. A real deployment must replace it.
PLACEHOLDER_PASSWORD = "change_me_in_dot_env"

_DRIVER = "postgresql+psycopg2"

# Defaults mirror the ``.env.example`` template so the module is usable in a
# fresh checkout before ``.env`` is populated (development / test only).
_DEFAULTS = {
    "host": "localhost",
    "port": "5432",
    "db": "insightforge",
    "user": "insightforge",
    "password": PLACEHOLDER_PASSWORD,
}


def load_env(path: str | os.PathLike[str] = ".env") -> bool:
    """Load ``.env`` into ``os.environ`` if it exists. Idempotent.

    Returns ``True`` when a file was found and loaded, ``False`` otherwise.
    Never raises when the file is absent or ``python-dotenv`` is missing.
    """
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:  # pragma: no cover - dotenv is a pinned dep
        logger.warning("python-dotenv not installed; skipping %s", env_path.name)
        return False
    load_dotenv(env_path, override=False)
    return True


@dataclass(frozen=True)
class PostgresSettings:
    """Resolved PostgreSQL connection parameters."""

    host: str
    port: int
    db: str
    user: str
    password: str

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PostgresSettings":
        """Build settings from environment variables.

        An explicit ``DATABASE_URL`` wins over the discrete ``POSTGRES_*`` parts;
        otherwise ``POSTGRES_HOST/PORT/DB/USER/PASSWORD`` are used, falling back
        to the ``.env.example`` defaults for anything unset.
        """
        env = os.environ if environ is None else environ

        database_url = env.get("DATABASE_URL", "").strip()
        if database_url:
            url = make_url(database_url)
            return cls(
                host=url.host or _DEFAULTS["host"],
                port=int(url.port or _DEFAULTS["port"]),
                db=url.database or _DEFAULTS["db"],
                user=url.username or _DEFAULTS["user"],
                password=url.password or "",
            )

        return cls(
            host=env.get("POSTGRES_HOST", _DEFAULTS["host"]),
            port=int(env.get("POSTGRES_PORT", _DEFAULTS["port"])),
            db=env.get("POSTGRES_DB", _DEFAULTS["db"]),
            user=env.get("POSTGRES_USER", _DEFAULTS["user"]),
            password=env.get("POSTGRES_PASSWORD", _DEFAULTS["password"]),
        )

    # ------------------------------------------------------------------ #
    # Derived representations
    # ------------------------------------------------------------------ #
    def _url_object(self) -> URL:
        return URL.create(
            _DRIVER,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.db,
        )

    @property
    def url(self) -> str:
        """Full SQLAlchemy URL, password included and correctly encoded."""
        return self._url_object().render_as_string(hide_password=False)

    @property
    def safe_url(self) -> str:
        """Same URL with the password masked - safe to log or display."""
        return self._url_object().render_as_string(hide_password=True)

    @property
    def libpq_dsn(self) -> str:
        """libpq keyword/value DSN (for ``pg_isready`` / psycopg2)."""
        return (
            f"host={self.host} port={self.port} dbname={self.db} "
            f"user={self.user} password={self.password}"
        )


def is_placeholder(settings: PostgresSettings) -> bool:
    """True when the password is still the ``.env.example`` placeholder."""
    return settings.password == PLACEHOLDER_PASSWORD


@lru_cache(maxsize=1)
def get_settings() -> PostgresSettings:
    """Load ``.env`` then resolve :class:`PostgresSettings` (cached).

    Raises :class:`RuntimeError` when the password is still the placeholder and
    ``INSIGHTFORGE_ENV=production``; only warns otherwise. The error message
    never contains the password (``docs/security.md``).
    """
    load_env()
    settings = PostgresSettings.from_env()
    if is_placeholder(settings):
        env_name = os.environ.get("INSIGHTFORGE_ENV", "development").strip().lower()
        if env_name == "production":
            raise RuntimeError(
                "POSTGRES_PASSWORD is still the .env.example placeholder; "
                "set a real password in .env before running in production."
            )
        logger.warning(
            "POSTGRES_PASSWORD is still the .env.example placeholder "
            "(acceptable for local development only)."
        )
    return settings
