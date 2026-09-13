"""InsightForge AI - forecasting & forecast evaluation (Phase 25 / spec
Phases 49-50, FR-18).

Exponential-smoothing forecasts for **revenue, profit, orders** at **7- and
30-day** horizons, evaluated with **MAE/RMSE/MAPE**, persisted to the
pre-built ``forecast_results`` table (``sql/schema.sql``, Phase 8).

The spec (verbatim, FR-18): "Exponential-smoothing forecasts for
revenue/profit/orders at 7 and 30 days, stored with MAE/RMSE/MAPE."

**Method is this project's own choice** (the spec names no formula): **Holt's
linear trend exponential smoothing** (level + trend, "double" exponential
smoothing) - the standard method for a series with trend, and it avoids
adding a new dependency (``statsmodels`` isn't in ``requirements.txt``; every
other statistical technique in this codebase - PSI, Z-score/IQR, rolling
baseline, RFM quintiles - is likewise hand-rolled with ``numpy``/pure Python,
reserving ``scikit-learn`` for genuinely complex ML like Isolation Forest).
``FORECAST_ALPHA`` (level smoothing, default 0.3) and ``FORECAST_BETA``
(trend smoothing, default 0.1) are this project's own documented defaults,
same pattern as ``ZSCORE_THRESHOLD``/the PSI bands. The confidence band
(one-step-ahead in-sample residual std, widened by ``sqrt(h)``, 95% z-band)
is likewise this project's own operational choice. Evaluation
(MAE/RMSE/MAPE) is a standard walk-forward holdout: hold out the last
``horizon`` days, fit on everything before that, forecast ``horizon`` days
ahead, and compare against the held-out actuals.

**On-demand, not auto-wired into ``src/orchestrator.py``'s per-file
pipeline - but does persist** (a new combination: Phases 20/21 persist *and*
auto-wire; Phases 17/22/23/24 are on-demand and don't persist at all).
Forecasting needs the metric's *whole* daily history
(:func:`src.anomaly_detection.daily_metric_series`, the same helper Phases
18-19 use), not just the dates in a just-ingested file - the same reason
RCA/impact/RFM (Phases 22-24) stayed on-demand rather than auto-wired like
anomaly fusion/drift (Phases 20-21, which only ever look at the
just-ingested file's own dates). ``docs/data-flow.md``'s stage table places
"Forecast" (13) in the same batch-analytics block as RCA (11) and Business
impact (12), after the two implemented orchestrator stages (7 = anomalies,
8 = drift). It still needs a ``run_id`` because ``forecast_results.run_id``
is ``NOT NULL`` (unlike RCA/impact/RFM, which have no table at all) - so
:func:`persist_forecast_results` takes ``run_id`` as a parameter, the same
shape as ``drift_detection.persist_drift_results``, just called on demand
(a script, a test, or later the Streamlit/recommendation layer) instead of
from ``src/orchestrator.py``.
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from src.anomaly_detection import daily_metric_series
from src.database import Database

FORECAST_METRICS = ("revenue", "profit", "orders")
HORIZONS = (7, 30)

DEFAULT_ALPHA = 0.3
DEFAULT_BETA = 0.1
DEFAULT_MIN_TRAIN_DAYS = 14

Z_95 = 1.959963984540054  # two-sided 95% normal critical value


def default_alpha() -> float:
    """``FORECAST_ALPHA`` from the environment (fresh each call)."""
    try:
        return float(os.environ.get("FORECAST_ALPHA", "").strip() or DEFAULT_ALPHA)
    except ValueError:
        return DEFAULT_ALPHA


def default_beta() -> float:
    """``FORECAST_BETA`` from the environment (fresh each call)."""
    try:
        return float(os.environ.get("FORECAST_BETA", "").strip() or DEFAULT_BETA)
    except ValueError:
        return DEFAULT_BETA


def min_train_days() -> int:
    """``FORECAST_MIN_TRAIN_DAYS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("FORECAST_MIN_TRAIN_DAYS", "").strip() or DEFAULT_MIN_TRAIN_DAYS)
    except ValueError:
        return DEFAULT_MIN_TRAIN_DAYS


@dataclass(frozen=True)
class ForecastPoint:
    metric: str
    horizon_days: int
    forecast_date: str
    forecast_value: float
    lower_bound: float | None
    upper_bound: float | None
    model: str
    mae: float | None
    rmse: float | None
    mape: float | None


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def holt_fit(
    series: list[float], alpha: float, beta: float,
) -> tuple[float, float, list[float]]:
    """Fit Holt's linear trend model over ``series``.

    Returns the final ``(level, trend)`` plus the list of one-step-ahead
    residuals (``actual - forecast``, forecast made *before* seeing that
    point) used to size the confidence band and, in a backtest, to judge
    accuracy. Needs at least 2 points - the first point seeds the level,
    the second seeds the trend.
    """
    level = series[0]
    trend = series[1] - series[0] if len(series) > 1 else 0.0
    residuals: list[float] = []
    for t in range(1, len(series)):
        one_step_forecast = level + trend
        residuals.append(series[t] - one_step_forecast)
        previous_level = level
        level = alpha * series[t] + (1 - alpha) * (level + trend)
        trend = beta * (level - previous_level) + (1 - beta) * trend
    return level, trend, residuals


