"""Phase 17 (spec Phases 33-34, FR-09/FR-10) - period comparison & business
change detection.

The pure core (``_pct_change``, ``_direction``, ``build_change_records``)
needs no database and gets direct unit tests. ``compare_period``/
``compare_all_periods`` query ``fact_sales`` for real, so they're
``INSIGHTFORGE_PG_INTEGRATION=1``-gated, matching every prior phase's
pattern for anything needing real relational data (development rule 1:
never fake functionality).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.change_detection import (
    DEFAULT_THRESHOLD_PCT,
    GRAINS,
    METRICS,
    ChangeRecord,
    _direction,
    _pct_change,
    build_change_records,
    compare_period,
    significant_change_threshold_pct,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase17_test__"


def test_phase17_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "change_detection.py",
        PROJECT_ROOT / "docs" / "change-detection.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.change_detection"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# _direction / _pct_change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "current, previous, expected",
    [
        (110, 100, "up"),
        (90, 100, "down"),
        (100, 100, "flat"),
        (None, 100, "flat"),
        (100, None, "flat"),
    ],
)
def test_direction(current, previous, expected):
    assert _direction(current, previous) == expected


@pytest.mark.parametrize(
    "current, previous, expected",
    [
        (118.4, 100.0, 18.4),
        (81.6, 100.0, -18.4),
        (100.0, 100.0, 0.0),
    ],
)
def test_pct_change_normal(current, previous, expected):
    assert _pct_change(current, previous) == expected


def test_pct_change_undefined_when_previous_zero():
    assert _pct_change(100, 0) is None
    assert _pct_change(0, 0) is None


def test_pct_change_undefined_when_either_side_none():
    assert _pct_change(None, 100) is None
    assert _pct_change(100, None) is None


# --------------------------------------------------------------------------- #
# build_change_records
# --------------------------------------------------------------------------- #
def test_build_change_records_above_threshold_is_significant():
    cur = {"revenue": 118.4}
    prev = {"revenue": 100.0}
    [rec] = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23",
                                 threshold_pct=10.0)
    assert isinstance(rec, ChangeRecord)
    assert rec.pct_change == 18.4
    assert rec.direction == "up"
    assert rec.magnitude == 18.4
    assert rec.significant is True


def test_build_change_records_below_threshold_is_not_significant():
    cur = {"revenue": 105.0}
    prev = {"revenue": 100.0}
    [rec] = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23",
                                 threshold_pct=10.0)
    assert rec.pct_change == 5.0
    assert rec.significant is False


def test_build_change_records_zero_to_nonzero_is_significant_with_no_pct():
    cur = {"profit": 50.0}
    prev = {"profit": 0.0}
    [rec] = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23")
    assert rec.pct_change is None
    assert rec.magnitude is None
    assert rec.direction == "up"
    assert rec.significant is True


def test_build_change_records_zero_to_zero_is_not_significant():
    cur = {"profit": 0.0}
    prev = {"profit": 0.0}
    [rec] = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23")
    assert rec.pct_change is None
    assert rec.significant is False
    assert rec.direction == "flat"


def test_build_change_records_skips_metrics_missing_from_either_row():
    cur = {"revenue": 100.0, "profit": 20.0}
    prev = {"revenue": 90.0}
    recs = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23")
    assert {r.metric for r in recs} == {"revenue"}


def test_build_change_records_covers_all_present_metrics():
    cur = {m: 1.0 for m in METRICS}
    prev = {m: 1.0 for m in METRICS}
    recs = build_change_records(cur, prev, "month", "2026-02-01", "2026-01-01")
    assert {r.metric for r in recs} == set(METRICS)
    assert all(r.grain == "month" for r in recs)
    assert all(r.period_key == "2026-02-01" for r in recs)
    assert all(r.previous_period_key == "2026-01-01" for r in recs)


# --------------------------------------------------------------------------- #
# threshold configuration
# --------------------------------------------------------------------------- #
def test_default_threshold(monkeypatch):
    monkeypatch.delenv("CHANGE_DETECTION_THRESHOLD_PCT", raising=False)
    assert significant_change_threshold_pct() == DEFAULT_THRESHOLD_PCT


def test_threshold_honours_env(monkeypatch):
    monkeypatch.setenv("CHANGE_DETECTION_THRESHOLD_PCT", "25.0")
    assert significant_change_threshold_pct() == 25.0

    cur = {"revenue": 120.0}
    prev = {"revenue": 100.0}
    [rec] = build_change_records(cur, prev, "day", "2026-02-24", "2026-02-23")
    assert rec.significant is False  # 20% move, below the 25% threshold


def test_invalid_threshold_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CHANGE_DETECTION_THRESHOLD_PCT", "not-a-number")
    assert significant_change_threshold_pct() == DEFAULT_THRESHOLD_PCT


# --------------------------------------------------------------------------- #
# compare_period - control flow (fewer than 2 periods, unknown grain)
# --------------------------------------------------------------------------- #
class _EmptyDB:
    def fetch_all(self, sql, params=None):
        return [{"period_key": "2026-02-24", "revenue": 100.0}]


def test_compare_period_returns_empty_with_fewer_than_two_periods():
    assert compare_period(_EmptyDB(), "day") == []


def test_compare_period_rejects_unknown_grain():
    with pytest.raises(ValueError):
        compare_period(_EmptyDB(), "quarter")


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
def _two_days_loaded(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=200, seed=17)
    dates = sorted(df["Order_Date"].unique())
    day_a, day_b = dates[len(dates) // 3], dates[2 * len(dates) // 3]
    df_a = df[df["Order_Date"] == day_a]
    df_b = df[df["Order_Date"] == day_b]

    name_a, name_b = f"{SENTINEL}_a.csv", f"{SENTINEL}_b.csv"
    df_a.to_csv(p.incoming / name_a, index=False, date_format="%Y-%m-%d")
    codes = [orchestrator.run_file(p.incoming / name_a)]
    df_b.to_csv(p.incoming / name_b, index=False, date_format="%Y-%m-%d")
    codes.append(orchestrator.run_file(p.incoming / name_b))

    try:
        yield db, df_a, df_b, day_a, day_b, codes
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_compare_period_day_matches_pandas(_two_days_loaded):
    db, df_a, df_b, day_a, day_b, codes = _two_days_loaded
    assert all(c == 0 for c in codes)

    recs = compare_period(db, "day")
    by_metric = {r.metric: r for r in recs}
    assert "revenue" in by_metric

    expected_current = round(float(df_b["Revenue"].sum()), 2)
    expected_previous = round(float(df_a["Revenue"].sum()), 2)
    rec = by_metric["revenue"]
    assert abs(float(rec.current) - expected_current) < 0.05
    assert abs(float(rec.previous) - expected_previous) < 0.05
    assert rec.period_key == str(day_b)
    assert rec.previous_period_key == str(day_a)


@pg_integration
def test_compare_period_all_grains_run_without_error(_two_days_loaded):
    db, df_a, df_b, day_a, day_b, codes = _two_days_loaded
    assert all(c == 0 for c in codes)
    for grain in GRAINS:
        recs = compare_period(db, grain)
        assert isinstance(recs, list)
