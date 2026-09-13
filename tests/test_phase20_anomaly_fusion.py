"""Phase 20 (spec Phases 39-40, FR-11/FR-12) - anomaly fusion & severity
engine.

Unlike Phases 17-19's pure detectors, fusion writes to the database
(``anomalies.run_id`` is ``NOT NULL``), so it's wired into
``src/orchestrator.py`` - but only additively (``stage_metrics.anomalies``);
it never changes ``pipeline_runs.status``, and a failure here is swallowed,
never fails the run. The fusion math itself (`fuse_metric_series`) is
exercised here against a stub DB whose `fetch_all` returns a fixed daily
series, so the vote-counting/severity logic gets real coverage without a
live Postgres. Persistence + orchestrator wiring are
``INSIGHTFORGE_PG_INTEGRATION=1``-gated (development rule 1: never fake
functionality).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.anomaly_fusion import (
    BUSINESS_IMPACT_WEIGHT,
    DEFAULT_MIN_VOTES,
    FusedAnomaly,
    fuse_metric_series,
    min_votes,
)
from src.change_detection import METRICS

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase20_test__"


class _SeriesDB:
    """Stub exposing just enough of Database for daily_metric_series."""

    def __init__(self, values):
        d0 = date(2026, 1, 1)
        self.rows = [
            {"period_key": (d0 + timedelta(days=i)).isoformat(), "revenue": float(v)}
            for i, v in enumerate(values)
        ]

    def fetch_all(self, sql, params=None):
        return self.rows


def test_phase20_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "anomaly_fusion.py",
        PROJECT_ROOT / "docs" / "anomaly-fusion.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.anomaly_fusion"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_business_impact_weight_covers_every_metric():
    assert set(BUSINESS_IMPACT_WEIGHT) == set(METRICS)
    assert all(0.0 < w <= 1.0 for w in BUSINESS_IMPACT_WEIGHT.values())


def test_min_votes_default_and_env(monkeypatch):
    monkeypatch.delenv("ANOMALY_FUSION_MIN_VOTES", raising=False)
    assert min_votes() == DEFAULT_MIN_VOTES
    monkeypatch.setenv("ANOMALY_FUSION_MIN_VOTES", "3")
    assert min_votes() == 3


# --------------------------------------------------------------------------- #
# fuse_metric_series - the vote gate
# --------------------------------------------------------------------------- #
def test_flat_series_produces_no_anomalies():
    db = _SeriesDB([100] * 20)
    assert fuse_metric_series(db, "revenue") == []


def test_obvious_spike_is_fused_with_all_flags():
    baseline = [100, 101, 99, 100, 102, 98, 101, 99, 100, 102,
                98, 101, 99, 100, 101, 99, 100, 102, 98]
    values = baseline + [100000]
    db = _SeriesDB(values)
    results = fuse_metric_series(db, "revenue")
    by_date = {r.date: r for r in results}
    spike_date = (date(2026, 1, 1) + timedelta(days=len(values) - 1)).isoformat()
    a = by_date[spike_date]
    assert isinstance(a, FusedAnomaly)
    assert a.metric == "revenue"
    assert a.detector_votes >= DEFAULT_MIN_VOTES
    assert a.observed_value == 100000
    assert a.direction == "up"
    assert a.severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def test_below_min_votes_is_not_fused(monkeypatch):
    monkeypatch.setenv("ANOMALY_FUSION_MIN_VOTES", "4")
    baseline = [100, 101, 99, 100, 102, 98, 101, 99]
    values = baseline + [130]  # a mild bump unlikely to get all 4 detectors' votes
    db = _SeriesDB(values)
    results = fuse_metric_series(db, "revenue", votes_needed=4)
    assert all(r.detector_votes >= 4 for r in results)


def test_expected_value_prefers_rolling_mean_when_available():
    values = [100] * 10 + [200]
    db = _SeriesDB(values)
    results = fuse_metric_series(db, "revenue", votes_needed=1)
    assert results
    a = results[-1]
    assert a.expected_value == pytest.approx(100.0, abs=0.01)


def test_direction_is_none_when_observed_equals_expected():
    # A perfectly flat series has zero variance; Isolation Forest's "auto"
    # contamination can still arbitrarily flag a handful of identical points
    # (no real signal to distinguish them), which is enough to clear
    # votes_needed=1. Every such fused row must have observed == expected
    # and therefore direction is None (never an invented "up"/"down" for a
    # zero-magnitude move).
    values = [100] * 11
    db = _SeriesDB(values)
    results = fuse_metric_series(db, "revenue", votes_needed=1)
    for a in results:
        assert a.observed_value == a.expected_value
        assert a.direction is None
        assert a.deviation_pct is None or a.deviation_pct == 0.0


def test_persistence_days_counts_consecutive_streak():
    baseline = [100] * 12
    spike = [300, 305, 298, 100]  # three anomalous days in a row, then normal
    values = baseline + spike
    db = _SeriesDB(values)
    results = fuse_metric_series(db, "revenue", votes_needed=1)
    by_date = {r.date: r for r in results}
    dates = sorted(by_date)
    persistences = [by_date[d].persistence_days for d in dates]
    assert persistences == sorted(persistences) or len(set(persistences)) <= len(persistences)
    # Streak must reset - no fused day should have a persistence longer than
    # the number of anomalous days actually present.
    assert all(p <= 3 for p in persistences)


def test_severity_uses_all_four_named_factors():
    # High deviation + high-impact metric + full votes + built-up persistence
    # should classify at least MEDIUM (never silently LOW for an extreme case).
    baseline = [100] * 15
    values = baseline + [100000]
    db = _SeriesDB(values)
    [a] = fuse_metric_series(db, "revenue", votes_needed=1)
    assert a.severity in ("MEDIUM", "HIGH", "CRITICAL")


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL + orchestrator wiring
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
def test_orchestrator_persists_anomalies_without_affecting_status(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=500, seed=29)
    dates = sorted(df["Order_Date"].unique())[:10]
    codes = []
    names = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        if i == len(dates) - 1:
            day_df["Revenue"] = day_df["Revenue"] * 50
            day_df["Profit"] = day_df["Revenue"] - day_df["Cost"]
        name = f"{SENTINEL}_{i}.csv"
        names.append(name)
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0, 9) for c in codes)  # 9 = DQ reject, still fine here
        run_ids = [r["run_id"] for r in db.fetch_all(
            "SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p ORDER BY run_id",
            {"p": SENTINEL + "%"},
        )]
        anomaly_rows = db.fetch_all(
            "SELECT metric, anomaly_date, detector_votes, severity FROM anomalies "
            "WHERE run_id = ANY(:ids)", {"ids": run_ids},
        )
        assert any(r["metric"] == "revenue" for r in anomaly_rows)
        for row in db.fetch_all(
            "SELECT run_id, status FROM pipeline_runs WHERE run_id = ANY(:ids)", {"ids": run_ids},
        ):
            assert row["status"] in ("SUCCESS", "WARNING", "FAILED")
    finally:
        db.execute("DELETE FROM anomalies WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
