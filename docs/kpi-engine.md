# InsightForge AI - Core SQL KPI Engine & Advanced SQL Analytics

> Phase 15 deliverable (spec Phases 30-31, FR-08). `sql/kpi_queries.sql` is
> the analytics layer that reads the star schema populated by Phase 13's ETL
> and computes the 10 core metrics the spec names. Pure SQL - no Python
> module runs or wraps these queries (the first Python analytics module is
> `src/change_detection.py`, Phase 17).

## 1. What the spec says (verbatim) and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 30-31:

> PHASE 30 - CORE SQL KPI ENGINE: Create `sql/kpi_queries.sql`. Calculate:
> Revenue, Profit, Margin, Orders, Customers, Units, AOV, Return Rate,
> Average Discount, Shipping Time.
> PHASE 31 - ADVANCED SQL ANALYTICS: Demonstrate JOIN, CASE, CTE, subqueries,
> window functions, RANK, DENSE_RANK, LAG, LEAD, rolling average, running
> total.

That's the entire spec text - one file name, ten metric names, eleven
technique names, **no formulas**. The formulas below are this project's own
resolved definition (same situation as Phase 14's data-quality dimensions),
computed against `fact_sales`' grain (one row per order line):

| Metric | Formula | Notes |
|---|---|---|
| Revenue | `SUM(revenue)` | |
| Profit | `SUM(profit)` | |
| Margin | `100 * SUM(profit) / NULLIF(SUM(revenue), 0)` | percentage |
| Orders | `COUNT(DISTINCT order_id)` | an order can span multiple lines |
| Customers | `COUNT(DISTINCT customer_id)` | distinct buyers in the grain |
| Units | `SUM(quantity)` | |
| AOV (Average Order Value) | `SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0)` | |
| Return Rate | `100 * COUNT(*) FILTER (WHERE is_returned) / NULLIF(COUNT(*), 0)` | line-level, not order-level - `fact_sales`' own grain |
| Average Discount | `100 * AVG(discount)` | `discount` is stored as a 0-1 fraction |
| Shipping Time | `AVG(shipping_days)` | `NULL` for `Pending`/`Cancelled` orders, excluded automatically by `AVG` |

## 2. FR-08's required grains, and where each lives

FR-08 (`docs/requirements.md`) requires the 10 metrics "available per
day/month/region/category/product/customer". Every grain is covered by one
of the 10 queries in `sql/kpi_queries.sql`:

| Grain | Query |
|---|---|
| (none - whole dataset) | `core_kpis_overall` |
| day | `core_kpis_daily`, `rolling_7day_avg_revenue`, `running_total_revenue` |
| month | `core_kpis_monthly`, `monthly_revenue_trend` |
| region | `regional_performance` |
| category | `category_performance_ranked` |
| product | `top_products_by_revenue` |
| customer | `customer_performance` |

Phase 16 (spec 32, a separate phase) wraps these same calculations into 6
persistent, queryable views (`daily_kpis`, `monthly_kpis`,
`regional_performance`, `category_performance`, `product_performance`,
`customer_performance`) - deliberately different query names are used here
to keep the two phases distinct; Phase 16 can lift these `SELECT` bodies
directly into `CREATE VIEW` statements.

## 3. The 10 queries and their techniques

| Query | Grain | Techniques |
|---|---|---|
| `core_kpis_overall` | whole dataset | `CASE`/`FILTER`, aggregates |
| `core_kpis_daily` | day | `JOIN` (`dim_date`), `GROUP BY` |
| `core_kpis_monthly` | month | `CTE`, `JOIN`, `date_trunc` |
| `regional_performance` | region | `JOIN` (`dim_region`), scalar subquery (regions above the overall average) |
| `category_performance_ranked` | category | `RANK()`, `DENSE_RANK()` |
| `top_products_by_revenue` | product | `JOIN` (`dim_product`), subquery (products above average product revenue), `RANK()` |
| `customer_performance` | customer | `JOIN` (`dim_customer`), `RANK()` |
| `monthly_revenue_trend` | month | `CTE`, `LAG()`, `LEAD()` |
| `rolling_7day_avg_revenue` | day | `CTE`, windowed rolling average (`ROWS BETWEEN 6 PRECEDING AND CURRENT ROW`) |
| `running_total_revenue` | day | `CTE`, windowed running total (`SUM() OVER (ORDER BY ...)`) |

Every one of the spec's 11 techniques (JOIN, CASE, CTE, subqueries, window
functions, RANK, DENSE_RANK, LAG, LEAD, rolling average, running total)
appears at least once above.

## 4. File convention

Each query is preceded by a `-- @query: <name>` marker comment. Every block
from one marker to the next (or end of file) is valid, directly-runnable SQL
on its own - the leading `--` comments are harmless to leave in when
executing via `psql`, a DB client, or `Database.fetch_all(sql_text)`. This
convention exists because, unlike `sql/schema.sql` (one whole-file DDL
script run by `scripts/apply_schema.py`), this file holds many independent
`SELECT`s meant to be run - or parsed and tested - individually.

## 5. Verify

```powershell
python scripts/postgres.py up
python scripts/apply_schema.py
python scripts/generate_dataset.py --clean
python run_pipeline.py --scan

pytest -q tests/test_phase15_kpi_queries.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase15_kpi_queries.py
```

Or run any single query directly:
```powershell
psql -d insightforge -f sql/kpi_queries.sql   # runs the whole file
```

## 6. Related documents

- [`database-schema.md`](database-schema.md) - `fact_sales`/dimension column reference
- [`star-schema-etl.md`](star-schema-etl.md) - how `fact_sales` gets populated
- [`data-quality-engine.md`](data-quality-engine.md) - the pattern this doc follows for a spec-silent formula set
