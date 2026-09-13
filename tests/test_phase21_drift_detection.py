"""Phase 21 (spec Phases 41-42, FR-13) - data drift detection & drift
reporting.

PSI (Population Stability Index) computation and classification are pure -
no database - and get direct unit tests. Fetching the baseline/current
windows from ``fact_sales`` and persisting to the new ``drift_results``
table need real data, so those are ``INSIGHTFORGE_PG_INTEGRATION=1``-gated
(development rule 1: never fake functionality). Like anomaly fusion (Phase
20), drift detection is wired into ``src/orchestrator.py`` but never
affects ``pipeline_runs.status`` - advisory analytics, not a gate.
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.drift_detection import (
    DEFAULT_BASELINE_DAYS,
    DEFAULT_CURRENT_DAYS,
    FEATURES,
    MIN_BASELINE_ROWS,
    MIN_CURRENT_ROWS,
    PSI_DRIFT_THRESHOLD,
    PSI_WARNING_THRESHOLD,
    DriftResult,
    baseline_days,
    classify_psi,
    compute_psi_categorical,
    compute_psi_continuous,
    current_days,
    detect_drift_for_feature,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase21_test__"


def test_phase21_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "drift_detection.py",
        PROJECT_ROOT / "docs" / "drift-detection.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.drift_detection"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_features_cover_fr13_six_distributions():
    assert set(FEATURES) == {
        "price", "quantity", "discount", "shipping", "category_mix", "region_mix",
    }


# --------------------------------------------------------------------------- #
# classify_psi
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "psi, expected",
    [(0.0, "Normal"), (0.09, "Normal"), (0.1, "Warning"), (0.2, "Warning"),
     (0.25, "Drift Detected"), (1.0, "Drift Detected")],
)
def test_classify_psi_thresholds(psi, expected):
    assert classify_psi(psi) == expected
    assert PSI_WARNING_THRESHOLD == 0.1
    assert PSI_DRIFT_THRESHOLD == 0.25


# --------------------------------------------------------------------------- #
# compute_psi_continuous / compute_psi_categorical
# --------------------------------------------------------------------------- #
def test_psi_continuous_identical_distribution_is_near_zero():
    rng = random.Random(1)
    baseline = [rng.gauss(100, 10) for _ in range(1000)]
    current = [rng.gauss(100, 10) for _ in range(1000)]
    psi = compute_psi_continuous(baseline, current)
    assert psi < PSI_WARNING_THRESHOLD


def test_psi_continuous_shifted_distribution_is_flagged():
    rng = random.Random(2)
    baseline = [rng.gauss(100, 10) for _ in range(500)]
    shifted = [rng.gauss(200, 10) for _ in range(500)]
    psi = compute_psi_continuous(baseline, shifted)
    assert psi >= PSI_DRIFT_THRESHOLD


def test_psi_categorical_identical_proportions_is_zero():
    baseline = ["A"] * 300 + ["B"] * 150 + ["C"] * 50
    current = ["A"] * 60 + ["B"] * 30 + ["C"] * 10  # same proportions, smaller n
    assert compute_psi_categorical(baseline, current) == pytest.approx(0.0, abs=1e-6)


def test_psi_categorical_shifted_mix_is_flagged():
    baseline = ["A"] * 300 + ["B"] * 150 + ["C"] * 50
    shifted = ["A"] * 10 + ["B"] * 10 + ["C"] * 80
    psi = compute_psi_categorical(baseline, shifted)
    assert psi >= PSI_DRIFT_THRESHOLD


def test_psi_handles_empty_current_without_error():
    assert compute_psi_continuous([1.0, 2.0, 3.0], []) == 0.0
    assert compute_psi_categorical(["A", "B"], []) == 0.0


# --------------------------------------------------------------------------- #
# detect_drift_for_feature - minimum sample gate
# --------------------------------------------------------------------------- #
def test_detect_drift_returns_none_below_min_baseline_rows():
    baseline = [1.0] * (MIN_BASELINE_ROWS - 1)
    current = [1.0] * MIN_CURRENT_ROWS
    assert detect_drift_for_feature(
        "price", baseline, current, False, "2026-01-01", "2026-01-30",
        "2026-02-01", "2026-02-07",
    ) is None


def test_detect_drift_returns_none_below_min_current_rows():
    baseline = [1.0] * MIN_BASELINE_ROWS
    current = [1.0] * (MIN_CURRENT_ROWS - 1)
    assert detect_drift_for_feature(
        "price", baseline, current, False, "2026-01-01", "2026-01-30",
        "2026-02-01", "2026-02-07",
    ) is None


def test_detect_drift_returns_result_with_enough_rows():
    rng = random.Random(3)
    baseline = [rng.gauss(50, 5) for _ in range(MIN_BASELINE_ROWS + 20)]
    current = [rng.gauss(50, 5) for _ in range(MIN_CURRENT_ROWS + 10)]
    result = detect_drift_for_feature(
        "price", baseline, current, False, "2026-01-01", "2026-01-30",
        "2026-02-01", "2026-02-07",
    )
    assert isinstance(result, DriftResult)
    assert result.feature == "price"
    assert result.status in ("Normal", "Warning", "Drift Detected")
    assert result.baseline_count == len(baseline)
    assert result.current_count == len(current)


# --------------------------------------------------------------------------- #
# window configuration
# --------------------------------------------------------------------------- #
def test_default_windows(monkeypatch):
    monkeypatch.delenv("DRIFT_BASELINE_DAYS", raising=False)
    monkeypatch.delenv("DRIFT_CURRENT_DAYS", raising=False)
    assert baseline_days() == DEFAULT_BASELINE_DAYS
    assert current_days() == DEFAULT_CURRENT_DAYS


def test_windows_honour_env(monkeypatch):
    monkeypatch.setenv("DRIFT_BASELINE_DAYS", "14")
    monkeypatch.setenv("DRIFT_CURRENT_DAYS", "3")
    assert baseline_days() == 14
    assert current_days() == 3


def test_invalid_window_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DRIFT_BASELINE_DAYS", "not-a-number")
    assert baseline_days() == DEFAULT_BASELINE_DAYS


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
def test_orchestrator_persists_drift_without_affecting_status(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=2000, seed=31)
    dates = sorted(df["Order_Date"].unique())[:40]
    codes = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0, 9) for c in codes)
        run_ids = [r["run_id"] for r in db.fetch_all(
            "SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p ORDER BY run_id",
            {"p": SENTINEL + "%"},
        )]
        drift_rows = db.fetch_all(
            "SELECT feature, status FROM drift_results WHERE run_id = ANY(:ids)",
            {"ids": run_ids},
        )
        assert drift_rows  # enough history by the later files to run at least once
        assert all(r["status"] in ("Normal", "Warning", "Drift Detected") for r in drift_rows)
        for row in db.fetch_all(
            "SELECT status FROM pipeline_runs WHERE run_id = ANY(:ids)", {"ids": run_ids},
        ):
            assert row["status"] in ("SUCCESS", "WARNING", "FAILED")
    finally:
        db.execute("DELETE FROM drift_results WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM anomalies WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
