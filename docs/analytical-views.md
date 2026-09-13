# InsightForge AI - Analytical Views

> Phase 16 deliverable (spec Phase 32). `sql/views.sql` wraps Phase 15's
> already-established KPI formulas (`docs/kpi-engine.md`) into 6 persistent
> PostgreSQL views so downstream consumers (Streamlit, Power BI, reporting,
> and the business questions in `docs/business-questions.md`) can query a
> named view instead of copy-pasting `sql/kpi_queries.sql` text.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phase 32 (verbatim, read during Phase 15's session):

> PHASE 32 - ANALYTICAL VIEWS: Create PostgreSQL views for: daily_kpis,
> monthly_kpis, regional_performance, category_performance,
> product_performance, customer_performance.

Only the 6 names are given - no column list. This phase reuses Phase 15's
already-documented formulas and, per FR-08 ("10 metrics available per
day/month/region/category/product/customer"), gives each view every metric
that's meaningful at its grain.

**Views are complete, never filtered.** Phase 15's `top_products_by_revenue`
query filtered to above-average products to demonstrate a subquery
technique; a real view needs every product, so `product_performance` keeps
all rows and exposes an `above_avg_revenue` boolean column instead.

## 2. The 6 views

| View | Grain | Notable columns beyond the 10 metrics |
|---|---|---|
| `daily_kpis` | day | `order_date`, `day_name`, `is_weekend` |
| `monthly_kpis` | month | `month_start`, `year`, `month`, `month_name` |
| `regional_performance` | region | `states_covered`, `above_avg_region_revenue` |
| `category_performance` | category | `rank_by_revenue`, `dense_rank_by_profit` |
| `product_performance` | product | `product_name`, `category`, `rank_by_revenue`, `above_avg_revenue` |
| `customer_performance` | customer | `customer_name`, `customer_segment`, `rank_by_revenue` (no `customers` column - the grain is already one row per customer) |

All 10 formulas are exactly `docs/kpi-engine.md`'s (Revenue, Profit, Margin,
Orders, Customers, Units, AOV, Return Rate, Average Discount, Shipping
Time) - not redefined here.

## 3. Idempotency

Views have no portable `CREATE VIEW IF NOT EXISTS` (only PG15+), so this
file uses `CREATE OR REPLACE VIEW` throughout - a plain re-apply is always a
safe no-op/refresh, matching `sql/schema.sql`'s idempotent convention as
closely as views allow.

## 4. Applying

`scripts/apply_schema.py` runs `sql/schema.sql` then `sql/views.sql` in the
same pass (views need their base tables to exist first). `--drop` runs
`sql/drop_schema.sql` first, which `CASCADE`s and removes the views
automatically before they're recreated. The script's summary now also
reports `views present: X/6`.

## 5. Verify

```powershell
python scripts/postgres.py up
python scripts/apply_schema.py
python scripts/generate_dataset.py --clean
python run_pipeline.py --scan

pytest -q tests/test_phase16_analytical_views.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase16_analytical_views.py
```

```sql
SELECT * FROM daily_kpis ORDER BY order_date;
SELECT * FROM product_performance ORDER BY revenue DESC;
```

## 6. Related documents

- [`kpi-engine.md`](kpi-engine.md) - the formulas and techniques these views wrap
- [`database-schema.md`](database-schema.md) - `fact_sales`/dimension column reference
- [`business-questions.md`](business-questions.md) - the questions these views answer
