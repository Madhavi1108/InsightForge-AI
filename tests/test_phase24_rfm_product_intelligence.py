"""Phase 24 (spec Phases 47-48, FR-16/FR-17) - customer RFM analysis &
product intelligence.

Two pure, on-demand modules (like Phase 17/22/23 before them) - no database
writes, not orchestrator-wired, covered by one test file (same precedent as
Phase 19 combining two spec phases into `test_phase19_rolling_isolation
_forest.py`). Pure math gets direct unit tests; the DB-querying wrappers
need real `fact_sales`/`dim_customer`/`dim_product`/`product_performance`
data, so those tests are `INSIGHTFORGE_PG_INTEGRATION=1`-gated (development
rule 1: never fake functionality).
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.product_intelligence import (
    DEFAULT_GROWTH_THRESHOLD_PCT,
    DEFAULT_GROWTH_WINDOW_DAYS,
    ProductIntelligence,
    classify_products,
    growth_threshold_pct,
    growth_window_days,
    percentile_rank,
)
from src.rfm import (
    SEGMENTS,
    CustomerRFM,
    build_customer_rfm,
    classify_segment,
    score_quintiles,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase24_test__"


def test_phase24_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "rfm.py",
        PROJECT_ROOT / "src" / "product_intelligence.py",
        PROJECT_ROOT / "docs" / "customer-rfm-analysis.md",
        PROJECT_ROOT / "docs" / "product-intelligence.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    for module in ("src.rfm", "src.product_intelligence"):
        r = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
        )
        assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# src.rfm - score_quintiles / classify_segment (pure)
# --------------------------------------------------------------------------- #
def test_score_quintiles_empty_is_empty():
    assert score_quintiles([]) == []


def test_score_quintiles_zero_spread_scores_neutral():
    assert score_quintiles([10.0] * 6) == [3] * 6


def test_score_quintiles_spans_1_to_5():
    values = list(range(1, 51))
    scores = score_quintiles([float(v) for v in values])
    assert set(scores) <= {1, 2, 3, 4, 5}
    assert min(scores) == 1
    assert max(scores) == 5
    # ascending values -> non-decreasing scores
    assert scores == sorted(scores)


def test_score_quintiles_reverse_flips_the_scale():
    values = [float(v) for v in range(1, 51)]
    forward = score_quintiles(values)
    reverse = score_quintiles(values, reverse=True)
    assert reverse == [6 - s for s in forward]


def test_classify_segment_champions():
    assert classify_segment(5, 5, 5) == "Champions"


def test_classify_segment_loyal():
    assert classify_segment(2, 4, 3) == "Loyal"


def test_classify_segment_at_risk():
    assert classify_segment(1, 3, 1) == "At Risk"


def test_classify_segment_lost():
    assert classify_segment(1, 1, 1) == "Lost"


def test_classify_segment_new():
    assert classify_segment(5, 1, 1) == "New"


def test_classify_segment_potential_loyalists_catch_all():
    assert classify_segment(3, 3, 3) == "Potential Loyalists"


def test_classify_segment_always_returns_one_of_the_six():
    for r in range(1, 6):
        for f in range(1, 6):
            for m in range(1, 6):
                assert classify_segment(r, f, m) in SEGMENTS


# --------------------------------------------------------------------------- #
# src.rfm - build_customer_rfm (pure, batch)
# --------------------------------------------------------------------------- #
def _customer_row(cid, name, segment, last_order_date, frequency, monetary):
    return {
        "customer_id": cid, "customer_name": name, "customer_segment": segment,
        "last_order_date": last_order_date, "frequency": frequency, "monetary": monetary,
    }


def test_build_customer_rfm_empty_rows():
    assert build_customer_rfm([], dt.date(2026, 1, 1)) == []


def test_build_customer_rfm_assigns_champions_to_best_customer():
    reference = dt.date(2026, 9, 8)
    rows = [
        _customer_row("C1", "Alice", "Consumer", dt.date(2026, 9, 7), 20, 50000.0),  # best on all 3
        _customer_row("C2", "Bob", "Consumer", dt.date(2026, 1, 1), 1, 100.0),        # worst on all 3
        _customer_row("C3", "Carol", "Corporate", dt.date(2026, 6, 1), 8, 8000.0),
        _customer_row("C4", "Dan", "Corporate", dt.date(2026, 7, 1), 5, 4000.0),
        _customer_row("C5", "Eve", "Home Office", dt.date(2026, 8, 1), 10, 12000.0),
    ]
    results = build_customer_rfm(rows, reference)
    assert len(results) == 5
    by_id = {r.customer_id: r for r in results}
    assert isinstance(by_id["C1"], CustomerRFM)
    assert by_id["C1"].segment == "Champions"
    assert by_id["C2"].segment == "Lost"
    assert by_id["C1"].recency_days == 1
    assert by_id["C2"].recency_days == (reference - dt.date(2026, 1, 1)).days


# --------------------------------------------------------------------------- #
# src.rfm - analyze_customer_rfm (stub DB)
# --------------------------------------------------------------------------- #
class _RfmStubDB:
    def __init__(self, rows, reference_date):
        self._rows = rows
        self._reference_date = reference_date

    def fetch_all(self, sql, params=None):
        return self._rows

    def scalar(self, sql, params=None):
        return self._reference_date


def test_analyze_customer_rfm_empty_db_returns_empty_list():
    from src.rfm import analyze_customer_rfm
    db = _RfmStubDB([], None)
    assert analyze_customer_rfm(db) == []


def test_analyze_customer_rfm_uses_db_reference_date_by_default():
    from src.rfm import analyze_customer_rfm
    reference = dt.date(2026, 9, 8)
    rows = [
        _customer_row("C1", "Alice", "Consumer", dt.date(2026, 9, 7), 20, 50000.0),
        _customer_row("C2", "Bob", "Consumer", dt.date(2026, 1, 1), 1, 100.0),
    ]
    db = _RfmStubDB(rows, reference)
    results = analyze_customer_rfm(db)
    assert {r.customer_id for r in results} == {"C1", "C2"}


# --------------------------------------------------------------------------- #
# src.product_intelligence - percentile_rank (pure)
# --------------------------------------------------------------------------- #
def test_percentile_rank_empty_is_empty():
    assert percentile_rank([]) == []


def test_percentile_rank_zero_spread_is_neutral():
    assert percentile_rank([5.0, 5.0, 5.0]) == [0.5, 0.5, 0.5]


def test_percentile_rank_spans_0_to_1():
    ranks = percentile_rank([10.0, 20.0, 30.0, 40.0, 50.0])
    assert ranks[0] == 0.0
    assert ranks[-1] == 1.0
    assert ranks == sorted(ranks)


def test_percentile_rank_ties_share_average_rank():
    ranks = percentile_rank([10.0, 10.0, 20.0])
    assert ranks[0] == ranks[1]


# --------------------------------------------------------------------------- #
# src.product_intelligence - classify_products (pure, batch)
# --------------------------------------------------------------------------- #
def _product_row(pid, revenue, profit, margin_pct, units, return_rate_pct):
    return {
        "product_id": pid, "product_name": f"{pid} name", "category": "Electronics",
        "revenue": revenue, "profit": profit, "margin_pct": margin_pct,
        "units": units, "orders": units, "return_rate_pct": return_rate_pct,
    }


def _five_product_totals():
    return [
        _product_row("P0", 1000.0, 300.0, 30.0, 100, 2.0),
        _product_row("P1", 500.0, 100.0, 20.0, 50, 5.0),
        _product_row("P2", 2000.0, 800.0, 40.0, 200, 1.0),
        _product_row("P3", 100.0, -20.0, -10.0, 10, 20.0),
        _product_row("P4", 1500.0, 400.0, 25.0, 150, 3.0),
    ]


def test_classify_products_empty_totals():
    assert classify_products([], {}) == []


def test_classify_products_star_is_high_revenue_high_profit_fast_growing():
    totals = _five_product_totals()
    growth = {"P0": 25.0, "P1": -25.0, "P2": 30.0, "P3": None, "P4": 0.0}
    results = classify_products(totals, growth)
    by_id = {r.product_id: r for r in results}
    assert isinstance(by_id["P2"], ProductIntelligence)
    assert by_id["P2"].is_star
    assert by_id["P2"].is_high_revenue
    assert by_id["P2"].is_high_profit
    assert by_id["P2"].is_fast_growing


def test_classify_products_worst_product_is_low_margin_and_high_return():
    totals = _five_product_totals()
    growth = {"P0": 25.0, "P1": -25.0, "P2": 30.0, "P3": None, "P4": 0.0}
    results = classify_products(totals, growth)
    by_id = {r.product_id: r for r in results}
    assert by_id["P3"].is_low_margin
    assert by_id["P3"].is_high_return
    assert by_id["P3"].is_slow_moving
    assert not by_id["P3"].is_star


def test_classify_products_declining_flag():
    totals = _five_product_totals()
    growth = {"P0": 25.0, "P1": -25.0, "P2": 30.0, "P3": None, "P4": 0.0}
    results = classify_products(totals, growth)
    by_id = {r.product_id: r for r in results}
    assert by_id["P1"].is_declining
    assert by_id["P1"].revenue_growth_pct == -25.0


def test_classify_products_none_growth_is_undefined_not_fast_or_declining():
    totals = _five_product_totals()
    growth = {"P0": 25.0, "P1": -25.0, "P2": 30.0, "P3": None, "P4": 0.0}
    results = classify_products(totals, growth)
    by_id = {r.product_id: r for r in results}
    assert by_id["P3"].revenue_growth_pct is None
    assert not by_id["P3"].is_fast_growing
    assert not by_id["P3"].is_declining


def test_classify_products_health_score_bounds_and_ordering():
    totals = _five_product_totals()
    growth = {"P0": 25.0, "P1": -25.0, "P2": 30.0, "P3": None, "P4": 0.0}
    results = classify_products(totals, growth)
    by_id = {r.product_id: r for r in results}
    for r in results:
        assert 0.0 <= r.health_score <= 100.0
    # P2 (best on every axis) should score higher than P3 (worst on every axis).
    assert by_id["P2"].health_score > by_id["P3"].health_score


def test_growth_window_and_threshold_defaults(monkeypatch):
    monkeypatch.delenv("PRODUCT_GROWTH_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("PRODUCT_GROWTH_THRESHOLD_PCT", raising=False)
    assert growth_window_days() == DEFAULT_GROWTH_WINDOW_DAYS
    assert growth_threshold_pct() == DEFAULT_GROWTH_THRESHOLD_PCT


def test_growth_window_and_threshold_honour_env(monkeypatch):
    monkeypatch.setenv("PRODUCT_GROWTH_WINDOW_DAYS", "14")
    monkeypatch.setenv("PRODUCT_GROWTH_THRESHOLD_PCT", "15.0")
    assert growth_window_days() == 14
    assert growth_threshold_pct() == 15.0


# --------------------------------------------------------------------------- #
# src.product_intelligence - analyze_product_intelligence (stub DB)
# --------------------------------------------------------------------------- #
class _ProductStubDB:
    def __init__(self, totals, reference_date, growth_rows):
        self._totals = totals
        self._reference_date = reference_date
        self._growth_rows = growth_rows

    def fetch_all(self, sql, params=None):
        if "product_performance" in sql:
            return self._totals
        return self._growth_rows

    def scalar(self, sql, params=None):
        return self._reference_date


def test_analyze_product_intelligence_empty_db_returns_empty_list():
    from src.product_intelligence import analyze_product_intelligence
    db = _ProductStubDB([], None, [])
    assert analyze_product_intelligence(db) == []


def test_analyze_product_intelligence_assembles_results():
    from src.product_intelligence import analyze_product_intelligence
    totals = _five_product_totals()
    growth_rows = [
        {"product_id": "P0", "current_revenue": 125.0, "previous_revenue": 100.0},
        {"product_id": "P2", "current_revenue": 130.0, "previous_revenue": 100.0},
    ]
    db = _ProductStubDB(totals, dt.date(2026, 9, 8), growth_rows)
    results = analyze_product_intelligence(db)
    assert {r.product_id for r in results} == {"P0", "P1", "P2", "P3", "P4"}
    by_id = {r.product_id: r for r in results}
    assert by_id["P0"].revenue_growth_pct == 25.0
    assert by_id["P1"].revenue_growth_pct is None  # not in growth_rows


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


def _generate_and_ingest(p, seed):
    from scripts import generate_dataset as gd
    from src import orchestrator

    df = gd.generate_dataset(rows=1500, seed=seed)
    dates = sorted(df["Order_Date"].unique())[:10]
    codes = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d]
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))
    return codes


def _cleanup(db):
    db.execute("DELETE FROM anomalies WHERE run_id IN "
               "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
               {"p": SENTINEL + "%"})
    db.execute("DELETE FROM drift_results WHERE run_id IN "
               "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
               {"p": SENTINEL + "%"})
    db.execute("DELETE FROM fact_sales WHERE run_id IN "
               "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
               {"p": SENTINEL + "%"})
    db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
    db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_analyze_customer_rfm_against_real_fact_sales(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.database import Database
    from src.rfm import SEGMENTS, analyze_customer_rfm

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    codes = _generate_and_ingest(p, seed=43)
    try:
        assert all(c in (0, 9) for c in codes)
        results = analyze_customer_rfm(db)
        assert results
        assert all(r.segment in SEGMENTS for r in results)
    finally:
        _cleanup(db)


@pg_integration
def test_analyze_product_intelligence_against_real_fact_sales(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.database import Database
    from src.product_intelligence import analyze_product_intelligence

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    codes = _generate_and_ingest(p, seed=47)
    try:
        assert all(c in (0, 9) for c in codes)
        results = analyze_product_intelligence(db)
        assert results
        for r in results:
            assert 0.0 <= r.health_score <= 100.0
    finally:
        _cleanup(db)
