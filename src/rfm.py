"""InsightForge AI - customer RFM analysis (Phase 24 / spec Phase 47, FR-16).

A pure, on-demand module - same shape as ``src/change_detection.py`` (Phase
17), ``src/root_cause.py`` (Phase 22) and ``src/impact_analysis.py`` (Phase
23): no database writes, not wired into ``src/orchestrator.py``. No table in
``sql/schema.sql`` stores RFM segments, and ``docs/system-components.md``'s
Phase 24 stub carries no "Outputs:" line (unlike Phase 25/26's
``forecast_results``/``recommendations``) - the same absence-of-evidence
that placed Phase 17/22/23 in this category.

The spec (verbatim): "PHASE 47 - CUSTOMER RFM ANALYSIS: Calculate Recency,
Frequency, Monetary. Create: Champions, Loyal, Potential Loyalists, New, At
Risk, Lost." No scoring formula, no segment rule, and no reference date for
"recency" are given - this project's own operational choices follow.

**Reference date** ("today", for recency purposes) is the dataset's own
latest order date (``MAX(order_date)`` across ``fact_sales``), never
wall-clock "today" - this is a fixed synthetic dataset (ending 2026-09-08 at
generation time); wall-clock "today" would call every customer "Lost".
Every other date-relative module in this codebase (Phase 19's rolling
baseline, Phase 21's drift window) is likewise dataset-relative, not
wall-clock.

**R/F/M scoring** is this project's own operationalization of the standard
RFM quintile technique (external, well-known method; the specific binning is
ours, same "standard method, our own thresholds" framing as Phase 21's PSI
bands): each of recency/frequency/monetary is split into 5 bins via the
20/40/60/80th percentiles of the *current customer population*
(:func:`score_quintiles`, the same ``numpy.percentile`` idiom
``anomaly_detection.detect_iqr`` uses for quartiles). Recency is reversed -
fewer days since the last order is *better*, so it gets the higher score.

**Segment assignment** (:func:`classify_segment`) is a documented,
priority-ordered rule mapping every possible ``(r, f, m)`` score combination
to exactly one of the spec's 6 named segments - FR-16 requires every active
customer get one of the 6, so there is no "Other" fallback; the final rule
is a catch-all ("Potential Loyalists" - recent-ish, not yet a Champion or
Loyal customer, the standard RFM-literature reading of that segment name).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.database import Database

SEGMENTS = ("Champions", "Loyal", "Potential Loyalists", "New", "At Risk", "Lost")


@dataclass(frozen=True)
class CustomerRFM:
    customer_id: str
    customer_name: str
    customer_segment: str
    recency_days: int
    frequency: int
    monetary: float
    r_score: int
    f_score: int
    m_score: int
    segment: str


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def score_quintiles(values: list[float], reverse: bool = False) -> list[int]:
    """Bin ``values`` into quintiles (1-5) via the 20/40/60/80th
    percentiles of ``values`` itself.

    ``reverse=True`` flips the scale (used for recency, where a *smaller*
    value is better and should score higher). Returns ``[]`` for empty
    input; a single distinct value (zero spread) scores every row ``3``
    (the neutral middle) rather than dividing by a zero-width band.
    """
    if not values:
        return []
    arr = np.asarray(values, dtype=float)
    edges = np.percentile(arr, [20, 40, 60, 80])
    if edges[0] == edges[-1]:
        return [3] * len(values)
    scores = np.digitize(arr, edges, right=True) + 1
    scores = np.clip(scores, 1, 5)
    if reverse:
        scores = 6 - scores
    return [int(s) for s in scores]


def classify_segment(r: int, f: int, m: int) -> str:
    """One of :data:`SEGMENTS` for an ``(r, f, m)`` score triple (each
    1-5). Priority-ordered - the first matching rule wins - so every
    combination resolves to exactly one segment, never none.
    """
    if r >= 4 and f >= 4 and m >= 4:
        return "Champions"
    if f >= 4 and m >= 3:
        return "Loyal"
    if r <= 2 and (f >= 3 or m >= 3):
        return "At Risk"
    if r <= 2 and f <= 2 and m <= 2:
        return "Lost"
    if r >= 4 and f <= 2:
        return "New"
    return "Potential Loyalists"


def build_customer_rfm(rows: list[dict], reference_date) -> list[CustomerRFM]:
    """Assemble :class:`CustomerRFM` for every row.

    ``rows`` is ``[{customer_id, customer_name, customer_segment,
    last_order_date, frequency, monetary}, ...]`` - one per customer.
    Scores are computed across the *whole* ``rows`` population (quintiles
    need the full population to mean anything), so this is a single batch
    operation, not one row at a time.
    """
    if not rows:
        return []
    recency_days = [(reference_date - r["last_order_date"]).days for r in rows]
    frequency = [r["frequency"] for r in rows]
    monetary = [r["monetary"] for r in rows]

    r_scores = score_quintiles(recency_days, reverse=True)
    f_scores = score_quintiles(frequency)
    m_scores = score_quintiles(monetary)

    results = []
    for row, days, r_score, f_score, m_score in zip(rows, recency_days, r_scores, f_scores, m_scores):
        results.append(CustomerRFM(
            customer_id=row["customer_id"], customer_name=row["customer_name"],
            customer_segment=row["customer_segment"],
            recency_days=days, frequency=row["frequency"], monetary=round(float(row["monetary"]), 2),
            r_score=r_score, f_score=f_score, m_score=m_score,
            segment=classify_segment(r_score, f_score, m_score),
        ))
    return results


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _fetch_customer_aggregates(db: Database) -> list[dict]:
    rows = db.fetch_all(
        "SELECT f.customer_id, c.customer_name, c.customer_segment, "
        "MAX(f.order_date) AS last_order_date, "
        "COUNT(DISTINCT f.order_id) AS frequency, "
        "SUM(f.revenue) AS monetary "
        "FROM fact_sales f JOIN dim_customer c ON c.customer_key = f.customer_key "
        "GROUP BY f.customer_id, c.customer_name, c.customer_segment"
    )
    return [dict(r) for r in rows]


def _fetch_reference_date(db: Database):
    return db.scalar("SELECT MAX(order_date) FROM fact_sales")


def analyze_customer_rfm(db: Database, reference_date=None) -> list[CustomerRFM]:
    """Every customer's RFM segment.

    ``reference_date`` defaults to the dataset's own latest order date
    (``MAX(order_date)`` across ``fact_sales``) - never wall-clock "today".
    Returns ``[]`` when there are no customers yet (an empty/young
    database), not an error.
    """
    rows = _fetch_customer_aggregates(db)
    if not rows:
        return []
    if reference_date is None:
        reference_date = _fetch_reference_date(db)
    return build_customer_rfm(rows, reference_date)
