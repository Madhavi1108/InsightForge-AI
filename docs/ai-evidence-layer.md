# InsightForge AI - AI Evidence Layer

> Phase 27 deliverable (spec Phase 53, part of FR-20). `src/ai_evidence.py`
> assembles a structured, verified evidence package for one metric - no new
> analytics, no LLM call.

## 1. What the spec says and the Phase 27/28 boundary

`docs/system-components.md` (verbatim): "`src/ai_evidence.py` (new Phase
27) - Responsibility: assemble a structured, verified evidence package
(metric, current, previous, change %, primary region, primary category,
impact, ...) before any LLM call."

That boundary is deliberate, not incidental. `docs/security.md` section 3:

> The LLM receives a **structured evidence package** built from verified
> analytical results - never raw user text concatenated into an
> instruction... The LLM **must not invent numbers** - every figure in an
> explanation traces to the evidence package.

**Phase 27 (this module) builds that package. Phase 28
(`src/ai_analyst.py`, spec Phases 54-55, not yet built) is what actually
calls Gemini or falls back to a deterministic template.** `src/ai_evidence.py`
never imports `google.generativeai` and never makes a network call - it is
pure composition of already-verified analytics output.

## 2. Composition, not new formulas

Every field is read straight from an existing, already-documented module's
own output - `src/ai_evidence.py` invents nothing:

| Evidence field | Source | Metrics covered |
|---|---|---|
| `current` / `previous` / `pct_change` / `direction` / `significant` | `src.change_detection.compare_period(db, "day")` | all 10 KPIs |
| `primary_region` / `primary_category` / `root_cause_contribution` / `root_cause_confidence` | `src.root_cause.analyze_root_cause` (RCA tiers) | revenue, profit, units |
| `expected` / `actual` / `gap` / `at_risk` / `customers_affected` / `orders_affected` | `src.impact_analysis.assess_business_impact` | revenue, profit |
| `forecast_trend` / `forecast_next_7d_value` | `src.forecasting.forecast_metric(horizon=7)` | revenue, profit, orders |
| `anomaly_severity` / `anomaly_confidence` | a small best-effort `anomalies` lookup (mirrors `recommendations._lookup_anomaly`) | any metric with a matching fused anomaly |
| `recommendations` | `src.recommendations.generate_recommendations(db, "day")`, filtered to this metric | any metric a rule fired for |

## 3. Best-effort, never fabricated

Every sub-lookup beyond the base day comparison is wrapped in `try/except`:
a failure means that field is simply absent (`None`, or an empty list for
recommendations) - never a fabricated number. This is the same "advisory,
never fails" convention Phase 20/21's orchestrator wiring and Phase 26's
RCA/forecast enrichment both use, applied here to evidence assembly.
`assemble_evidence` itself returns `None` only when there isn't yet a
day-over-day comparison for the metric at all (too little history).

## 4. Traceability: `sources`

`sources` lists exactly which modules actually contributed a field to this
package (`change_detection` always; `root_cause`/`impact_analysis`/
`forecasting`/`anomalies`/`recommendations` only when they returned
something). This makes `docs/security.md`'s "every figure traces to the
evidence package" requirement checkable one level further back - every
*evidence field* traces to a named, verified source module, not just every
sentence in a later explanation.

## 5. Aggregate confidence

This project's own documented choice (the spec gives Phase 27 no formula),
the same "own aggregate" pattern as RCA's `0.7*concentration + 0.3*evidence
_strength`:

```
confidence = mean of whichever signals are present:
  - magnitude-based confidence (min(1.0, magnitude / 100)) - always present
  - root_cause_confidence     - when RCA ran for this metric
  - anomaly_confidence        - when a matching fused anomaly exists
```

`0.0` only when `signals` is empty, which cannot happen in practice since
the magnitude-based signal is always computed from the base day comparison.

## 6. Why on-demand, no persistence

Same reasoning as Phases 17/22/23/24 (change detection/RCA/impact/RFM): no
table in `sql/schema.sql` stores an "evidence package" (there is no
`evidence` table), and assembling one needs cross-cutting, whole-history
analytics, not a single just-ingested file's dates - so it isn't wired into
`src/orchestrator.py`. Unlike Phase 25/26 (forecasting/recommendations),
there is no persistence table at all here, matching a package that exists
only to be handed to Phase 28 for one explanation at a time.

## 7. API

| Symbol | Purpose |
|---|---|
| `EvidencePackage` | the assembled package for one metric |
| `evidence_package_to_dict(pkg)` | flat, JSON-serialisable form - the exact shape Phase 28 will hand to Gemini/the template |
| `aggregate_confidence(signals)` | pure - the mean-of-present-signals formula |
| `assemble_evidence(db, metric)` | the entry point - `None` below the minimum history |

## 8. Verify

```powershell
pytest -q tests/test_phase27_ai_evidence.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase27_ai_evidence.py
```

```python
from src.database import Database
from src.ai_evidence import assemble_evidence, evidence_package_to_dict

db = Database()
pkg = assemble_evidence(db, "revenue")
print(evidence_package_to_dict(pkg))
```

## 9. Related documents

- [`security.md`](security.md) - section 3, the AI/LLM safety boundary this module exists to enforce
- [`root-cause-analysis.md`](root-cause-analysis.md), [`business-impact-engine.md`](business-impact-engine.md), [`forecasting.md`](forecasting.md), [`recommendations.md`](recommendations.md) - the composed sources
- [`data-flow.md`](data-flow.md) - stage 15
