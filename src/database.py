"""InsightForge AI - database connection layer (Phase 9 / spec Phase 18).

The **only** module the rest of the platform uses to talk to PostgreSQL
(``docs/system-components.md``, ``docs/data-flow.md`` §4, ``docs/security.md`` §2).
It provides:

* **connection management + pooling** - a lazily-created, pooled SQLAlchemy
  ``Engine`` sourced from ``src.config.get_settings().url``;
* **transactions** - :meth:`Database.transaction` (BEGIN / COMMIT / ROLLBACK);
* **retries** - transient connection failures are retried up to
  ``DB_MAX_RETRIES`` times with exponential backoff (``tenacity``);
* **error handling** - failures raise the typed, **sanitised**
  :class:`DatabaseError` hierarchy: no SQL text, bind parameters, DSN, host or
  raw driver stack trace ever reaches a caller or a log line;
* **safe query execution** - every statement goes through ``sqlalchemy.text()``
  and every value is a **bound parameter**.

Never build SQL with f-strings / ``.format`` / ``%`` and user or file input -
pass values via the ``params`` mapping (``:name`` placeholders).

Importing this module opens no connection. The one-shot DDL tool
``scripts/apply_schema.py`` is the only code allowed to use raw ``psycopg2``,
and only for migration-time DDL.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Connection, Engine

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tunables (env-overridable; modest defaults)
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
DB_POOL_MAX_OVERFLOW = _env_int("DB_POOL_MAX_OVERFLOW", 5)
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 30)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)
DB_CONNECT_TIMEOUT = _env_int("DB_CONNECT_TIMEOUT", 10)
DB_STATEMENT_TIMEOUT_MS = _env_int("DB_STATEMENT_TIMEOUT_MS", 0)  # 0 = server default

DB_MAX_RETRIES = _env_int("DB_MAX_RETRIES", 3)
DB_RETRY_BASE_DELAY = _env_float("DB_RETRY_BASE_DELAY", 0.5)
DB_RETRY_MAX_DELAY = _env_float("DB_RETRY_MAX_DELAY", 5.0)


# --------------------------------------------------------------------------- #
# Typed, sanitised error hierarchy
# --------------------------------------------------------------------------- #
class DatabaseError(RuntimeError):
    """Base class for every error surfaced by this module.

    ``str()`` is a fixed shape - ``"<what> failed: <ErrorClass> [SQLSTATE xxxxx]
    (DriverError)"`` - and never contains SQL, bind values, a connection string,
    a host, or a driver stack trace.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None,
                 cause_type: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.cause_type = cause_type


class DatabaseConnectionError(DatabaseError):
    """The database was unreachable and retries were exhausted."""


class QueryExecutionError(DatabaseError):
    """A statement failed for a non-transient reason (integrity, syntax, ...)."""


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _cause_type(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return type(orig).__name__ if orig is not None else type(exc).__name__


def _sanitise(exc: BaseException, what: str) -> str:
    parts = [f"{what} failed: {type(exc).__name__}"]
    code = _sqlstate(exc)
    if code:
        parts.append(f"[SQLSTATE {code}]")
    ct = _cause_type(exc)
    if ct and ct != type(exc).__name__:
        parts.append(f"({ct})")
    return " ".join(parts)


def _is_transient(exc: BaseException) -> bool:
    """True for failures that a retry might fix (server down, dropped socket)."""
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError)):
        return True
    if isinstance(exc, sa_exc.DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return False


def _wrap(exc: BaseException, what: str) -> BaseException:
    if isinstance(exc, DatabaseError):
        return exc
    if _is_transient(exc):
        return DatabaseConnectionError(
            _sanitise(exc, what), sqlstate=_sqlstate(exc), cause_type=_cause_type(exc)
        )
    if isinstance(exc, sa_exc.SQLAlchemyError):
        return QueryExecutionError(
            _sanitise(exc, what), sqlstate=_sqlstate(exc), cause_type=_cause_type(exc)
        )
    return exc  # a non-database error (bug in caller code) - pass through unchanged


def _before_sleep(what: str):
    def _hook(retry_state) -> None:  # tenacity RetryCallState
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "database retry %d/%d for %s: %s",
            retry_state.attempt_number, DB_MAX_RETRIES, what,
            _sanitise(exc, what) if exc else "unknown",
        )
    return _hook


def _run_with_retry(fn, *, what: str):
    """Call ``fn()`` retrying only transient connection failures.

    Up to ``DB_MAX_RETRIES`` retries follow the initial attempt. Non-transient
    SQLAlchemy errors are not retried. Whatever ultimately propagates is
    converted to the :class:`DatabaseError` hierarchy.
    """
    retryer = Retrying(
        stop=stop_after_attempt(1 + max(0, DB_MAX_RETRIES)),
        wait=wait_exponential(multiplier=DB_RETRY_BASE_DELAY, max=DB_RETRY_MAX_DELAY),
        retry=retry_if_exception(_is_transient),
        before_sleep=_before_sleep(what),
        reraise=True,
    )
    try:
        return retryer(fn)
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad; re-raised sanitised
        wrapped = _wrap(exc, what)
        if wrapped is exc:
            raise
        raise wrapped from None


