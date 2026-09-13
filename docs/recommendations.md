# InsightForge AI - Recommendation Engine & Priority Engine

> Phase 26 deliverable (spec Phases 51-52, FR-19). `src/recommendations.py`
> turns significant period-over-period movements into transparent,
> rule-based recommendations, prioritized 0-100 by Severity x Impact x
> Confidence, and persists them to the pre-built `recommendations` table.

## 1. What the spec says and what it leaves open

`docs/requirements.md`, FR-19 (verbatim): "Transparent rule-based
recommendations with a 0-100 priority (severity x impact x confidence)."
The spec gives exactly **one** worked example - "revenue down + orders down
+ price stable -> investigate demand / inventory" - and the priority
*formula shape* (severity x impact x confidence), but no full rule set, no
severity/impact/confidence formulas, and no normalisation scale. Everything
below beyond that one example is this project's own documented operational
choice, the same situation as every prior phase's spec-silent formula.

## 2. Trigger: day-grain period comparison

`src.change_detection.compare_period(db, "day")` already computes
current-vs-previous-day `ChangeRecord`s for all 10 KPIs with
`direction`/`magnitude`/`significant` decided (`CHANGE_DETECTION_THRESHOLD_PCT`,
Phase 17) - reused directly rather than re-querying `fact_sales`.

## 3. Rule set

| Rule | Trigger | Recommendation |
|---|---|---|
| `demand_decline` | revenue down & orders down (both significant) | Investigate declining demand & inventory levels |
| `pricing_pressure` | revenue down significant, orders not significantly down | Investigate pricing & discounting |
| `margin_erosion` | profit down significant, without a matching revenue decline | Investigate margin erosion |
| `quality_risk` | return rate up significant | Investigate product quality & fulfillment issues |
| `logistics_delay` | avg shipping days up significant | Investigate logistics & fulfillment delays |
| `demand_surge` | revenue up & orders up (both significant) | Scale fulfillment & inventory for demand growth |
| `generic_watch` | any other metric flagged significant, uncovered above | Monitor `<metric>` - significant `<direction>` movement |

Only the first row is the spec's own example. The rest are natural
extensions covering the other KPIs `change_detection` already tracks, and
`generic_watch` guarantees every significant movement gets *some*
recommendation - the same "never silently drop a significant/zero-to-nonzero
move" convention `change_detection` itself follows.

## 4. Severity & confidence

Default (no DB lookup needed): straight from the triggering `ChangeRecord`'s
own `magnitude` -

```
confidence = min(1.0, magnitude / 100)
severity   = banded off the same scale via SEVERITY_CRITICAL/HIGH/MEDIUM
             (70 / 50 / 30 - reused from src.anomaly_fusion, Phase 20)
```

**Best-effort upgrade**: a matching persisted `anomalies` row (`metric` +
`anomaly_date = period_key`, highest confidence) supersedes both - Phase
20's fusion across 4 independent detectors is stronger evidence than a
single day-over-day magnitude - and sets `linked_anomaly_id`. Wrapped in
`try/except`: a DB hiccup here degrades to the magnitude-based estimate,
never a failure.

## 5. Impact

Best-effort `src.impact_analysis.assess_business_impact(db, date=period_key)`
supplies `revenue_at_risk` (for `revenue`-driven rules) or `profit_at_risk`
(for `profit`-driven rules); other rules take whichever is larger, as "how
much revenue was on the line that day". `None` (too little rolling-baseline
history yet) is never fabricated into a number - `impact_weight` falls back
to a documented neutral **0.5** instead.

```
impact_weight = min(1.0, impact_value / PRIORITY_IMPACT_REFERENCE)   # impact_value known
impact_weight = 0.5                                                  # impact_value is None
```

`PRIORITY_IMPACT_REFERENCE` (default **50000.0**) is a new `.env`-tunable
normalisation scale, same pattern as `ZSCORE_THRESHOLD`/the PSI bands.

## 6. RCA / forecast enrichment (rationale only, never gates a rule)

