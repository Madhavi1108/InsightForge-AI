"""InsightForge AI - root-cause engine, contribution analysis & RCA
confidence (Phase 22 / spec Phases 43-45, FR-14).

A pure, on-demand analytics module - same shape as
``src/change_detection.py`` (Phase 17): no database writes, not wired into
``src/orchestrator.py``. Confirmed this session from several independent
signals (no RCA table in ``sql/schema.sql``; ``docs/data-flow.md`` stage
11's "Writes" column is plain text, not a backtick-quoted real table like
Phase 20/21's ``anomalies``/``drift_results``; `docs/business-questions.md`
describes RCA as something *invoked* - "Change detection -> root-cause
engine -> AI Analyst" - not run automatically per file). Meant to be called
later by Phase 23 (business impact), Phase 26 (recommendations), Phase 28
(the AI Analyst), or a Streamlit page, whenever "why did metric X change"
needs answering.

The spec (verbatim): "PHASE 43 - ROOT-CAUSE ENGINE: Create
src/root_cause.py. Drill down: Region -> Category -> Subcategory -> Product
-> Customer Segment." / "PHASE 44 - CONTRIBUTION ANALYSIS: Calculate
contribution to KPI changes. Example: Revenue decline = Rs.8.7L - West
-Rs.5.1L, South -Rs.2.0L, North -Rs.1.1L, Other -Rs.0.5L." / "PHASE 45 -
ROOT-CAUSE CONFIDENCE: Every RCA result should contain: Metric, Change,
Primary Driver, Evidence, Contribution, Confidence."

**Contribution formula** (this project's own - the spec gives an example,
not a formula): for a metric and a dimension, each value's contribution is
``sum(metric, current period, that value) - sum(metric, previous period,
that value)`` - the natural additive decomposition matching the spec's own
worked example (the per-region amounts literally sum to the total change).
This only holds mathematically for **summable** metrics, so RCA is scoped
to :data:`RCA_SUPPORTED_METRICS` - Revenue/Profit/Units are true sums;
Margin/AOV/Return Rate/Avg Discount/Avg Shipping Time are ratios whose
"contribution" can't be additively decomposed this way without fabricating
a number, so this module refuses them (:class:`ValueError`) rather than
faking a result.

**Drill-down** is nested and sequential, not five independent breakdowns:
find the primary Region, then *within* that region find the primary
Category, then *within* region+category the primary Sub-Category, then
Product, then Customer Segment - matching
``docs/business-questions.md``'s "region tier of src/root_cause.py" /
"category tier" language.

**Confidence** is this project's own two-factor score
(``docs/root-cause-analysis.md``): ``0.7 * concentration + 0.3 *
evidence_strength``, where ``concentration`` measures how dominant the
single primary driver is versus a diffuse spread across many values, and
``evidence_strength`` rewards having enough supporting rows
(:data:`MIN_EVIDENCE_ROWS`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from src.database import Database

RCA_SUPPORTED_METRICS = ("revenue", "profit", "units")
_METRIC_COLUMN = {"revenue": "revenue", "profit": "profit", "units": "quantity"}

HIERARCHY = ("region", "category", "sub_category", "product", "customer_segment")
_DIMENSION_COLUMN = {
    "region": "region", "category": "category", "sub_category": "sub_category",
    "product": "product_id", "customer_segment": "customer_segment",
}

DEFAULT_MIN_EVIDENCE_ROWS = 30


def min_evidence_rows() -> int:
    """``RCA_MIN_EVIDENCE_ROWS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("RCA_MIN_EVIDENCE_ROWS", "").strip()
                   or DEFAULT_MIN_EVIDENCE_ROWS)
    except ValueError:
        return DEFAULT_MIN_EVIDENCE_ROWS


@dataclass(frozen=True)
class DimensionContribution:
    dimension: str
    value: str
    current_value: float
    previous_value: float
    contribution: float
    current_count: int
    previous_count: int


@dataclass(frozen=True)
class RootCauseTier:
    dimension: str
    filters: dict
    primary_value: str | None
    contribution: float | None
    confidence: float | None
    contributions: list[DimensionContribution] = field(default_factory=list)


