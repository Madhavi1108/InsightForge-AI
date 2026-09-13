# InsightForge AI - Product Intelligence

> Phase 24 deliverable (spec Phase 48, FR-17). `src/product_intelligence.py`
> is a pure, on-demand analytics module - no database writes, not wired into
> `src/orchestrator.py`.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phase 48 (verbatim):

> PHASE 48 - PRODUCT INTELLIGENCE: Classify products: Star, High Profit,
> High Revenue, Fast Growing, Declining, High Return, Low Margin, Slow
> Moving. Create product health score.

No thresholds, no growth window, and no "Star"/health-score formula are
given - only the eight class names and a bare requirement for a health
score, same situation as every prior phase.

## 2. Why this module is not wired into the orchestrator

Same reasoning as `src/rfm.py` (this phase's sister module) and Phase
17/22/23 before it: no table in `sql/schema.sql`, no "Outputs:" line in
`docs/system-components.md`'s Phase 24 stub, and `docs/business-questions.md`
Q4 describes product intelligence as something a Streamlit/Power BI page
consumes, not something computed automatically per ingested file.

## 3. Base aggregates: reused, not re-derived

Revenue/profit/margin_pct/units/orders/return_rate_pct come straight from
Phase 16's `product_performance` view (`SELECT * FROM product_performance`)
- exactly the numbers `docs/business-questions.md` Q4 already names as this
module's source. No SQL is duplicated for figures the view already computes.

## 4. Growth: the one thing the view doesn't have

`product_performance` is all-time/complete-history - it has no trend, so
Fast Growing/Declining can't come from it. This module compares each
product's revenue over the trailing `PRODUCT_GROWTH_WINDOW_DAYS` (default
30) against the equal-length window immediately before it, both anchored to
`MAX(order_date)` across `fact_sales` (the same dataset-relative "today" as
`src.rfm`'s recency reference - never wall-clock).

A product with no revenue in the previous window (new to the catalogue, or
simply had none) gets `revenue_growth_pct = None` - undefined, not
fabricated as `+inf` or `0` - matching `change_detection._pct_change`'s
convention for the same "previous == 0" situation.

## 5. Classification thresholds (this project's own, quartile-relative)

Every "High X"/"Low X" tag is relative to the *current product population*,
via the same `numpy.percentile` idiom `anomaly_detection.detect_iqr` uses
for quartiles:

| Flag | Rule |
|---|---|
| `is_high_revenue` | revenue >= p75(revenue) |
| `is_high_profit` | profit >= p75(profit) |
| `is_high_return` | return_rate_pct >= p75(return_rate_pct) |
| `is_low_margin` | margin_pct <= p25(margin_pct) |
| `is_slow_moving` | units <= p25(units) |
| `is_fast_growing` | revenue_growth_pct >= `PRODUCT_GROWTH_THRESHOLD_PCT` (default 20.0) |
| `is_declining` | revenue_growth_pct <= -`PRODUCT_GROWTH_THRESHOLD_PCT` |
| `is_star` | `is_high_revenue and is_high_profit and is_fast_growing` |

"Star" has no independent threshold of its own in the spec - it's listed
alongside the single-factor tags with no definition. This project reads it
as a BCG-matrix-style "all-round winner" composite: a product doing well on
revenue, profit, *and* growth simultaneously.

## 6. Product health score (this project's own weighted composite)

Same shape as Phase 20's anomaly severity engine - several documented
factors combined with fixed weights, each normalised to `[0, 1]` via
`percentile_rank()` (0 = lowest in the population, 1 = highest):

```
health_score = 100 * (0.35 * profit_rank
                     + 0.25 * margin_rank
                     + 0.20 * growth_rank
                     + 0.20 * (1 - return_rank))
```

`growth_rank` defaults to `0.5` (neutral) when `revenue_growth_pct is None`
- a product with no growth data isn't penalised or rewarded for it. Weights
favour profit and margin (the two most direct profitability signals) over
growth and returns, but every factor contributes.

## 7. `ProductIntelligence` fields

| Field | Meaning |
|---|---|
| `product_id`, `product_name`, `category` | identity |
| `revenue`, `profit`, `margin_pct`, `units`, `orders`, `return_rate_pct` | from `product_performance` |
| `revenue_growth_pct` | trailing-window vs. prior-window % change, or `None` |
| `is_star`, `is_high_profit`, `is_high_revenue`, `is_fast_growing`, `is_declining`, `is_high_return`, `is_low_margin`, `is_slow_moving` | the spec's 8 classes (independent boolean tags - a product can carry several at once) |
| `health_score` | 0-100 weighted composite |

## 8. API

| Symbol | Purpose |
|---|---|
| `percentile_rank(values)` | pure - no DB |
| `classify_products(totals, growth_by_product, threshold_pct=None)` | pure batch assembly - no DB |
| `growth_window_days()` / `growth_threshold_pct()` | read `PRODUCT_GROWTH_WINDOW_DAYS` / `PRODUCT_GROWTH_THRESHOLD_PCT`, fresh each call |
| `analyze_product_intelligence(db, growth_window_days_=None, growth_threshold_pct_=None)` | fetches `product_performance` + the growth window query, assembles results |

## 9. Configuration

New env vars:
```
PRODUCT_GROWTH_WINDOW_DAYS=30
PRODUCT_GROWTH_THRESHOLD_PCT=20.0
```

## 10. Verify

```powershell
pytest -q tests/test_phase24_rfm_product_intelligence.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase24_rfm_product_intelligence.py
```

```python
from src.product_intelligence import analyze_product_intelligence
from src.database import Database
results = analyze_product_intelligence(Database())
stars = [r for r in results if r.is_star]
print(len(stars), "star products")
```

## 11. Related documents

- [`customer-rfm-analysis.md`](customer-rfm-analysis.md) - this phase's
  sister module
- [`analytical-views.md`](analytical-views.md) - `product_performance`,
  the view this module's base aggregates are reused from
- [`anomaly-fusion.md`](anomaly-fusion.md) - the sibling "weighted composite
  of documented factors" scoring precedent (severity engine)
