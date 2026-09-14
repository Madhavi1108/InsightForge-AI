"""InsightForge AI - AI evidence layer (Phase 27 / spec Phase 53, FR-20).

The spec (verbatim, ``docs/system-components.md``): "assemble a structured,
verified evidence package (metric, current, previous, change %, primary
region, primary category, impact, ...) before any LLM call." ``docs/data-flow
.md`` row 15: "``anomalies``, ``recommendations``, RCA, impact -> evidence
package -> AI Analyst text | LLM consumes verified evidence only; template
fallback if no LLM."

This module makes **no LLM call** and adds no new dependency, environment
variable, or database table - that is Phase 28 (``src/ai_analyst.py``, the
explanation engine, not built yet). Phase 27's entire job is to be the
strict, typed contract Phase 28's prompt builder (or template fallback) can
rely on **by construction**: every :class:`EvidencePackage` field is either
copied verbatim from an already-verified module's dataclass/DB row (Phases
17/20/22/23/25/26 - ``change_detection``, the persisted ``anomalies`` table,
``root_cause``, ``impact_analysis``, ``forecasting``, the persisted
``recommendations`` table), or is a small, documented, deterministic
derivation (:func:`compute_confidence`, a forecast trend label) with its
formula spelled out below - never a number invented on the spot. This is
what "the LLM is an explanation/interface layer only ... and never invents
figures" (dev rule 3) means in practice: Phase 27 is the enforcement point.

**Design choice - the anomaly -> recommendation join key.** ``sql/schema
.sql``'s ``recommendations`` table has no ``metric``/``period_key`` column,
only ``linked_anomaly_id``. So a linked recommendation can only be found
*through* a linked anomaly (``anomalies.anomaly_id ->
recommendations.linked_anomaly_id``) - never by fuzzy-matching
``rationale``/``detail`` text. When no anomaly is found for a metric/date,
:attr:`EvidencePackage.recommendation` stays ``None`` even if ``src
.recommendations``' own magnitude-based rules would have fired one - a
documented limitation, not a bug (Phase 28 should prefer anomaly-confirmed
evidence anyway).

**On-demand, not wired into ``src/orchestrator.py``** - same shape as
Phases 22-26 (RCA/impact/RFM/forecasting/recommendations): assembling
evidence needs a period-over-period comparison plus best-effort
cross-module context, not a single just-ingested file's dates. No table in
``sql/schema.sql`` stores this output either - confirmed no ``evidence``
table exists - so nothing here is persisted; the caller (Phase 28, or later
a Streamlit page) consumes :class:`EvidencePackage` directly.

**Day-grain-only enrichment.** RCA and business impact both need a single
date; the ``anomalies`` table is populated per-day, not per-week/month.
So :func:`assemble_evidence` only runs the RCA/impact/forecast/anomaly/
recommendation lookups for ``grain="day"`` - the same restriction ``src
.recommendations`` documents for its own enrichment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.change_detection import ChangeRecord, compare_period
from src.database import Database
from src.forecasting import FORECAST_METRICS, forecast_metric
from src.impact_analysis import BusinessImpactResult, assess_business_impact
from src.root_cause import RCA_SUPPORTED_METRICS, RootCauseResult, analyze_root_cause


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RootCauseSummary:
    """Flattened view of a :class:`~src.root_cause.RootCauseResult` - named
    ``primary_*`` fields per hierarchy tier (Region -> Category ->
    Sub-Category -> Product -> Customer Segment), matching the spec's own
    "primary region, primary category" wording instead of a generic tier
    list. A tier the drill-down never reached (it stops once a tier has no
    rows left) stays ``None`` - never fabricated."""
    primary_driver: str | None
    primary_region: str | None
    primary_category: str | None
    primary_sub_category: str | None
    primary_product: str | None
    primary_customer_segment: str | None
    contribution: float | None
    confidence: float | None


@dataclass(frozen=True)
class ForecastSummary:
    """Compact corroboration signal - trend direction + magnitude, not the
    full list of forecast points (a "does the forecast agree" flag, not a
    chart)."""
    horizon_days: int
    trend: str  # "up" | "down" | "flat"
    first_value: float | None
    last_value: float | None
    mape: float | None


@dataclass(frozen=True)
class AnomalyLink:
    """Best-effort persisted ``anomalies`` row for this metric/date (Phase
    20's fused, 4-detector severity/confidence)."""
    anomaly_id: int
    severity: str
    confidence: float | None


@dataclass(frozen=True)
class RecommendationLink:
    """Best-effort persisted ``recommendations`` row, reached only via
    :attr:`AnomalyLink.anomaly_id` (see module docstring - the only clean
    join key)."""
    recommendation_id: int
    title: str
    priority_band: str
    priority_score: float | None


@dataclass(frozen=True)
class EvidencePackage:
    """One structured, verified evidence package for one metric's latest
    complete-period change - the sole input Phase 28's LLM prompt (or
    template fallback) may use. Every field traces to an existing verified
    module; nothing here is computed from scratch beyond the documented,
    deterministic derivations in this module."""
    metric: str
    grain: str
    period_key: str
    previous_period_key: str
    current: float | int | None
    previous: float | int | None
    pct_change: float | None
    direction: str  # "up" | "down" | "flat"
    magnitude: float | None
    significant: bool
    root_cause: RootCauseSummary | None
    impact: BusinessImpactResult | None
    forecast: ForecastSummary | None
    anomaly: AnomalyLink | None
    recommendation: RecommendationLink | None
    confidence: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serializable representation for Phase 28's LLM prompt -
        ``dataclasses.asdict`` handles the nested frozen dataclasses (and
        their ``None``\\ s) automatically; no field is duplicated by hand."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def _magnitude_confidence(magnitude: float | None) -> float:
    """Same formula as ``src.recommendations._magnitude_confidence``
    (documented, intentionally mirrored rather than imported - keeps this
    module a self-contained pure core, like every prior phase)."""
    if magnitude is None:
        return 0.6  # zero-to-nonzero / undefined pct change - moderate, documented default
    return round(min(1.0, magnitude / 100.0), 4)


def compute_confidence(
    magnitude: float | None,
    rca_confidence: float | None,
    anomaly_confidence: float | None,
) -> float:
    """0-1 composite evidence confidence.

    Precedence (documented, not fabricated - mirrors ``src.recommendations``'
    "anomaly upgrade supersedes magnitude estimate" convention):
    - both ``rca_confidence`` and ``anomaly_confidence`` available -> their
      mean (two independent corroborating signals);
    - only one available -> that one;
    - neither available -> :func:`_magnitude_confidence`, the same
      change-record-only baseline ``src.recommendations`` falls back to.
    """
    signals = [c for c in (rca_confidence, anomaly_confidence) if c is not None]
    if signals:
        return round(sum(signals) / len(signals), 4)
    return _magnitude_confidence(magnitude)


def summarize_root_cause(result: RootCauseResult | None) -> RootCauseSummary | None:
    """``None``-safe flatten of ``result.tiers`` (keyed by dimension) into
    named ``primary_*`` fields. ``None`` when ``result`` is ``None`` or has
    no tiers."""
    if result is None or not result.tiers:
        return None
    by_dim = {t.dimension: t.primary_value for t in result.tiers}
    return RootCauseSummary(
        primary_driver=result.primary_driver,
        primary_region=by_dim.get("region"),
        primary_category=by_dim.get("category"),
        primary_sub_category=by_dim.get("sub_category"),
        primary_product=by_dim.get("product"),
        primary_customer_segment=by_dim.get("customer_segment"),
        contribution=result.contribution,
        confidence=result.confidence,
    )


def summarize_forecast(points: list | None, horizon_days: int) -> ForecastSummary | None:
    """Pure trend summary from a ``forecast_metric()`` list of forecast
    points (same first/last-value trend logic as ``src.recommendations
    ._forecast_note``, but returns a structured summary instead of prose).
    ``None`` for an empty/``None`` list."""
    if not points:
        return None
    first, last = points[0].forecast_value, points[-1].forecast_value
    trend = "up" if last > first else "down" if last < first else "flat"
    return ForecastSummary(
        horizon_days=horizon_days, trend=trend,
        first_value=first, last_value=last, mape=points[-1].mape,
    )


def build_notes(
    change: ChangeRecord,
    root_cause: RootCauseSummary | None,
    forecast: ForecastSummary | None,
    anomaly: AnomalyLink | None,
) -> list[str]:
    """Human-readable evidence trail - each entry traceable to a field
    already on the package. Used by Phase 28's deterministic template
    fallback when no LLM is available, and optionally surfaced in the LLM
    prompt as pre-verified talking points the model must not contradict."""
    notes: list[str] = []
    pct = f"{change.pct_change:.1f}%" if change.pct_change is not None else "an undefined amount"
    notes.append(f"{change.metric} moved {change.direction} {pct} "
                 f"({change.previous_period_key} -> {change.period_key}).")
    if root_cause is not None:
        notes.append(f"Primary driver: {root_cause.primary_driver} "
                      f"(contribution {root_cause.contribution}, confidence {root_cause.confidence}).")
    if forecast is not None:
        notes.append(f"{forecast.horizon_days}-day forecast trend: {forecast.trend}.")
    if anomaly is not None:
        notes.append(f"Linked anomaly (severity {anomaly.severity}, "
                      f"confidence {anomaly.confidence}).")
    return notes


# --------------------------------------------------------------------------- #
# DB-querying wrapper - best-effort, mirrors src.recommendations exactly
# --------------------------------------------------------------------------- #
def _lookup_anomaly(db: Database, metric: str, period_key: str) -> AnomalyLink | None:
    """Highest-confidence persisted ``anomalies`` row for this metric/date,
    or ``None`` on no match or any DB error - best-effort, never fatal."""
    try:
        rows = db.fetch_all(
            "SELECT anomaly_id, severity, confidence FROM anomalies "
            "WHERE metric = :metric AND anomaly_date = :date "
            "ORDER BY confidence DESC NULLS LAST LIMIT 1",
            {"metric": metric, "date": period_key},
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment, never fatal
        return None
    if not rows:
        return None
    row = rows[0]
    return AnomalyLink(
        anomaly_id=row["anomaly_id"], severity=row["severity"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


def _lookup_recommendation(db: Database, anomaly_id: int | None) -> RecommendationLink | None:
    """Best-effort ``recommendations`` row linked to this ``anomaly_id`` -
    the only clean join key (see module docstring). ``None`` when
    ``anomaly_id`` is ``None``, on no match, or any DB error - never
    fuzzy-matched from text."""
    if anomaly_id is None:
        return None
    try:
        rows = db.fetch_all(
            "SELECT recommendation_id, title, priority_band, priority_score "
            "FROM recommendations WHERE linked_anomaly_id = :aid "
            "ORDER BY priority_score DESC NULLS LAST LIMIT 1",
            {"aid": anomaly_id},
        )
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    row = rows[0]
    return RecommendationLink(
        recommendation_id=row["recommendation_id"], title=row["title"],
        priority_band=row["priority_band"],
        priority_score=float(row["priority_score"]) if row["priority_score"] is not None else None,
    )


def _lookup_root_cause(
    db: Database, metric: str, period_key: str, previous_period_key: str,
) -> RootCauseSummary | None:
    """Best-effort ``analyze_root_cause`` -> :func:`summarize_root_cause`;
    ``None`` for a metric outside ``RCA_SUPPORTED_METRICS``, insufficient
    data, or any error. Day-grain only (RCA needs single-day start==end
    bounds, same restriction ``src.recommendations`` documents)."""
    if metric not in RCA_SUPPORTED_METRICS:
        return None
    try:
        result = analyze_root_cause(
            db, metric, period_key, period_key, previous_period_key, previous_period_key,
        )
    except Exception:  # noqa: BLE001
        return None
    return summarize_root_cause(result)


def _lookup_impact(db: Database, period_key: str) -> BusinessImpactResult | None:
    """Best-effort ``assess_business_impact`` for this date (always
    computes both revenue and profit gap/at-risk, regardless of the
    triggering metric - ``impact_analysis`` has no per-metric mode). ``None``
    on no rolling-baseline coverage yet or any DB error."""
    try:
        return assess_business_impact(db, date=period_key)
    except Exception:  # noqa: BLE001
        return None


def _lookup_forecast(db: Database, metric: str, horizon: int = 7) -> ForecastSummary | None:
    """Best-effort ``forecast_metric`` -> :func:`summarize_forecast`;
    ``None`` for a metric outside ``FORECAST_METRICS``, insufficient
    history, or any error."""
    if metric not in FORECAST_METRICS:
        return None
    try:
        points = forecast_metric(db, metric, horizon=horizon)
    except Exception:  # noqa: BLE001
        return None
    return summarize_forecast(points, horizon)


# --------------------------------------------------------------------------- #
# Top-level entry points
# --------------------------------------------------------------------------- #
def _build_package(db: Database, record: ChangeRecord) -> EvidencePackage:
    """Shared assembly for one ``ChangeRecord`` - the actual lookups, notes,
    and confidence composition, factored out so :func:`assemble_evidence`
    and :func:`assemble_all_evidence` never duplicate this logic."""
    root_cause = None
    impact = None
    forecast = None
    anomaly = None
    recommendation = None

    if record.grain == "day":
        root_cause = _lookup_root_cause(db, record.metric, record.period_key, record.previous_period_key)
        impact = _lookup_impact(db, record.period_key)
        forecast = _lookup_forecast(db, record.metric)
        anomaly = _lookup_anomaly(db, record.metric, record.period_key)
        recommendation = _lookup_recommendation(db, anomaly.anomaly_id if anomaly else None)

    confidence = compute_confidence(
        record.magnitude,
        root_cause.confidence if root_cause else None,
        anomaly.confidence if anomaly else None,
    )
    notes = build_notes(record, root_cause, forecast, anomaly)

    return EvidencePackage(
        metric=record.metric, grain=record.grain,
        period_key=record.period_key, previous_period_key=record.previous_period_key,
        current=record.current, previous=record.previous, pct_change=record.pct_change,
        direction=record.direction, magnitude=record.magnitude, significant=record.significant,
        root_cause=root_cause, impact=impact, forecast=forecast,
        anomaly=anomaly, recommendation=recommendation,
        confidence=confidence, notes=notes,
    )


def assemble_evidence(db: Database, metric: str, grain: str = "day") -> EvidencePackage | None:
    """The evidence package for one metric's latest complete-period change,
    or ``None`` when ``compare_period(db, grain)`` has no ``ChangeRecord``
    for this metric yet (too little history - never fabricated).

    No ``period_key``/``previous_period_key`` parameters: every upstream
    module (``compare_period``, ``analyze_root_cause``,
    ``assess_business_impact``) already resolves "latest complete period"
    itself, so this function assembles evidence for whatever period those
    modules already agree is current - matching
    ``generate_recommendations(db, grain="day", ...)``'s own signature
    shape.
    """
    records = compare_period(db, grain)
    record = next((r for r in records if r.metric == metric), None)
    if record is None:
        return None
    return _build_package(db, record)


def assemble_all_evidence(db: Database, grain: str = "day") -> list[EvidencePackage]:
    """:func:`assemble_evidence` for every metric ``compare_period(db,
    grain)`` returns a ``ChangeRecord`` for - the "every metric" shape a
    caller answering a broad question ("Summarize this month", "What are
    the biggest risks?") needs, without looping over metrics itself."""
    records = compare_period(db, grain)
    return [_build_package(db, r) for r in records]
