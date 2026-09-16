"""InsightForge AI - data drift detection & drift reporting (Phase 21 /
spec Phases 41-42, FR-13).

Monitors 6 distributions FR-13 names - price, quantity, discount, shipping
(continuous) and category mix, region mix (categorical) - via the
**Population Stability Index (PSI)**, classifying each
Normal/Warning/Drift Detected. Persists to the new ``drift_results`` table
(``sql/schema.sql``) and is wired into ``src/orchestrator.py`` as stage 8,
same reasoning as Phase 20's anomaly fusion: ``drift_results.run_id`` is
``NOT NULL``, so a row only makes sense tied to the run that checked it.
Like anomaly fusion, **drift detection never affects the run's terminal
status** - it's advisory analytics, not a correctness gate.

The spec (verbatim): "PHASE 41 - DATA DRIFT DETECTION: Create
src/drift_detection.py. Monitor: price distribution, quantity distribution,
discount distribution, shipping distribution, category mix, region mix." /
"PHASE 42 - DRIFT REPORTING: Generate Normal/Warning/Drift Detected. Store
historical drift results." Phase 42 names no separate file - its
classification and storage are folded into this same module, the same way
Phase 20's severity engine folded into ``anomaly_fusion.py``.

**Method and thresholds are this project's own choice** (the spec names no
statistical technique): PSI is the industry-standard method for exactly this
kind of "has this distribution shifted" question, and its own conventional
thresholds (``< 0.1`` no significant shift, ``0.1-0.25`` moderate shift,
``>= 0.25`` significant shift) map directly onto the spec's three-tier
output - unlike most other spec-silent formulas in this project, these
thresholds are an established external convention, not invented here.
Window sizes (``DRIFT_BASELINE_DAYS``, ``DRIFT_CURRENT_DAYS``) are this
project's own defaults.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.database import Database

DEFAULT_BASELINE_DAYS = 30
DEFAULT_CURRENT_DAYS = 7
MIN_BASELINE_ROWS = 30
MIN_CURRENT_ROWS = 5
PSI_BINS = 10

#: (column, is_categorical) for each of FR-13's 6 monitored distributions.
FEATURES: dict[str, tuple[str, bool]] = {
    "price": ("unit_price", False),
    "quantity": ("quantity", False),
    "discount": ("discount", False),
    "shipping": ("shipping_days", False),
    "category_mix": ("category", True),
    "region_mix": ("region", True),
}

PSI_WARNING_THRESHOLD = 0.1
PSI_DRIFT_THRESHOLD = 0.25


def baseline_days() -> int:
    """``DRIFT_BASELINE_DAYS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("DRIFT_BASELINE_DAYS", "").strip() or DEFAULT_BASELINE_DAYS)
    except ValueError:
        return DEFAULT_BASELINE_DAYS


def current_days() -> int:
    """``DRIFT_CURRENT_DAYS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("DRIFT_CURRENT_DAYS", "").strip() or DEFAULT_CURRENT_DAYS)
    except ValueError:
        return DEFAULT_CURRENT_DAYS


@dataclass(frozen=True)
class DriftResult:
    feature: str
    psi_score: float
    status: str  # "Normal" | "Warning" | "Drift Detected"
    baseline_start: str
    baseline_end: str
    current_start: str
    current_end: str
    baseline_count: int
    current_count: int
    detail: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure PSI core - no database
# --------------------------------------------------------------------------- #
def classify_psi(psi: float) -> str:
    if psi >= PSI_DRIFT_THRESHOLD:
        return "Drift Detected"
    if psi >= PSI_WARNING_THRESHOLD:
        return "Warning"
    return "Normal"


def _psi_from_distributions(baseline_counts: dict, current_counts: dict) -> float:
    """PSI given two frequency distributions over the same bin/category keys."""
    baseline_total = sum(baseline_counts.values())
    current_total = sum(current_counts.values())
    if baseline_total == 0 or current_total == 0:
        return 0.0
    keys = set(baseline_counts) | set(current_counts)
    epsilon = 1e-4  # avoid log(0) / division by zero for an empty bin
    psi = 0.0
    for key in keys:
        b_pct = max(baseline_counts.get(key, 0) / baseline_total, epsilon)
        c_pct = max(current_counts.get(key, 0) / current_total, epsilon)
        psi += (c_pct - b_pct) * math.log(c_pct / b_pct)
    return round(psi, 6)


def _quantile_bin_edges(values: list[float], bins: int) -> list[float]:
    """``bins - 1`` interior edges from the baseline's own quantiles."""
    if not values:
        return []
    sorted_values = sorted(values)
    n = len(sorted_values)
    edges = []
    for i in range(1, bins):
        idx = min(n - 1, int(round(i / bins * (n - 1))))
        edges.append(sorted_values[idx])
    return sorted(set(edges))


def _bin_counts(values: list[float], edges: list[float]) -> dict[int, int]:
    counts: dict[int, int] = {i: 0 for i in range(len(edges) + 1)}
    for v in values:
        bucket = 0
        while bucket < len(edges) and v > edges[bucket]:
            bucket += 1
        counts[bucket] += 1
    return counts


