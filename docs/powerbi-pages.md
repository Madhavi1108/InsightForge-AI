# InsightForge AI - Power BI Report Pages

> Phase 32 deliverable (spec Phases 61-63, part of FR-23). The 7 report
> pages built on Phase 31's data model (`dashboard/data_model.pq`,
> `dashboard/measures.dax`) plus this phase's `dashboard/page_measures.dax`.
> No `.pbix` - same reasoning as Phase 31 (section 2 there): a GUI-produced
> binary this environment can't author or verify. This document is the
> page-by-page specification a person builds from in Power BI Desktop.

## 1. What the spec says

`docs/system-components.md` (verbatim): "**Pages**: Executive Overview;
Business Drivers; Customer Intelligence; Product Intelligence; Risk &
Anomalies; Forecast; Pipeline Health." No visual layout, chart type, or
field list is specified - this project's own documented design follows,
mapped onto `docs/business-questions.md`'s own question-to-page
assignments (already established before this phase).

## 2. An honest constraint: 3 pages can't show their "official" analytics
module's output, because it was never persisted

`docs/business-questions.md` pairs **Business Drivers** with RCA
contribution analysis (`src/root_cause.py`) and **Customer
Intelligence**/**Product Intelligence** with RFM segments (`src/rfm.py`)
and product health flags (`src/product_intelligence.py`). All three are
**on-demand Python modules with no table in `sql/schema.sql`** - already
documented that way in their own Phase 22/24 docs ("not persisted, not
orchestrator-wired"). Power BI only reads Postgres tables (Phase 31's
model) - it cannot call a Python function - so it cannot show an RFM
segment label or an RCA primary-driver chain that was computed once, in
memory, for one prior request, and never written anywhere.

Rather than silently omitting those pages or inventing a fake data source,
each of the 3 affected pages is built from **the raw `fact_sales` +
dimension aggregates instead** - the same underlying rows the Python
modules themselves read, sliced and compared natively in Power BI (a
region-by-region revenue matrix with a period-variance column answers
"which region drove the change" the same way RCA's contribution tier does,
just via direct aggregation instead of a stored decomposition). Each
page's section below states this explicitly where it applies.

## 3. Executive Overview

**Answers**: Q1 "What happened?" (`docs/business-questions.md`).

- **KPI cards**: `Revenue`, `Profit`, `Margin %`, `Orders`, `Customers`
  (Phase 31 measures) for the selected date range.
- **Trend line**: `Revenue`/`Profit`/`Orders` by `dim_date[date_key]`
  (day grain).
- **Period-comparison cards**: `Revenue % Change (Day)`, `Profit % Change (Day)`, `Orders % Change (Day)`, and `Revenue % Change (Month)` (this
  phase's new measures, each built on its own helper measure: `Revenue Prev Day`, `Profit Prev Day`, `Orders Prev Day`, `Revenue Prev Month` -
  the day-over-prior-day / month-over-prior-month view
  `src/change_detection.py`'s own day/month grains compute in Python, read
  here straight from the star schema via DAX time intelligence).
- **Early-warning row**: `Critical Anomalies`, `Open Recommendations`,
  `Pipeline Success Rate %` (Phase 31 measures) - one glance at whether
  today needs attention.
- **Slicers**: date range, region, category.

## 4. Business Drivers

**Answers**: Q3 "Which region caused the decline?", Q9 "Which categories
drove the change?"

**Limitation** (section 2): RCA's specific region -> category ->
sub-category drill-down (`src/root_cause.py`) isn't persisted. This page
answers the same question via a **Region x Category matrix** of `Revenue`/
`Profit`, each cell showing `Revenue % Change (Day)`/`(Month)` (this
phase's measures) as the variance column - sorting/filtering the matrix by
variance surfaces the same "which region/category drove it" answer RCA's
contribution tier would, from data that actually exists in the model.

- **Matrix**: rows = `dim_region[region]`, columns = `dim_product[category]`,
  values = `Revenue`, `Profit`, `Revenue % Change (Month)`.
- **Drill-through**: region/category -> `dim_product[sub_category]` detail
  table.
- **Slicers**: date range.

## 5. Customer Intelligence

**Answers**: Q5 "Which customers are at risk?"

**Limitation** (section 2): RFM segment labels (Champions/At Risk/Lost,
`src/rfm.py`) aren't persisted. This page shows the same underlying
recency/frequency/monetary figures instead, by the one real segment field
the star schema has (`dim_customer[customer_segment]`:
Consumer/Corporate/Home Office).

- **Table**: `dim_customer[customer_name]`, `customer_segment`, `Revenue`,
  `Orders`, `AOV`, `Customer Recency Days` (this phase's measure) - sortable
  by recency (highest first) as a proxy for "at risk".
- **Cards**: `Customers`, `Dataset Max Date` (this phase's measure, the
  recency reference point).
- **Slicers**: `customer_segment`, date range.

## 6. Product Intelligence

**Answers**: Q4 "Which products are underperforming?"

**Limitation** (section 2): the Star/Declining/Low-Margin/Slow-Moving flags
(`src/product_intelligence.py`) aren't persisted. This page shows the raw
metrics those flags are computed from instead.

- **Table**: `dim_product[product_name]`, `category`, `sub_category`,
  `Revenue`, `Profit`, `Margin %`, `Return Rate %`, `Units` (all Phase 31
  measures) - sortable by any column so low-margin/high-return/slow-moving
  products surface the same way the Python flags would.
- **Slicers**: `category`, date range.

## 7. Risk & Anomalies

**Answers**: Q10 "Is today's data statistically abnormal?" - fully
persisted, no substitution needed.

- **Cards**: `Anomaly Count`, `Critical Anomalies`, `Avg Anomaly
  Confidence`, `Open Recommendations`, `Avg Priority Score` (Phase 31
  measures).
- **Table (anomalies)**: `metric`, `anomaly_date`, `deviation_pct`,
  `direction`, `severity`, `confidence`, `persistence_days`.
- **Table (recommendations)**: `title`, `severity`, `priority_score`,
  `priority_band`, `status`, linked via `linked_anomaly_id`.
- **Slicers**: `severity`/`priority_band`, date range.

## 8. Forecast

**Answers**: Q7 "What will happen next month?", Q15 "How accurate have our
forecasts been?" - fully persisted, no substitution needed.

- **Line chart**: `Revenue`/`Profit`/`Orders` (actual, historical) and
  `Forecast Value` with `Forecast Lower Bound`/`Forecast Upper Bound` as a
  shaded band (this phase's measures), sharing `dim_date` via
  `forecast_results[forecast_date]`'s Phase 31 relationship.
- **Cards**: `Avg Forecast MAPE %`, `Latest Forecast Value` (Phase 31
  measures).
- **Slicers**: `metric` (revenue/profit/orders), `horizon_days` (7/30).

## 9. Pipeline Health

**Answers**: Q11 "Has the shape of the data changed (drift)?", Q12 "How
healthy was today's data?", Q16 "Is the pipeline healthy?" - fully
persisted, no substitution needed.

- **Cards**: `Pipeline Success Rate %`, `Failed Runs`, `Latest DQ Score`,
  `Drift Features Flagged` (Phase 31 + this phase's measures).
- **Table (pipeline_runs)**: `file_name`, `status`, `started_at`,
  `duration_s`, `dq_score`.
- **Table (drift_results)**: `feature`, `psi_score`, `status`,
  `current_start`/`current_end`, filtered to the latest `run_id`.
- **Slicers**: `status`, date range.

## 10. How to build

```
1. Complete docs/powerbi-data-model.md's setup first (Phase 31).
2. For each measure in dashboard/page_measures.dax: right-click the named
   table -> New measure -> paste the formula.
3. Add 7 report pages, one per section above; add the visuals, fields,
   slicers, and drill-through named in that section.
```

```powershell
pytest -q tests/test_phase32_powerbi_pages.py
```

## 11. Related documents

- [`powerbi-data-model.md`](powerbi-data-model.md) - the data model these pages are built on
- [`business-questions.md`](business-questions.md) - the question-to-page mapping this phase follows
- [`root-cause-analysis.md`](root-cause-analysis.md), [`customer-rfm-analysis.md`](customer-rfm-analysis.md), [`product-intelligence.md`](product-intelligence.md) - the on-demand, non-persisted modules section 2 substitutes for
