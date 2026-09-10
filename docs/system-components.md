# InsightForge AI - System Components

> Phase 1 deliverable. One entry per component: **responsibility, inputs,
> outputs, dependencies, failure behaviour**. Names in `code font` are the planned
> module / artifact paths; they arrive in the phase noted in
> [`PHASE_MAP.md`](PHASE_MAP.md).

## Data generation

### `scripts/generate_dataset.py` (new Phase 2-6)
- **Responsibility**: generate >=150,000 realistic retail order rows with fixed
  random seed; realistic pricing, customers, products, Indian geography,
  seasonality, returns, shipping; inject controlled bad data and one documented
  business anomaly (West -> Electronics -> Laptop: orders down, revenue down,
  returns up, shipping days up, discount up); split into daily
  `sales_YYYY_MM_DD.csv` files.
- **Inputs**: `RANDOM_SEED` and row count (config / CLI args).
- **Outputs**: daily CSVs in `data/incoming/`; `docs/anomaly-ground-truth.md`
  (period, segment, expected deltas) for validating anomaly detection & RCA.
- **Dependencies**: pandas, numpy.
- **Failure behaviour**: deterministic; regenerates identically from the seed.

## Ingestion

### `src/ingestion.py` (new Phase 10)
- **Responsibility**: watch `data/incoming/`, validate the file is readable,
  collect metadata (name, size, row count, mtime), compute SHA-256, check
  `file_registry` for a duplicate hash, mint a pipeline run ID, move the raw file
  to `data/raw/` and copy to `data/archive/`.
- **Inputs**: a file path (from the watcher or `run_pipeline.py`).
- **Outputs**: `file_registry` row (name, `file_hash`, size, rows, first-seen
  run ID); `pipeline_runs` row (`RUNNING`); file relocated.
- **Dependencies**: watchdog, hashlib, DB connection layer.
- **Failure behaviour**: duplicate hash -> run `SKIPPED_DUPLICATE`, no mutation;
  unreadable / empty file -> run `FAILED`, reason recorded, alert.

## Validation

### `config/data_contract.yaml` (new Phase 11)
- **Responsibility**: declarative contract - required columns, types, numeric
  ranges, accepted categories, nullability, uniqueness keys.
- **Consumed by**: `src/validation.py`, the data quality engine, and the Alteryx
  data-quality workflow.

### `src/validation.py` (new Phase 11)
- **Responsibility**: enforce the contract - missing columns, extra columns, wrong
  types, out-of-range values, missing mandatory fields.
- **Inputs**: raw dataframe + `data_contract.yaml`.
- **Outputs**: pass/fail; on structural failure the file is rejected; on row-level
  failures the offending rows are handed to rejected-record management.
- **Failure behaviour**: structural failure -> run `FAILED`, alert; never loads an
  invalid schema.

## ETL

### Alteryx workflows + Python fallback (new Phase 12-13)
- **Artifacts**: `alteryx/01_ingestion.yxmd`, `alteryx/02_data_quality.yxmd`,
  `alteryx/03_sales_etl.yxmd`, `alteryx/04_customer_product_etl.yxmd`; plus a
  Python ETL module producing **identical contract output** when Alteryx is not
  installed (`ALTERYX_ENGINE_CMD` blank).
- **Responsibility**: clean records, standardise text, derive `Revenue` and
  `Profit`, join/prepare `dim_customer` / `dim_product` / `dim_region` / `dim_date`,
  load `fact_sales` and dimensions into PostgreSQL.
- **Inputs**: validated daily file.
- **Outputs**: populated star schema; ETL stage metrics in `pipeline_runs`.
- **Failure behaviour**: engine missing -> Python fallback (logged as unverified
  Alteryx execution, not faked); load error -> retry (<=3) then stage `FAILED`.

## Storage

### PostgreSQL `insightforge` (new Phase 7-9)
- **Provisioning (Phase 7)**: `config/docker-compose.postgres.yml` (PostgreSQL 16
  container, credentials from `.env`); runtime config assembled by
  `src/config.py`; lifecycle helper `scripts/postgres.py`; runbook
  [`database-setup.md`](database-setup.md).
