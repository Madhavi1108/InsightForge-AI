"""Phase 16 (spec Phase 32) - analytical views.

No Python module exists for this phase (the spec's only artifact is
``sql/views.sql``), so - matching ``tests/test_phase15_kpi_queries.py`` /
``tests/test_phase08_schema.py``'s precedent for a pure-``.sql`` deliverable -
the static checks below parse the file and pass with no database; the
correctness checks apply the real schema + views, run the pipeline to
populate ``fact_sales``, and verify the views are queryable, complete
(unfiltered), and numerically consistent with independently-computed pandas
aggregates. They are skipped unless ``INSIGHTFORGE_PG_INTEGRATION=1``
(development rule 1: never fake functionality).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIEWS_SQL = PROJECT_ROOT / "sql" / "views.sql"
APPLY_SCRIPT = PROJECT_ROOT / "scripts" / "apply_schema.py"

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase16_test__"

EXPECTED_VIEWS = [
    "daily_kpis", "monthly_kpis", "regional_performance",
    "category_performance", "product_performance", "customer_performance",
]


@pytest.fixture(scope="module")
def views_text() -> str:
    return VIEWS_SQL.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# files / static structure
# --------------------------------------------------------------------------- #
def test_phase16_files_exist():
    for p in (VIEWS_SQL, PROJECT_ROOT / "docs" / "analytical-views.md"):
        assert p.is_file(), f"missing {p}"


@pytest.mark.parametrize("name", EXPECTED_VIEWS)
def test_each_view_is_defined(views_text, name):
    pattern = rf"CREATE OR REPLACE VIEW\s+{name}\s+AS"
    assert re.search(pattern, views_text, re.IGNORECASE), f"{name} not found"


def test_every_view_reads_fact_sales(views_text):
    # crude per-block check: split on a line starting with the DDL keyword
    # (not a mention of it inside a "--" comment).
    blocks = re.split(r"(?m)(?=^CREATE OR REPLACE VIEW)", views_text)
    view_blocks = [b for b in blocks if b.startswith("CREATE OR REPLACE VIEW")]
    assert len(view_blocks) == len(EXPECTED_VIEWS)
    for block in view_blocks:
        assert "fact_sales" in block


def test_apply_schema_dry_run_includes_views():
    r = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--dry-run"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "views.sql" in r.stdout
    assert "create or replace view daily_kpis" in r.stdout.lower()


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(APPLY_SCRIPT)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pytest.fixture
def _populated(tmp_path, monkeypatch):
    """Apply schema+views, load a small generated file for real."""
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_clean.csv"
    df = gd.generate_dataset(rows=150, seed=13)
    df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")

    db = Database()
    code = orchestrator.run_file(p.incoming / name)
    row = db.fetch_one(
        "SELECT run_id FROM pipeline_runs WHERE file_name = :n "
        "ORDER BY run_id DESC LIMIT 1", {"n": name},
    )
    run_id = row["run_id"] if row else None
    try:
        yield db, df, run_id, code
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id = :r", {"r": run_id})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_all_views_exist_in_information_schema(_populated):
    db, df, run_id, code = _populated
    assert code == 0
    rows = db.fetch_all(
        "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
    )
    present = {r["table_name"] for r in rows}
    assert set(EXPECTED_VIEWS).issubset(present)


@pg_integration
@pytest.mark.parametrize("view", EXPECTED_VIEWS)
def test_view_is_queryable_and_returns_rows(_populated, view):
    db, df, run_id, code = _populated
    assert code == 0
    rows = db.fetch_all(f"SELECT * FROM {view}")
    assert len(rows) >= 1


@pg_integration
def test_daily_kpis_row_count_matches_distinct_order_dates(_populated):
    db, df, run_id, code = _populated
    assert code == 0
    rows = db.fetch_all("SELECT * FROM daily_kpis")
    assert len(rows) == df["Order_Date"].nunique()


@pg_integration
def test_product_and_customer_views_are_complete_not_filtered(_populated):
    db, df, run_id, code = _populated
    assert code == 0

    products = db.fetch_all("SELECT product_id FROM product_performance")
    assert {r["product_id"] for r in products} == set(df["Product_ID"].unique())

    customers = db.fetch_all("SELECT customer_id FROM customer_performance")
    assert {r["customer_id"] for r in customers} == set(df["Customer_ID"].unique())


@pg_integration
def test_regional_performance_revenue_matches_pandas(_populated):
    db, df, run_id, code = _populated
    assert code == 0

    rows = db.fetch_all("SELECT region, revenue FROM regional_performance")
    by_region = {r["region"]: float(r["revenue"]) for r in rows}
    expected = df.groupby("Region")["Revenue"].sum().round(2).to_dict()
    assert set(by_region) == set(expected)
    for region, revenue in expected.items():
        assert abs(by_region[region] - revenue) < 0.05


@pg_integration
def test_category_performance_rank_matches_order_by_revenue(_populated):
    db, df, run_id, code = _populated
    assert code == 0

    rows = db.fetch_all("SELECT revenue, rank_by_revenue FROM category_performance "
                        "ORDER BY rank_by_revenue")
    revenues = [float(r["revenue"]) for r in rows]
    assert revenues == sorted(revenues, reverse=True)
    assert rows[0]["rank_by_revenue"] == 1


@pg_integration
def test_reapply_is_idempotent(_populated):
    db, df, run_id, code = _populated
    assert code == 0
    r = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    rows = db.fetch_all("SELECT count(*) AS n FROM daily_kpis")
    assert rows[0]["n"] >= 1
