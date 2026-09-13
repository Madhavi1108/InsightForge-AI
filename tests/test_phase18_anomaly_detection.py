"""Phase 18 (spec Phases 35-36, part of FR-11) - Z-score & IQR anomaly
detection.

The pure core (``detect_zscore``, ``detect_iqr``) needs no database and gets
direct unit tests. The DB-querying wrapper (``detect_all_zscore``,
``detect_all_iqr``, ``daily_metric_series``) needs real ``fact_sales`` data,
so it's ``INSIGHTFORGE_PG_INTEGRATION=1``-gated, matching every prior
phase's pattern (development rule 1: never fake functionality). This module
never touches the database itself - fusion + persistence to ``anomalies``
is Phase 20.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.anomaly_detection import (
    ANOMALY_MIN_SAMPLE_SIZE,
    DEFAULT_IQR_MULTIPLIER,
    DEFAULT_ZSCORE_THRESHOLD,
    IqrResult,
    ZScoreResult,
    daily_metric_series,
    detect_iqr,
    detect_zscore,
    iqr_multiplier,
    zscore_threshold,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase18_test__"


def _series(*values, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def test_phase18_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "anomaly_detection.py",
        PROJECT_ROOT / "docs" / "anomaly-detection.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.anomaly_detection"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# detect_zscore
# --------------------------------------------------------------------------- #
def test_zscore_flags_obvious_outlier():
    # A single outlier's max achievable |z| among n points is bounded by
    # ~(n-1)/sqrt(n) regardless of how extreme its value is - use enough
    # baseline points that an extreme outlier can clear the default threshold.
    baseline = [100, 102, 98, 101, 99, 100, 101, 99, 100, 102,
                98, 101, 99, 100, 101, 99, 100, 102, 98]
    series = _series(*baseline, 100000)
    results = detect_zscore(series, "revenue")
    assert len(results) == len(baseline) + 1
    last = results[-1]
    assert last.value == 100000
    assert last.is_anomaly is True
    assert last.z_score is not None and last.z_score > DEFAULT_ZSCORE_THRESHOLD


def test_zscore_tight_series_flags_nothing():
    series = _series(100, 101, 99, 100, 102, 98, 101)
    results = detect_zscore(series, "revenue")
    assert len(results) == 7
    assert all(not r.is_anomaly for r in results)


def test_zscore_constant_series_has_no_score():
    series = _series(100, 100, 100, 100, 100)
    results = detect_zscore(series, "revenue")
    assert all(r.z_score is None for r in results)
    assert all(r.std == 0.0 for r in results)
    assert all(not r.is_anomaly for r in results)


def test_zscore_below_min_sample_size_returns_empty():
    series = _series(100, 200, 300)
    assert len(series) < ANOMALY_MIN_SAMPLE_SIZE
    assert detect_zscore(series, "revenue") == []


def test_zscore_result_type_and_fields():
    series = _series(100, 101, 99, 100, 102)
    [r] = detect_zscore(series, "revenue")[:1]
    assert isinstance(r, ZScoreResult)
    assert r.metric == "revenue"
    assert r.threshold == DEFAULT_ZSCORE_THRESHOLD


def test_zscore_threshold_override_changes_flagged_set():
    series = _series(100, 102, 98, 101, 99, 100, 130)
    default_results = detect_zscore(series, "revenue")
    loose_results = detect_zscore(series, "revenue", threshold=0.5)
    assert not any(r.is_anomaly for r in default_results)
    assert any(r.is_anomaly for r in loose_results)


# --------------------------------------------------------------------------- #
# detect_iqr
# --------------------------------------------------------------------------- #
def test_iqr_flags_outlier_beyond_bounds():
    series = _series(10, 11, 9, 10, 12, 8, 500)
    results = detect_iqr(series, "revenue")
    assert len(results) == 7
    last = results[-1]
    assert last.value == 500
    assert last.is_outlier is True
    assert last.value > last.upper_bound


def test_iqr_tight_series_flags_nothing():
    series = _series(10, 11, 9, 10, 12, 8, 10)
    results = detect_iqr(series, "revenue")
    assert all(not r.is_outlier for r in results)


def test_iqr_below_min_sample_size_returns_empty():
    series = _series(10, 20, 30)
    assert detect_iqr(series, "revenue") == []


def test_iqr_result_type_and_fields():
    series = _series(10, 11, 9, 10, 12)
    [r] = detect_iqr(series, "revenue")[:1]
    assert isinstance(r, IqrResult)
    assert r.metric == "revenue"
    assert r.iqr == round(r.q3 - r.q1, 4)
    assert r.lower_bound == round(r.q1 - DEFAULT_IQR_MULTIPLIER * r.iqr, 4)
    assert r.upper_bound == round(r.q3 + DEFAULT_IQR_MULTIPLIER * r.iqr, 4)


def test_iqr_multiplier_override_changes_flagged_set():
    series = _series(10, 11, 9, 10, 12, 8, 14)
    default_results = detect_iqr(series, "revenue")
    strict_results = detect_iqr(series, "revenue", multiplier=0.1)
    assert not any(r.is_outlier for r in default_results)
    assert any(r.is_outlier for r in strict_results)


# --------------------------------------------------------------------------- #
# threshold / multiplier configuration
# --------------------------------------------------------------------------- #
def test_default_zscore_threshold(monkeypatch):
    monkeypatch.delenv("ZSCORE_THRESHOLD", raising=False)
    assert zscore_threshold() == DEFAULT_ZSCORE_THRESHOLD


def test_zscore_threshold_honours_env(monkeypatch):
    monkeypatch.setenv("ZSCORE_THRESHOLD", "2.0")
    assert zscore_threshold() == 2.0


def test_default_iqr_multiplier(monkeypatch):
    monkeypatch.delenv("IQR_MULTIPLIER", raising=False)
    assert iqr_multiplier() == DEFAULT_IQR_MULTIPLIER


def test_iqr_multiplier_honours_env(monkeypatch):
    monkeypatch.setenv("IQR_MULTIPLIER", "3.0")
    assert iqr_multiplier() == 3.0


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ZSCORE_THRESHOLD", "not-a-number")
    assert zscore_threshold() == DEFAULT_ZSCORE_THRESHOLD


# --------------------------------------------------------------------------- #
# daily_metric_series - control flow
# --------------------------------------------------------------------------- #
class _FakeDB:
    def fetch_all(self, sql, params=None):
        return [
            {"period_key": "2026-01-01", "revenue": 100.0, "avg_shipping_days": None},
            {"period_key": "2026-01-02", "revenue": 200.0, "avg_shipping_days": 3.0},
        ]


def test_daily_metric_series_extracts_and_skips_none():
    series = daily_metric_series(_FakeDB(), "revenue")
    assert series == [("2026-01-01", 100.0), ("2026-01-02", 200.0)]

    shipping = daily_metric_series(_FakeDB(), "avg_shipping_days")
    assert shipping == [("2026-01-02", 3.0)]


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
    from src.anomaly_detection import detect_all_iqr, detect_all_zscore
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=400, seed=19)
    dates = sorted(df["Order_Date"].unique())[:8]
    codes = []
    names = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        if i == len(dates) - 1:
            # Inject an extreme revenue day: multiply every line's revenue.
            day_df["Revenue"] = day_df["Revenue"] * 50
            day_df["Profit"] = day_df["Revenue"] - day_df["Cost"]
        name = f"{SENTINEL}_{i}.csv"
        names.append(name)
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0,) for c in codes)
        zscore = detect_all_zscore(db, metrics=("revenue",))
        iqr = detect_all_iqr(db, metrics=("revenue",))
        assert zscore["revenue"], "expected enough days for z-score detection"
        assert any(r.is_anomaly for r in zscore["revenue"])
        assert any(r.is_outlier for r in iqr["revenue"])
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
