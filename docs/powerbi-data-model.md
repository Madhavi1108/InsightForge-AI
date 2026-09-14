# InsightForge AI - Power BI Data Model

> Phase 31 deliverable (spec Phase 60, part of FR-23). `dashboard/data_model.pq`
> (Power Query M source queries) and `dashboard/measures.dax` (the DAX
> measures library) - the data model Phase 32's report pages will be built
> on. No `.pbix` file - see section 2.

## 1. What the spec says and what it leaves open

`docs/system-components.md` (verbatim): "Power BI (`dashboard/`, new Phase
31-32) - **Data model**: connect to PostgreSQL / analytical outputs;
relationships, measures, calculated metrics, date hierarchy, DAX. **Pages**:
Executive Overview; Business Drivers; Customer Intelligence; Product
Intelligence; Risk & Anomalies; Forecast; Pipeline Health. **Refresh**:
documented manual / gateway step." Phase 31 is the **data model** half of
that; the report pages are Phase 32 (spec Phases 61-63), not this phase. No
table, relationship, measure formula, or refresh strategy is specified
anywhere - this project's own documented choices follow, same situation as
every other spec-silent phase.

## 2. Why there is no `.pbix` file

A `.pbix` is a binary/zip container that only Power BI Desktop's GUI
produces - it cannot be hand-authored as text, and generating one from a
script without the actual application would mean fabricating a binary this
environment can neither open nor verify. That is exactly what this
project's "never fake functionality" convention forbids - the same
reasoning Phase 12 already applied to Alteryx: hand-authored, *real*,
syntactically valid `.yxmd` XML, explicitly documented as "not executed
against a real engine here" rather than a faked execution result.

Phase 31's deliverable is the same shape: real, syntactically valid,
hand-authored text a person pastes into Power BI Desktop -
**`dashboard/data_model.pq`** (Power Query M, the source layer) and
**`dashboard/measures.dax`** (DAX, the measures layer) - plus this document
explaining exactly how to use them. `docs/PHASE_STATUS.md`'s own
environment notes already establish this convention: "Power BI Desktop:
installed. Automated dataset refresh is a documented manual/gateway step."

## 3. Imported tables

| Table | Role | Source |
|---|---|---|
| `dim_date` | date dimension, marked as the Power BI Date Table | Phase 8 |
| `dim_customer` | customer dimension | Phase 8 |
| `dim_product` | product dimension | Phase 8 |
| `dim_region` | region/state/city dimension | Phase 8 |
| `fact_sales` | grain = one order line | Phase 8 |
| `pipeline_runs` | one row per pipeline execution | Phase 8 |
| `data_quality_results` | 7 dimensions + Overall, per run | Phase 8/14 |
| `anomalies` | fused anomaly detector output | Phase 8/20 |
| `drift_results` | PSI per monitored distribution, per run | Phase 8/21 |
| `forecast_results` | 7/30-day forecasts + MAE/RMSE/MAPE | Phase 8/25 |
| `recommendations` | rule-based recommendations + priority | Phase 8/26 |

The star schema is imported **raw** (not the pre-aggregated Phase 16 views)
- the Power BI/Kimball best practice: DAX measures aggregate on demand, so
every Phase 32 page can still slice/filter by any dimension, which a
pre-aggregated view would prevent.

## 4. Relationships

| From | To | Note |
|---|---|---|
| `fact_sales[date_key]` | `dim_date[date_key]` | many-to-one |
| `fact_sales[customer_key]` | `dim_customer[customer_key]` | many-to-one |
| `fact_sales[product_key]` | `dim_product[product_key]` | many-to-one |
| `fact_sales[region_key]` | `dim_region[region_key]` | many-to-one |
| `pipeline_runs[run_id]` | (parent of) `data_quality_results`, `anomalies`, `drift_results`, `forecast_results`, `recommendations` | each insight table's own `run_id` FK |
| `anomalies[anomaly_date]` | `dim_date[date_key]` | lets Risk & Anomalies reuse the shared date hierarchy |
| `forecast_results[forecast_date]` | `dim_date[date_key]` | lets Forecast reuse the shared date hierarchy |

