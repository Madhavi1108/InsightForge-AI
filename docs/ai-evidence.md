# InsightForge AI - AI Evidence Layer

> Phase 27 deliverable (spec Phase 53, FR-20). `src/ai_evidence.py` assembles
> a structured, verified **evidence package** for one metric's latest
> period-over-period change, drawing only from already-verified outputs of
> prior phases - before any LLM is involved. No LLM call, no new dependency,
> no new table.

## 1. What the spec says and what it leaves open

`docs/system-components.md` (verbatim): "assemble a structured, verified
evidence package (metric, current, previous, change %, primary region,
primary category, impact, ...) before any LLM call." `docs/data-flow.md`
row 15: "`anomalies`, `recommendations`, RCA, impact -> evidence package ->
AI Analyst text | LLM consumes verified evidence only; template fallback if
no LLM."

The spec names three concrete fields (metric, current, previous, change %)
plus "primary region, primary category, impact, ..." with an open-ended
"...", and gives no schema. This phase's own documented choice: a fully
typed `EvidencePackage`, not a generic dict - see section 5 for why.

## 2. Inputs reused

| Module | What flows into `EvidencePackage` |
|---|---|
| `src.change_detection.compare_period` (Phase 17) | `metric`, `grain`, `period_key`, `previous_period_key`, `current`, `previous`, `pct_change`, `direction`, `magnitude`, `significant` - the whole `ChangeRecord`, verbatim |
| `anomalies` table (Phase 20 fusion) | best-effort `AnomalyLink` (`anomaly_id`, `severity`, `confidence`) for the metric/date |
| `src.root_cause.analyze_root_cause` (Phase 22) | best-effort `RootCauseSummary` - `primary_driver` plus the drill-down's per-tier `primary_value` flattened into named `primary_region`/`primary_category`/`primary_sub_category`/`primary_product`/`primary_customer_segment` fields, `contribution`, `confidence` |
| `src.impact_analysis.assess_business_impact` (Phase 23) | best-effort `BusinessImpactResult`, embedded whole (expected/actual/gap for revenue & profit, at-risk figures, customers/orders affected) |
| `src.forecasting.forecast_metric` (Phase 25) | best-effort `ForecastSummary` - trend direction/magnitude + MAPE, not the full point list |
| `recommendations` table (Phase 26) | best-effort `RecommendationLink`, reached only through a linked anomaly (section 5) |

## 3. Field-by-field

| Field | Type | Source | Presence |
|---|---|---|---|
| `metric`, `grain`, `period_key`, `previous_period_key`, `current`, `previous`, `pct_change`, `direction`, `magnitude`, `significant` | various | `ChangeRecord` | always (the triggering record) |
| `root_cause` | `RootCauseSummary \| None` | `src.root_cause` | best-effort, day-grain only |
| `impact` | `BusinessImpactResult \| None` | `src.impact_analysis` | best-effort, day-grain only |
| `forecast` | `ForecastSummary \| None` | `src.forecasting` | best-effort, day-grain only |
| `anomaly` | `AnomalyLink \| None` | `anomalies` table | best-effort, day-grain only |
| `recommendation` | `RecommendationLink \| None` | `recommendations` table, via `anomaly.anomaly_id` | best-effort, day-grain only, requires a linked anomaly |
| `confidence` | `float` (0-1) | `compute_confidence` | always, see section 6 |
| `notes` | `list[str]` | `build_notes` | always, see section 4 |

`RootCauseSummary` fields not reached by the drill-down (it stops once a
tier has no rows left, `src.root_cause.drill_down`) stay `None` - never
fabricated.

## 4. Notes

`build_notes` produces a short, human-readable evidence trail - one line
for the triggering change, and one more line per present component
(`root_cause`/`forecast`/`anomaly`). Every line is traceable to a field
already on the package. Phase 28's deterministic template fallback (no LLM
available) can render straight from this list; an LLM prompt can surface it
as pre-verified talking points the model must not contradict.

## 5. The anomaly -> recommendation join key limitation

`sql/schema.sql`'s `recommendations` table has no `metric` or `period_key`
column - only `linked_anomaly_id` (FK -> `anomalies`). So `recommendation`
can only be populated by first finding a linked `anomalies` row, then
looking up a `recommendations` row whose `linked_anomaly_id` matches -
never by fuzzy-matching `rationale`/`detail` text. This is a documented,
known gap, not a bug: a recommendation that `src.recommendations`' own
magnitude-based rules triggered, but that no anomaly-fusion detector fired
on for that date, will never surface here. Acceptable because Phase 28
should prefer anomaly-confirmed evidence over a magnitude-only trigger
anyway.