@dataclass(frozen=True)
class RootCauseResult:
    metric: str
    change: float
    primary_driver: str
    contribution: float
    confidence: float
    evidence: dict
    tiers: list[RootCauseTier] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def contribution_by_dimension(
    current_rows: list[dict], previous_rows: list[dict], dimension: str, metric: str,
) -> list[DimensionContribution]:
    """Group rows by ``dimension`` and contrast each value's ``metric`` sum
    across the two periods. Contributions always sum to the total change.
    """
    column = _DIMENSION_COLUMN[dimension]
    metric_column = _METRIC_COLUMN[metric]

    def _sums(rows: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in rows:
            key = row[column]
            sums[key] = sums.get(key, 0.0) + float(row[metric_column])
            counts[key] = counts.get(key, 0) + 1
        return sums, counts

    current_sums, current_counts = _sums(current_rows)
    previous_sums, previous_counts = _sums(previous_rows)

    results = []
    for value in sorted(set(current_sums) | set(previous_sums)):
        cur = current_sums.get(value, 0.0)
        prev = previous_sums.get(value, 0.0)
        results.append(DimensionContribution(
            dimension=dimension, value=value, current_value=round(cur, 2),
            previous_value=round(prev, 2), contribution=round(cur - prev, 2),
            current_count=current_counts.get(value, 0), previous_count=previous_counts.get(value, 0),
        ))
    results.sort(key=lambda c: abs(c.contribution), reverse=True)
    return results


def _tier_confidence(contributions: list[DimensionContribution], evidence_rows: int) -> float:
    total_abs = sum(abs(c.contribution) for c in contributions)
    if total_abs == 0 or not contributions:
        concentration = 0.0
    else:
        concentration = abs(contributions[0].contribution) / total_abs
    evidence_strength = min(1.0, evidence_rows / min_evidence_rows())
    return round(0.7 * concentration + 0.3 * evidence_strength, 4)


def drill_down(
    current_rows: list[dict], previous_rows: list[dict], metric: str,
    hierarchy: tuple[str, ...] = HIERARCHY,
) -> list[RootCauseTier]:
    """Nested, sequential drill-down through ``hierarchy``.

    Each tier is scored only over the rows still matching every earlier
    tier's chosen ``primary_value``. Stops (fewer tiers than the full
    hierarchy) once a tier has no rows left to drill into.
    """
    tiers: list[RootCauseTier] = []
    filters: dict[str, str] = {}
    cur_rows, prev_rows = current_rows, previous_rows

    for dimension in hierarchy:
        if not cur_rows and not prev_rows:
            break
        contributions = contribution_by_dimension(cur_rows, prev_rows, dimension, metric)
        if not contributions:
            break
        primary = contributions[0]
        evidence_rows = primary.current_count + primary.previous_count
        confidence = _tier_confidence(contributions, evidence_rows)
        tiers.append(RootCauseTier(
            dimension=dimension, filters=dict(filters), primary_value=primary.value,
            contribution=primary.contribution, confidence=confidence,
            contributions=contributions,
        ))
        filters[dimension] = primary.value
        column = _DIMENSION_COLUMN[dimension]
        cur_rows = [r for r in cur_rows if r[column] == primary.value]
        prev_rows = [r for r in prev_rows if r[column] == primary.value]

    return tiers


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _fetch_fact_rows(db: Database, start: str, end: str) -> list[dict]:
    rows = db.fetch_all(
        "SELECT f.region, f.category, f.sub_category, f.product_id, "
        "p.product_name, f.customer_segment, f.revenue, f.profit, f.quantity "
        "FROM fact_sales f "
        "JOIN dim_product p ON p.product_key = f.product_key "
        "WHERE f.order_date >= :start AND f.order_date <= :end",
        {"start": start, "end": end},
    )
    return [dict(r) for r in rows]


def analyze_root_cause(
    db: Database, metric: str,
    current_start: str, current_end: str, previous_start: str, previous_end: str,
) -> RootCauseResult:
    """Fetch both periods' rows once, drill down, assemble the flat +
    tiered result.

    Raises :class:`ValueError` for a metric outside
    :data:`RCA_SUPPORTED_METRICS` - never fabricates a decomposition for a
    ratio metric it can't additively decompose.
    """
    if metric not in RCA_SUPPORTED_METRICS:
        raise ValueError(
            f"root cause analysis supports {RCA_SUPPORTED_METRICS}, got {metric!r} "
            "(ratio metrics can't be additively decomposed by dimension)"
        )

    current_rows = _fetch_fact_rows(db, current_start, current_end)
    previous_rows = _fetch_fact_rows(db, previous_start, previous_end)
    metric_column = _METRIC_COLUMN[metric]

    current_total = round(sum(float(r[metric_column]) for r in current_rows), 2)
    previous_total = round(sum(float(r[metric_column]) for r in previous_rows), 2)
    change = round(current_total - previous_total, 2)

    tiers = drill_down(current_rows, previous_rows, metric)

    primary_driver = " > ".join(t.primary_value for t in tiers) if tiers else "unknown"
    contribution = tiers[-1].contribution if tiers else 0.0
    confidences = [t.confidence for t in tiers if t.confidence is not None]
    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0

    evidence = {
        "current_period": [current_start, current_end],
        "previous_period": [previous_start, previous_end],
        "current_total": current_total, "previous_total": previous_total,
        "current_rows": len(current_rows), "previous_rows": len(previous_rows),
    }

    return RootCauseResult(
        metric=metric, change=change, primary_driver=primary_driver,
        contribution=contribution, confidence=confidence, evidence=evidence, tiers=tiers,
    )
