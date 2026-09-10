"""Phase 9 (spec Phase 18) - database connection layer.

The bulk of these tests need no database: they cover the retry classifier, the
error sanitiser, the retry driver and the engine singleton with the driver stubbed
out. The live checks are skipped unless ``INSIGHTFORGE_PG_INTEGRATION=1`` with a
running PostgreSQL and the Phase 8 schema applied (development rule 1 - never fake).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import sqlalchemy.exc as sa_exc

from src import database as db
from src.database import (
    Database,
    DatabaseConnectionError,
    DatabaseError,
    QueryExecutionError,
    _is_transient,
    _run_with_retry,
    _sanitise,
    dispose_engine,
    get_engine,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HAS_PSYCOPG2 = importlib.util.find_spec("psycopg2") is not None

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)

SENTINEL_FILE = "__phase09_test__"


@pytest.fixture(autouse=True)
def _reset_db_state(monkeypatch):
    """Fresh engine singleton + instant retries for every test."""
    from src.config import get_settings

    db._ENGINE = None
    get_settings.cache_clear()
    monkeypatch.setattr(db, "DB_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(db, "DB_RETRY_MAX_DELAY", 0.0)
    yield
    db._ENGINE = None
    get_settings.cache_clear()


def _op_error(kind, msg="boom", pgcode=None):
    orig = Exception(msg)
    if pgcode is not None:
        orig.pgcode = pgcode
    return kind("SELECT 1", {}, orig)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def test_phase09_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "database.py",
        PROJECT_ROOT / "docs" / "database-layer.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.database"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# error taxonomy
# --------------------------------------------------------------------------- #
def test_error_hierarchy():
    assert issubclass(DatabaseConnectionError, DatabaseError)
    assert issubclass(QueryExecutionError, DatabaseError)
    assert issubclass(DatabaseError, RuntimeError)


def test_sanitise_never_leaks_credentials_or_sql():
    secret_dsn = "postgresql://insightforge:s3cr3t_pw@db.internal:5432/insightforge"

    class Boom(Exception):
        def __str__(self):  # noqa: D105
            return f"could not connect to {secret_dsn} password=s3cr3t_pw"

    orig = Boom()
    orig.pgcode = "08006"
    err = sa_exc.OperationalError("SELECT * FROM secret_table WHERE pw = 's3cr3t_pw'",
                                  {"pw": "s3cr3t_pw"}, orig)

    msg = _sanitise(err, "healthcheck")

    for leak in ("s3cr3t_pw", "db.internal", "secret_table", "postgresql://", "password="):
        assert leak not in msg
    assert "healthcheck failed" in msg
    assert "SQLSTATE 08006" in msg


# --------------------------------------------------------------------------- #
# transient classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "err, expected",
    [
        (_op_error(sa_exc.OperationalError), True),
        (_op_error(sa_exc.InterfaceError), True),
        (_op_error(sa_exc.IntegrityError), False),
        (_op_error(sa_exc.ProgrammingError), False),
        (_op_error(sa_exc.DataError), False),
        (RuntimeError("nope"), False),
        (ValueError("nope"), False),
    ],
)
def test_is_transient(err, expected):
    assert _is_transient(err) is expected


def test_dbapi_error_with_invalidated_connection_is_transient():
    err = sa_exc.DBAPIError("SELECT 1", {}, Exception("gone"))
    err.connection_invalidated = True
    assert _is_transient(err) is True


# --------------------------------------------------------------------------- #
# retry driver
# --------------------------------------------------------------------------- #
def test_retry_recovers_after_transient_failures(monkeypatch):
    monkeypatch.setattr(db, "DB_MAX_RETRIES", 3)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise _op_error(sa_exc.OperationalError, "server restarting")
        return "ok"

    assert _run_with_retry(fn, what="probe") == "ok"
    assert len(calls) == 3  # initial + 2 retries


def test_retry_exhaustion_raises_connection_error(monkeypatch):
    monkeypatch.setattr(db, "DB_MAX_RETRIES", 3)
    calls = []

    def fn():
        calls.append(1)
        raise _op_error(sa_exc.OperationalError, "down", pgcode="08006")

    with pytest.raises(DatabaseConnectionError) as excinfo:
        _run_with_retry(fn, what="probe")
    assert len(calls) == 4  # initial attempt + DB_MAX_RETRIES (3)
    assert excinfo.value.sqlstate == "08006"
    assert "down" not in str(excinfo.value)


def test_non_transient_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(db, "DB_MAX_RETRIES", 3)
    calls = []

    def fn():
        calls.append(1)
        raise _op_error(sa_exc.ProgrammingError, "syntax error at or near")

    with pytest.raises(QueryExecutionError):
        _run_with_retry(fn, what="probe")
    assert len(calls) == 1


def test_retry_logs_warning_on_retry(monkeypatch, caplog):
    monkeypatch.setattr(db, "DB_MAX_RETRIES", 2)

    def fn():
        raise _op_error(sa_exc.OperationalError, "flaky")

    with caplog.at_level(logging.WARNING, logger="src.database"):
        with pytest.raises(DatabaseConnectionError):
            _run_with_retry(fn, what="probe")
    assert any("database retry" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# engine singleton (driver stubbed - no real connection)
# --------------------------------------------------------------------------- #
def test_get_engine_is_cached_singleton(monkeypatch):
    made = []
    monkeypatch.setattr(db, "create_engine",
                        lambda *a, **k: made.append(k) or mock.MagicMock())
    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2
    assert len(made) == 1


def test_get_engine_uses_settings_url_and_pooling(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return mock.MagicMock()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    from src.config import get_settings

    get_engine()
    assert captured["url"] == get_settings().url
    assert captured["pool_pre_ping"] is True
    assert captured["pool_size"] == db.DB_POOL_SIZE
    assert captured["pool_timeout"] == db.DB_POOL_TIMEOUT


def test_dispose_engine_disposes_and_clears(monkeypatch):
    engine = mock.MagicMock()
    monkeypatch.setattr(db, "create_engine", lambda *a, **k: engine)
    get_engine()
    dispose_engine()
    engine.dispose.assert_called_once()
    assert db._ENGINE is None


def test_connect_args_include_statement_timeout_when_set(monkeypatch):
    monkeypatch.setattr(db, "DB_STATEMENT_TIMEOUT_MS", 4000)
    args = db._connect_args()
    assert args["options"] == "-c statement_timeout=4000"
    assert args["application_name"] == "insightforge"


# --------------------------------------------------------------------------- #
# Database facade with a fake engine
# --------------------------------------------------------------------------- #
def test_ping_true_when_select_1_round_trips():
    engine = mock.MagicMock()
    ctx = engine.connect.return_value.__enter__.return_value
    ctx.execute.return_value.scalar.return_value = 1
    assert Database(engine=engine).ping() is True


def test_ping_false_on_connection_failure():
    engine = mock.MagicMock()
    engine.connect.side_effect = _op_error(sa_exc.OperationalError, "refused")
    assert Database(engine=engine).ping() is False


def test_transaction_commits_on_success():
    engine = mock.MagicMock()
    conn = engine.connect.return_value
    trans = conn.begin.return_value
    with Database(engine=engine).transaction() as c:
        assert c is conn
    trans.commit.assert_called_once()
    trans.rollback.assert_not_called()
    conn.close.assert_called_once()


def test_transaction_rolls_back_on_error():
    engine = mock.MagicMock()
    conn = engine.connect.return_value
    trans = conn.begin.return_value
    with pytest.raises(ValueError):
        with Database(engine=engine).transaction():
            raise ValueError("boom")
    trans.rollback.assert_called_once()
    trans.commit.assert_not_called()
    conn.close.assert_called_once()


def test_execute_many_empty_is_noop():
    engine = mock.MagicMock()
    assert Database(engine=engine).execute_many("INSERT INTO t VALUES (:x)", []) == 0
    engine.begin.assert_not_called()


# --------------------------------------------------------------------------- #
# self-check CLI
# --------------------------------------------------------------------------- #
def test_cli_reports_unreachable_database_without_leaking():
    env = {
        **os.environ,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "59999",
        "POSTGRES_PASSWORD": "p9_distinct_secret",
        "DATABASE_URL": "",
        "DB_MAX_RETRIES": "1",
        "DB_CONNECT_TIMEOUT": "2",
        "DB_RETRY_BASE_DELAY": "0",
        "DB_RETRY_MAX_DELAY": "0",
        "INSIGHTFORGE_ENV": "development",
    }
    r = subprocess.run(
        [sys.executable, "-m", "src.database"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False, env=env,
    )
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "Traceback" not in out
    assert "p9_distinct_secret" not in out
    assert "target :" in r.stdout
    assert "***" in r.stdout


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL (needs the Phase 8 schema)
# --------------------------------------------------------------------------- #
@pg_integration
def test_healthcheck_and_scalar():
    pytest.importorskip("psycopg2")
    database = Database()
    database.healthcheck()
    assert database.ping() is True
    assert database.scalar("SELECT 1") == 1


@pg_integration
def test_parameterised_read_is_injection_safe():
    pytest.importorskip("psycopg2")
    _apply_schema()
    database = Database()
    evil = "x'; DROP TABLE pipeline_runs; --"
    rows = database.fetch_all(
        "SELECT run_id FROM pipeline_runs WHERE file_name = :n", {"n": evil}
    )
    assert rows == []
    # table still there
    assert database.scalar("SELECT count(*) FROM pipeline_runs") is not None


@pg_integration
def test_transaction_commit_then_rollback():
    pytest.importorskip("psycopg2")
    _apply_schema()
    database = Database()
    try:
        with database.transaction() as conn:
            run_id = conn.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO pipeline_runs (file_name) VALUES (:f) RETURNING run_id"
                ),
                {"f": SENTINEL_FILE},
            ).scalar()
        assert database.fetch_one(
            "SELECT status FROM pipeline_runs WHERE run_id = :r", {"r": run_id}
        )["status"] == "RUNNING"

        with pytest.raises(RuntimeError):
            with database.transaction() as conn:
                conn.execute(
                    __import__("sqlalchemy").text(
                        "INSERT INTO pipeline_runs (file_name) VALUES (:f)"
                    ),
                    {"f": SENTINEL_FILE + "_rollback"},
                )
                raise RuntimeError("abort")
        assert database.fetch_all(
            "SELECT 1 FROM pipeline_runs WHERE file_name = :f",
            {"f": SENTINEL_FILE + "_rollback"},
        ) == []
    finally:
        database.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p",
                         {"p": SENTINEL_FILE + "%"})


def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
