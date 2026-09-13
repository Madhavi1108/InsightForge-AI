"""Phase 15 (spec Phases 30-31) - core SQL KPI engine & advanced SQL
analytics.

No Python module exists for this phase (the spec's only artifact is
``sql/kpi_queries.sql``), so - matching ``tests/test_phase08_schema.py``'s
precedent for a pure-``.sql`` deliverable - the static checks below parse the
file and pass with no database; the correctness checks apply the real schema,
run the pipeline to populate ``fact_sales``, and cross-check results against
independently-computed pandas aggregates. They are skipped unless
``INSIGHTFORGE_PG_INTEGRATION=1`` (development rule 1: never fake
functionality).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KPI_SQL = PROJECT_ROOT / "sql" / "kpi_queries.sql"

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase15_test__"

EXPECTED_QUERIES = [
    "core_kpis_overall", "core_kpis_daily", "core_kpis_monthly",
    "regional_performance", "category_performance_ranked",
    "top_products_by_revenue", "customer_performance",
    "monthly_revenue_trend", "rolling_7day_avg_revenue", "running_total_revenue",
]

_MARKER_RE = re.compile(r"--\s*@query:\s*(\w+)\b")


def parse_queries(sql_text: str) -> dict[str, str]:
    """Split the file into {name: sql_text} by ``-- @query: <name>`` markers."""
    matches = list(_MARKER_RE.finditer(sql_text))
    queries: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(sql_text)
        queries[m.group(1)] = sql_text[start:end].strip()
    return queries


@pytest.fixture(scope="module")
def sql_text() -> str:
    return KPI_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def queries(sql_text: str) -> dict[str, str]:
    return parse_queries(sql_text)


# --------------------------------------------------------------------------- #
# files / static structure
# --------------------------------------------------------------------------- #
def test_phase15_files_exist():
    for p in (KPI_SQL, PROJECT_ROOT / "docs" / "kpi-engine.md"):
        assert p.is_file(), f"missing {p}"


def test_all_expected_queries_present(queries):
    assert set(queries) == set(EXPECTED_QUERIES)


@pytest.mark.parametrize("name", EXPECTED_QUERIES)
def test_each_query_is_nonempty_select(queries, name):
    text = queries[name].upper()
    assert "SELECT" in text
    assert text.rstrip().endswith(";")


@pytest.mark.parametrize(
    "technique, pattern",
    [
        ("JOIN", r"\bJOIN\b"),
        ("CASE/FILTER", r"\bFILTER\s*\("),
        ("CTE", r"\bWITH\b"),
        ("subquery", r"\(\s*SELECT\b"),
        ("window function", r"\bOVER\s*\("),
        ("RANK", r"\bRANK\s*\("),
        ("DENSE_RANK", r"\bDENSE_RANK\s*\("),
        ("LAG", r"\bLAG\s*\("),
        ("LEAD", r"\bLEAD\s*\("),
        ("rolling average", r"\bAVG\s*\([^)]*\)\s*OVER\s*\(\s*ORDER BY[^)]*ROWS BETWEEN"),
        ("running total", r"\bSUM\s*\([^)]*\)\s*OVER\s*\(\s*ORDER BY"),
    ],
)
def test_every_technique_is_demonstrated(sql_text, technique, pattern):
    assert re.search(pattern, sql_text, re.IGNORECASE), f"{technique} not found"


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pytest.fixture
def _populated_fact_sales(tmp_path, monkeypatch, queries):
    """Apply the schema, load a small generated file for real, yield (db, df, run_id)."""
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_clean.csv"
    df = gd.generate_dataset(rows=120, seed=11)
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
def test_core_kpis_overall_matches_pandas(_populated_fact_sales, queries):
    db, df, run_id, code = _populated_fact_sales
    assert code == 0

    result = db.fetch_all(queries["core_kpis_overall"])
    assert len(result) == 1
    row = result[0]

    expected_revenue = round(float(df["Revenue"].sum()), 2)
    expected_orders = df["Order_ID"].nunique()
    expected_customers = df["Customer_ID"].nunique()
    expected_units = int(df["Quantity"].sum())
    expected_returned = int((df["Return_Status"] == "Returned").sum())
    expected_return_rate = round(100.0 * expected_returned / len(df), 2)
    expected_avg_discount = round(100.0 * float(df["Discount"].mean()), 2)
    shipping = df["Shipping_Days"].dropna()
    expected_shipping = round(float(shipping.mean()), 2) if len(shipping) else None

    assert abs(float(row["revenue"]) - expected_revenue) < 0.05
    assert row["orders"] == expected_orders
    assert row["customers"] == expected_customers
    assert row["units"] == expected_units
    assert abs(float(row["return_rate_pct"]) - expected_return_rate) < 0.05
    assert abs(float(row["avg_discount_pct"]) - expected_avg_discount) < 0.05
    if expected_shipping is not None:
        assert abs(float(row["avg_shipping_days"]) - expected_shipping) < 0.05


@pg_integration
def test_category_and_customer_rank_matches_order_by_revenue(_populated_fact_sales, queries):
    db, df, run_id, code = _populated_fact_sales
    assert code == 0

    cats = db.fetch_all(queries["category_performance_ranked"])
    revenues = [float(r["revenue"]) for r in cats]
    assert revenues == sorted(revenues, reverse=True)
    assert cats[0]["rank_by_revenue"] == 1

    customers = db.fetch_all(queries["customer_performance"])
    if customers:
        revenues = [float(r["revenue"]) for r in customers]
        assert revenues == sorted(revenues, reverse=True)
        assert customers[0]["rank_by_revenue"] == 1


@pg_integration
def test_running_total_is_non_decreasing(_populated_fact_sales, queries):
    db, df, run_id, code = _populated_fact_sales
    assert code == 0

    rows = db.fetch_all(queries["running_total_revenue"])
    totals = [float(r["running_total_revenue"]) for r in rows]
    assert totals == sorted(totals)


@pg_integration
@pytest.mark.parametrize("name", EXPECTED_QUERIES)
def test_every_query_executes_and_returns_rows(_populated_fact_sales, queries, name):
    db, df, run_id, code = _populated_fact_sales
    assert code == 0
    rows = db.fetch_all(queries[name])
    assert len(rows) >= 1