def holt_forecast(
    series: list[float], horizon: int, alpha: float, beta: float,
) -> list[tuple[float, float, float]]:
    """``horizon`` steps of ``(value, lower_bound, upper_bound)`` beyond
    ``series``'s last point, via Holt's linear trend model.

    The band widens with ``sqrt(h)`` around a 95% z-interval built from the
    in-sample one-step-ahead residual std (0.0 when there aren't enough
    residuals to judge spread).
    """
    level, trend, residuals = holt_fit(series, alpha, beta)
    resid_std = statistics.pstdev(residuals) if len(residuals) >= 2 else 0.0
    forecasts = []
    for h in range(1, horizon + 1):
        value = level + h * trend
        margin = Z_95 * resid_std * math.sqrt(h)
        forecasts.append((value, value - margin, value + margin))
    return forecasts


def evaluate_forecast_accuracy(
    actual: list[float], predicted: list[float],
) -> tuple[float, float, float | None]:
    """MAE/RMSE/MAPE between paired ``actual``/``predicted`` sequences.

    MAPE skips any point where ``actual`` is 0 (undefined percentage
    error) and is ``None`` when every point was skipped that way.
    """
    errors = [a - p for a, p in zip(actual, predicted)]
    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    pct_errors = [abs(e) / abs(a) for e, a in zip(errors, actual) if a != 0]
    mape = 100.0 * sum(pct_errors) / len(pct_errors) if pct_errors else None
    return round(mae, 4), round(rmse, 4), round(mape, 4) if mape is not None else None


def backtest_forecast(
    series: list[float], horizon: int, alpha: float, beta: float,
) -> tuple[float, float, float | None] | None:
    """Walk-forward holdout: fit on everything but the last ``horizon``
    points, forecast ``horizon`` steps ahead, and score against the actual
    held-out values. ``None`` when there isn't enough history to both train
    (``min_train_days()`` points) and hold out ``horizon`` points.
    """
    if len(series) < horizon + min_train_days():
        return None
    train, actual = series[:-horizon], series[-horizon:]
    predicted = [value for value, _, _ in holt_forecast(train, horizon, alpha, beta)]
    return evaluate_forecast_accuracy(actual, predicted)


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def forecast_metric(
    db: Database, metric: str, horizon: int,
    alpha: float | None = None, beta: float | None = None,
) -> list[ForecastPoint] | None:
    """``horizon`` days of :class:`ForecastPoint` for ``metric``, or
    ``None`` when there isn't enough daily history
    (:func:`src.anomaly_detection.daily_metric_series`) to both train and
    backtest a forecast of that length.
    """
    alpha = default_alpha() if alpha is None else alpha
    beta = default_beta() if beta is None else beta

    series_rows = daily_metric_series(db, metric)
    if len(series_rows) < horizon + min_train_days():
        return None

    dates = [row[0] for row in series_rows]
    values = [row[1] for row in series_rows]
    accuracy = backtest_forecast(values, horizon, alpha, beta)
    mae, rmse, mape = accuracy if accuracy is not None else (None, None, None)

    last_date = date.fromisoformat(dates[-1])
    forecasts = holt_forecast(values, horizon, alpha, beta)
    return [
        ForecastPoint(
            metric=metric, horizon_days=horizon,
            forecast_date=(last_date + timedelta(days=h)).isoformat(),
            forecast_value=round(value, 4),
            lower_bound=round(lower, 4), upper_bound=round(upper, 4),
            model="exponential_smoothing", mae=mae, rmse=rmse, mape=mape,
        )
        for h, (value, lower, upper) in enumerate(forecasts, start=1)
    ]


def generate_all_forecasts(
    db: Database, metrics: tuple[str, ...] = FORECAST_METRICS,
    horizons: tuple[int, ...] = HORIZONS,
) -> list[ForecastPoint]:
    """:func:`forecast_metric` for every ``metrics`` x ``horizons`` combo,
    skipping combos without enough history."""
    results: list[ForecastPoint] = []
    for metric in metrics:
        for horizon in horizons:
            points = forecast_metric(db, metric, horizon)
            if points:
                results.extend(points)
    return results


def persist_forecast_results(db: Database, run_id: int, points: list[ForecastPoint]) -> int:
    """Bulk-insert ``forecast_results`` rows for this run. Returns the count written."""
    if not points:
        return 0
    params = [
        {
            "run": run_id, "metric": p.metric, "horizon": p.horizon_days,
            "date": p.forecast_date, "value": p.forecast_value,
            "lower": p.lower_bound, "upper": p.upper_bound, "model": p.model,
            "mae": p.mae, "rmse": p.rmse, "mape": p.mape,
        }
        for p in points
    ]
    return db.execute_many(
        "INSERT INTO forecast_results "
        "(run_id, metric, horizon_days, forecast_date, forecast_value, "
        "lower_bound, upper_bound, model, mae, rmse, mape) "
        "VALUES (:run, :metric, :horizon, :date, :value, :lower, :upper, :model, "
        ":mae, :rmse, :mape)",
        params,
    )


def generate_and_persist_forecast(
    db: Database, run_id: int, metrics: tuple[str, ...] = FORECAST_METRICS,
    horizons: tuple[int, ...] = HORIZONS,
) -> list[ForecastPoint]:
    """:func:`generate_all_forecasts` then :func:`persist_forecast_results`."""
    results = generate_all_forecasts(db, metrics, horizons)
    persist_forecast_results(db, run_id, results)
    return results
