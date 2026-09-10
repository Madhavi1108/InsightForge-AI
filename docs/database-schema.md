# InsightForge AI - Database Schema

> Phase 8 deliverable (spec Phases 15-17 - *star schema*, *operational tables*,
> *indexing*). Defined in [`../sql/schema.sql`](../sql/schema.sql), applied with
> `python scripts/apply_schema.py`. PostgreSQL provisioning is Phase 7
> ([`database-setup.md`](database-setup.md)); the connection layer
> (`src/database.py`) is Phase 9; the ETL that populates these tables is
> Phases 12-13.

## 1. Star schema (Phase 15)

```
                       ┌───────────────┐
                       │   dim_date    │
                       │  date_key PK  │
                       └───────┬───────┘
                               │ date_key
      ┌───────────────┐        │        ┌────────────────┐
      │ dim_customer  │ customer_key    │  dim_product   │
      │ customer_key  ├────────┼────────┤  product_key   │
      │      PK       │        │        │      PK        │
      └───────────────┘   ┌────┴─────┐  └────────────────┘
                          │fact_sales│
      ┌───────────────┐   │ grain =  │
      │  dim_region   │   │ 1 order  │
      │  region_key   ├───┤  line    │
      │      PK       │region_key    │
      └───────────────┘   └────┬─────┘
                               │ run_id
                       ┌───────┴───────┐
                       │ pipeline_runs │  (lineage - see §3)
                       └───────────────┘
```

### `fact_sales` - grain: one order line (one CSV row)

Surrogate PK `sales_key`. `order_id` is a **degenerate dimension** (it repeats
across the lines of a multi-item order). The four `*_key` columns are the
dimension foreign keys; the block of denormalised business attributes
(`order_date`, `customer_id`, `product_id`, `region`, `category`,
`sub_category`, `customer_segment`) is carried on the fact so the KPI / RCA
queries and the Phase 17 indexes hit one table without a join.

| Column | Type | Notes |
|--------|------|-------|
| `sales_key` | BIGSERIAL PK | surrogate |
| `run_id` | BIGINT FK → `pipeline_runs` | lineage; `ON DELETE CASCADE`; insert-only per run |
| `loaded_at` | TIMESTAMPTZ | load timestamp |
| `date_key` | DATE FK → `dim_date` | |
| `customer_key` | BIGINT FK → `dim_customer` | |
| `product_key` | BIGINT FK → `dim_product` | |
| `region_key` | BIGINT FK → `dim_region` | |
| `order_id` | TEXT | `ORD-########`, degenerate dimension |
| `order_date` | DATE | denormalised copy of `dim_date.date_key` |
| `customer_id` / `product_id` | TEXT | denormalised business keys |
| `region` / `category` / `sub_category` / `customer_segment` | TEXT | denormalised for roll-ups |
| `quantity` | INTEGER | `CHECK >= 1` |
| `unit_price` | NUMERIC(12,2) | `CHECK > 0` |
| `discount` | NUMERIC(4,3) | `CHECK 0 <= discount < 1` (anomaly rows may be high but valid) |
| `revenue` | NUMERIC(14,2) | `= quantity * unit_price * (1 - discount)`; `CHECK >= 0` |
| `cost` | NUMERIC(14,2) | `CHECK >= 0` |
| `profit` | NUMERIC(14,2) | `= revenue - cost`; may be negative |
| `payment_method` | TEXT | |
| `shipping_days` | INTEGER | nullable (pending orders); `CHECK >= 0` |
| `order_status` | TEXT | `Completed` / `Pending` / `Cancelled` / `Returned` |
| `return_status` | TEXT | `Not Returned` / `Returned` |
| `is_returned` | BOOLEAN | `GENERATED ALWAYS AS (return_status = 'Returned') STORED` - drives the Return Rate KPI |

Only **valid** rows reach `fact_sales`; invalid rows go to `rejected_records`.
The ETL re-derives `revenue` / `profit` and asserts them on load (FR-05).

### Dimensions

| Table | Grain | Key | Key attributes |
|-------|-------|-----|----------------|
| `dim_date` | one calendar date | `date_key DATE` PK (natural) | `year, quarter, month, month_name, day, day_of_week` (ISO 1-7), `day_name, week_of_year, is_weekend, is_month_end` |
| `dim_customer` | one customer | `customer_key` PK, `customer_id` UNIQUE | `customer_name`, `customer_segment` CHECK ∈ {Consumer, Corporate, Home Office} |
| `dim_product` | one product | `product_key` PK, `product_id` UNIQUE | `product_name`, `category` CHECK ∈ 5 categories, `sub_category` |
| `dim_region` | one valid Region→State→City combination | `region_key` PK, `UNIQUE (region, state, city)` | `region` CHECK ∈ {North, South, East, West, Central}, `state`, `city` |

Each dimension also has `first_seen_run_id` (FK → `pipeline_runs`,
`ON DELETE SET NULL`) and `created_at`.

### Relationships

| Child | Column | Parent | On delete |
|-------|--------|--------|-----------|
| `fact_sales` | `date_key` | `dim_date.date_key` | restrict |
| `fact_sales` | `customer_key` | `dim_customer.customer_key` | restrict |
| `fact_sales` | `product_key` | `dim_product.product_key` | restrict |
| `fact_sales` | `region_key` | `dim_region.region_key` | restrict |
| `fact_sales` | `run_id` | `pipeline_runs.run_id` | cascade |
| `dim_*` | `first_seen_run_id` | `pipeline_runs.run_id` | set null |

