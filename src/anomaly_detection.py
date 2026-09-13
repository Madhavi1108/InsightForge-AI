"""InsightForge AI - anomaly detection: Z-score, IQR (Phase 18 / spec Phases
35-36), rolling baseline & Isolation Forest (Phase 19 / spec Phases 37-38) -
part of FR-11.

All four detectors FR-11 names live in this one module. Fusing them into one
result per metric/date and writing it to the ``anomalies`` table is Phase 20
- **this module does not touch the database**. ``docs/system-components.md``
attributes "persist to ``anomalies``" only to the fusion step, and the
table's columns (``detector_votes``, ``confidence``, ``severity``,
``persistence_days``) are all fusion/severity concepts that don't make sense
per individual detector.

All four detectors run against the same daily KPI time series
``src.change_detection.fetch_period_series`` already computes from
``fact_sales`` - reused here rather than writing a 4th/5th/6th copy of the
same 10-metric SQL (already in ``sql/kpi_queries.sql``, ``sql/views.sql``,
and ``src/change_detection.py``).

The spec (verbatim): "PHASE 35 - Z-SCORE ANOMALY DETECTION: ... Store:
metric, date, score, threshold, anomaly status." / "PHASE 36 - IQR ANOMALY
DETECTION: ... Use: Q1, Q3, IQR, Lower Bound, Upper Bound. Detect
outliers." / "PHASE 37 - ROLLING BASELINE: Create rolling mean, median,
standard deviation. Compare current values against expected range." /
"PHASE 38 - ISOLATION FOREST: Implement sklearn IsolationForest. Use
appropriate features. Return anomaly score, prediction, confidence." None of
the four give a numeric threshold/multiplier/window/contamination rate;
``ZSCORE_THRESHOLD`` (3.0), ``IQR_MULTIPLIER`` (1.5),
``ROLLING_WINDOW_DAYS`` (7), ``ROLLING_BASELINE_MULTIPLIER`` (2.0),
``IFOREST_CONTAMINATION`` (``"auto"``) and ``IFOREST_RANDOM_STATE`` (42) are
this project's own documented operational defaults
(``docs/anomaly-detection.md``).

Rolling baseline is deliberately **trailing**, unlike Z-score/IQR's
whole-series baseline: for each day, mean/median/std come from the
*preceding* ``ROLLING_WINDOW_DAYS`` days only, and *today's* value is tested
against that - the standard, causally-correct meaning of "rolling baseline"
(predict the expected range from history, then test the new point), and why
it's a genuinely different detector rather than a re-run of Z-score.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

from src.change_detection import METRICS, fetch_period_series
from src.database import Database

#: Fewer daily points than this and a detector returns [] for that metric -
#: not enough history to judge, not an error.
ANOMALY_MIN_SAMPLE_SIZE = 5

DEFAULT_ZSCORE_THRESHOLD = 3.0
DEFAULT_IQR_MULTIPLIER = 1.5
DEFAULT_ROLLING_WINDOW_DAYS = 7
DEFAULT_ROLLING_MULTIPLIER = 2.0
DEFAULT_IFOREST_CONTAMINATION = "auto"
DEFAULT_IFOREST_RANDOM_STATE = 42


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def zscore_threshold() -> float:
    """``ZSCORE_THRESHOLD`` from the environment (fresh each call)."""
    return _env_float("ZSCORE_THRESHOLD", DEFAULT_ZSCORE_THRESHOLD)


def iqr_multiplier() -> float:
    """``IQR_MULTIPLIER`` from the environment (fresh each call)."""
    return _env_float("IQR_MULTIPLIER", DEFAULT_IQR_MULTIPLIER)


def rolling_window_days() -> int:
    """``ROLLING_WINDOW_DAYS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("ROLLING_WINDOW_DAYS", "").strip()
                   or DEFAULT_ROLLING_WINDOW_DAYS)
    except ValueError:
        return DEFAULT_ROLLING_WINDOW_DAYS


def rolling_baseline_multiplier() -> float:
    """``ROLLING_BASELINE_MULTIPLIER`` from the environment (fresh each call)."""
    return _env_float("ROLLING_BASELINE_MULTIPLIER", DEFAULT_ROLLING_MULTIPLIER)


def iforest_contamination() -> float | str:
    """``IFOREST_CONTAMINATION`` from the environment - ``"auto"`` or a float."""
    raw = os.environ.get("IFOREST_CONTAMINATION", "").strip() or DEFAULT_IFOREST_CONTAMINATION
    if raw == "auto":
        return "auto"
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_IFOREST_CONTAMINATION