def compute_psi_continuous(baseline_values: list[float], current_values: list[float],
                           bins: int = PSI_BINS) -> float:
    """PSI for a continuous feature - bin edges come from the baseline's own
    quantiles, so the baseline distribution is uniform across bins by
    construction and all drift signal comes from how the current values
    redistribute across those same bins."""
    edges = _quantile_bin_edges(baseline_values, bins)
    baseline_counts = _bin_counts(baseline_values, edges)
    current_counts = _bin_counts(current_values, edges)
    return _psi_from_distributions(baseline_counts, current_counts)


def compute_psi_categorical(baseline_values: list[str], current_values: list[str]) -> float:
    """PSI for a categorical feature - natural category values are the bins."""
    baseline_counts: dict[str, int] = {}
    for v in baseline_values:
        baseline_counts[v] = baseline_counts.get(v, 0) + 1
    current_counts: dict[str, int] = {}
    for v in current_values:
        current_counts[v] = current_counts.get(v, 0) + 1
    return _psi_from_distributions(baseline_counts, current_counts)


def detect_drift_for_feature(
    feature: str, baseline_values: list, current_values: list, is_categorical: bool,
    baseline_start: str, baseline_end: str, current_start: str, current_end: str,
) -> DriftResult | None:
    """Returns ``None`` when either window has too few rows to judge."""
    if len(baseline_values) < MIN_BASELINE_ROWS or len(current_values) < MIN_CURRENT_ROWS:
        return None
    if is_categorical:
        psi = compute_psi_categorical(baseline_values, current_values)
    else:
        psi = compute_psi_continuous(baseline_values, current_values)
    return DriftResult(
        feature=feature, psi_score=psi, status=classify_psi(psi),
        baseline_start=baseline_start, baseline_end=baseline_end,
        current_start=current_start, current_end=current_end,
        baseline_count=len(baseline_values), current_count=len(current_values),
        detail={"bins" if not is_categorical else "categories":
               sorted(set(baseline_values)) if is_categorical else PSI_BINS},
    )


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _fetch_column(db: Database, column: str, start: date, end: date) -> list:
    rows = db.fetch_all(
        f"SELECT {column} AS value FROM fact_sales "
        f"WHERE order_date >= :start AND order_date <= :end AND {column} IS NOT NULL",
        {"start": start, "end": end},
    )
    return [float(r["value"]) if isinstance(r["value"], (int, float)) else r["value"]
            for r in rows if r["value"] is not None]


def detect_all_drift(
    db: Database, current_end_date: date | str,
    baseline_window: int | None = None, current_window: int | None = None,
) -> list[DriftResult]:
    """Drift for all 6 FR-13 features, comparing the trailing ``current_window``
    days (ending at ``current_end_date``) against the ``baseline_window`` days
    immediately before that.
    """
    if isinstance(current_end_date, str):
        current_end_date = date.fromisoformat(current_end_date)
    current_window = current_window if current_window is not None else current_days()
    baseline_window = baseline_window if baseline_window is not None else baseline_days()

    current_start = current_end_date - timedelta(days=current_window - 1)
    baseline_end = current_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=baseline_window - 1)

    results = []
    for feature, (column, is_categorical) in FEATURES.items():
        baseline_values = _fetch_column(db, column, baseline_start, baseline_end)
        current_values = _fetch_column(db, column, current_start, current_end_date)
        result = detect_drift_for_feature(
            feature, baseline_values, current_values, is_categorical,
            baseline_start.isoformat(), baseline_end.isoformat(),
            current_start.isoformat(), current_end_date.isoformat(),
        )
        if result is not None:
            results.append(result)
    return results


def persist_drift_results(db: Database, run_id: int, results: list[DriftResult]) -> int:
    """Bulk-insert ``drift_results`` rows for this run. Returns the count written."""
    if not results:
        return 0
    params = [
        {
            "run": run_id, "feature": r.feature, "psi": r.psi_score, "status": r.status,
            "b_start": r.baseline_start, "b_end": r.baseline_end,
            "c_start": r.current_start, "c_end": r.current_end,
            "b_count": r.baseline_count, "c_count": r.current_count,
            "detail": json.dumps(r.detail, ensure_ascii=False, default=str),
        }
        for r in results
    ]
    return db.execute_many(
        "INSERT INTO drift_results "
        "(run_id, feature, psi_score, status, baseline_start, baseline_end, "
        "current_start, current_end, baseline_count, current_count, detail) "
        "VALUES (:run, :feature, :psi, :status, :b_start, :b_end, :c_start, :c_end, "
        ":b_count, :c_count, CAST(:detail AS JSONB))",
        params,
    )


def detect_and_persist_drift(
    db: Database, run_id: int, current_end_date: date | str,
    baseline_window: int | None = None, current_window: int | None = None,
) -> list[DriftResult]:
    """:func:`detect_all_drift` then :func:`persist_drift_results`."""
    results = detect_all_drift(db, current_end_date, baseline_window, current_window)
    persist_drift_results(db, run_id, results)
    return results
