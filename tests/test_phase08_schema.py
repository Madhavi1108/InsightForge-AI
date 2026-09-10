"""Phase 8 (spec Phases 15-17) - star schema, operational tables & indexing.

The static checks parse ``sql/schema.sql`` and pass with no database. The
integration checks apply the schema to a real PostgreSQL and introspect it; they
are skipped unless ``INSIGHTFORGE_PG_INTEGRATION=1`` (development rule 1).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = PROJECT_ROOT / "sql" / "schema.sql"
DROP_SQL = PROJECT_ROOT / "sql" / "drop_schema.sql"
APPLY_SCRIPT = PROJECT_ROOT / "scripts" / "apply_schema.py"

STAR_TABLES = ["dim_date", "dim_customer", "dim_product", "dim_region", "fact_sales"]
OPERATIONAL_TABLES = [
    "pipeline_runs", "file_registry", "data_quality_results", "rejected_records",
    "anomalies", "recommendations", "forecast_results",
]
ALL_TABLES = OPERATIONAL_TABLES + STAR_TABLES

SPEC_INDEX_COLUMNS = ["order_id", "order_date", "customer_id", "product_id", "region", "category"]

FACT_COLUMNS = [
    "sales_key", "run_id", "date_key", "customer_key", "product_key", "region_key",
    "order_id", "order_date", "customer_id", "product_id", "region", "category",
    "sub_category", "customer_segment", "quantity", "unit_price", "discount",
    "revenue", "cost", "profit", "payment_method", "shipping_days",
    "order_status", "return_status", "is_returned",
]


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema_norm(schema_text: str) -> str:
    """Lower-cased, whitespace-collapsed, comment-stripped SQL."""
    no_comments = re.sub(r"--[^\n]*", "", schema_text)
    return re.sub(r"\s+", " ", no_comments).lower()


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def test_phase08_files_exist():
    for p in (SCHEMA_SQL, DROP_SQL, APPLY_SCRIPT,
              PROJECT_ROOT / "docs" / "database-schema.md"):
        assert p.is_file(), f"missing {p}"


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", ALL_TABLES)
def test_every_table_is_created_idempotently(schema_norm: str, table: str):
    assert f"create table if not exists {table} (" in schema_norm


def test_no_create_table_without_if_not_exists(schema_norm: str):
    creates = re.findall(r"create table (if not exists )?", schema_norm)
    assert creates and all(c.strip() == "if not exists" for c in creates)


def test_no_create_index_without_if_not_exists(schema_norm: str):
    creates = re.findall(r"create index (if not exists )?", schema_norm)
    assert creates and all(c.strip() == "if not exists" for c in creates)


def test_fact_sales_has_all_expected_columns(schema_norm: str):
    body = schema_norm.split("create table if not exists fact_sales (", 1)[1]
    body = body.split(");", 1)[0]
    missing = [c for c in FACT_COLUMNS if not re.search(rf"\b{c}\b", body)]
    assert not missing, f"fact_sales missing columns: {missing}"


def test_fact_sales_is_returned_is_generated(schema_norm: str):
    assert "is_returned boolean generated always as" in schema_norm


def test_derived_measure_checks_present(schema_norm: str):
    assert "check (revenue >= 0)" in schema_norm
    assert "check (quantity >= 1)" in schema_norm
    assert "check (discount >= 0 and discount < 1)" in schema_norm


# --------------------------------------------------------------------------- #
# relationships
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ref",
    ["dim_date(date_key)", "dim_customer(customer_key)",
     "dim_product(product_key)", "dim_region(region_key)",
     "pipeline_runs(run_id)"],
)
def test_fact_sales_foreign_keys(schema_norm: str, ref: str):
    body = schema_norm.split("create table if not exists fact_sales (", 1)[1]
    body = body.split(");", 1)[0]
    assert f"references {ref}".lower() in body


@pytest.mark.parametrize(
    "table",
    ["data_quality_results", "rejected_records", "anomalies",
     "recommendations", "forecast_results"],
)
def test_insight_tables_reference_pipeline_runs(schema_norm: str, table: str):
    body = schema_norm.split(f"create table if not exists {table} (", 1)[1]
    body = body.split(");", 1)[0]
    assert "references pipeline_runs(run_id) on delete cascade" in body


# --------------------------------------------------------------------------- #
# indexing (Phase 17)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("column", SPEC_INDEX_COLUMNS)
def test_spec_named_index_on_fact_sales(schema_norm: str, column: str):
    assert re.search(
        rf"create index if not exists \w+ on fact_sales \({column}\)",
        schema_norm,
    ), f"no fact_sales index on {column}"


def test_composite_rollup_index_present(schema_norm: str):
    assert "on fact_sales (region, category, order_date)" in schema_norm


# --------------------------------------------------------------------------- #
# drop script
# --------------------------------------------------------------------------- #
def test_drop_schema_drops_every_table_with_cascade():
    text = DROP_SQL.read_text(encoding="utf-8").lower()
    for table in ALL_TABLES:
        assert re.search(rf"drop table if exists {table}\s+cascade", text), table


# --------------------------------------------------------------------------- #
# apply_schema.py
# --------------------------------------------------------------------------- #
def test_apply_schema_help_runs():
    r = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--help"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0


def test_apply_schema_dry_run_needs_no_database():
    r = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--dry-run"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "dry-run" in r.stdout
    assert "create table if not exists fact_sales" in r.stdout.lower()


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)


@pg_integration
def test_schema_applies_and_introspects():
    psycopg2 = pytest.importorskip("psycopg2")
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.config import get_settings

    # apply from scratch
    r = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--drop"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # re-apply is idempotent
    r2 = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr

    conn = psycopg2.connect(get_settings().libpq_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = {row[0] for row in cur.fetchall()}
            assert set(ALL_TABLES).issubset(tables)

            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            indexes = {row[0] for row in cur.fetchall()}
            for col in SPEC_INDEX_COLUMNS:
                assert f"idx_fact_sales_{col}" in indexes

            cur.execute(
                """
                SELECT count(*) FROM information_schema.table_constraints
                WHERE table_name = 'fact_sales' AND constraint_type = 'FOREIGN KEY'
                """
            )
            assert cur.fetchone()[0] >= 5
    finally:
        conn.close()


@pg_integration
def test_fact_sales_run_id_foreign_key_is_enforced():
    psycopg2 = pytest.importorskip("psycopg2")
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.config import get_settings

    subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--drop"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    conn = psycopg2.connect(get_settings().libpq_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fact_sales (run_id, date_key, customer_key, product_key, "
                "region_key, order_id, order_date, customer_id, product_id, region, "
                "category, sub_category, customer_segment, quantity, unit_price, "
                "discount, revenue, cost, profit, order_status, return_status) "
                "VALUES (999999, '2026-01-01', 1, 1, 1, 'ORD-1', '2026-01-01', "
                "'CUST-1', 'PROD-1', 'West', 'Electronics', 'Laptop', 'Consumer', "
                "1, 100, 0, 100, 80, 20, 'Completed', 'Not Returned')"
            )
    except psycopg2.errors.ForeignKeyViolation:
        pass
    else:
        pytest.fail("expected a foreign-key violation for run_id = 999999")
    finally:
        conn.rollback()
        conn.close()
