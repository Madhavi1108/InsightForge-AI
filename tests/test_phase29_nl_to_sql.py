"""Phase 29 (spec Phases 56-57, FR-21) - NL-to-SQL & AI security.

`validate_sql` is pure and is this phase's core safety guarantee - gets
exhaustive direct unit tests. `generate_sql`/`_execute_readonly` are
tested with monkeypatched/stub collaborators, never a real network or DB
call. `ask` is tested end-to-end with every collaborator monkeypatched,
asserting each stage short-circuits correctly. The real
question -> SQL -> PostgreSQL round trip needs real data (and, for a
non-template engine, a real API key), so real execution is
`INSIGHTFORGE_PG_INTEGRATION=1`-gated (development rule 1: never fake
functionality). Like Phases 22-28, NL-to-SQL is **not** wired into
`src/orchestrator.py` and persists nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.config import LlmSettings
from src.nl_to_sql import (
    NlSqlResult,
    _apply_row_limit,
    _clean_sql_candidate,
    _execute_readonly,
    _explain_result,
    ask,
    generate_sql,
    validate_sql,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)

_UNCONFIGURED = LlmSettings(provider="none", api_key=None, model="x", timeout_seconds=30)
_CONFIGURED = LlmSettings(provider="gemini", api_key="fake-key", model="gemini-1.5-pro", timeout_seconds=30)


def test_phase29_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "nl_to_sql.py",
        PROJECT_ROOT / "docs" / "nl-to-sql.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.nl_to_sql"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# validate_sql - pure, the core safety guarantee
# --------------------------------------------------------------------------- #
def test_validate_sql_happy_path_appends_limit():
    result = validate_sql("SELECT * FROM daily_kpis", row_limit=100)
    assert result.is_safe is True
    assert result.tables == ("daily_kpis",)
    assert "LIMIT 100" in result.sql


def test_validate_sql_allows_with_cte():
    result = validate_sql(
        "WITH t AS (SELECT * FROM fact_sales) SELECT * FROM t", row_limit=100,
    )
    assert result.is_safe is True


@pytest.mark.parametrize("sql", [
    "DROP TABLE fact_sales",
    "DELETE FROM fact_sales",
    "UPDATE fact_sales SET revenue = 0",
    "INSERT INTO fact_sales VALUES (1)",
    "ALTER TABLE fact_sales ADD COLUMN x INT",
    "TRUNCATE fact_sales",
    "GRANT ALL ON fact_sales TO public",
    "SELECT * FROM fact_sales; DROP TABLE fact_sales",
])
def test_validate_sql_blocks_banned_or_multi_statement(sql):
    result = validate_sql(sql, row_limit=100)
    assert result.is_safe is False
    assert result.reason


def test_validate_sql_blocks_multiple_statements():
    result = validate_sql("SELECT 1; SELECT 2", row_limit=100)
    assert result.is_safe is False
    assert "single SQL statement" in result.reason


def test_validate_sql_blocks_non_select_start():
    result = validate_sql("EXPLAIN SELECT * FROM fact_sales", row_limit=100)
    assert result.is_safe is False
    assert "SELECT" in result.reason


@pytest.mark.parametrize("table", ["pipeline_runs", "pg_shadow", "recommendations"])
def test_validate_sql_blocks_non_whitelisted_table(table):
    result = validate_sql(f"SELECT * FROM {table}", row_limit=100)
    assert result.is_safe is False
    assert table in result.reason


def test_validate_sql_empty_input():
    result = validate_sql("   ", row_limit=100)
    assert result.is_safe is False
    assert result.reason == "empty query"


# --------------------------------------------------------------------------- #
# _apply_row_limit - pure
# --------------------------------------------------------------------------- #
def test_apply_row_limit_appends_when_missing():
    assert _apply_row_limit("SELECT * FROM fact_sales", 100) == "SELECT * FROM fact_sales LIMIT 100"


def test_apply_row_limit_leaves_lower_limit_alone():
    assert _apply_row_limit("SELECT * FROM fact_sales LIMIT 10", 100) == "SELECT * FROM fact_sales LIMIT 10"


def test_apply_row_limit_clamps_higher_limit():
    result = _apply_row_limit("SELECT * FROM fact_sales LIMIT 5000", 100)
    assert result.endswith("LIMIT 100")
    assert "5000" not in result


# --------------------------------------------------------------------------- #
# _clean_sql_candidate - pure
# --------------------------------------------------------------------------- #
def test_clean_sql_candidate_strips_fence():
    assert _clean_sql_candidate("```sql\nSELECT 1\n```") == "SELECT 1"


def test_clean_sql_candidate_plain_sql_unchanged():
    assert _clean_sql_candidate("SELECT 1") == "SELECT 1"


# --------------------------------------------------------------------------- #
# generate_sql - monkeypatched _call_gemini
# --------------------------------------------------------------------------- #
def test_generate_sql_unconfigured_never_calls_gemini(monkeypatch):
    import src.nl_to_sql as nl_to_sql

    def _boom(*args, **kwargs):
        raise AssertionError("must not call Gemini when unconfigured")

    monkeypatch.setattr(nl_to_sql, "_call_gemini", _boom)
    assert generate_sql("How much revenue?", _UNCONFIGURED) is None


def test_generate_sql_returns_cleaned_sql(monkeypatch):
    import src.nl_to_sql as nl_to_sql
    monkeypatch.setattr(nl_to_sql, "_call_gemini", lambda prompt, settings: "```sql\nSELECT 1\n```")
    assert generate_sql("How much revenue?", _CONFIGURED) == "SELECT 1"


def test_generate_sql_none_on_gemini_failure(monkeypatch):
    import src.nl_to_sql as nl_to_sql
    monkeypatch.setattr(nl_to_sql, "_call_gemini", lambda prompt, settings: None)
    assert generate_sql("How much revenue?", _CONFIGURED) is None


# --------------------------------------------------------------------------- #
# _execute_readonly - sanitized error path
# --------------------------------------------------------------------------- #
class _RaisingConn:
    def execute(self, *args, **kwargs):
        raise SQLAlchemyError("boom")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RaisingEngine:
    def begin(self):
        return _RaisingConn()


class _RaisingDB:
    engine = _RaisingEngine()


def test_execute_readonly_sanitizes_errors():
    rows, error = _execute_readonly(_RaisingDB(), "SELECT 1", timeout_ms=1000)
    assert rows is None
    assert error is not None
    assert "boom" not in error  # raw driver detail never leaks


# --------------------------------------------------------------------------- #
# _explain_result - pure
# --------------------------------------------------------------------------- #
def test_explain_result_empty():
    assert "no matching rows" in _explain_result([])


def test_explain_result_non_empty():
    text = _explain_result([{"revenue": 100, "region": "West"}, {"revenue": 50, "region": "East"}])
    assert "2 row" in text
    assert "revenue" in text and "region" in text


# --------------------------------------------------------------------------- #
# ask - monkeypatched generate_sql / _execute_readonly
# --------------------------------------------------------------------------- #
def test_ask_no_llm_configured(monkeypatch):
    import src.nl_to_sql as nl_to_sql

    def _boom(*args, **kwargs):
        raise AssertionError("must not execute when SQL generation failed")

    monkeypatch.setattr(nl_to_sql, "generate_sql", lambda question, settings: None)
    monkeypatch.setattr(nl_to_sql, "_execute_readonly", _boom)

    result = ask(db=object(), question="How much revenue?", settings=_UNCONFIGURED)
    assert isinstance(result, NlSqlResult)
    assert result.engine == "unavailable"
    assert result.is_safe is False
    assert result.rows == []


def test_ask_blocks_unsafe_generated_sql(monkeypatch):
    import src.nl_to_sql as nl_to_sql

    def _boom(*args, **kwargs):
        raise AssertionError("must not execute a blocked query")

    monkeypatch.setattr(nl_to_sql, "generate_sql", lambda question, settings: "DROP TABLE fact_sales")
    monkeypatch.setattr(nl_to_sql, "_execute_readonly", _boom)

    result = ask(db=object(), question="Delete everything", settings=_CONFIGURED)
    assert result.is_safe is False
    assert result.blocked_reason
    assert result.rows == []


def test_ask_happy_path(monkeypatch):
    import src.nl_to_sql as nl_to_sql
    monkeypatch.setattr(nl_to_sql, "generate_sql", lambda question, settings: "SELECT * FROM daily_kpis")
    monkeypatch.setattr(
        nl_to_sql, "_execute_readonly",
        lambda db, sql, timeout_ms: ([{"revenue": 100.0}], None),
    )
    result = ask(db=object(), question="Show daily KPIs", settings=_CONFIGURED)
    assert result.is_safe is True
    assert result.row_count == 1
    assert "1 row" in result.explanation
    json.dumps(result.to_dict())


def test_ask_execution_error(monkeypatch):
    import src.nl_to_sql as nl_to_sql
    monkeypatch.setattr(nl_to_sql, "generate_sql", lambda question, settings: "SELECT * FROM daily_kpis")
    monkeypatch.setattr(
        nl_to_sql, "_execute_readonly",
        lambda db, sql, timeout_ms: (None, "the query could not be executed (it may have timed out or been invalid)"),
    )
    result = ask(db=object(), question="Show daily KPIs", settings=_CONFIGURED)
    assert result.is_safe is True
    assert result.error is not None
    assert result.rows == []


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
@pg_integration
def test_execute_readonly_round_trip():
    pytest.importorskip("psycopg2")
    from src.database import Database

    db = Database()
    validation = validate_sql("SELECT * FROM fact_sales", row_limit=5)
    assert validation.is_safe is True
    rows, error = _execute_readonly(db, validation.sql, timeout_ms=5000)
    assert error is None
    assert rows is not None
    assert len(rows) <= 5


@pg_integration
def test_ask_round_trip_no_api_key_is_honest():
    pytest.importorskip("psycopg2")
    from src.database import Database

    db = Database()
    result = ask(db, "What is total revenue by region?")
    assert isinstance(result, NlSqlResult)
    if not LlmSettings.from_env().is_configured():
        assert result.engine == "unavailable"
    json.dumps(result.to_dict())
