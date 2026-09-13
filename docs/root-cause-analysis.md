# InsightForge AI - Root-Cause Engine, Contribution Analysis & RCA Confidence

> Phase 22 deliverable (spec Phases 43-45, FR-14). `src/root_cause.py` is a
> pure, on-demand analytics module - no database writes, not wired into
> `src/orchestrator.py`.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 43-45 (verbatim):

> PHASE 43 - ROOT-CAUSE ENGINE: Create `src/root_cause.py`. Drill down:
> Region -> Category -> Subcategory -> Product -> Customer Segment.
> PHASE 44 - CONTRIBUTION ANALYSIS: Calculate contribution to KPI changes.
> Example: Revenue decline = ₹8.7L - West -₹5.1L, South -₹2.0L,
> North -₹1.1L, Other -₹0.5L.
> PHASE 45 - ROOT-CAUSE CONFIDENCE: Every RCA result should contain:
> Metric, Change, Primary Driver, Evidence, Contribution, Confidence.

No contribution formula, no confidence formula, and no persistence
requirement is given - only a worked example (used as a literal test
fixture below, since the module reproduces its exact numbers) and a list of
six required output fields.

## 2. Why this module is not wired into the orchestrator

Unlike Phase 20 (anomaly fusion) and Phase 21 (drift detection), several
independent signals confirmed this session point away from persistence/
wiring:

- `grep -i "root_cause\|rca\|contribution" sql/schema.sql` finds no table -
  no `run_id NOT NULL` constraint forces a per-run write, unlike
  `anomalies`/`drift_results`.
- `docs/data-flow.md` stage 11's "Writes" column is plain text ("RCA
  results (metric, driver, evidence, contribution, confidence)"), not
  backtick-quoted like every genuinely persisted stage - matching Phase
  17's own unpersisted `change records` convention exactly.
- `docs/business-questions.md` row 2 describes RCA as **invoked**:
  "Change detection -> root-cause engine (`src/root_cause.py`) -> AI
  Analyst" - triggered when a question needs answering, not run
  automatically per file.

So this module returns plain Python values, meant to be called by later
phases (business impact, recommendations, the AI Analyst) or a Streamlit
page whenever "why did metric X change" needs answering.

## 3. Scope: summable metrics only

`RCA_SUPPORTED_METRICS = ("revenue", "profit", "units")`. The contribution
formula (below) only holds mathematically for metrics that are true sums.
Margin/AOV/Return Rate/Avg Discount/Avg Shipping Time are ratios - their
"contribution" can't be additively decomposed the same way without
fabricating a number, so `analyze_root_cause` raises `ValueError` for them
rather than faking a result (development rule 1: never fake
functionality).

## 4. Contribution formula

For a metric and a dimension, each value's contribution:

```
contribution(value) = sum(metric, current period, dimension == value)
                     - sum(metric, previous period, dimension == value)
```

Contributions always sum to the total change - verified directly against
the spec's own worked example: West -5.1L, South -2.0L, North -1.1L,
Other -0.5L sum to -8.7L.

## 5. The drill-down (nested, sequential)

The spec's 5-step list is not five independent breakdowns - it's a nested
drill-down, matching `docs/business-questions.md`'s "region tier of
`src/root_cause.py`" / "category tier" language:

1. Score **Region** across all rows -> pick the primary region.
2. Filter to that region; score **Category** -> pick the primary category.
3. Filter to region+category; score **Sub-Category**.
4. Filter further; score **Product**.
5. Filter further; score **Customer Segment**.

Each tier is scored only over the rows still matching every earlier tier's
chosen value. Drill-down stops early (fewer than 5 tiers) once a tier has
no rows left.

## 6. RCA confidence (this project's own formula)

Echoing `docs/business-questions.md` row 14's own hint ("contribution
shares, evidence strength"):

```
confidence = 0.7 * concentration + 0.3 * evidence_strength

concentration = |primary driver's contribution| / sum(|every value's contribution| at that tier)
evidence_strength = min(1.0, (current_rows + previous_rows) / RCA_MIN_EVIDENCE_ROWS)
```

A single dominant driver (concentration near 1.0) with plenty of supporting
rows scores near 1.0; a diffuse change spread evenly across many values
scores low, even with plenty of data - the module never claims high
confidence in a driver that isn't actually dominant. The overall
`RootCauseResult.confidence` is the mean of every tier that was actually
scored.

## 7. `RootCauseResult` - the spec's 6 fields, plus the full drill-down

| Field | Spec name | Meaning |
|---|---|---|
| `metric` | Metric | one of `RCA_SUPPORTED_METRICS` |
| `change` | Change | `current_total - previous_total` |
| `primary_driver` | Primary Driver | the `" > "`-joined chain of every tier's chosen value |
| `contribution` | Contribution | the deepest tier's contribution amount |
| `confidence` | Confidence | mean of the scored tiers' confidence |
| `evidence` | Evidence | totals, periods, row counts for both periods |
| `tiers` | (the full drill-down) | `list[RootCauseTier]`, one per hierarchy level actually resolved |

## 8. API

| Symbol | Purpose |
|---|---|
| `RCA_SUPPORTED_METRICS`, `HIERARCHY` | scope constants |
| `min_evidence_rows()` | reads `RCA_MIN_EVIDENCE_ROWS`, fresh each call |
| `contribution_by_dimension(current_rows, previous_rows, dimension, metric)` | pure - no DB |
| `drill_down(current_rows, previous_rows, metric, hierarchy=HIERARCHY)` | pure - no DB |
| `analyze_root_cause(db, metric, current_start, current_end, previous_start, previous_end)` | fetches `fact_sales` rows for both periods, drills down, assembles the result |

## 9. Configuration

New env var:
```
RCA_MIN_EVIDENCE_ROWS=30
```

## 10. Verify

```powershell
pytest -q tests/test_phase22_root_cause.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase22_root_cause.py
```

```python
from src.root_cause import analyze_root_cause
from src.database import Database
result = analyze_root_cause(Database(), "revenue", "2026-02-01", "2026-02-07", "2026-01-25", "2026-01-31")
print(result.primary_driver, result.contribution, result.confidence)
```

## 11. Related documents

- [`change-detection.md`](change-detection.md) - the sibling on-demand, unpersisted analytics module this one follows
- [`database-schema.md`](database-schema.md) - `fact_sales` and dimension columns this module reads
- [`data-flow.md`](data-flow.md) - stage 11
