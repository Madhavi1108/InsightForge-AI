"""Phase 23 (spec Phase 46, FR-15) - business impact engine.

A pure, on-demand module (like Phase 17's change_detection.py and Phase 22's
root_cause.py) - no database writes, not orchestrator-wired. `compute_gap`
is pure and gets direct unit tests; `assess_business_impact`/`_series` are
exercised against a stub `Database` (mirrors `daily_metric_series`'s and
`fact_sales`'s query shapes) for control-flow assembly, plus a real-Postgres
integration test gated behind `INSIGHTFORGE_PG_INTEGRATION=1` (development
rule 1: never fake functionality).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.impact_analysis import (
    IMPACT_METRICS,
    BusinessImpactResult,
    assess_business_impact,
    assess_business_impact_series,
    compute_gap,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase23_test__"


def test_phase23_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "impact_analysis.py",
        PROJECT_ROOT / "docs" / "business-impact-engine.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.impact_analysis"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# compute_gap
# --------------------------------------------------------------------------- #
def test_compute_gap_shortfall_is_negative_gap_and_positive_risk():
    gap, at_risk = compute_gap(expected=100.0, actual=80.0)
    assert gap == -20.0
    assert at_risk == 20.0


def test_compute_gap_surplus_is_positive_gap_and_zero_risk():
    gap, at_risk = compute_gap(expected=100.0, actual=130.0)
    assert gap == 30.0
    assert at_risk == 0.0


def test_compute_gap_exact_match_is_zero_both():
    gap, at_risk = compute_gap(expected=100.0, actual=100.0)
    assert gap == 0.0
    assert at_risk == 0.0


def test_compute_gap_rounds_to_two_decimals():
    gap, at_risk = compute_gap(expected=10.0 / 3, actual=1.0 / 3)
    assert gap == round(1.0 / 3 - 10.0 / 3, 2)
    assert at_risk == round(10.0 / 3 - 1.0 / 3, 2)


# --------------------------------------------------------------------------- #
# assess_business_impact / assess_business_impact_series - stub DB
# --------------------------------------------------------------------------- #
class _StubDB:
    """Stands in for `Database`: `fetch_all` answers the daily KPI series
    query (`src.change_detection.fetch_period_series`'s day-grain SQL, keyed
    by presence of `"period_key"`... in practice we just always return the
    day series since `daily_metric_series` is the only `fetch_all` caller
    here); `fetch_one` answers the per-date customers/orders-affected query.
    """

    def __init__(self, day_rows: list[dict], affected_by_date: dict[str, tuple[int, int]]):
        self._day_rows = day_rows
        self._affected_by_date = affected_by_date

    def fetch_all(self, sql, params=None):
        return self._day_rows

    def fetch_one(self, sql, params=None):
        date = params["date"]
        customers, orders = self._affected_by_date.get(date, (0, 0))
        return {"customers": customers, "orders": orders}


def _make_series_rows(revenues: list[float], profits: list[float]) -> list[dict]:
    assert len(revenues) == len(profits)
    return [
        {"period_key": f"2026-01-{i + 1:02d}", "revenue": r, "profit": p}
        for i, (r, p) in enumerate(zip(revenues, profits))
    ]


def test_assess_business_impact_none_when_insufficient_history():
    # Fewer rows than ANOMALY_MIN_SAMPLE_SIZE (5) -> detect_rolling_baseline
    # returns [] for both metrics -> no date is covered.
    db = _StubDB(_make_series_rows([100.0, 110.0], [20.0, 22.0]), {})
    assert assess_business_impact(db) is None


def test_assess_business_impact_defaults_to_latest_covered_date():
    # 7 days flat at 100/20, then a shortfall on the 8th day (window=7).
    revenues = [100.0] * 7 + [60.0]
    profits = [20.0] * 7 + [12.0]
    db = _StubDB(
        _make_series_rows(revenues, profits),
        {"2026-01-08": (3, 4)},
    )
    result = assess_business_impact(db, window=7, multiplier=2.0)
    assert isinstance(result, BusinessImpactResult)
    assert result.date == "2026-01-08"
    assert result.expected_revenue == pytest.approx(100.0)
    assert result.actual_revenue == 60.0
    assert result.revenue_gap == pytest.approx(-40.0)
    assert result.revenue_at_risk == pytest.approx(40.0)
    assert result.expected_profit == pytest.approx(20.0)
    assert result.actual_profit == 12.0
    assert result.profit_gap == pytest.approx(-8.0)
    assert result.profit_at_risk == pytest.approx(8.0)
    assert result.customers_affected == 3
    assert result.orders_affected == 4


def test_assess_business_impact_surplus_day_has_zero_at_risk():
    revenues = [100.0] * 7 + [150.0]
    profits = [20.0] * 7 + [30.0]
    db = _StubDB(
        _make_series_rows(revenues, profits),
        {"2026-01-08": (5, 6)},
    )
    result = assess_business_impact(db, window=7, multiplier=2.0)
    assert result.revenue_gap == pytest.approx(50.0)
    assert result.revenue_at_risk == 0.0
    assert result.profit_gap == pytest.approx(10.0)
    assert result.profit_at_risk == 0.0


def test_assess_business_impact_unknown_date_returns_none():
    revenues = [100.0] * 7 + [60.0]
    profits = [20.0] * 7 + [12.0]
    db = _StubDB(_make_series_rows(revenues, profits), {})
    assert assess_business_impact(db, date="2099-12-31", window=7, multiplier=2.0) is None


def test_assess_business_impact_series_covers_every_baseline_date():
    # 7 days of history + 3 more days -> 3 dates get a rolling baseline.
    revenues = [100.0] * 7 + [60.0, 100.0, 140.0]
    profits = [20.0] * 7 + [12.0, 20.0, 28.0]
    db = _StubDB(
        _make_series_rows(revenues, profits),
        {"2026-01-08": (1, 1), "2026-01-09": (2, 2), "2026-01-10": (3, 3)},
    )
    results = assess_business_impact_series(db, window=7, multiplier=2.0)
    assert [r.date for r in results] == ["2026-01-08", "2026-01-09", "2026-01-10"]
    assert results == sorted(results, key=lambda r: r.date)


def test_impact_metrics_is_revenue_and_profit():
    assert IMPACT_METRICS == ("revenue", "profit")


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


@pg_integration
def test_assess_business_impact_against_real_fact_sales(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=1500, seed=41)
    dates = sorted(df["Order_Date"].unique())[:10]

    codes = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d]
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0, 9) for c in codes)  # 9 = DQ reject, still fine here
        result = assess_business_impact(db)
        assert result is not None
        assert result.customers_affected >= 0
        assert result.orders_affected >= 0
        assert result.revenue_at_risk >= 0.0
        assert result.profit_at_risk >= 0.0

        series = assess_business_impact_series(db)
        assert all(isinstance(r, BusinessImpactResult) for r in series)
        assert [r.date for r in series] == sorted(r.date for r in series)
    finally:
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
