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


# --------------------------------------------------------------------------- #
# Pipeline filesystem paths
# --------------------------------------------------------------------------- #
# Defaults mirror ``.env.example``. A relative value is resolved against
# ``PROJECT_ROOT``; an absolute value is used unchanged.
_PATH_DEFAULTS = {
    "incoming": "data/incoming",
    "raw": "data/raw",
    "processed": "data/processed",
    "rejected": "data/rejected",
    "archive": "data/archive",
    "logs": "logs",
    "reports": "reports",
}
_PATH_ENV = {
    "incoming": "DATA_INCOMING_DIR",
    "raw": "DATA_RAW_DIR",
    "processed": "DATA_PROCESSED_DIR",
    "rejected": "DATA_REJECTED_DIR",
    "archive": "DATA_ARCHIVE_DIR",
    "logs": "LOGS_DIR",
    "reports": "REPORTS_DIR",
}


def _resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass(frozen=True)
class PipelinePaths:
    """Resolved absolute paths for the pipeline's working directories."""

    incoming: Path
    raw: Path
    processed: Path
    rejected: Path
    archive: Path
    logs: Path
    reports: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PipelinePaths":
        env = os.environ if environ is None else environ
        return cls(**{
            field: _resolve_path(env.get(_PATH_ENV[field], _PATH_DEFAULTS[field]))
            for field in _PATH_DEFAULTS
        })

    def ensure(self) -> None:
        """Create every directory (idempotent)."""
        for field in _PATH_DEFAULTS:
            getattr(self, field).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_paths() -> PipelinePaths:
    """Load ``.env`` then resolve :class:`PipelinePaths` (cached)."""
    load_env()
    return PipelinePaths.from_env()


# --------------------------------------------------------------------------- #
# Alteryx integration (Phase 12-13)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AlteryxSettings:
    """Engine-selection settings for the Alteryx workflows + Python fallback.

    Per ``docs/system-components.md``: a blank/unset ``ALTERYX_ENGINE_CMD``
    (or one that doesn't point at a real executable) means Alteryx isn't
    available here, and every workflow runs via its Python fallback instead -
    logged as unverified, never faked.
    """

    engine_cmd: str | None
    workflow_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AlteryxSettings":
        env = os.environ if environ is None else environ
        cmd = env.get("ALTERYX_ENGINE_CMD", "").strip() or None
        workflow_dir = env.get("ALTERYX_WORKFLOW_DIR", "alteryx").strip() or "alteryx"
        return cls(engine_cmd=cmd, workflow_dir=_resolve_path(workflow_dir))

    def is_configured(self) -> bool:
        """True only when ``engine_cmd`` is set and actually exists on disk."""
        return bool(self.engine_cmd) and Path(self.engine_cmd).is_file()

    def workflow_path(self, name: str) -> Path:
        return self.workflow_dir / name


@lru_cache(maxsize=1)
def get_alteryx_settings() -> AlteryxSettings:
    """Load ``.env`` then resolve :class:`AlteryxSettings` (cached)."""
    load_env()
    return AlteryxSettings.from_env()


# --------------------------------------------------------------------------- #
# LLM / AI Analyst (Phase 28)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LlmSettings:
    """Provider-selection settings for the AI Analyst (``src/ai_analyst.py``).

    Same "configured means a real call is attempted, else a deterministic
    fallback" shape as :class:`AlteryxSettings`: a blank/unset
    ``GEMINI_API_KEY`` (or ``LLM_PROVIDER`` set to anything but ``gemini``)
    means no LLM call is ever attempted - the AI Analyst runs its template
    path exclusively, never faked as an LLM answer.
    """

    provider: str  # "gemini" | "none" (template-only)
    api_key: str | None
    model: str
    timeout_seconds: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LlmSettings":
        env = os.environ if environ is None else environ
        provider = (env.get("LLM_PROVIDER", "gemini").strip() or "gemini").lower()
        api_key = env.get("GEMINI_API_KEY", "").strip() or None
        model = env.get("GEMINI_MODEL", "gemini-1.5-pro").strip() or "gemini-1.5-pro"
        try:
            timeout_seconds = int(env.get("LLM_TIMEOUT_SECONDS", "30").strip() or 30)
        except ValueError:
            timeout_seconds = 30
        return cls(provider=provider, api_key=api_key, model=model, timeout_seconds=timeout_seconds)

    def is_configured(self) -> bool:
        """True only when the provider is ``"gemini"`` and an API key is
        set - same "configured means real, else fallback" shape as
        :meth:`AlteryxSettings.is_configured`."""
        return self.provider == "gemini" and bool(self.api_key)


@lru_cache(maxsize=1)
def get_llm_settings() -> LlmSettings:
    """Load ``.env`` then resolve :class:`LlmSettings` (cached)."""
    load_env()
    return LlmSettings.from_env()