Day-grain only (both need a single date):
- **RCA**: for revenue/profit (`src.root_cause.RCA_SUPPORTED_METRICS`),
  best-effort `analyze_root_cause` over the same current/previous day
  appends `"Primary driver: <dimension chain> (contribution <n>)."` to the
  rationale.
- **Forecast**: for revenue/profit/orders (`src.forecasting.FORECAST_METRICS`),
  best-effort `forecast_metric(db, metric, horizon=7)` checks whether the
  7-day forecast trend agrees with the triggering direction; if so, the
  rationale notes it and confidence gets a capped **+0.05** bonus.

Both are wrapped in `try/except` - advisory corroboration, exactly the
"advisory, never fails" convention Phases 20/21 use for orchestrator
wiring, applied here to on-demand enrichment instead.

## 7. Priority score

```
priority_score = round(100 * severity_weight * impact_weight * confidence, 2)

severity_weight: LOW=0.25, MEDIUM=0.5, HIGH=0.75, CRITICAL=1.0
```

`priority_band` reuses the *same* `SEVERITY_CRITICAL`/`HIGH`/`MEDIUM`
(70/50/30) cut points, applied directly to the final 0-100
`priority_score` - one shared scale doing double duty (severity banding
*and* priority banding), a deliberate consistency choice, not an accident.

## 8. Persistence: the pre-built `recommendations` table

Existed in `sql/schema.sql` since Phase 8:

```sql
CREATE TABLE recommendations (
    recommendation_id, run_id (FK -> pipeline_runs, NOT NULL), title,
    rationale, rule_id, linked_anomaly_id (FK -> anomalies, nullable),
    severity (CHECK IN (...)), impact_value, confidence,
    priority_score (CHECK BETWEEN 0 AND 100),
    priority_band (CHECK IN (...)), status DEFAULT 'open', detail JSONB,
    created_at
);
```

## 9. Why recommendations are on-demand but still persist

Same reasoning as Phase 25 (forecasting): needs a period comparison (and
best-effort RCA/impact/forecast context), not a single just-ingested
file's dates - so it isn't auto-wired into `src/orchestrator.py`, the same
shape as RCA/impact/RFM (Phases 22-24). But `recommendations.run_id` is
`NOT NULL` (like `forecast_results`), so `persist_recommendations(db,
run_id, recs)` still takes a `run_id` - called on demand rather than from
the per-file pipeline.

## 10. API

| Symbol | Purpose |
|---|---|
| `Recommendation` | mirrors one `recommendations` row |
| `evaluate_rules(records_by_metric)` | pure - the 7-rule set |
| `severity_weight(severity)` / `compute_priority(severity, impact_value, confidence, impact_reference=None)` | pure |
| `default_impact_reference()` | reads `PRIORITY_IMPACT_REFERENCE`, fresh each call |
| `generate_recommendations(db, grain="day", impact_reference=None)` | the on-demand entry point |
| `persist_recommendations(db, run_id, recs)` | bulk-insert into `recommendations` |
| `generate_and_persist_recommendations(db, run_id, grain="day", impact_reference=None)` | generate + persist |

## 11. Configuration

New env var:
```
PRIORITY_IMPACT_REFERENCE=50000.0
```

## 12. Verify

```powershell
python scripts/apply_schema.py   # recommendations already exists (Phase 8)
pytest -q tests/test_phase26_recommendations.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase26_recommendations.py
```

```sql
SELECT title, severity, priority_score, priority_band
FROM recommendations ORDER BY priority_score DESC;
```

## 13. Related documents

- [`database-schema.md`](database-schema.md) - the `recommendations` table (Phase 8)
- [`change-detection.md`](change-detection.md) - the trigger input
- [`anomaly-fusion.md`](anomaly-fusion.md) - severity/confidence upgrade source
- [`business-impact-engine.md`](business-impact-engine.md) - the impact input
- [`forecasting.md`](forecasting.md) - the forecast corroboration input
