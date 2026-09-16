"""InsightForge AI - product intelligence (Phase 24 / spec Phase 48, FR-17).

A pure, on-demand module - same shape as ``src/rfm.py`` (this phase's sister
module) and every other unwired analytics module (Phases 17/22/23): no
database writes, not wired into ``src/orchestrator.py``.

The spec (verbatim): "PHASE 48 - PRODUCT INTELLIGENCE: Classify products:
Star, High Profit, High Revenue, Fast Growing, Declining, High Return, Low
Margin, Slow Moving. Create product health score." No thresholds, no growth
window, no "Star"/health-score formula are given - this project's own
operational choices follow.

**Base aggregates are reused, not re-derived**: revenue/profit/margin_pct/
units/orders/return_rate_pct come straight from Phase 16's
``product_performance`` view (``SELECT * FROM product_performance``) - the
same numbers ``docs/business-questions.md`` Q4 already names as this
module's source.

**Growth** (Fast Growing/Declining) needs something ``product_performance``
doesn't have - a trend. This module compares each product's revenue over the
trailing ``PRODUCT_GROWTH_WINDOW_DAYS`` (default 30) against the equal-length
window immediately before it, both anchored to the dataset's own latest
order date (same dataset-relative convention as ``src.rfm``'s reference
date - never wall-clock "today"). A product absent from the previous window
(no prior revenue to compare against) gets ``growth_pct=None`` - undefined,
not fabricated as +infinity or 0 (matching
``change_detection._pct_change``'s convention for the same situation).

**Classification** is quartile-relative (this project's own thresholds,
the same ``numpy.percentile`` idiom ``anomaly_detection.detect_iqr`` uses):
a product is "High X" when it sits in the top quartile of the current
product population for that metric, "Low X"/"Slow Moving" in the bottom
quartile. "Star" is this project's own composite - high revenue *and* high
profit *and* fast growing simultaneously (a BCG-matrix-style "all-round
winner" reading of the spec listing "Star" alongside independent
single-factor tags with no definition of its own).

**Health score** (0-100) is this project's own weighted composite, the same
"combine several documented factors with fixed weights" shape as Phase 20's
anomaly severity engine: profit rank, margin rank, growth rank and
(1 - return rank), each a 0-1 percentile rank within the current product
population, weighted 0.35/0.25/0.20/0.20.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from src.database import Database

DEFAULT_GROWTH_WINDOW_DAYS = 30
DEFAULT_GROWTH_THRESHOLD_PCT = 20.0

_HEALTH_WEIGHTS = {"profit": 0.35, "margin": 0.25, "growth": 0.20, "return": 0.20}


def growth_window_days() -> int:
    """``PRODUCT_GROWTH_WINDOW_DAYS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("PRODUCT_GROWTH_WINDOW_DAYS", "").strip()
                   or DEFAULT_GROWTH_WINDOW_DAYS)
    except ValueError:
        return DEFAULT_GROWTH_WINDOW_DAYS


def growth_threshold_pct() -> float:
    """``PRODUCT_GROWTH_THRESHOLD_PCT`` from the environment (fresh each call)."""
    try:
        return float(os.environ.get("PRODUCT_GROWTH_THRESHOLD_PCT", "").strip()
                     or DEFAULT_GROWTH_THRESHOLD_PCT)
    except ValueError:
        return DEFAULT_GROWTH_THRESHOLD_PCT


@dataclass(frozen=True)
class ProductIntelligence:
    product_id: str
    product_name: str
    category: str
    revenue: float
    profit: float
    margin_pct: float
    units: int
    orders: int
    return_rate_pct: float
    revenue_growth_pct: float | None
    is_star: bool
    is_high_profit: bool
    is_high_revenue: bool
    is_fast_growing: bool
    is_declining: bool
    is_high_return: bool
    is_low_margin: bool
    is_slow_moving: bool
    health_score: float


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def percentile_rank(values: list[float]) -> list[float]:
    """Each value's rank in ``[0.0, 1.0]`` among ``values`` (0 = lowest, 1 =
    highest). Ties share the average rank. A single distinct value (zero
    spread) ranks every row ``0.5`` (neutral) rather than an arbitrary 0/1.
    """
    if not values:
        return []
    arr = np.asarray(values, dtype=float)
    if arr.max() == arr.min():
        return [0.5] * len(values)
    order = arr.argsort()
    ranks = np.empty(len(arr), dtype=float)
    ranks[order] = np.arange(len(arr))
    # average rank for ties
    for value in np.unique(arr):
        mask = arr == value
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    return (ranks / (len(arr) - 1)).tolist()


