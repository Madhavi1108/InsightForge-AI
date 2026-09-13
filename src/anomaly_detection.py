"""InsightForge AI - Z-score & IQR anomaly detection (Phase 18 / spec Phases
35-36, part of FR-11).

Two of the four detectors FR-11 names (Z-score, IQR, rolling baseline,
Isolation Forest). Rolling baseline and Isolation Forest arrive in Phase 19
(same module); fusing all four into one result per metric/date and writing
it to the ``anomalies`` table is Phase 20 - **this module does not touch the
database**. ``docs/system-components.md`` attributes "persist to
``anomalies``" only to the fusion step, and the table's columns
(``detector_votes``, ``confidence``, ``severity``, ``persistence_days``) are
all fusion/severity concepts that don't make sense per individual detector.

Both detectors run against the same daily KPI time series
``src.change_detection.fetch_period_series`` already computes from
``fact_sales`` - reused here rather than writing a 4th copy of the same
10-metric SQL (already in ``sql/kpi_queries.sql``, ``sql/views.sql``, and
``src/change_detection.py``).

The spec (verbatim): "PHASE 35 - Z-SCORE ANOMALY DETECTION: ... Store:
metric, date, score, threshold, anomaly status." / "PHASE 36 - IQR ANOMALY
DETECTION: ... Use: Q1, Q3, IQR, Lower Bound, Upper Bound. Detect
outliers." Neither gives a numeric threshold/multiplier;
``ZSCORE_THRESHOLD`` (default 3.0) and ``IQR_MULTIPLIER`` (default 1.5) are
this project's own documented operational defaults (``docs/anomaly-detection.md``),
the standard textbook values for each method.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass

import numpy as np

from src.change_detection import METRICS, fetch_period_series
from src.database import Database

#: Fewer daily points than this and a detector returns [] for that metric -
#: not enough history to judge, not an error.
ANOMALY_MIN_SAMPLE_SIZE = 5

DEFAULT_ZSCORE_THRESHOLD = 3.0
DEFAULT_IQR_MULTIPLIER = 1.5


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