## 6. Confidence formula

```
confidence = mean(rca_confidence, anomaly_confidence)   # both available
confidence = whichever of the two is available           # exactly one available
confidence = min(1.0, magnitude / 100)                    # neither available
```

Two independent corroborating signals (RCA's evidence-concentration
confidence and anomaly-fusion's 4-detector-vote confidence) average
together when both exist; otherwise the package falls back exactly as
`src.recommendations` does for its own confidence, down to the same
change-record-only baseline. Documented, not fabricated - the same framing
every prior phase's spec-silent formula uses.

## 7. Day-grain-only enrichment

RCA and business impact both need a single date; the `anomalies` table is
populated per-day (Phase 20 wiring), not per-week/month. So
`assemble_evidence`/`assemble_all_evidence` only run the
RCA/impact/forecast/anomaly/recommendation lookups when `grain == "day"` -
the same restriction `src.recommendations` documents for its own
enrichment. The triggering `ChangeRecord` itself is produced for any grain
`src.change_detection.compare_period` supports.

## 8. Wiring: not persisted, not orchestrator-wired

Not wired into `src/orchestrator.py` - same reasoning as Phases 22-26: this
needs a period-over-period comparison (and best-effort cross-module
context), not a single just-ingested file's dates. No table to persist to
either - confirmed no `evidence` table in `sql/schema.sql`; unlike Phase 26
(which persists to the pre-built `recommendations` table), Phase 27's
output is consumed directly by its caller (Phase 28's `src/ai_analyst.py`,
or later a Streamlit page) and never written to the database itself -
matching the no-persistence precedent of RCA (Phase 22) and impact analysis
(Phase 23), since evidence assembly is itself a read-only synthesis of
already-persisted/already-computed data, not a new computation worth
storing.

## 9. Why a typed dataclass, not a dict

`src.recommendations.Recommendation` carries a free-form `detail: dict` for
supplementary context. `EvidencePackage` deliberately does not: this
module's entire purpose is to be the strict contract an LLM prompt builder
(Phase 28) can rely on by construction - "the LLM never invents figures"
(dev rule 3) is only as strong as the thing that hands it numbers. An
untyped bucket would undermine that guarantee. `to_dict()` (plain
`dataclasses.asdict`) still provides a JSON-serializable payload for the
eventual LLM prompt.

## 10. API

| Symbol | Purpose |
|---|---|
| `EvidencePackage` | the full package - see section 3 |
| `RootCauseSummary` / `ForecastSummary` / `AnomalyLink` / `RecommendationLink` | typed sub-components |
| `compute_confidence(magnitude, rca_confidence, anomaly_confidence)` | pure - section 6 |
| `summarize_root_cause(result)` / `summarize_forecast(points, horizon_days)` | pure flattening helpers |
| `build_notes(change, root_cause, forecast, anomaly)` | pure - section 4 |
| `assemble_evidence(db, metric, grain="day")` | the on-demand entry point, one metric |
| `assemble_all_evidence(db, grain="day")` | every metric `compare_period` returns a `ChangeRecord` for |

## 11. Configuration

No new environment variables (a design decision, not an oversight) - every
tunable this module's lookups depend on
(`CHANGE_DETECTION_THRESHOLD_PCT`, `RCA_MIN_EVIDENCE_ROWS`,
`PRIORITY_IMPACT_REFERENCE`, `FORECAST_ALPHA`/`FORECAST_BETA`) already
belongs to the module that owns that computation and is read fresh by that
module already. `LLM_PROVIDER`/`GEMINI_API_KEY`/`GEMINI_MODEL`/
`LLM_TIMEOUT_SECONDS` (already in `.env.example`) stay unused until Phase 28.

## 12. Verify

```powershell
pytest -q tests/test_phase27_ai_evidence.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase27_ai_evidence.py
```

## 13. Related documents

- [`change-detection.md`](change-detection.md) - the triggering `ChangeRecord`
- [`anomaly-fusion.md`](anomaly-fusion.md) - the linked `anomalies` row
- [`root-cause-analysis.md`](root-cause-analysis.md) - the RCA input
- [`business-impact-engine.md`](business-impact-engine.md) - the impact input
- [`forecasting.md`](forecasting.md) - the forecast corroboration input
- [`recommendations.md`](recommendations.md) - the linked `recommendations` row
