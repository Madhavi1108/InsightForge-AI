# InsightForge AI - Customer RFM Analysis

> Phase 24 deliverable (spec Phase 47, FR-16). `src/rfm.py` is a pure,
> on-demand analytics module - no database writes, not wired into
> `src/orchestrator.py`.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phase 47 (verbatim):

> PHASE 47 - CUSTOMER RFM ANALYSIS: Calculate Recency, Frequency, Monetary.
> Create: Champions, Loyal, Potential Loyalists, New, At Risk, Lost.

No scoring formula, no segment-assignment rule, and no reference date for
"recency" is given - only the three metrics and six segment names, same
situation as every prior phase.

## 2. Why this module is not wired into the orchestrator

Same reasoning as Phase 17/22/23: no table in `sql/schema.sql` stores RFM
segments, `docs/system-components.md`'s Phase 24 stub carries no "Outputs:"
line, and `docs/business-questions.md` Q5 describes RFM as something
consumed by a Streamlit page / Power BI, not computed automatically per
ingested file. So this module returns plain Python values, called on demand.

## 3. Recency's reference date

There is no forecasting or "current date" concept anywhere upstream, and
this is a fixed synthetic dataset (generated with an end date, not a live
feed). Using wall-clock `datetime.now()` would call every customer "Lost"
the moment the generated data ages past a few weeks. Instead, `recency_days`
is measured against **`MAX(order_date)` across `fact_sales`** - the
dataset's own "latest known day" - matching how Phase 19's rolling baseline
and Phase 21's drift detection are always relative to the data, never
wall-clock.

## 4. R/F/M quintile scoring

Each of Recency/Frequency/Monetary is split into 5 bins via the
20/40/60/80th percentiles of the *current customer population* -
`score_quintiles()`, the same `numpy.percentile`-based binning
`anomaly_detection.detect_iqr` already uses for quartiles, just at
quintiles. This is the standard RFM technique (external, well-known method);
the specific implementation (percentile edges via `numpy.digitize`) is this
project's own.

Recency is scored in reverse: a *smaller* `recency_days` (customer ordered
more recently) is better, so it gets the *higher* score (5), not the lower
one a naive ascending bin would give it.

A customer population with zero spread on a metric (every value identical)
scores everyone `3` (the neutral middle) rather than dividing by a
zero-width band.

## 5. Segment assignment (this project's own priority rule)

FR-16 requires *every* active customer get one of the six named segments -
no "Other" bucket. `classify_segment(r, f, m)` is a priority-ordered rule
(first match wins) that always resolves to exactly one segment:

```
1. r>=4 and f>=4 and m>=4      -> Champions              (recent, frequent, big spender)
2. f>=4 and m>=3               -> Loyal                  (frequent + solid spend, any recency)
3. r<=2 and (f>=3 or m>=3)     -> At Risk                 (used to matter, has gone quiet)
4. r<=2 and f<=2 and m<=2      -> Lost                    (quiet, low frequency, low spend)
5. r>=4 and f<=2               -> New                     (just arrived, few orders yet)
6. otherwise                   -> Potential Loyalists     (catch-all: recent-ish, not yet proven)
```

Verified exhaustively (`test_classify_segment_always_returns_one_of_the_six`
over every `(r, f, m) in {1..5}^3`) - the rule never falls through undefined.

## 6. `CustomerRFM` fields

| Field | Meaning |
|---|---|
| `customer_id`, `customer_name`, `customer_segment` | identity + the existing demographic segment (Consumer/Corporate/Home Office - unrelated to RFM segment) |
| `recency_days` | days since last order, relative to the dataset's latest order date |
| `frequency` | distinct orders |
| `monetary` | total revenue |
| `r_score`/`f_score`/`m_score` | 1-5 quintile scores |
| `segment` | one of the 6 spec-named segments |

## 7. API

| Symbol | Purpose |
|---|---|
| `SEGMENTS` | the 6 segment name constants |
| `score_quintiles(values, reverse=False)` | pure - no DB |
| `classify_segment(r, f, m)` | pure - no DB |
| `build_customer_rfm(rows, reference_date)` | pure batch assembly - no DB |
| `analyze_customer_rfm(db, reference_date=None)` | fetches per-customer aggregates, defaults `reference_date` to `MAX(order_date)`, assembles results |

## 8. Configuration

No new environment variables - quintiles are fixed at 5 bins by definition.

## 9. Verify

```powershell
pytest -q tests/test_phase24_rfm_product_intelligence.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase24_rfm_product_intelligence.py
```

```python
from src.rfm import analyze_customer_rfm
from src.database import Database
results = analyze_customer_rfm(Database())
at_risk = [r for r in results if r.segment == "At Risk"]
print(len(at_risk), "customers at risk")
```

## 10. Related documents

- [`product-intelligence.md`](product-intelligence.md) - this phase's sister
  module
- [`root-cause-analysis.md`](root-cause-analysis.md) - the sibling
  on-demand, unpersisted analytics module this one follows
- [`analytical-views.md`](analytical-views.md) - `customer_performance`,
  the view this module's frequency/monetary definitions match