- **Schema (Phase 8)**: `sql/schema.sql` (idempotent DDL), applied by
  `scripts/apply_schema.py`; every table, relationship and index is documented in
  [`database-schema.md`](database-schema.md). `sql/drop_schema.sql` resets it.
- **Star schema**: `fact_sales` (grain = one order line), `dim_date`,
  `dim_customer`, `dim_product`, `dim_region`.
- **Operational / audit tables**: `pipeline_runs`, `file_registry`,
  `data_quality_results`, `rejected_records`, `anomalies`, `recommendations`,
  `forecast_results` - all run-scoped rows carry `run_id` -> `pipeline_runs`.
- **Indexes**: the six spec-named `fact_sales` indexes (`order_id`, `order_date`,
  `customer_id`, `product_id`, `region`, `category`) plus FK, composite
  `(region, category, order_date)`, and operational-table indexes.
- **`src/database.py`** (Phase 9 - **delivered**): pooled SQLAlchemy `Engine`
  (`get_engine()` / `dispose_engine()`, lazy - import opens no connection) and a
  `Database` facade: `fetch_all` / `fetch_one` / `scalar` (reads),
  `execute` / `execute_many` / `insert_returning` (writes, each in its own
  transaction), `transaction()` context manager, `ping()` / `healthcheck()`.
  Every statement goes through `sqlalchemy.text()` with bound parameters. Full
  contract in [`database-layer.md`](database-layer.md). Every other component
  reaches the database only through this module (the sole exception is
  `scripts/apply_schema.py`, raw psycopg2, DDL only).
- **Failure behaviour**: transient connection failures (`OperationalError`,
  `InterfaceError`, invalidated connection) retry up to `DB_MAX_RETRIES` (3) with
  exponential backoff, then raise `DatabaseConnectionError`; non-transient errors
  raise `QueryExecutionError` immediately. Both are sanitised (no SQL, params,
  DSN, host, or stack trace) and recorded by the orchestrator as run `FAILED`.

## Analytics

### `sql/kpi_queries.sql` + `sql/views.sql` (new Phase 15-16)
- **Responsibility**: Revenue, Profit, Margin, Orders, Customers, Units, AOV,
  Return Rate, Average Discount, Shipping Time; advanced SQL (JOIN, CASE, CTE,
  subqueries, window functions, RANK/DENSE_RANK, LAG/LEAD, rolling average,
  running total); views `daily_kpis`, `monthly_kpis`, `regional_performance`,
  `category_performance`, `product_performance`, `customer_performance`.
- **Inputs**: star schema.
- **Outputs**: KPI result sets consumed by intelligence, reporting, BI.

### `src/change_detection.py` (new Phase 17)
- **Responsibility**: period comparison (day/week/month vs previous) and
  significant-movement detection with percentage deltas.
- **Outputs**: change records (metric, current, previous, % change, direction).

## Intelligence

### Anomaly detection (new Phase 18-20)
- **Z-score**, **IQR** (Q1/Q3/IQR/bounds), **rolling baseline** (mean/median/std
  vs expected range), **Isolation Forest** (`sklearn`, anomaly score +
  prediction + confidence).
- **Anomaly fusion**: combine the four detectors into one unified result per
  metric/date; persist to `anomalies`.
- **Severity engine**: classify LOW / MEDIUM / HIGH / CRITICAL from percentage
  deviation, business impact, confidence, persistence.

### `src/drift_detection.py` (new Phase 21)
- **Responsibility**: monitor distributions (price, quantity, discount, shipping,
  category mix, region mix); classify Normal / Warning / Drift Detected; store
  history.

### `src/root_cause.py` (new Phase 22)
- **Responsibility**: hierarchical drill-down Region -> Category -> Subcategory ->
  Product -> Customer Segment; contribution analysis (each dimension's share of a
  KPI change); every result carries metric, change, primary driver, evidence,
  contribution, confidence.

### `src/impact_analysis.py` (new Phase 23)
- **Responsibility**: expected vs actual revenue & profit, revenue/profit gap,
  revenue/profit at risk, customers affected, orders affected.