## 2. Operational / audit tables (Phase 16)

| Table | Grain | Purpose | Key columns |
|-------|-------|---------|-------------|
| `pipeline_runs` | one pipeline execution | run ledger; every run-scoped row points here | `run_id` PK, `file_name`, `file_hash`, `status` CHECK ∈ {RUNNING, SUCCESS, PARTIAL, FAILED, SKIPPED_DUPLICATE}, `started_at`, `finished_at`, `duration_s`, `rows_received/valid/rejected`, `dq_score`, `stage_metrics` JSONB, `error` |
| `file_registry` | one distinct file (by hash) | SHA-256 dedupe | `file_id` PK, `file_hash` UNIQUE, `file_name`, `file_size_bytes`, `row_count`, `first_seen_run_id` |
| `data_quality_results` | one DQ dimension per run | 7 dimensions + an `Overall` row | `dq_id` PK, `run_id` FK, `dimension`, `score`, `passed`, `records_checked/failed`, `detail` JSONB, `UNIQUE (run_id, dimension)` |
| `rejected_records` | one rejected source row | nothing dropped silently (FR-07) | `rejected_id` PK, `run_id` FK, `source_row_number`, `order_id`, `rejection_category`, `rejection_reason`, `dq_dimension`, `raw_record` JSONB |
| `anomalies` | one metric / date / grain | fused detector output + severity | `anomaly_id` PK, `run_id` FK, `metric`, `anomaly_date`, `grain`, `observed/expected_value`, `deviation_pct`, `direction`, `{zscore,iqr,rolling,iforest}_flag`, `detector_votes`, `confidence`, `severity` CHECK ∈ {LOW, MEDIUM, HIGH, CRITICAL}, `persistence_days`, `detail` JSONB |
| `recommendations` | one recommendation | rule-based, prioritised | `recommendation_id` PK, `run_id` FK, `title`, `rationale`, `rule_id`, `linked_anomaly_id` FK → `anomalies` (SET NULL), `severity`, `impact_value`, `confidence`, `priority_score` CHECK 0-100, `priority_band`, `status` |
| `forecast_results` | one metric / horizon / date | exp-smoothing forecast + metrics | `forecast_id` PK, `run_id` FK, `metric`, `horizon_days` CHECK ∈ {7, 30}, `forecast_date`, `forecast_value`, `lower/upper_bound`, `model`, `mae`, `rmse`, `mape`, `UNIQUE (run_id, metric, horizon_days, forecast_date)` |

`data_quality_results`, `rejected_records`, `anomalies`, `recommendations`,
`forecast_results` all FK `run_id → pipeline_runs` with `ON DELETE CASCADE`.

## 3. Lineage

`fact_sales.run_id → pipeline_runs.run_id`, and
`pipeline_runs.file_hash → file_registry.file_hash → data/archive/<file>`.
Given any KPI number: value → SQL query → `fact_sales` rows → `run_id` →
`file_hash` → the immutable archived file (NFR-04).

## 4. Indexing (Phase 17)

The six indexes the specification names, all on `fact_sales`:

| Index | Column |
|-------|--------|
| `idx_fact_sales_order_id` | `order_id` |
| `idx_fact_sales_order_date` | `order_date` |
| `idx_fact_sales_customer_id` | `customer_id` |
| `idx_fact_sales_product_id` | `product_id` |
| `idx_fact_sales_region` | `region` |
| `idx_fact_sales_category` | `category` |

Supporting indexes: the four `fact_sales` FK columns (`date_key`,
`customer_key`, `product_key`, `region_key`) and `run_id`; a composite
`(region, category, order_date)` for the Region→Category→time roll-up used by the
KPI and root-cause engines; and access-path indexes on the operational tables
(`pipeline_runs.status` / `.started_at`, `*_results.run_id`,
`anomalies (metric, anomaly_date)`, `recommendations (priority_score DESC)`,
`forecast_results (metric, forecast_date)`, `rejected_records.rejection_category`).

## 5. Applying the schema

```powershell
# database must be up first - see docs/database-setup.md
python scripts/postgres.py up
python scripts/postgres.py wait --timeout 60

python scripts/apply_schema.py            # create / verify (idempotent)
python scripts/apply_schema.py --dry-run  # print the plan only
python scripts/apply_schema.py --drop     # DEV ONLY: drop then recreate
```

`apply_schema.py` reads connection parameters from `src.config` (i.e. `.env`),
runs `sql/schema.sql` in one transaction, then reports the table and index
counts it can see in `information_schema` / `pg_indexes`.

## 6. Verify

```powershell
pytest -q tests/test_phase08_schema.py                      # static checks, no DB
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase08_schema.py
```

## 7. Related documents

- [`database-setup.md`](database-setup.md) - PostgreSQL provisioning
- [`data-flow.md`](data-flow.md) - which stage writes which table
- [`system-components.md`](system-components.md) - "Storage" component contract
- [`dataset-design.md`](dataset-design.md) - the 22 source fields
- [`security.md`](security.md) §2 - database safety
