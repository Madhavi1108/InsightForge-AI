"""InsightForge AI - recommendation engine & priority engine (Phase 26 /
spec Phases 51-52, FR-19).

Transparent, rule-based recommendations, prioritized 0-100 by **Severity x
Impact x Confidence**, persisted to the pre-built ``recommendations`` table
(``sql/schema.sql``, Phase 8).

The spec (verbatim, FR-19): "Transparent rule-based recommendations with a
0-100 priority (severity x impact x confidence)", with one worked example -
"revenue down + orders down + price stable -> investigate demand /
inventory". `docs/data-flow.md` stage 14 names the inputs: **change + RCA +
impact + forecast**.

**On-demand, not wired into ``src/orchestrator.py``'s per-file pipeline**
- same shape as Phase 25 (forecasting) and Phases 22-24 (RCA/impact/RFM):
recommendations need a period-over-period comparison plus (best-effort)
RCA/impact/forecast context, not a single just-ingested file's dates.
``recommendations.run_id`` is ``NOT NULL`` (like ``forecast_results``), so
:func:`persist_recommendations` still takes ``run_id`` as a parameter - it
is simply called on demand (a script, a test, or later the Streamlit
Overview / AI Analyst page) rather than from ``src/orchestrator.py``.

**Trigger: day-grain period comparison.** :func:`src.change_detection
.compare_period` already computes current-vs-previous-day ``ChangeRecord``s
for all 10 KPIs with ``direction``/``magnitude``/``significant`` decided -
reused directly rather than re-querying ``fact_sales``.

**Rule set** (only the first rule is the spec's own example; the rest are
this project's own documented extensions, following the same "never
silently drop a significant move" convention as ``change_detection``'s
zero-to-nonzero handling): ``demand_decline`` (revenue down & orders down),
``pricing_pressure`` (revenue down, orders not down - a price/value effect
rather than volume), ``margin_erosion`` (profit down without a matching
revenue decline - cost/discount pressure), ``quality_risk`` (return rate
up), ``logistics_delay`` (shipping days up), ``demand_surge`` (revenue up &
orders up - a positive-opportunity recommendation, not only negative ones),
and a ``generic_watch`` fallback for any other metric flagged significant
that no dedicated rule covers.

**Severity & confidence** default to the triggering ``ChangeRecord``'s own
``magnitude`` (``confidence = min(1.0, magnitude / 100)``; severity banded
off the same scale), then get a **best-effort upgrade** from a matching
persisted ``anomalies`` row (Phase 20's already-fused, 4-detector
severity/confidence) when one exists for that metric/date -
``linked_anomaly_id`` is set in that case. Severity bands reuse
``src.anomaly_fusion``'s existing ``SEVERITY_CRITICAL``/``HIGH``/``MEDIUM``
(70/50/30) constants rather than inventing new ones - and the same three
cut points, applied directly to the final 0-100 ``priority_score``, double
as ``priority_band``'s thresholds (documented, not coincidental).

**Impact** (dollars) is a best-effort call to
:func:`src.impact_analysis.assess_business_impact` for the triggering
date - ``revenue_at_risk``/``profit_at_risk`` depending on the metric.
``None`` (too little rolling-baseline history yet) falls back to a
documented neutral impact weight of ``0.5`` rather than fabricating a
number. ``PRIORITY_IMPACT_REFERENCE`` (default **50000.0**, this project's
own `.env`-tunable normalisation scale) turns a dollar figure into a 0-1
weight: ``impact_weight = min(1.0, impact_value / PRIORITY_IMPACT_REFERENCE)``.

**RCA / forecast enrichment** - rationale text plus a small, capped
confidence corroboration; never gates whether a rule fires, and every call
is wrapped so a failure degrades gracefully rather than breaking the
recommendation. For revenue/profit (``src.root_cause.RCA_SUPPORTED_METRICS``),
best-effort :func:`src.root_cause.analyze_root_cause` over the same
current/previous day appends a "Primary driver: ..." note. For
revenue/profit/orders (``src.forecasting.FORECAST_METRICS``), best-effort
:func:`src.forecasting.forecast_metric` checks whether the 7-day forecast
trend agrees with the triggering direction, adding a corroboration note and
a capped ``+0.05`` confidence bonus when it does. Both are day-grain only
(they need a single date), so this enrichment only runs when
``grain="day"`` - the rule-triggering logic itself works for any grain.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from src.anomaly_fusion import SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM
from src.change_detection import ChangeRecord, compare_period
from src.database import Database
from src.forecasting import FORECAST_METRICS, forecast_metric
from src.impact_analysis import assess_business_impact
from src.root_cause import RCA_SUPPORTED_METRICS, analyze_root_cause

DEFAULT_IMPACT_REFERENCE = 50000.0
FORECAST_CONFIDENCE_BONUS = 0.05

SEVERITY_WEIGHT = {"LOW": 0.25, "MEDIUM": 0.5, "HIGH": 0.75, "CRITICAL": 1.0}


def default_impact_reference() -> float:
    """``PRIORITY_IMPACT_REFERENCE`` from the environment (fresh each call)."""
    try:
        return float(os.environ.get("PRIORITY_IMPACT_REFERENCE", "").strip()
                     or DEFAULT_IMPACT_REFERENCE)
    except ValueError:
        return DEFAULT_IMPACT_REFERENCE


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    title: str
    rationale: str
    metric: str
    period_key: str
    previous_period_key: str
    direction: str
    magnitude: float | None


@dataclass(frozen=True)
class Recommendation:
    rule_id: str
    title: str
    rationale: str
    metric: str
    period_key: str
    direction: str
    severity: str
    confidence: float
    impact_value: float | None
    priority_score: float
    priority_band: str
    linked_anomaly_id: int | None
    detail: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure core - no database
# --------------------------------------------------------------------------- #
def _fmt_pct(pct: float | None) -> str:
    return f"{pct:.1f}%" if pct is not None else "an undefined amount"


def evaluate_rules(records_by_metric: dict[str, ChangeRecord]) -> list[RuleMatch]:
    """The 7-rule set over one period's ``ChangeRecord``s, keyed by metric.

    Every metric flagged ``significant=True`` ends up covered by exactly
    one rule - either a dedicated one, or the ``generic_watch`` fallback -
    never silently dropped.
    """
    revenue = records_by_metric.get("revenue")
    profit = records_by_metric.get("profit")
    orders = records_by_metric.get("orders")
    return_rate = records_by_metric.get("return_rate_pct")
    shipping = records_by_metric.get("avg_shipping_days")

    matches: list[RuleMatch] = []
    matched_metrics: set[str] = set()

    def _match(rule_id, title, rationale, record) -> RuleMatch:
        return RuleMatch(
            rule_id=rule_id, title=title, rationale=rationale, metric=record.metric,
            period_key=record.period_key, previous_period_key=record.previous_period_key,
            direction=record.direction, magnitude=record.magnitude,
        )

    if revenue and revenue.significant:
        if revenue.direction == "down":
            if orders and orders.significant and orders.direction == "down":
                matches.append(_match(
                    "demand_decline", "Investigate declining demand & inventory levels",
                    f"Revenue moved down {_fmt_pct(revenue.pct_change)} and orders moved down "
                    f"{_fmt_pct(orders.pct_change)} over the same period - both volume signals "
                    "point the same way.", revenue,
                ))
                matched_metrics.update({"revenue", "orders"})
            else:
                matches.append(_match(
                    "pricing_pressure", "Investigate pricing & discounting",
                    f"Revenue moved down {_fmt_pct(revenue.pct_change)} while orders did not move "
                    "significantly - the drop looks price/value-driven rather than volume-driven.",
                    revenue,
                ))
                matched_metrics.add("revenue")
        elif revenue.direction == "up":
            if orders and orders.significant and orders.direction == "up":
                matches.append(_match(
                    "demand_surge", "Scale fulfillment & inventory for demand growth",
                    f"Revenue moved up {_fmt_pct(revenue.pct_change)} and orders moved up "
                    f"{_fmt_pct(orders.pct_change)} together - a genuine demand increase worth "
                    "capitalizing on.", revenue,
                ))
                matched_metrics.update({"revenue", "orders"})

    if profit and profit.significant:
        matched_metrics.add("profit")
        if profit.direction == "down" and not (revenue and revenue.significant
                                               and revenue.direction == "down"):
            matches.append(_match(
                "margin_erosion", "Investigate margin erosion",
                f"Profit moved down {_fmt_pct(profit.pct_change)} without a matching revenue "
                "decline - cost or discount pressure is squeezing margin.", profit,
            ))

    if return_rate and return_rate.significant and return_rate.direction == "up":
        matches.append(_match(
            "quality_risk", "Investigate product quality & fulfillment issues",
            f"Return rate moved up {_fmt_pct(return_rate.pct_change)} over the same period.",
            return_rate,
        ))
        matched_metrics.add("return_rate_pct")

    if shipping and shipping.significant and shipping.direction == "up":
        matches.append(_match(
            "logistics_delay", "Investigate logistics & fulfillment delays",
            f"Average shipping days moved up {_fmt_pct(shipping.pct_change)} over the same period.",
            shipping,
        ))
        matched_metrics.add("avg_shipping_days")

    for metric, record in records_by_metric.items():
        if record.significant and metric not in matched_metrics:
            matches.append(_match(
                "generic_watch", f"Monitor {metric} - significant {record.direction} movement",
                f"{metric} moved {record.direction} {_fmt_pct(record.pct_change)} over the same "
                "period - no dedicated rule covers this metric yet.", record,
            ))
            matched_metrics.add(metric)

    return matches


def _magnitude_confidence(magnitude: float | None) -> float:
    if magnitude is None:
        return 0.6  # zero-to-nonzero / undefined pct change - moderate, documented default
    return round(min(1.0, magnitude / 100.0), 4)


def _severity_from_magnitude(magnitude: float | None) -> str:
    score = min(magnitude, 100.0) if magnitude is not None else 60.0
    return _band(score)


def _band(score: float) -> str:
    if score >= SEVERITY_CRITICAL:
        return "CRITICAL"
    if score >= SEVERITY_HIGH:
        return "HIGH"
    if score >= SEVERITY_MEDIUM:
        return "MEDIUM"
    return "LOW"


def severity_weight(severity: str) -> float:
    return SEVERITY_WEIGHT.get(severity, SEVERITY_WEIGHT["LOW"])


def compute_priority(
    severity: str, impact_value: float | None, confidence: float,
    impact_reference: float | None = None,
) -> tuple[float, str]:
    """``(priority_score, priority_band)`` - severity x impact x confidence,
    normalised 0-100. ``impact_value=None`` falls back to a neutral 0.5
    impact weight rather than fabricating a number.
    """
    impact_reference = impact_reference if impact_reference is not None else default_impact_reference()
    if impact_value is None:
        impact_weight = 0.5
    else:
        impact_weight = min(1.0, abs(impact_value) / impact_reference) if impact_reference > 0 else 0.5
    score = round(100.0 * severity_weight(severity) * impact_weight * confidence, 2)
    return score, _band(score)


# --------------------------------------------------------------------------- #
# DB-querying wrapper
# --------------------------------------------------------------------------- #
def _lookup_anomaly(db: Database, metric: str, period_key: str) -> dict | None:
    """Best-effort: the highest-confidence persisted ``anomalies`` row for
    this metric/date, or ``None`` on no match or any DB error."""
    try:
        rows = db.fetch_all(
            "SELECT anomaly_id, severity, confidence FROM anomalies "
            "WHERE metric = :metric AND anomaly_date = :date "
            "ORDER BY confidence DESC NULLS LAST LIMIT 1",
            {"metric": metric, "date": period_key},
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment, never fatal
        return None
    return dict(rows[0]) if rows else None


def _lookup_impact(db: Database, metric: str, period_key: str) -> float | None:
    """Best-effort revenue/profit at-risk figure for this date, or ``None``."""
    try:
        result = assess_business_impact(db, date=period_key)
    except Exception:  # noqa: BLE001
        return None
    if result is None:
        return None
    if metric == "revenue":
        return result.revenue_at_risk
    if metric == "profit":
        return result.profit_at_risk
    return max(result.revenue_at_risk, result.profit_at_risk)


def _rca_note(db: Database, metric: str, period_key: str, previous_period_key: str) -> str | None:
    """Best-effort "Primary driver: ..." note from root-cause analysis."""
    if metric not in RCA_SUPPORTED_METRICS:
        return None
    try:
        result = analyze_root_cause(
            db, metric, period_key, period_key, previous_period_key, previous_period_key,
        )
    except Exception:  # noqa: BLE001
        return None
    if not result.tiers:
        return None
    return f"Primary driver: {result.primary_driver} (contribution {result.contribution})."


def _forecast_note(db: Database, metric: str, direction: str) -> tuple[str | None, float]:
    """Best-effort ``(note, confidence_bonus)`` when the 7-day forecast
    trend agrees with the triggering direction."""
    if metric not in FORECAST_METRICS or direction not in ("up", "down"):
        return None, 0.0
    try:
        points = forecast_metric(db, metric, horizon=7)
    except Exception:  # noqa: BLE001
        return None, 0.0
    if not points:
        return None, 0.0
    first, last = points[0].forecast_value, points[-1].forecast_value
    trend = "up" if last > first else "down" if last < first else "flat"
    if trend == direction:
        return f"7-day forecast corroborates the {direction} trend.", FORECAST_CONFIDENCE_BONUS
    return None, 0.0


def generate_recommendations(
    db: Database, grain: str = "day", impact_reference: float | None = None,
) -> list[Recommendation]:
    """Every rule-triggered :class:`Recommendation` for the latest complete
    ``grain`` period, ranked by ``priority_score`` descending.

    RCA/impact/forecast enrichment only runs for ``grain="day"`` (they all
    need a single date); rule triggering itself works for any grain.
    """
    records = compare_period(db, grain)
    if not records:
        return []
    records_by_metric = {r.metric: r for r in records}
    matches = evaluate_rules(records_by_metric)

    recommendations = []
    for match in matches:
        confidence = _magnitude_confidence(match.magnitude)
        severity = _severity_from_magnitude(match.magnitude)
        linked_anomaly_id = None
        impact_value = None
        rationale = match.rationale

        if grain == "day":
            anomaly_row = _lookup_anomaly(db, match.metric, match.period_key)
            if anomaly_row is not None:
                severity = anomaly_row.get("severity") or severity
                if anomaly_row.get("confidence") is not None:
                    confidence = float(anomaly_row["confidence"])
                linked_anomaly_id = anomaly_row.get("anomaly_id")

            impact_value = _lookup_impact(db, match.metric, match.period_key)

            rca_note = _rca_note(db, match.metric, match.period_key, match.previous_period_key)
            if rca_note:
                rationale = f"{rationale} {rca_note}"

            forecast_note, bonus = _forecast_note(db, match.metric, match.direction)
            if forecast_note:
                rationale = f"{rationale} {forecast_note}"
                confidence = round(min(1.0, confidence + bonus), 4)

        priority_score, priority_band = compute_priority(
            severity, impact_value, confidence, impact_reference,
        )

        recommendations.append(Recommendation(
            rule_id=match.rule_id, title=match.title, rationale=rationale,
            metric=match.metric, period_key=match.period_key, direction=match.direction,
            severity=severity, confidence=confidence, impact_value=impact_value,
            priority_score=priority_score, priority_band=priority_band,
            linked_anomaly_id=linked_anomaly_id,
            detail={"period_key": match.period_key, "previous_period_key": match.previous_period_key},
        ))

    recommendations.sort(key=lambda r: r.priority_score, reverse=True)
    return recommendations


def persist_recommendations(db: Database, run_id: int, recs: list[Recommendation]) -> int:
    """Bulk-insert ``recommendations`` rows for this run. Returns the count written."""
    if not recs:
        return 0
    params = [
        {
            "run": run_id, "title": r.title, "rationale": r.rationale, "rule_id": r.rule_id,
            "anomaly_id": r.linked_anomaly_id, "severity": r.severity,
            "impact": r.impact_value, "confidence": r.confidence,
            "priority_score": r.priority_score, "priority_band": r.priority_band,
            "detail": json.dumps(r.detail, ensure_ascii=False, default=str),
        }
        for r in recs
    ]
    return db.execute_many(
        "INSERT INTO recommendations "
        "(run_id, title, rationale, rule_id, linked_anomaly_id, severity, impact_value, "
        "confidence, priority_score, priority_band, detail) "
        "VALUES (:run, :title, :rationale, :rule_id, :anomaly_id, :severity, :impact, "
        ":confidence, :priority_score, :priority_band, CAST(:detail AS JSONB))",
        params,
    )


def generate_and_persist_recommendations(
    db: Database, run_id: int, grain: str = "day", impact_reference: float | None = None,
) -> list[Recommendation]:
    """:func:`generate_recommendations` then :func:`persist_recommendations`."""
    recommendations = generate_recommendations(db, grain, impact_reference)
    persist_recommendations(db, run_id, recommendations)
    return recommendations