# --------------------------------------------------------------------------- #
# Engine singleton
# --------------------------------------------------------------------------- #
_ENGINE: Engine | None = None


def _connect_args() -> dict[str, Any]:
    args: dict[str, Any] = {
        "connect_timeout": DB_CONNECT_TIMEOUT,
        "application_name": "insightforge",
    }
    if DB_STATEMENT_TIMEOUT_MS > 0:
        args["options"] = f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"
    return args


def get_engine() -> Engine:
    """Return the shared, pooled :class:`~sqlalchemy.engine.Engine` (lazy)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(
            get_settings().url,
            pool_pre_ping=True,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_POOL_MAX_OVERFLOW,
            pool_timeout=DB_POOL_TIMEOUT,
            pool_recycle=DB_POOL_RECYCLE,
            connect_args=_connect_args(),
            future=True,
        )
    return _ENGINE


def dispose_engine() -> None:
    """Dispose the pool and drop the singleton (tests, config reload)."""
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
        _ENGINE = None


# --------------------------------------------------------------------------- #
# Database facade
# --------------------------------------------------------------------------- #
def _as_text(statement: str | Any):
    return text(statement) if isinstance(statement, str) else statement


class Database:
    """Thin, safe gateway over the shared engine.

    ``statement`` is a ``str`` (wrapped in ``sqlalchemy.text()``) or a
    ``TextClause``; ``params`` is a mapping of bind values for ``:name``
    placeholders. Writes each run in their own transaction and are retried on a
    transient connection loss (nothing was committed, so replay is safe).
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine if engine is not None else get_engine()

    @property
    def engine(self) -> Engine:
        return self._engine

    # -- health / connection management ---------------------------------- #
    def ping(self) -> bool:
        """``True`` if a ``SELECT 1`` round-trips; never raises."""
        try:
            return self.scalar("SELECT 1") == 1
        except Exception:  # noqa: BLE001 - ping is a boolean probe
            return False

    def healthcheck(self) -> None:
        """Raise :class:`DatabaseConnectionError` if the database is unreachable."""
        def _op() -> None:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        _run_with_retry(_op, what="healthcheck")

    # -- reads --------------------------------------------------------- #
    def fetch_all(self, statement: str | Any,
                  params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        stmt = _as_text(statement)

        def _op() -> list[dict[str, Any]]:
            with self._engine.connect() as conn:
                result = conn.execute(stmt, dict(params or {}))
                return [dict(row) for row in result.mappings().all()]
        return _run_with_retry(_op, what="fetch_all")

    def fetch_one(self, statement: str | Any,
                  params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        stmt = _as_text(statement)

        def _op() -> dict[str, Any] | None:
            with self._engine.connect() as conn:
                row = conn.execute(stmt, dict(params or {})).mappings().first()
                return dict(row) if row is not None else None
        return _run_with_retry(_op, what="fetch_one")

    def scalar(self, statement: str | Any,
               params: Mapping[str, Any] | None = None) -> Any:
        stmt = _as_text(statement)

        def _op() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(stmt, dict(params or {})).scalar()
        return _run_with_retry(_op, what="scalar")

    # -- writes ------------------------------------------------------- #
    def execute(self, statement: str | Any,
                params: Mapping[str, Any] | None = None) -> int:
        """Run one DML statement in its own transaction; return the row count."""
        stmt = _as_text(statement)

        def _op() -> int:
            with self._engine.begin() as conn:
                return conn.execute(stmt, dict(params or {})).rowcount
        return _run_with_retry(_op, what="execute")

    def execute_many(self, statement: str | Any,
                     seq_of_params: Sequence[Mapping[str, Any]]) -> int:
        """Executemany for bulk rows (ETL). Empty input is a no-op returning 0."""
        rows = [dict(p) for p in seq_of_params]
        if not rows:
            return 0
        stmt = _as_text(statement)

        def _op() -> int:
            with self._engine.begin() as conn:
                return conn.execute(stmt, rows).rowcount
        return _run_with_retry(_op, what="execute_many")

    def insert_returning(self, statement: str | Any,
                         params: Mapping[str, Any] | None = None) -> Any:
        """Run ``INSERT ... RETURNING <col>`` and return that single value."""
        stmt = _as_text(statement)

        def _op() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(stmt, dict(params or {})).scalar()
        return _run_with_retry(_op, what="insert_returning")

    # -- explicit transaction --------------------------------------- #
    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """BEGIN a transaction and yield the connection.

        COMMIT on clean exit, ROLLBACK on any exception. Acquiring the
        connection is retried on transient failure; statements executed on the
        yielded connection raise raw SQLAlchemy errors (use bound parameters).
        """
        conn = _run_with_retry(self._engine.connect, what="transaction")
        trans = conn.begin()
        try:
            yield conn
        except Exception:
            trans.rollback()
            raise
        else:
            trans.commit()
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Self-check CLI:  python -m src.database
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    print(f"target : {settings.safe_url}")
    try:
        Database().healthcheck()
    except DatabaseError as exc:
        print(f"[error] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - e.g. driver missing / bad config
        print(f"[error] healthcheck failed: {type(exc).__name__}")
        return 1
    print("[ok] ping ok")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