def classify_products(
    totals: list[dict], growth_by_product: dict[str, float | None],
    threshold_pct: float | None = None,
) -> list[ProductIntelligence]:
    """Classify every product in ``totals``.

    ``totals`` is ``[{product_id, product_name, category, revenue, profit,
    margin_pct, units, orders, return_rate_pct}, ...]`` (``product
    _performance``'s shape). ``growth_by_product`` maps ``product_id ->
    growth_pct | None``. Thresholds/ranks are computed across the whole
    ``totals`` population, so this is a single batch operation.
    """
    if not totals:
        return []
    threshold = threshold_pct if threshold_pct is not None else growth_threshold_pct()

    # Postgres NUMERIC columns come back as decimal.Decimal (psycopg2), which
    # np.percentile can't interpolate against a float weight - coerce once
    # here rather than at every call site.
    revenue = [float(t["revenue"]) for t in totals]
    profit = [float(t["profit"]) for t in totals]
    margin = [float(t["margin_pct"]) for t in totals]
    units = [float(t["units"]) for t in totals]
    return_rate = [float(t["return_rate_pct"]) for t in totals]

    revenue_p75 = float(np.percentile(revenue, 75))
    profit_p75 = float(np.percentile(profit, 75))
    return_p75 = float(np.percentile(return_rate, 75))
    margin_p25 = float(np.percentile(margin, 25))
    units_p25 = float(np.percentile(units, 25))

    profit_ranks = percentile_rank(profit)
    margin_ranks = percentile_rank(margin)
    return_ranks = percentile_rank(return_rate)
    growth_values = [growth_by_product.get(t["product_id"]) for t in totals]
    growth_ranks = percentile_rank([g for g in growth_values if g is not None])
    growth_rank_by_index: dict[int, float] = {}
    _idx = 0
    for i, g in enumerate(growth_values):
        if g is not None:
            growth_rank_by_index[i] = growth_ranks[_idx]
            _idx += 1

    results = []
    for i, t in enumerate(totals):
        growth_pct = growth_values[i]
        is_high_revenue = t["revenue"] >= revenue_p75
        is_high_profit = t["profit"] >= profit_p75
        is_high_return = t["return_rate_pct"] >= return_p75
        is_low_margin = t["margin_pct"] <= margin_p25
        is_slow_moving = t["units"] <= units_p25
        is_fast_growing = growth_pct is not None and growth_pct >= threshold
        is_declining = growth_pct is not None and growth_pct <= -threshold
        is_star = is_high_revenue and is_high_profit and is_fast_growing

        growth_rank = growth_rank_by_index.get(i, 0.5)
        health_score = round(100.0 * (
            _HEALTH_WEIGHTS["profit"] * profit_ranks[i]
            + _HEALTH_WEIGHTS["margin"] * margin_ranks[i]
            + _HEALTH_WEIGHTS["growth"] * growth_rank
            + _HEALTH_WEIGHTS["return"] * (1.0 - return_ranks[i])
        ), 2)

        results.append(ProductIntelligence(
            product_id=t["product_id"], product_name=t["product_name"], category=t["category"],
            revenue=t["revenue"], profit=t["profit"], margin_pct=t["margin_pct"],
            units=t["units"], orders=t["orders"], return_rate_pct=t["return_rate_pct"],
            revenue_growth_pct=growth_pct,
            is_star=is_star, is_high_profit=is_high_profit, is_high_revenue=is_high_revenue,
            is_fast_growing=is_fast_growing, is_declining=is_declining,
            is_high_return=is_high_return, is_low_margin=is_low_margin, is_slow_moving=is_slow_moving,
            health_score=health_score,
        ))
    return results


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _fetch_product_totals(db: Database) -> list[dict]:
    rows = db.fetch_all("SELECT * FROM product_performance")
    return [dict(r) for r in rows]


def _fetch_reference_date(db: Database):
    return db.scalar("SELECT MAX(order_date) FROM fact_sales")


def _fetch_growth_by_product(db: Database, reference_date, window_days: int) -> dict[str, float | None]:
    rows = db.fetch_all(
        "SELECT product_id, "
        "SUM(revenue) FILTER (WHERE order_date > :mid AND order_date <= :end) AS current_revenue, "
        "SUM(revenue) FILTER (WHERE order_date > :start AND order_date <= :mid) AS previous_revenue "
        "FROM fact_sales WHERE order_date > :start AND order_date <= :end "
        "GROUP BY product_id",
        {
            "end": reference_date,
            "mid": reference_date - timedelta(days=window_days),
            "start": reference_date - timedelta(days=2 * window_days),
        },
    )
    growth: dict[str, float | None] = {}
    for row in rows:
        current = float(row["current_revenue"] or 0.0)
        previous = row["previous_revenue"]
        if previous is None or float(previous) == 0.0:
            growth[row["product_id"]] = None
        else:
            growth[row["product_id"]] = round(100.0 * (current - float(previous)) / float(previous), 2)
    return growth


def analyze_product_intelligence(
    db: Database, growth_window_days_: int | None = None, growth_threshold_pct_: float | None = None,
) -> list[ProductIntelligence]:
    """Every product's classification and health score.

    Growth is measured over the trailing ``growth_window_days_`` (default
    :func:`growth_window_days`) against the equal-length window before it,
    anchored to the dataset's own latest order date. Returns ``[]`` when
    there are no products yet.
    """
    totals = _fetch_product_totals(db)
    if not totals:
        return []
    window = growth_window_days_ if growth_window_days_ is not None else growth_window_days()
    reference_date = _fetch_reference_date(db)
    growth_by_product = _fetch_growth_by_product(db, reference_date, window)
    return classify_products(totals, growth_by_product, growth_threshold_pct_)
