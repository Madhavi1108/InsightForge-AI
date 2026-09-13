"""Phase 25 (spec Phases 49-50, FR-18) - forecasting & forecast evaluation.

Holt's linear trend exponential smoothing, backtest evaluation
(MAE/RMSE/MAPE) and env-var configuration are pure - no database - and get
direct unit tests. `forecast_metric`/`generate_all_forecasts` are tested
against a stub DB (monkeypatching `daily_metric_series`, same pattern as
Phase 24's `_RfmStubDB`); `persist_forecast_results` against a minimal stub
`Database` capturing `execute_many` calls. The real `fact_sales` ->
`forecast_results` round trip needs real data, so that's
`INSIGHTFORGE_PG_INTEGRATION=1`-gated (development rule 1: never fake
functionality). Unlike anomaly fusion/drift (Phases 20-21), forecasting is
**not** wired into `src/orchestrator.py` - it's on-demand, same shape as
RCA/impact/RFM (Phases 22-24), just with a persistence table.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.forecasting import (
    DEFAULT_ALPHA,
    DEFAULT_BETA,
    DEFAULT_MIN_TRAIN_DAYS,
    FORECAST_METRICS,
    HORIZONS,
    ForecastPoint,
    backtest_forecast,
    default_alpha,
    default_beta,
    evaluate_forecast_accuracy,
    forecast_metric,
    generate_all_forecasts,
    holt_fit,
    holt_forecast,
    min_train_days,
    persist_forecast_results,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase25_test__"


def test_phase25_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "forecasting.py",
        PROJECT_ROOT / "docs" / "forecasting.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.forecasting"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_fr18_covers_revenue_profit_orders_at_7_and_30_days():
    assert set(FORECAST_METRICS) == {"revenue", "profit", "orders"}
    assert set(HORIZONS) == {7, 30}


# --------------------------------------------------------------------------- #
# holt_fit / holt_forecast - pure
# --------------------------------------------------------------------------- #
def test_holt_fit_perfectly_linear_series_has_near_zero_residuals():
    series = [10.0 + 5.0 * i for i in range(20)]  # exact linear trend, slope 5
    level, trend, residuals = holt_fit(series, alpha=0.5, beta=0.5)
    assert trend == pytest.approx(5.0, abs=0.5)
    # after the model has locked onto the trend, residuals shrink toward 0
    assert abs(residuals[-1]) < abs(residuals[0]) + 1e-6


def test_holt_forecast_linear_series_extrapolates_trend():
    series = [100.0 + 10.0 * i for i in range(30)]  # slope 10/day
    forecasts = holt_forecast(series, horizon=7, alpha=0.5, beta=0.5)
    assert len(forecasts) == 7
    values = [v for v, _, _ in forecasts]
    # each successive forecast should keep climbing, close to the true slope
    assert values[-1] > values[0]
    expected_h7 = series[-1] + 10.0 * 7
    assert values[-1] == pytest.approx(expected_h7, rel=0.15)


def test_holt_forecast_bounds_widen_with_horizon():
    series = [50.0 + (-1) ** i * 3.0 + 0.5 * i for i in range(40)]  # noisy trend
    forecasts = holt_forecast(series, horizon=10, alpha=0.3, beta=0.1)
    widths = [upper - lower for _, lower, upper in forecasts]
    assert widths[-1] > widths[0]


def test_holt_forecast_flat_series_has_zero_width_band_with_no_noise():
    series = [42.0] * 10
    forecasts = holt_forecast(series, horizon=5, alpha=0.3, beta=0.1)
    for value, lower, upper in forecasts:
        assert value == pytest.approx(42.0)
        assert lower == pytest.approx(42.0)
        assert upper == pytest.approx(42.0)


# --------------------------------------------------------------------------- #
# evaluate_forecast_accuracy - pure
# --------------------------------------------------------------------------- #
def test_evaluate_forecast_accuracy_matches_hand_computed_values():
    actual = [100.0, 200.0, 300.0]
    predicted = [110.0, 190.0, 320.0]
    mae, rmse, mape = evaluate_forecast_accuracy(actual, predicted)
    assert mae == pytest.approx((10 + 10 + 20) / 3, abs=1e-3)
    assert rmse == pytest.approx(((10**2 + 10**2 + 20**2) / 3) ** 0.5, abs=1e-3)
    expected_mape = 100.0 * ((10 / 100) + (10 / 200) + (20 / 300)) / 3
    assert mape == pytest.approx(expected_mape, abs=1e-3)


def test_evaluate_forecast_accuracy_perfect_prediction_is_zero():
    mae, rmse, mape = evaluate_forecast_accuracy([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert mae == 0.0
    assert rmse == 0.0
    assert mape == 0.0


def test_evaluate_forecast_accuracy_mape_none_when_all_actuals_zero():
    _, _, mape = evaluate_forecast_accuracy([0.0, 0.0], [1.0, -1.0])
    assert mape is None


# --------------------------------------------------------------------------- #
# backtest_forecast - pure, minimum-history gate
# --------------------------------------------------------------------------- #
def test_backtest_forecast_none_below_minimum_history():
    series = [10.0 + i for i in range(7 + 13)]  # horizon=7 needs 14 train days -> 1 short
    assert backtest_forecast(series, horizon=7, alpha=0.3, beta=0.1) is None


def test_backtest_forecast_returns_accuracy_above_minimum_history():
    series = [10.0 + i for i in range(7 + 14)]  # exactly enough
    result = backtest_forecast(series, horizon=7, alpha=0.3, beta=0.1)
    assert result is not None
    mae, rmse, mape = result
    assert mae >= 0.0 and rmse >= 0.0
    # a clean linear series should backtest with a very small error
    assert mae < 5.0


# --------------------------------------------------------------------------- #
# env var configuration
# --------------------------------------------------------------------------- #
def test_defaults(monkeypatch):
    monkeypatch.delenv("FORECAST_ALPHA", raising=False)
    monkeypatch.delenv("FORECAST_BETA", raising=False)
    monkeypatch.delenv("FORECAST_MIN_TRAIN_DAYS", raising=False)
    assert default_alpha() == DEFAULT_ALPHA
    assert default_beta() == DEFAULT_BETA
    assert min_train_days() == DEFAULT_MIN_TRAIN_DAYS


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("FORECAST_ALPHA", "0.6")
    monkeypatch.setenv("FORECAST_BETA", "0.2")
    monkeypatch.setenv("FORECAST_MIN_TRAIN_DAYS", "21")
    assert default_alpha() == 0.6
    assert default_beta() == 0.2
    assert min_train_days() == 21


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FORECAST_ALPHA", "not-a-number")
    assert default_alpha() == DEFAULT_ALPHA


# --------------------------------------------------------------------------- #
# forecast_metric / generate_all_forecasts - stub DB (monkeypatch daily_metric_series)
# --------------------------------------------------------------------------- #
def _series(n, start=100.0, slope=2.0, start_date=date(2026, 1, 1)):
    return [
        ((start_date + timedelta(days=i)).isoformat(), start + slope * i)
        for i in range(n)
    ]


def test_forecast_metric_none_below_minimum_history(monkeypatch):
    import src.forecasting as forecasting
    monkeypatch.setattr(forecasting, "daily_metric_series", lambda db, metric: _series(10))
    assert forecast_metric(db=object(), metric="revenue", horizon=7) is None


def test_forecast_metric_happy_path_produces_horizon_rows(monkeypatch):
    import src.forecasting as forecasting
    series = _series(30)
    monkeypatch.setattr(forecasting, "daily_metric_series", lambda db, metric: series)

    points = forecast_metric(db=object(), metric="revenue", horizon=7)
    assert points is not None
    assert len(points) == 7
    assert all(isinstance(p, ForecastPoint) for p in points)
    assert all(p.metric == "revenue" and p.horizon_days == 7 for p in points)

    last_date = date.fromisoformat(series[-1][0])
    expected_dates = [(last_date + timedelta(days=h)).isoformat() for h in range(1, 8)]
    assert [p.forecast_date for p in points] == expected_dates
    # a clean upward-trending series should keep the forecast trending up
    assert points[-1].forecast_value > points[0].forecast_value
    assert all(p.mae is not None and p.rmse is not None for p in points)


def test_generate_all_forecasts_skips_combos_without_enough_history(monkeypatch):
    import src.forecasting as forecasting
    short_series = _series(10)  # enough for nothing (min horizon 7 needs 21 points)
    monkeypatch.setattr(forecasting, "daily_metric_series", lambda db, metric: short_series)
    assert generate_all_forecasts(db=object()) == []


def test_generate_all_forecasts_covers_every_metric_and_horizon(monkeypatch):
    import src.forecasting as forecasting
    long_series = _series(60)
    monkeypatch.setattr(forecasting, "daily_metric_series", lambda db, metric: long_series)
    results = generate_all_forecasts(db=object())
    seen = {(p.metric, p.horizon_days) for p in results}
    assert seen == {(m, h) for m in FORECAST_METRICS for h in HORIZONS}


# --------------------------------------------------------------------------- #
# persist_forecast_results - stub Database
# --------------------------------------------------------------------------- #
class _StubDatabase:
    def __init__(self):
        self.calls = []

    def execute_many(self, sql, seq):
        seq = list(seq)
        self.calls.append((sql, seq))
        return len(seq)


def test_persist_forecast_results_empty_is_noop():
    db = _StubDatabase()
    assert persist_forecast_results(db, run_id=1, points=[]) == 0
    assert db.calls == []


def test_persist_forecast_results_writes_one_row_per_point():
    db = _StubDatabase()
    points = [
        ForecastPoint(
            metric="revenue", horizon_days=7, forecast_date="2026-09-09",
            forecast_value=1000.0, lower_bound=900.0, upper_bound=1100.0,
            model="exponential_smoothing", mae=10.0, rmse=12.0, mape=1.5,
        ),
        ForecastPoint(
            metric="revenue", horizon_days=7, forecast_date="2026-09-10",
            forecast_value=1010.0, lower_bound=905.0, upper_bound=1115.0,
            model="exponential_smoothing", mae=10.0, rmse=12.0, mape=1.5,
        ),
    ]
    written = persist_forecast_results(db, run_id=7, points=points)
    assert written == 2
    sql, seq = db.calls[0]
    assert "INSERT INTO forecast_results" in sql
    assert len(seq) == 2
    assert seq[0]["run"] == 7
    assert seq[0]["metric"] == "revenue"
    assert seq[0]["horizon"] == 7


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL, fact_sales -> forecast_results round trip
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


@pg_integration
def test_generate_and_persist_forecast_round_trip():
    pytest.importorskip("psycopg2")
    from src.database import Database
    from src.forecasting import generate_and_persist_forecast

    _apply_schema()
    db = Database()

    run_id = db.insert_returning(
        "INSERT INTO pipeline_runs (file_name, status, started_at) "
        "VALUES (:f, 'RUNNING', now()) RETURNING run_id",
        {"f": f"{SENTINEL}.csv"},
    )
    try:
        points = generate_and_persist_forecast(db, run_id)
        rows = db.fetch_all(
            "SELECT metric, horizon_days, mae, rmse, mape FROM forecast_results "
            "WHERE run_id = :r", {"r": run_id},
        )
        if points:
            assert rows
            assert all(r["horizon_days"] in (7, 30) for r in rows)
            assert all(r["metric"] in FORECAST_METRICS for r in rows)
    finally:
        db.execute("DELETE FROM forecast_results WHERE run_id = :r", {"r": run_id})
        db.execute("DELETE FROM pipeline_runs WHERE run_id = :r", {"r": run_id})