def iforest_random_state() -> int:
    """``IFOREST_RANDOM_STATE`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("IFOREST_RANDOM_STATE", "").strip()
                   or DEFAULT_IFOREST_RANDOM_STATE)
    except ValueError:
        return DEFAULT_IFOREST_RANDOM_STATE


@dataclass(frozen=True)
class ZScoreResult:
    metric: str
    date: str
    value: float
    mean: float
    std: float
    z_score: float | None  # None when std == 0 (a constant series)
    threshold: float
    is_anomaly: bool


@dataclass(frozen=True)
class IqrResult:
    metric: str
    date: str
    value: float
    q1: float
    q3: float
    iqr: float
    lower_bound: float
    upper_bound: float
    is_outlier: bool


@dataclass(frozen=True)
class RollingBaselineResult:
    metric: str
    date: str
    value: float
    rolling_mean: float
    rolling_median: float
    rolling_std: float
    lower_bound: float
    upper_bound: float
    is_anomaly: bool


@dataclass(frozen=True)
class IsolationForestResult:
    metric: str
    date: str
    value: float
    anomaly_score: float
    prediction: str  # "anomaly" | "normal"
    confidence: float
    is_anomaly: bool


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def detect_zscore(
    series: list[tuple[str, float]], metric: str, threshold: float | None = None,
) -> list[ZScoreResult]:
    """Population mean/std over the whole ``series`` (the point being tested
    is included in its own baseline - the simplest defensible choice given
    the spec gives no leave-one-out instruction).

    ``series`` is ``[(date, value), ...]``. Returns ``[]`` when
    ``len(series) < ANOMALY_MIN_SAMPLE_SIZE``.
    """
    if len(series) < ANOMALY_MIN_SAMPLE_SIZE:
        return []
    threshold = threshold if threshold is not None else zscore_threshold()
    values = [v for _, v in series]
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    results = []
    for date, value in series:
        z = None if std == 0 else round((value - mean) / std, 4)
        is_anomaly = z is not None and abs(z) > threshold
        results.append(ZScoreResult(
            metric=metric, date=date, value=value, mean=round(mean, 4),
            std=round(std, 4), z_score=z, threshold=threshold, is_anomaly=is_anomaly,
        ))
    return results


def detect_iqr(
    series: list[tuple[str, float]], metric: str, multiplier: float | None = None,
) -> list[IqrResult]:
    """Q1/Q3 via linear interpolation (``numpy.percentile``'s default).

    ``series`` is ``[(date, value), ...]``. Returns ``[]`` when
    ``len(series) < ANOMALY_MIN_SAMPLE_SIZE``.
    """
    if len(series) < ANOMALY_MIN_SAMPLE_SIZE:
        return []
    multiplier = multiplier if multiplier is not None else iqr_multiplier()
    values = [v for _, v in series]
    q1 = float(np.percentile(values, 25))
    q3 = float(np.percentile(values, 75))
    iqr = q3 - q1
    lower_bound = q1 - multiplier * iqr
    upper_bound = q3 + multiplier * iqr
    results = []
    for date, value in series:
        is_outlier = value < lower_bound or value > upper_bound
        results.append(IqrResult(
            metric=metric, date=date, value=value,
            q1=round(q1, 4), q3=round(q3, 4), iqr=round(iqr, 4),
            lower_bound=round(lower_bound, 4), upper_bound=round(upper_bound, 4),
            is_outlier=is_outlier,
        ))
    return results


def detect_rolling_baseline(
    series: list[tuple[str, float]], metric: str,
    window: int | None = None, multiplier: float | None = None,
) -> list[RollingBaselineResult]:
    """Trailing rolling mean/median/std vs. an expected range.

    For each day from index ``window`` onward, the baseline is computed from
    the ``window`` days strictly *before* it (never including the day being
    tested), then that day's value is checked against
    ``[rolling_mean - multiplier*rolling_std, rolling_mean + multiplier*rolling_std]``.
    The first ``window`` days have no trailing window yet and are omitted
    from the result entirely (not flagged `False` - genuinely not judged).
    Returns ``[]`` when ``len(series) < ANOMALY_MIN_SAMPLE_SIZE``.
    """
    if len(series) < ANOMALY_MIN_SAMPLE_SIZE:
        return []
    window = window if window is not None else rolling_window_days()
    multiplier = multiplier if multiplier is not None else rolling_baseline_multiplier()
    if len(series) <= window:
        return []
    results = []
    for i in range(window, len(series)):
        date, value = series[i]
        history = [v for _, v in series[i - window:i]]
        mean = statistics.fmean(history)
        median = statistics.median(history)
        std = statistics.pstdev(history)
        lower_bound = mean - multiplier * std
        upper_bound = mean + multiplier * std
        is_anomaly = value < lower_bound or value > upper_bound
        results.append(RollingBaselineResult(
            metric=metric, date=date, value=value,
            rolling_mean=round(mean, 4), rolling_median=round(median, 4),
            rolling_std=round(std, 4), lower_bound=round(lower_bound, 4),
            upper_bound=round(upper_bound, 4), is_anomaly=is_anomaly,
        ))
    return results


def _iforest_confidence(decision_value: float) -> float:
    """Logistic squash of the raw decision function into ``(0, 1)``.

    Not an sklearn built-in - this project's own mapping (higher confidence
    for a more negative decision value, i.e. a more anomalous point).
    """
    return round(float(1.0 / (1.0 + np.exp(decision_value))), 4)


def detect_isolation_forest(
    series: list[tuple[str, float]], metric: str,
    contamination: float | str | None = None, random_state: int | None = None,
) -> list[IsolationForestResult]:
    """``sklearn.ensemble.IsolationForest`` over the metric's own daily values.

    Feature set: the value itself, reshaped ``(n, 1)`` - the simplest
    defensible choice for a univariate KPI series (the spec names no
    concrete multivariate features to build). ``decision_function`` is
    lower for more anomalous points; this negates it so a higher
    ``anomaly_score`` always means "more anomalous", matching Z-score/IQR's
    direction. Returns ``[]`` when ``len(series) < ANOMALY_MIN_SAMPLE_SIZE``.
    """
    if len(series) < ANOMALY_MIN_SAMPLE_SIZE:
        return []
    contamination = contamination if contamination is not None else iforest_contamination()
    random_state = random_state if random_state is not None else iforest_random_state()

    values = np.array([[v] for _, v in series])
    model = IsolationForest(contamination=contamination, random_state=random_state)
    predictions = model.fit_predict(values)  # -1 = anomaly, 1 = normal
    decision = model.decision_function(values)  # lower = more anomalous

    results = []
    for (date, value), pred, dec in zip(series, predictions, decision):
        is_anomaly = bool(pred == -1)
        results.append(IsolationForestResult(
            metric=metric, date=date, value=value,
            anomaly_score=round(-float(dec), 4),
            prediction="anomaly" if is_anomaly else "normal",
            confidence=_iforest_confidence(float(dec)),
            is_anomaly=is_anomaly,
        ))
    return results


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def daily_metric_series(db: Database, metric: str) -> list[tuple[str, float]]:
    """One metric's non-``None`` daily values, ordered ascending by date."""
    rows = fetch_period_series(db, "day")
    return [
        (str(row["period_key"]), float(row[metric]))
        for row in rows
        if row.get(metric) is not None
    ]


