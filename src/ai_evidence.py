"""InsightForge AI - AI evidence layer (Phase 27 / spec Phase 53, part of
FR-20).

Assembles a structured, **verified** evidence package for one metric -
current/previous/change, primary region/category, business impact, forecast
trend, matching anomaly and recommendations - from the outputs of
already-built analytics modules. **No new analytics, no LLM call.**

``docs/system-components.md`` (verbatim): "`src/ai_evidence.py` (new Phase
27) - Responsibility: assemble a structured, verified evidence package
(metric, current, previous, change %, primary region, primary category,
impact, ...) before any LLM call." That boundary is deliberate -
``docs/security.md`` section 3: "The LLM receives a **structured evidence
package** built from verified analytical results... The LLM must not invent
numbers - every figure in an explanation traces to the evidence package."
Phase 27 (this module) builds that package; Phase 28 (``src/ai_analyst.py``,
not yet built) is what actually calls Gemini or falls back to a
deterministic template - this module never imports ``google.generativeai``
and never makes a network call.

**On-demand, no persistence, not orchestrator-wired** - same shape as
Phases 17/22/23/24 (change detection/RCA/impact/RFM): no table in
``sql/schema.sql`` stores an "evidence package" (there is no ``evidence``
table), and assembling one needs cross-cutting, whole-history analytics
(change, RCA, impact, forecast, recommendations, anomalies), not a single
just-ingested file's dates.

**Composition, not new formulas.** Every field is read from an existing
module's own already-documented output:

- current/previous/pct_change/direction/significant -
  :func:`src.change_detection.compare_period` (day grain).
- primary_region/primary_category/root_cause_contribution/root_cause
  _confidence - :func:`src.root_cause.analyze_root_cause`'s tiers, for
  metrics it supports (revenue/profit/units).
- expected/actual/gap/at_risk/customers_affected/orders_affected -
  :func:`src.impact_analysis.assess_business_impact`, for revenue/profit.
- forecast_trend/forecast_next_7d_value -
  :func:`src.forecasting.forecast_metric` (7-day horizon), for
  revenue/profit/orders.
- anomaly_severity/anomaly_confidence - a small best-effort ``anomalies``
  lookup, the same query shape as ``src.recommendations._lookup_anomaly``.
- recommendations - :func:`src.recommendations.generate_recommendations`,
  filtered to this metric.

Every sub-lookup beyond the base day comparison is **best-effort and
wrapped** (a failure degrades to that field being absent, never a
fabricated number) - the same "advisory, never fails" convention every
prior phase uses. ``sources`` records exactly which modules actually
contributed a field, so every number in the package is traceable back to
the analytics call that produced it (`docs/security.md`'s traceability
requirement, one level further back than the explanation text itself).

**Aggregate confidence** (this project's own documented choice, same "own
aggregate formula" pattern as RCA's ``0.7*concentration + 0.3*evidence
_strength``): the mean of whichever confidence signals are present -
magnitude-based confidence (``min(1.0, magnitude / 100)``, the same formula
``src.recommendations._magnitude_confidence`` uses) always contributes;
``root_cause_confidence`` and ``anomaly_confidence`` contribute when
available.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.change_detection import compare_period
from src.database import Database
from src.forecasting import FORECAST_METRICS, forecast_metric
from src.impact_analysis import IMPACT_METRICS, assess_business_impact
from src.recommendations import generate_recommendations
from src.root_cause import RCA_SUPPORTED_METRICS, analyze_root_cause


@dataclass(frozen=True)
class EvidencePackage:
    metric: str
    period_key: str
    previous_period_key: str
    current: float | int | None
    previous: float | int | None
    pct_change: float | None
    direction: str
    significant: bool
    primary_region: str | None
    primary_category: str | None
    root_cause_contribution: float | None
    root_cause_confidence: float | None
    expected: float | None
    actual: float | None
    gap: float | None
    at_risk: float | None
    customers_affected: int | None
    orders_affected: int | None
    forecast_trend: str | None
    forecast_next_7d_value: float | None
    anomaly_severity: str | None
    anomaly_confidence: float | None
    recommendations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)


def evidence_package_to_dict(pkg: EvidencePackage) -> dict:
    """Flat, JSON-serialisable form - the exact shape Phase 28 will hand to
    Gemini (or the deterministic template)."""
    return {
        "metric": pkg.metric, "period_key": pkg.period_key,
        "previous_period_key": pkg.previous_period_key,
        "current": pkg.current, "previous": pkg.previous, "pct_change": pkg.pct_change,
        "direction": pkg.direction, "significant": pkg.significant,
        "primary_region": pkg.primary_region, "primary_category": pkg.primary_category,
        "root_cause_contribution": pkg.root_cause_contribution,
        "root_cause_confidence": pkg.root_cause_confidence,
        "expected": pkg.expected, "actual": pkg.actual, "gap": pkg.gap, "at_risk": pkg.at_risk,
        "customers_affected": pkg.customers_affected, "orders_affected": pkg.orders_affected,
        "forecast_trend": pkg.forecast_trend, "forecast_next_7d_value": pkg.forecast_next_7d_value,
        "anomaly_severity": pkg.anomaly_severity, "anomaly_confidence": pkg.anomaly_confidence,
        "recommendations": list(pkg.recommendations),
        "confidence": pkg.confidence, "sources": list(pkg.sources),
    }


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def aggregate_confidence(signals: list[float]) -> float:
    """The mean of whichever confidence signals are present. ``0.0`` for an
    empty list - never fabricates confidence from nothing."""
    if not signals:
        return 0.0
    return round(sum(signals) / len(signals), 4)


def _magnitude_confidence(magnitude: float | None) -> float:
    """Same formula as ``src.recommendations._magnitude_confidence`` -
    duplicated (not imported) since it's a small, module-owned pure helper,
    the same convention every other phase follows for its own thresholds."""
    if magnitude is None:
        return 0.6
    return round(min(1.0, magnitude / 100.0), 4)


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _lookup_anomaly(db: Database, metric: str, period_key: str) -> dict | None:
    """Best-effort: the highest-confidence persisted ``anomalies`` row for
    this metric/date, or ``None`` on no match or any DB error. Mirrors
    ``src.recommendations._lookup_anomaly``'s query shape."""
    try:
        rows = db.fetch_all(
            "SELECT severity, confidence FROM anomalies "
            "WHERE metric = :metric AND anomaly_date = :date "
            "ORDER BY confidence DESC NULLS LAST LIMIT 1",
            {"metric": metric, "date": period_key},
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment, never fatal
        return None
    return dict(rows[0]) if rows else None


def _root_cause_fields(db: Database, metric: str, period_key: str, previous_period_key: str):
    """``(primary_region, primary_category, contribution, confidence)``,
    all ``None`` when unsupported or on any failure."""
    if metric not in RCA_SUPPORTED_METRICS:
        return None, None, None, None
    try:
        result = analyze_root_cause(
            db, metric, period_key, period_key, previous_period_key, previous_period_key,
        )
    except Exception:  # noqa: BLE001
        return None, None, None, None
    if not result.tiers:
        return None, None, None, None
    region = next((t.primary_value for t in result.tiers if t.dimension == "region"), None)
    category = next((t.primary_value for t in result.tiers if t.dimension == "category"), None)
    return region, category, result.contribution, result.confidence


def _impact_fields(db: Database, metric: str, period_key: str):
    """``(expected, actual, gap, at_risk, customers_affected,
    orders_affected)``, all ``None`` when unsupported or on any failure."""
    if metric not in IMPACT_METRICS:
        return None, None, None, None, None, None
    try:
        result = assess_business_impact(db, date=period_key)
    except Exception:  # noqa: BLE001
        return None, None, None, None, None, None
    if result is None:
        return None, None, None, None, None, None
    if metric == "revenue":
        return (result.expected_revenue, result.actual_revenue, result.revenue_gap,
                result.revenue_at_risk, result.customers_affected, result.orders_affected)
    return (result.expected_profit, result.actual_profit, result.profit_gap,
            result.profit_at_risk, result.customers_affected, result.orders_affected)


def _forecast_fields(db: Database, metric: str):
    """``(trend, next_7d_value)``, ``(None, None)`` when unsupported, no
    forecast could be produced, or on any failure."""
    if metric not in FORECAST_METRICS:
        return None, None
    try:
        points = forecast_metric(db, metric, horizon=7)
    except Exception:  # noqa: BLE001
        return None, None
    if not points:
        return None, None
    first, last = points[0].forecast_value, points[-1].forecast_value
    trend = "up" if last > first else "down" if last < first else "flat"
    return trend, last


def _matching_recommendations(db: Database, metric: str) -> list[dict]:
    """Best-effort: this metric's slice of :func:`src.recommendations
    .generate_recommendations`'s day-grain output, as plain dicts."""
    try:
        recs = generate_recommendations(db, grain="day")
    except Exception:  # noqa: BLE001
        return []
    return [
        {"rule_id": r.rule_id, "title": r.title, "priority_score": r.priority_score,
         "priority_band": r.priority_band}
        for r in recs if r.metric == metric
    ]


def assemble_evidence(db: Database, metric: str) -> EvidencePackage | None:
    """The full :class:`EvidencePackage` for ``metric``, or ``None`` when
    there isn't yet a day-over-day comparison for it (too little history -
    :func:`src.change_detection.compare_period` needs 2+ complete days).
    """
    records = compare_period(db, "day")
    records_by_metric = {r.metric: r for r in records}
    record = records_by_metric.get(metric)
    if record is None:
        return None

    sources = ["change_detection"]

    region, category, rc_contribution, rc_confidence = _root_cause_fields(
        db, metric, record.period_key, record.previous_period_key,
    )
    if rc_confidence is not None:
        sources.append("root_cause")

    expected, actual, gap, at_risk, customers_affected, orders_affected = _impact_fields(
        db, metric, record.period_key,
    )
    if expected is not None:
        sources.append("impact_analysis")

    forecast_trend, forecast_next_7d_value = _forecast_fields(db, metric)
    if forecast_trend is not None:
        sources.append("forecasting")

    anomaly_row = _lookup_anomaly(db, metric, record.period_key)
    anomaly_severity = anomaly_row.get("severity") if anomaly_row else None
    anomaly_confidence = anomaly_row.get("confidence") if anomaly_row else None
    if anomaly_row is not None:
        sources.append("anomalies")
    if anomaly_confidence is not None:
        anomaly_confidence = float(anomaly_confidence)

    recs = _matching_recommendations(db, metric)
    if recs:
        sources.append("recommendations")

    confidence_signals = [_magnitude_confidence(record.magnitude)]
    if rc_confidence is not None:
        confidence_signals.append(rc_confidence)
    if anomaly_confidence is not None:
        confidence_signals.append(anomaly_confidence)

    return EvidencePackage(
        metric=metric, period_key=record.period_key, previous_period_key=record.previous_period_key,
        current=record.current, previous=record.previous, pct_change=record.pct_change,
        direction=record.direction, significant=record.significant,
        primary_region=region, primary_category=category,
        root_cause_contribution=rc_contribution, root_cause_confidence=rc_confidence,
        expected=expected, actual=actual, gap=gap, at_risk=at_risk,
        customers_affected=customers_affected, orders_affected=orders_affected,
        forecast_trend=forecast_trend, forecast_next_7d_value=forecast_next_7d_value,
        anomaly_severity=anomaly_severity, anomaly_confidence=anomaly_confidence,
        recommendations=recs, confidence=aggregate_confidence(confidence_signals),
        sources=sources,
    )