### Customer & product intelligence (new Phase 24)
- **RFM**: Recency / Frequency / Monetary -> Champions, Loyal, Potential
  Loyalists, New, At Risk, Lost.
- **Product intelligence**: classify Star / High Profit / High Revenue / Fast
  Growing / Declining / High Return / Low Margin / Slow Moving; product health
  score.

### `src/forecasting.py` (new Phase 25)
- **Responsibility**: exponential smoothing forecasts for revenue, profit, orders
  at 7 and 30 days; evaluation with MAE / RMSE / MAPE; documented limitations.
- **Outputs**: `forecast_results`.

### `src/recommendations.py` + priority engine (new Phase 26)
- **Responsibility**: transparent rule-based recommendations (e.g. revenue down +
  orders down + price stable -> investigate demand / inventory); priority =
  Severity x Impact x Confidence, normalised 0-100, classified LOW/MEDIUM/HIGH/
  CRITICAL.
- **Outputs**: `recommendations`.

## AI

### `src/ai_evidence.py` (new Phase 27)
- **Responsibility**: assemble a structured, verified evidence package (metric,
  current, previous, change %, primary region, primary category, impact, ...)
  before any LLM call.

### `src/ai_analyst.py` + explanation engine (new Phase 28)
- **Responsibility**: answer "Why did revenue decrease?", "Which region performed
  worst?", "Which products need attention?", "What caused profit to decline?",
  "What are the biggest risks?", "Summarize this month." Output is structured:
  Summary / Evidence / Root Cause / Impact / Recommendation / Confidence.
- **Failure behaviour**: no API key or LLM error -> deterministic template
  explanation from the evidence package. **Never invents numbers.**

### `src/nl_to_sql.py` + AI security (new Phase 29)
- **Responsibility**: Question -> Intent -> SQL -> Validation -> PostgreSQL ->
  Result -> Explanation. Read-only. Blocks DROP/DELETE/UPDATE/ALTER/TRUNCATE/
  INSERT; table whitelist; statement timeout; row limit; prompt-injection
  protection; error sanitization.

## Presentation

### Streamlit app (`streamlit/`, new Phase 30)
- **Pages**: Overview, Pipeline, Data Quality, Anomalies, Root Cause, Business
  Impact, Forecast, Customers, Products, AI Analyst, Logs.
- **Pipeline control**: status, last run, duration, records, quality, anomalies,
  failures; controlled actions (run pipeline, inspect run, inspect errors) - no
  arbitrary execution.

### Power BI (`dashboard/`, new Phase 31-32)
- **Data model**: connect to PostgreSQL / analytical outputs; relationships,
  measures, calculated metrics, date hierarchy, DAX.
- **Pages**: Executive Overview; Business Drivers; Customer Intelligence; Product
  Intelligence; Risk & Anomalies; Forecast; Pipeline Health.
- **Refresh**: documented manual / gateway step.

## Automation & reporting

### `src/reporting.py` (new Phase 33)
- **Responsibility**: `InsightForge_Report_DATE.xlsx` (Executive Summary, KPIs,
  Regions, Categories, Products, Customers, Anomalies, Root Causes, Impact,
  Recommendations, Forecast, Data Quality) and
  `InsightForge_Executive_Report_DATE.pdf` (management-friendly summary).
- **Outputs**: files in `reports/`.

### `src/alerts.py` (new Phase 34)
- **Responsibility**: severity-routed alerts - LOW -> dashboard, MEDIUM ->
  Streamlit, HIGH -> email, CRITICAL -> immediate email. Emails carry issue,
  evidence, impact, recommendation; attach PDF/Excel where appropriate.
- **Failure behaviour**: SMTP failure is logged and does not fail the run.

### Scheduler (new Phase 35)
- **Responsibility**: APScheduler cron loop for scheduled-mode runs
  (`run_pipeline.py --scheduler`); `SCHEDULER_ENABLED` / `SCHEDULER_CRON` in
  `.env`.

## Observability

### `src/observability.py` (new Phase 35)
- **Responsibility**: structured logging to `logs/`, pipeline run IDs, execution
  durations, retry counts, failure states, sanitized error messages, recovery
  states, audit trail. Retries safe failed components up to three times.
