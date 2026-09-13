"""InsightForge AI - business impact engine (Phase 23 / spec Phase 46, FR-15).

A pure, on-demand module - same shape as ``src/change_detection.py`` (Phase
17) and ``src/root_cause.py`` (Phase 22): no database writes, not wired into
``src/orchestrator.py``. No table in ``sql/schema.sql`` stores this output
(``docs/data-flow.md`` stage 12's "Writes" column is plain text - "impact
figures" - not a backtick-quoted real table like Phase 20/21's
``anomalies``/``drift_results``).

The spec (verbatim): "PHASE 46 - BUSINESS IMPACT ENGINE: Create
src/impact_analysis.py. Calculate: Expected Revenue, Actual Revenue, Revenue
Gap, Expected Profit, Actual Profit, Profit Gap, Revenue at Risk, Profit at
Risk, Customers Affected, Orders Affected." No formula for "expected" is
given (same situation as every prior phase).

**"Expected" reuses Phase 19's rolling baseline** (``src.anomaly_detection
.detect_rolling_baseline``), which already computes a trailing
``ROLLING_WINDOW_DAYS``-day mean and documents it as the "expected range" for
a metric/date - exactly the concept ``docs/business-questions.md`` Q6 names
as this module's input ("KPI expected vs actual, RCA output"). Reusing it
means Phase 23 needs no new environment settings
(``ROLLING_WINDOW_DAYS``/``ROLLING_BASELINE_MULTIPLIER`` apply as-is) and
stays consistent with how "expected" is defined everywhere else in this
codebase.

**Gap vs. at-risk**: ``gap = actual - expected`` (signed - positive is
upside, negative is a shortfall, mirroring ``change_detection``'s
current-minus-previous convention). "At risk" is this project's own
operational reading of the spec's bare phrase: only a *shortfall* is a risk,
so ``at_risk = max(0.0, expected - actual)`` - a positive gap contributes
``0.0``, never a negative "risk".

**Customers/Orders Affected** come from ``fact_sales`` directly (same
query shape as ``src.root_cause``'s ``_fetch_fact_rows``): the distinct
customers/orders transacting on the assessed date - the population whose
activity produced that day's actual result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.anomaly_detection import daily_metric_series, detect_rolling_baseline
from src.database import Database

IMPACT_METRICS = ("revenue", "profit")


@dataclass(frozen=True)
class BusinessImpactResult:
    date: str
    expected_revenue: float
    actual_revenue: float
    revenue_gap: float
    expected_profit: float
    actual_profit: float
    profit_gap: float
    revenue_at_risk: float
    profit_at_risk: float
    customers_affected: int
    orders_affected: int
    evidence: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def compute_gap(expected: float, actual: float) -> tuple[float, float]:
    """``(gap, at_risk)`` for one metric.

    ``gap = actual - expected`` (signed). ``at_risk = max(0.0, expected -
    actual)`` - only a shortfall counts as risk; a positive gap (actual beat
    expected) contributes ``0.0``, never a negative "risk".
    """
    gap = round(actual - expected, 2)
    at_risk = round(max(0.0, expected - actual), 2)
    return gap, at_risk


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _fetch_customers_orders_affected(db: Database, date: str) -> tuple[int, int]:
    row = db.fetch_one(
        "SELECT COUNT(DISTINCT customer_id) AS customers, "
        "COUNT(DISTINCT order_id) AS orders "
        "FROM fact_sales WHERE order_date = :date",
        {"date": date},
    )
    if row is None:
        return 0, 0
    return int(row["customers"] or 0), int(row["orders"] or 0)


def _rolling_baseline_by_date(
    db: Database, metric: str, window: int | None, multiplier: float | None,
) -> dict:
    series = daily_metric_series(db, metric)
    results = detect_rolling_baseline(series, metric, window, multiplier)
    return {r.date: r for r in results}


def _build_result(db: Database, date: str, revenue_baseline, profit_baseline) -> BusinessImpactResult:
    revenue_gap, revenue_at_risk = compute_gap(revenue_baseline.rolling_mean, revenue_baseline.value)
    profit_gap, profit_at_risk = compute_gap(profit_baseline.rolling_mean, profit_baseline.value)
    customers_affected, orders_affected = _fetch_customers_orders_affected(db, date)

    return BusinessImpactResult(
        date=date,
        expected_revenue=revenue_baseline.rolling_mean, actual_revenue=revenue_baseline.value,
        revenue_gap=revenue_gap,
        expected_profit=profit_baseline.rolling_mean, actual_profit=profit_baseline.value,
        profit_gap=profit_gap,
        revenue_at_risk=revenue_at_risk, profit_at_risk=profit_at_risk,
        customers_affected=customers_affected, orders_affected=orders_affected,
        evidence={
            "revenue_rolling_std": revenue_baseline.rolling_std,
            "profit_rolling_std": profit_baseline.rolling_std,
            "revenue_lower_bound": revenue_baseline.lower_bound,
            "revenue_upper_bound": revenue_baseline.upper_bound,
            "profit_lower_bound": profit_baseline.lower_bound,
            "profit_upper_bound": profit_baseline.upper_bound,
        },
    )


def assess_business_impact(
    db: Database, date: str | None = None,
    window: int | None = None, multiplier: float | None = None,
) -> BusinessImpactResult | None:
    """Business impact for one date (default: the latest date the rolling
    baseline covers).

    Returns ``None`` when ``date`` isn't covered by the rolling baseline -
    either too little history overall, or a date within the leading
    ``window`` days that have no trailing baseline yet (``detect_rolling
    _baseline``'s own convention) - never fabricates an "expected" figure
    from an incomplete baseline.
    """
    revenue_by_date = _rolling_baseline_by_date(db, "revenue", window, multiplier)
    profit_by_date = _rolling_baseline_by_date(db, "profit", window, multiplier)

    if date is None:
        common_dates = sorted(set(revenue_by_date) & set(profit_by_date))
        if not common_dates:
            return None
        date = common_dates[-1]

    revenue_baseline = revenue_by_date.get(date)
    profit_baseline = profit_by_date.get(date)
    if revenue_baseline is None or profit_baseline is None:
        return None

    return _build_result(db, date, revenue_baseline, profit_baseline)


def assess_business_impact_series(
    db: Database, window: int | None = None, multiplier: float | None = None,
) -> list[BusinessImpactResult]:
    """:func:`assess_business_impact` for every date the rolling baseline
    covers (ascending by date) - a trend view for callers (a Streamlit page,
    the recommendation engine) that want more than just the latest day.

    Fetches each metric's rolling baseline once (not once per date) - unlike
    calling :func:`assess_business_impact` in a loop, which would redo both
    metrics' full daily-series + rolling-baseline computation per date.
    """
    revenue_by_date = _rolling_baseline_by_date(db, "revenue", window, multiplier)
    profit_by_date = _rolling_baseline_by_date(db, "profit", window, multiplier)
    common_dates = sorted(set(revenue_by_date) & set(profit_by_date))
    return [
        _build_result(db, date, revenue_by_date[date], profit_by_date[date])
        for date in common_dates
    ]