def detect_all_zscore(
    db: Database, metrics: tuple[str, ...] = METRICS, threshold: float | None = None,
) -> dict[str, list[ZScoreResult]]:
    """:func:`detect_zscore` for every metric in ``metrics``."""
    return {m: detect_zscore(daily_metric_series(db, m), m, threshold) for m in metrics}


def detect_all_iqr(
    db: Database, metrics: tuple[str, ...] = METRICS, multiplier: float | None = None,
) -> dict[str, list[IqrResult]]:
    """:func:`detect_iqr` for every metric in ``metrics``."""
    return {m: detect_iqr(daily_metric_series(db, m), m, multiplier) for m in metrics}


def detect_all_rolling_baseline(
    db: Database, metrics: tuple[str, ...] = METRICS,
    window: int | None = None, multiplier: float | None = None,
) -> dict[str, list[RollingBaselineResult]]:
    """:func:`detect_rolling_baseline` for every metric in ``metrics``."""
    return {m: detect_rolling_baseline(daily_metric_series(db, m), m, window, multiplier)
            for m in metrics}


def detect_all_isolation_forest(
    db: Database, metrics: tuple[str, ...] = METRICS,
    contamination: float | str | None = None, random_state: int | None = None,
) -> dict[str, list[IsolationForestResult]]:
    """:func:`detect_isolation_forest` for every metric in ``metrics``."""
    return {m: detect_isolation_forest(daily_metric_series(db, m), m, contamination, random_state)
            for m in metrics}
