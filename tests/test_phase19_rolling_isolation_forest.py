"""Phase 19 (spec Phases 37-38, part of FR-11) - rolling baseline &
Isolation Forest anomaly detection.

The pure core (``detect_rolling_baseline``, ``detect_isolation_forest``)
needs no database and gets direct unit tests. The DB-querying wrapper needs
real ``fact_sales`` data, so it's ``INSIGHTFORGE_PG_INTEGRATION=1``-gated,
matching every prior phase's pattern (development rule 1: never fake
functionality). This module never touches the database itself - fusion +
persistence to ``anomalies`` is Phase 20.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.anomaly_detection import (
    ANOMALY_MIN_SAMPLE_SIZE,
    DEFAULT_IFOREST_RANDOM_STATE,
    DEFAULT_ROLLING_MULTIPLIER,
    DEFAULT_ROLLING_WINDOW_DAYS,
    IsolationForestResult,
    RollingBaselineResult,
    detect_isolation_forest,
    detect_rolling_baseline,
    iforest_contamination,
    iforest_random_state,
    rolling_baseline_multiplier,
    rolling_window_days,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase19_test__"


def _series(*values, start="2026-01-01"):
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def test_phase19_files_exist():
    assert (PROJECT_ROOT / "src" / "anomaly_detection.py").is_file()
    assert (PROJECT_ROOT / "docs" / "anomaly-detection.md").is_file()


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.anomaly_detection"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# detect_rolling_baseline
# --------------------------------------------------------------------------- #
def test_rolling_baseline_flat_series_flags_nothing():
    series = _series(*([100] * 15))
    results = detect_rolling_baseline(series, "revenue")
    assert len(results) == 15 - DEFAULT_ROLLING_WINDOW_DAYS
    assert all(not r.is_anomaly for r in results)


def test_rolling_baseline_omits_first_window_days():
    series = _series(*range(1, 16))  # 15 points
    results = detect_rolling_baseline(series, "revenue", window=7)
    dates_covered = {r.date for r in results}
    first_window_dates = {d for d, _ in series[:7]}
    assert not (dates_covered & first_window_dates)
    assert len(results) == len(series) - 7


def test_rolling_baseline_flags_spike_after_history_exists():
    values = [100, 101, 99, 100, 102, 98, 100, 101, 99, 500, 100, 101]
    series = _series(*values)
    results = detect_rolling_baseline(series, "revenue", window=7)
    by_date = {r.date: r for r in results}
    spike_date = series[9][0]
    assert by_date[spike_date].is_anomaly is True


def test_rolling_baseline_below_min_sample_returns_empty():
    series = _series(100, 101, 102)
    assert len(series) < ANOMALY_MIN_SAMPLE_SIZE
    assert detect_rolling_baseline(series, "revenue") == []


def test_rolling_baseline_insufficient_window_returns_empty():
    series = _series(*([100] * 6))  # >= MIN_SAMPLE_SIZE but <= default window (7)
    assert detect_rolling_baseline(series, "revenue") == []


def test_rolling_baseline_result_type_and_bounds():
    values = [100, 101, 99, 100, 102, 98, 100, 105]
    series = _series(*values)
    [r] = detect_rolling_baseline(series, "revenue", window=7)
    assert isinstance(r, RollingBaselineResult)
    assert r.lower_bound == pytest.approx(r.rolling_mean - DEFAULT_ROLLING_MULTIPLIER * r.rolling_std, abs=1e-3)
    assert r.upper_bound == pytest.approx(r.rolling_mean + DEFAULT_ROLLING_MULTIPLIER * r.rolling_std, abs=1e-3)


def test_rolling_baseline_window_and_multiplier_env_overrides(monkeypatch):
    monkeypatch.setenv("ROLLING_WINDOW_DAYS", "3")
    monkeypatch.setenv("ROLLING_BASELINE_MULTIPLIER", "1.0")
    assert rolling_window_days() == 3
    assert rolling_baseline_multiplier() == 1.0

    series = _series(100, 101, 99, 100, 200)
    results = detect_rolling_baseline(series, "revenue")
    assert len(results) == 2
    assert results[-1].is_anomaly is True


# --------------------------------------------------------------------------- #
# detect_isolation_forest
# --------------------------------------------------------------------------- #
def test_isolation_forest_flags_obvious_outlier():
    values = [100, 101, 99, 100, 102, 98, 100, 101, 99, 100000, 100, 101]
    series = _series(*values)
    results = detect_isolation_forest(series, "revenue")
    assert len(results) == len(series)
    by_date = {r.date: r for r in results}
    outlier_date = series[9][0]
    outlier = by_date[outlier_date]
    assert outlier.is_anomaly is True
    assert outlier.prediction == "anomaly"
    assert 0.0 <= outlier.confidence <= 1.0


def test_isolation_forest_below_min_sample_returns_empty():
    series = _series(100, 101, 102)
    assert detect_isolation_forest(series, "revenue") == []


def test_isolation_forest_result_type():
    series = _series(100, 101, 99, 100, 102, 98, 100)
    [r] = detect_isolation_forest(series, "revenue")[:1]
    assert isinstance(r, IsolationForestResult)
    assert r.prediction in ("anomaly", "normal")


def test_isolation_forest_is_deterministic_with_fixed_random_state():
    values = [100, 101, 99, 100, 102, 98, 100, 101, 99, 100000, 100, 101]
    series = _series(*values)
    r1 = detect_isolation_forest(series, "revenue", random_state=DEFAULT_IFOREST_RANDOM_STATE)
    r2 = detect_isolation_forest(series, "revenue", random_state=DEFAULT_IFOREST_RANDOM_STATE)
    assert r1 == r2


def test_isolation_forest_contamination_and_random_state_env(monkeypatch):
    monkeypatch.setenv("IFOREST_CONTAMINATION", "0.1")
    monkeypatch.setenv("IFOREST_RANDOM_STATE", "7")
    assert iforest_contamination() == 0.1
    assert iforest_random_state() == 7


def test_iforest_contamination_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("IFOREST_CONTAMINATION", raising=False)
    assert iforest_contamination() == "auto"


def test_iforest_invalid_contamination_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("IFOREST_CONTAMINATION", "not-a-number")
    assert iforest_contamination() == "auto"


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
def test_detect_all_flags_injected_extreme_day(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.anomaly_detection import detect_all_isolation_forest, detect_all_rolling_baseline
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=500, seed=23)
    dates = sorted(df["Order_Date"].unique())[:10]
    codes = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        if i == len(dates) - 1:
            day_df["Revenue"] = day_df["Revenue"] * 50
            day_df["Profit"] = day_df["Revenue"] - day_df["Cost"]
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        # The injected day's 50x revenue also fails the DQ accuracy check,
        # correctly dropping that run's DQ score below the REJECT gate (9) -
        # this test only needs the anomaly detectors to fire, not every run
        # to have "succeeded".
        assert all(c in (0, 9) for c in codes)
        rolling = detect_all_rolling_baseline(db, metrics=("revenue",))
        iforest = detect_all_isolation_forest(db, metrics=("revenue",))
        assert any(r.is_anomaly for r in rolling["revenue"])
        assert any(r.is_anomaly for r in iforest["revenue"])
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
