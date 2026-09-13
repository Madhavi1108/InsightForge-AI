# InsightForge AI - Business Impact Engine

> Phase 23 deliverable (spec Phase 46, FR-15). `src/impact_analysis.py` is a
> pure, on-demand analytics module - no database writes, not wired into
> `src/orchestrator.py`.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phase 46 (verbatim):

> PHASE 46 - BUSINESS IMPACT ENGINE: Create `src/impact_analysis.py`.
> Calculate: Expected Revenue, Actual Revenue, Revenue Gap, Expected Profit,
> Actual Profit, Profit Gap, Revenue at Risk, Profit at Risk, Customers
> Affected, Orders Affected.

No formula for "expected", "gap", or "at risk" is given - only the list of
ten required figures, same situation as every prior phase.

## 2. Why this module is not wired into the orchestrator

Same reasoning as Phase 17 (`change_detection.py`) and Phase 22
(`root_cause.py`):

- No table in `sql/schema.sql` names revenue/profit expected-vs-actual
  figures - no `run_id NOT NULL` constraint forces a per-run write, unlike
  `anomalies`/`drift_results`.
- `docs/data-flow.md` stage 12's "Writes" column is plain text ("impact
  figures"), not backtick-quoted like a genuinely persisted stage.
- `docs/business-questions.md` Q6 describes this as something the
  recommendation engine and AI Analyst *consume*, not something computed
  automatically per ingested file.

So this module returns plain Python values, meant to be called later by the
recommendation engine (Phase 26), the AI evidence layer (Phase 27+), or a
Streamlit "Business Impact" page.

## 3. What "expected" means here

The spec names no forecasting model at this point in the build (forecasting
is Phase 25, not yet built) and no baseline formula. Rather than invent a
parallel definition, this module reuses **Phase 19's rolling baseline**
(`src.anomaly_detection.detect_rolling_baseline`), which already computes,
for each date, the trailing `ROLLING_WINDOW_DAYS`-day mean and documents it
as the *"expected range"* for a metric - the same word `docs
/business-questions.md` Q6 uses ("KPI expected vs actual"). Concretely:

```
expected_revenue(date) = rolling_mean(revenue, preceding ROLLING_WINDOW_DAYS days)
actual_revenue(date)   = revenue(date)
```

...and identically for profit. Reusing Phase 19's machinery directly means:

- No new environment settings - `ROLLING_WINDOW_DAYS` (7) /
  `ROLLING_BASELINE_MULTIPLIER` (2.0) apply exactly as they do for anomaly
  detection.
- A date with no trailing window yet (the first `ROLLING_WINDOW_DAYS` days
  of history) has no "expected" figure and is simply not assessed - never a
  fabricated baseline from too little data (development rule 1).

## 4. Gap and at-risk formulas (this project's own)

```
gap      = actual - expected                  # signed: positive = upside, negative = shortfall
at_risk  = max(0.0, expected - actual)         # only a shortfall counts as risk
```

`gap` mirrors `change_detection`'s current-minus-previous convention
exactly. `at_risk` is this project's own reading of the spec's bare phrase
"Revenue at Risk" / "Profit at Risk": a day that *beat* its expectation
carries no risk, so surplus days contribute `0.0`, never a negative "risk".

## 5. Customers Affected / Orders Affected

Queried directly from `fact_sales` for the assessed date - the same query
shape as `src.root_cause`'s `_fetch_fact_rows`:

```sql
SELECT COUNT(DISTINCT customer_id) AS customers, COUNT(DISTINCT order_id) AS orders
FROM fact_sales WHERE order_date = :date
```

These are the customers/orders that transacted on that date - the
population whose activity produced the actual (vs. expected) result, not a
conditional "only when there's a shortfall" count (the spec lists them as
unconditional calculations).

## 6. `BusinessImpactResult` - the spec's 10 fields

| Field | Spec name |
|---|---|
| `expected_revenue` | Expected Revenue |
| `actual_revenue` | Actual Revenue |
| `revenue_gap` | Revenue Gap |
| `expected_profit` | Expected Profit |
| `actual_profit` | Actual Profit |
| `profit_gap` | Profit Gap |
| `revenue_at_risk` | Revenue at Risk |
| `profit_at_risk` | Profit at Risk |
| `customers_affected` | Customers Affected |
| `orders_affected` | Orders Affected |

Plus `date` (the assessed date) and `evidence` (rolling std/bounds for both
metrics, for callers that want to show the baseline band).

## 7. API

| Symbol | Purpose |
|---|---|
| `IMPACT_METRICS` | `("revenue", "profit")` |
| `compute_gap(expected, actual)` | pure - no DB; returns `(gap, at_risk)` |
| `assess_business_impact(db, date=None, window=None, multiplier=None)` | one date's impact (default: latest date the rolling baseline covers); `None` if that date isn't covered |
| `assess_business_impact_series(db, window=None, multiplier=None)` | every covered date, ascending - a trend view |

## 8. Configuration

No new environment variables - reuses Phase 19's `ROLLING_WINDOW_DAYS` /
`ROLLING_BASELINE_MULTIPLIER`.

## 9. Verify

```powershell
pytest -q tests/test_phase23_business_impact.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase23_business_impact.py
```

```python
from src.impact_analysis import assess_business_impact
from src.database import Database
result = assess_business_impact(Database())
print(result.revenue_gap, result.revenue_at_risk, result.customers_affected)
```

## 10. Related documents

- [`root-cause-analysis.md`](root-cause-analysis.md) - the sibling on-demand,
  unpersisted analytics module this one follows
- [`anomaly-detection.md`](anomaly-detection.md) - the rolling baseline this
  module's "expected" figures are reused from
- [`data-flow.md`](data-flow.md) - stage 12
