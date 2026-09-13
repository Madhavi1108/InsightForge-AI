"""InsightForge AI - anomaly fusion & severity engine (Phase 20 / spec
Phases 39-40, FR-11/FR-12).

Combines the four detectors in ``src/anomaly_detection.py`` (Z-score, IQR,
rolling baseline, Isolation Forest) into one unified result per metric/date
and classifies its severity. **This is the only anomaly-related module that
writes to the database** - ``anomalies.run_id`` is ``NOT NULL REFERENCES
pipeline_runs(run_id)``, so every row must belong to a specific run. That's
why, unlike the pure detectors (Phases 17-19, never orchestrator-wired),
fusion *is* wired into ``src/orchestrator.py``: an ``anomalies`` row only
makes sense as "what fusion found for the date(s) this run just ingested."

Fusion never affects the run's terminal status - it's advisory analytics,
not a correctness gate (the Phase 14 data-quality gate already owns
SUCCESS/WARNING/FAILED).

The spec (verbatim): "PHASE 39 - ANOMALY FUSION: Combine Z-score, IQR,
Rolling baseline, Isolation Forest. Generate a unified anomaly result." /
"PHASE 40 - SEVERITY ENGINE: Classify LOW/MEDIUM/HIGH/CRITICAL. Use:
percentage deviation, business impact, confidence, persistence." Neither
gives a vote-agreement rule, an expected-value formula, a confidence
formula, or severity weights/thresholds - all of it is this project's own
documented operational definition (``docs/anomaly-fusion.md``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from src.anomaly_detection import (
    daily_metric_series,
    detect_iqr,
    detect_isolation_forest,
    detect_rolling_baseline,
    detect_zscore,
)
from src.change_detection import METRICS
from src.database import Database

DEFAULT_MIN_VOTES = 2

#: This project's own "business impact" weighting per metric (spec names the
#: factor but gives no definition) - financial metrics weighted highest.
BUSINESS_IMPACT_WEIGHT: dict[str, float] = {
    "revenue": 1.0, "profit": 1.0, "margin_pct": 0.8, "orders": 0.7,
    "customers": 0.6, "aov": 0.6, "return_rate_pct": 0.7, "units": 0.5,
    "avg_discount_pct": 0.4, "avg_shipping_days": 0.3,
}

SEVERITY_CRITICAL = 70.0
SEVERITY_HIGH = 50.0
SEVERITY_MEDIUM = 30.0


def min_votes() -> int:
    """``ANOMALY_FUSION_MIN_VOTES`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("ANOMALY_FUSION_MIN_VOTES", "").strip() or DEFAULT_MIN_VOTES)
    except ValueError:
        return DEFAULT_MIN_VOTES


@dataclass(frozen=True)
class FusedAnomaly:
    metric: str
    date: str
    grain: str
    observed_value: float
    expected_value: float | None
    deviation_pct: float | None
    direction: str | None  # "up" | "down" | None (observed == expected)
    zscore_flag: bool
    iqr_flag: bool
    rolling_flag: bool
    iforest_flag: bool
    detector_votes: int
    confidence: float
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    persistence_days: int
    detail: dict = field(default_factory=dict)


def _severity(deviation_pct: float | None, metric: str, confidence: float,
             persistence_days: int) -> str:
    deviation_component = min(abs(deviation_pct), 100.0) if deviation_pct is not None else 0.0
    impact_component = BUSINESS_IMPACT_WEIGHT.get(metric, 0.5) * 100.0
    confidence_component = confidence * 100.0
    persistence_component = min(persistence_days, 7) / 7.0 * 100.0
    score = (0.35 * deviation_component + 0.25 * impact_component
             + 0.20 * confidence_component + 0.20 * persistence_component)
    if score >= SEVERITY_CRITICAL:
        return "CRITICAL"
    if score >= SEVERITY_HIGH:
        return "HIGH"
    if score >= SEVERITY_MEDIUM:
        return "MEDIUM"
    return "LOW"


def fuse_metric_series(db: Database, metric: str, votes_needed: int | None = None) -> list[FusedAnomaly]:
    """Fuse all four detectors for ``metric`` over its full daily history.

    Returns only the dates whose vote count clears ``votes_needed``
    (default :func:`min_votes`) - not every day.
    """
    votes_needed = votes_needed if votes_needed is not None else min_votes()
    series = daily_metric_series(db, metric)

    zscore = {r.date: r for r in detect_zscore(series, metric)}
    iqr = {r.date: r for r in detect_iqr(series, metric)}
    rolling = {r.date: r for r in detect_rolling_baseline(series, metric)}
    iforest = {r.date: r for r in detect_isolation_forest(series, metric)}
    values = dict(series)

    results: list[FusedAnomaly] = []
    streak = 0
    for date, observed in series:
        z = zscore.get(date)
        i = iqr.get(date)
        r = rolling.get(date)
        f = iforest.get(date)

        zscore_flag = bool(z and z.is_anomaly)
        iqr_flag = bool(i and i.is_outlier)
        rolling_flag = bool(r and r.is_anomaly)
        iforest_flag = bool(f and f.is_anomaly)
        votes = sum([zscore_flag, iqr_flag, rolling_flag, iforest_flag])

        streak = streak + 1 if votes >= votes_needed else 0

        if votes < votes_needed:
            continue

        expected = r.rolling_mean if r is not None else (z.mean if z is not None else None)
        if expected is None or expected == 0:
            deviation_pct = None
        else:
            deviation_pct = round(100.0 * (observed - expected) / abs(expected), 2)
        if expected is None or observed == expected:
            direction = None
        else:
            direction = "up" if observed > expected else "down"

        confidence = round(votes / 4.0, 4)
        severity = _severity(deviation_pct, metric, confidence, streak)

        results.append(FusedAnomaly(
            metric=metric, date=date, grain="business", observed_value=observed,
            expected_value=expected, deviation_pct=deviation_pct, direction=direction,
            zscore_flag=zscore_flag, iqr_flag=iqr_flag, rolling_flag=rolling_flag,
            iforest_flag=iforest_flag, detector_votes=votes, confidence=confidence,
            severity=severity, persistence_days=streak,
            detail={
                "zscore": z.z_score if z else None, "iqr_bounds": [i.lower_bound, i.upper_bound] if i else None,
                "rolling_bounds": [r.lower_bound, r.upper_bound] if r else None,
                "iforest_score": f.anomaly_score if f else None,
            },
        ))
    return results


def fuse_all_metrics(db: Database, metrics: tuple[str, ...] = METRICS,
                     votes_needed: int | None = None) -> list[FusedAnomaly]:
    """:func:`fuse_metric_series` for every metric in ``metrics``."""
    out: list[FusedAnomaly] = []
    for metric in metrics:
        out.extend(fuse_metric_series(db, metric, votes_needed))
    return out


def persist_anomalies(db: Database, run_id: int, anomalies: list[FusedAnomaly]) -> int:
    """Bulk-insert ``anomalies`` rows for this run. Returns the count written."""
    if not anomalies:
        return 0
    params = [
        {
            "run": run_id, "metric": a.metric, "date": a.date, "grain": a.grain,
            "observed": a.observed_value, "expected": a.expected_value,
            "deviation": a.deviation_pct, "direction": a.direction,
            "zscore": a.zscore_flag, "iqr": a.iqr_flag, "rolling": a.rolling_flag,
            "iforest": a.iforest_flag, "votes": a.detector_votes,
            "confidence": a.confidence, "severity": a.severity,
            "persistence": a.persistence_days,
            "detail": json.dumps(a.detail, ensure_ascii=False),
        }
        for a in anomalies
    ]
    return db.execute_many(
        "INSERT INTO anomalies "
        "(run_id, metric, anomaly_date, grain, observed_value, expected_value, "
        "deviation_pct, direction, zscore_flag, iqr_flag, rolling_flag, iforest_flag, "
        "detector_votes, confidence, severity, persistence_days, detail) "
        "VALUES (:run, :metric, :date, :grain, :observed, :expected, :deviation, "
        ":direction, :zscore, :iqr, :rolling, :iforest, :votes, :confidence, "
        ":severity, :persistence, CAST(:detail AS JSONB))",
        params,
    )


def detect_and_persist_anomalies(
    db: Database, run_id: int, dates: set[str] | None = None,
    metrics: tuple[str, ...] = METRICS, votes_needed: int | None = None,
) -> list[FusedAnomaly]:
    """Fuse all metrics, optionally restrict to ``dates``, then persist.

    ``dates`` is normally the just-ingested file's own ``Order_Date`` values
    (as strings) - passed by the orchestrator so a new file never re-persists
    anomalies for dates an earlier run already recorded.
    """
    fused = fuse_all_metrics(db, metrics, votes_needed)
    if dates is not None:
        fused = [a for a in fused if a.date in dates]
    persist_anomalies(db, run_id, fused)
    return fused