`drift_results` and `data_quality_results` relate only via `run_id` - they
don't carry a single clean date column to key into `dim_date` (`drift
_results` has 4 window-boundary dates; `data_quality_results` is scored per
file, not per calendar day). Documented limitation, not an oversight.

## 5. Date hierarchy

`dim_date` is marked as the Date Table (Power BI: Table tools -> Mark as
Date Table -> `date_key`). Hierarchy: `year -> quarter -> month -> day`
(`dim_date`'s own columns, `sql/schema.sql` Phase 8) - no new date logic,
just the standard Power BI step applied to columns that already exist.

## 6. DAX measures

The 10 KPI measures are a direct DAX translation of the exact formulas
already documented in [`kpi-engine.md`](kpi-engine.md) /
`sql/kpi_queries.sql` (Phase 15) - nothing new invented:

| Measure | DAX | Matches (SQL) |
|---|---|---|
| Revenue | `SUM(fact_sales[revenue])` | `SUM(revenue)` |
| Profit | `SUM(fact_sales[profit])` | `SUM(profit)` |
| Margin % | `DIVIDE([Profit], [Revenue])` | `100 * SUM(profit) / NULLIF(SUM(revenue), 0)` |
| Orders | `DISTINCTCOUNT(fact_sales[order_id])` | `COUNT(DISTINCT order_id)` |
| Customers | `DISTINCTCOUNT(fact_sales[customer_id])` | `COUNT(DISTINCT customer_id)` |
| Units | `SUM(fact_sales[quantity])` | `SUM(quantity)` |
| AOV | `DIVIDE([Revenue], [Orders])` | `SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0)` |
| Return Rate % | `DIVIDE(CALCULATE(COUNTROWS(fact_sales), fact_sales[is_returned]=TRUE), COUNTROWS(fact_sales))` | `100 * COUNT(*) FILTER (WHERE is_returned) / NULLIF(COUNT(*), 0)` |
| Avg Discount % | `AVERAGE(fact_sales[discount]) * 100` | `100 * AVG(discount)` |
| Avg Shipping Days | `AVERAGE(fact_sales[shipping_days])` | `AVG(shipping_days)` |

`DIVIDE(...)` is used instead of a raw `/` throughout - DAX's own
divide-by-zero-safe idiom, the same role `NULLIF(..., 0)` plays in the SQL
versions.

Plus a small set of **insight-table measures** for Phase 32's Risk &
Anomalies / Forecast / Pipeline Health pages (spec names the pages, not
their measures - this project's own reasonable additions, same "spec
silent, own documented choice" pattern as every prior phase): `Anomaly
Count`, `Critical Anomalies`, `Avg Anomaly Confidence`, `Open
Recommendations`, `Avg Priority Score`, `Avg Forecast MAPE %`, `Latest
Forecast Value`, `Latest DQ Score`, `Pipeline Success Rate %`, `Failed
Runs`. Full definitions: `dashboard/measures.dax`.

## 7. Import mode, not DirectQuery

This project's own choice (the spec names no refresh strategy) - consistent
with `docs/data-flow.md` stage 16 and `docs/PHASE_STATUS.md`'s own
environment notes: "Automated dataset refresh is a documented manual/
gateway step." DirectQuery is a viable alternative (always-live data, no
refresh step) but Import is the default here, matching every other
already-documented "refresh is manual" convention in this project.

## 8. Credentials

`dashboard/data_model.pq` never embeds a password - Power BI's own
credential manager (prompted the first time the `PGServer`/`PGDatabase`
parameters are used) holds that outside the M text, exactly `docs/security
.md`'s "no secret ever hard-coded" rule applied to this new surface. The
two parameters' documented defaults match `.env.example`:
`PGServer = "localhost:5432"`, `PGDatabase = "insightforge"`.

## 9. Verify / use

```
1. Power BI Desktop -> Get Data -> PostgreSQL database.
2. Home -> Manage Parameters -> New: PGServer ("localhost:5432"),
   PGDatabase ("insightforge").
3. For each "// Query: <name>" block in dashboard/data_model.pq: Get Data ->
   Blank Query -> Advanced Editor -> paste the block -> rename the query
   to <name>.
4. Model view: verify/create the 7 relationships in section 4.
5. Table tools -> dim_date -> Mark as Date Table (date_key); build the
   Year -> Quarter -> Month -> Day hierarchy.
6. For each measure in dashboard/measures.dax: right-click the named table
   -> New measure -> paste the formula.
```

```powershell
pytest -q tests/test_phase31_powerbi_data_model.py
```

## 10. Related documents

- [`kpi-engine.md`](kpi-engine.md) - the 10 KPI formulas this model's measures translate
- [`database-schema.md`](database-schema.md) - every imported table's real column list
- [`alteryx-workflows.md`](alteryx-workflows.md) - the sibling "real hand-authored artifact, not executed here" precedent
- [`data-flow.md`](data-flow.md) - stage 16
