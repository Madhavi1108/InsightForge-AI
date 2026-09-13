"""InsightForge AI - period comparison & business change detection
(Phase 17 / spec Phases 33-34, FR-09/FR-10).

The first Python analytics module. Compares the two most recent complete
periods - day vs previous day, week vs previous week, month vs previous
month (spec-named grains, nothing else) - across the 10 core KPIs
(`docs/kpi-engine.md`) and flags movements beyond a documented threshold.

**Not persisted, not orchestrator-wired.** No table in ``sql/schema.sql``
stores this output (``docs/data-flow.md`` stage 8: "Writes: change records" -
not a table); this is a downstream/batch analytics module consumed directly
by callers (later, anomaly detection and recommendations), not part of
``src/orchestrator.py``'s per-file pipeline.

Queries ``fact_sales`` directly per grain (not the Phase 16 views) so
distinct-count metrics (``customers``, ``orders``) are correct at the week
grain - summing daily distinct counts would double-count a customer who
ordered on two different days in the same week.

The spec names the grains and gives an example output shape but no
significance threshold; ``CHANGE_DETECTION_THRESHOLD_PCT`` (default 10.0) is
this project's own documented operational definition (``docs/change-detection.md``),
same pattern as ``src.data_quality``'s thresholds.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from src.database import Database

GRAINS = ("day", "week", "month")

#: The 10 KPI columns every per-grain query returns (docs/kpi-engine.md).
METRICS = (
    "revenue", "profit", "margin_pct", "orders", "customers", "units",
    "aov", "return_rate_pct", "avg_discount_pct", "avg_shipping_days",
)

_METRIC_SELECT = """\
    ROUND(SUM(revenue), 2)                                             AS revenue,
    ROUND(SUM(profit), 2)                                              AS profit,
    ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 2)            AS margin_pct,
    COUNT(DISTINCT order_id)                                           AS orders,
    COUNT(DISTINCT customer_id)                                        AS customers,
    SUM(quantity)                                                      AS units,
    ROUND(SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0), 2)       AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(discount), 2)                                    AS avg_discount_pct,
    ROUND(AVG(shipping_days), 2)                                       AS avg_shipping_days
"""

_DAY_SQL = f"""
SELECT
    order_date AS period_key,
{_METRIC_SELECT}
FROM fact_sales
GROUP BY order_date
ORDER BY period_key
"""

_WEEK_SQL = f"""
SELECT
    date_trunc('week', order_date)::date AS period_key,
{_METRIC_SELECT}
FROM fact_sales
GROUP BY date_trunc('week', order_date)
ORDER BY period_key
"""

_MONTH_SQL = f"""
SELECT
    date_trunc('month', order_date)::date AS period_key,
{_METRIC_SELECT}
FROM fact_sales
GROUP BY date_trunc('month', order_date)
ORDER BY period_key
"""

_GRAIN_SQL = {"day": _DAY_SQL, "week": _WEEK_SQL, "month": _MONTH_SQL}

DEFAULT_THRESHOLD_PCT = 10.0


def significant_change_threshold_pct() -> float:
    """``CHANGE_DETECTION_THRESHOLD_PCT`` from the environment (fresh each call)."""
    try:
        return float(os.environ.get("CHANGE_DETECTION_THRESHOLD_PCT", "").strip()
                     or DEFAULT_THRESHOLD_PCT)
    except ValueError:
        return DEFAULT_THRESHOLD_PCT


@dataclass(frozen=True)
class ChangeRecord:
    grain: str
    metric: str
    period_key: str
    previous_period_key: str
    current: float | int | None
    previous: float | int | None
    pct_change: float | None
    direction: str  # "up" | "down" | "flat"
    magnitude: float | None
    significant: bool


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def _direction(current, previous) -> str:
    if current is None or previous is None:
        return "flat"
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def _pct_change(current, previous) -> float | None:
    """``None`` when undefined (either side missing, or ``previous == 0``)."""
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    return round(100.0 * (float(current) - float(previous)) / abs(float(previous)), 2)


def build_change_records(
    current_row: dict, previous_row: dict, grain: str,
    period_key: str, previous_period_key: str, threshold_pct: float | None = None,
) -> list[ChangeRecord]:
    """One :class:`ChangeRecord` per metric present in both rows.

    A move from ``0`` to a non-zero value has an undefined percentage
    (``pct_change=None``) but is still flagged ``significant=True`` - never
    silently dropped just because the ratio can't be computed.
    """
    threshold = threshold_pct if threshold_pct is not None else significant_change_threshold_pct()
    records = []
    for metric in METRICS:
        if metric not in current_row or metric not in previous_row:
            continue
        current = current_row[metric]
        previous = previous_row[metric]
        pct = _pct_change(current, previous)
        direction = _direction(current, previous)
        magnitude = abs(pct) if pct is not None else None
        zero_to_nonzero = (previous == 0 and current not in (None, 0))
        significant = zero_to_nonzero or (magnitude is not None and magnitude >= threshold)
        records.append(ChangeRecord(
            grain=grain, metric=metric,
            period_key=str(period_key), previous_period_key=str(previous_period_key),
            current=current, previous=previous, pct_change=pct,
            direction=direction, magnitude=magnitude, significant=significant,
        ))
    return records


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def compare_period(db: Database, grain: str, threshold_pct: float | None = None) -> list[ChangeRecord]:
    """Compare the two most recent complete periods for ``grain``.

    ``grain`` is one of :data:`GRAINS`. Returns ``[]`` when fewer than two
    periods of data exist yet (a young dataset - not an error).
    """
    if grain not in _GRAIN_SQL:
        raise ValueError(f"unknown grain {grain!r}, expected one of {GRAINS}")
    rows = db.fetch_all(_GRAIN_SQL[grain])
    if len(rows) < 2:
        return []
    previous_row, current_row = rows[-2], rows[-1]
    return build_change_records(
        current_row, previous_row, grain,
        current_row["period_key"], previous_row["period_key"], threshold_pct,
    )


def compare_all_periods(db: Database, threshold_pct: float | None = None) -> list[ChangeRecord]:
    """:func:`compare_period` for every grain in :data:`GRAINS`."""
    records: list[ChangeRecord] = []
    for grain in GRAINS:
        records.extend(compare_period(db, grain, threshold_pct))
    return records
