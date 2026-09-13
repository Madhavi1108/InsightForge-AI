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

### `src/ingestion.py` (Phase 10 - **delivered**)
- **Responsibility**: validate an incoming file is a readable, non-empty `.csv`
  *before parsing*; collect metadata (name, size, row count, mtime); compute a
  streamed SHA-256; check `file_registry` for a duplicate hash; mint a
  `pipeline_runs` row; **move** the file `data/incoming/ -> data/raw/` and
  **copy** `data/raw/ -> data/archive/`; write the `file_registry` row.
- **API**: `sha256_file`, `collect_metadata -> FileMetadata`,
  `ingest_file(path, db, paths) -> IngestionResult`, `IngestionError`, and the
  `watchdog` watcher `watch(incoming_dir, dispatch, ...)` /
  `wait_until_stable(...)`. Contract in [`ingestion.md`](ingestion.md).
- **Inputs**: a file path (from the watcher or `run_pipeline.py`); a
  `src.database.Database`; a `src.config.PipelinePaths`.
- **Outputs**: `file_registry` row; `pipeline_runs` row (`RUNNING`, later closed
  by the orchestrator); file relocated.
- **Failure behaviour**: duplicate hash -> `SKIPPED_DUPLICATE` run, re-drop moved
  to `data/archive/`, no `file_registry`/warehouse change; unreadable / empty /
  wrong-type file -> `IngestionError` -> run `FAILED` with a sanitised reason.

### `src/orchestrator.py` (Phase 10 - **delivered**)
- **Responsibility**: the sole pipeline sequencer. `main(args)` is called by both
  entry points (`run_pipeline.py` and the watcher) with the same parsed args and
  dispatches `--file` / `--scan` / `--watch` / `--scheduler` (+ `--dry-run`).
  Runs the DB healthcheck, calls `ingest_file`, and - until validation/ETL exist
  (Phase 11+) - closes an ingested run as `PARTIAL` with `stage_metrics.ingest`
  and a `data/processed/<name>.json` summary.
- **Failure behaviour**: DB unavailable -> exit 4 with a sanitised message (no
  traceback); a file that fails ingestion -> exit 5; `--scheduler` -> exit 3
  (APScheduler wiring is Phase 35). Structured `logs/` output is Phase 35.

## Validation

### `config/data_contract.yaml` (Phase 11 - **delivered**)
- **Responsibility**: declarative contract for the 22-field CSV - exact
  `columns` (order enforced) + `allow_extra_columns`, `mandatory` set, per-field
  `type` / `pattern` / `min`-`max` / `values` / `nullable` / length, and the
  `Category->Sub_Category` / `Region->State->City` hierarchies. `uniqueness` is
  declared but scored by the Phase 14 data-quality engine, not this validator.
- **Consumed by**: `src/validation.py`, the Phase 14 data-quality engine
  (`src/data_quality.py`), and the Alteryx data-quality workflow
  (`src/alteryx.py`, Phase 12).

### `src/validation.py` (Phase 11 - **delivered**)
- **Responsibility**: enforce the contract - `get_contract()` +
  `validate_csv` / `validate_frame` -> `ValidationResult`
  (`structural_ok`, `structural_errors`, `row_violations`,
  `rows_valid` / `rows_rejected`, `rejected_rows`). Structural checks
  (missing / extra / reordered columns) reject the file; per-row checks (type,
  pattern, range, category, mandatory presence, hierarchy) flag rows. Pure - no
  DB, no side effects on import. Contract in [`validation.md`](validation.md).
- **Inputs**: a CSV path (or dataframe) + `config/data_contract.yaml`.
- **Outputs**: consumed by the orchestrator's stage 3 - structural failure -> one
  `rejected_records` `'schema'` row + run `FAILED` + exit code 6; row violations
  -> one `rejected_records` row per bad data-row (raw row as JSONB) + run
  `PARTIAL` with `rows_valid` / `rows_rejected`.
- **Failure behaviour**: structural failure -> run `FAILED`; never loads an
  invalid schema. The 7-dimension DQ score and PASS/WARN/REJECT gate are the
  Phase 14 data quality engine (below).

## ETL

### Alteryx workflows + Python fallback (Phase 12-13 - **delivered**)
- **Artifacts**: `alteryx/01_ingestion.yxmd`, `alteryx/02_data_quality.yxmd`
  (Phase 12), `alteryx/03_sales_etl.yxmd`, `alteryx/04_customer_product_etl.yxmd`
  (Phase 13) - hand-authored, valid Alteryx XML, not executed against a real
  engine (none installed in this environment); plus a Python ETL module
  producing **identical contract output** when Alteryx is not installed
  (`ALTERYX_ENGINE_CMD` blank).
- **Responsibility split**:
  - *Phase 12* (`src/alteryx.py`, `src/config.py::AlteryxSettings`):
    re-affirm structural contract compliance and run the per-row data-quality
    check as an Alteryx-workflow-shaped stage 4, reporting engine used
    (`alteryx` / `python_fallback`) and whether it was `verified`. Does not
    touch the star schema.
  - *Phase 13* (`src/etl.py`): clean records, re-derive `Revenue` and
    `Profit` (FR-05 - never trusts the CSV), upsert `dim_customer` /
    `dim_product` / `dim_region` / `dim_date`, bulk-load `fact_sales` inside
    one transaction. Does not set the run's terminal status - that's the
    Phase 14 data quality gate (below).
- **Inputs**: validated daily file (`data/raw/<name>.csv`, post Phase 11).
- **Outputs (Phase 12)**: `stage_metrics.alteryx_ingestion` /
  `stage_metrics.alteryx_dq` on `pipeline_runs`; the same two keys in
  `data/processed/<name>.json`.
- **Outputs (Phase 13)**: populated star schema; `stage_metrics.etl`.
- **Failure behaviour**: engine missing/misconfigured -> Python fallback
  (logged as unverified Alteryx execution, not faked, retried <=3 attempts
  against the real engine first when configured); an unexpected error in the
  Phase 12 workflow stage closes the run `FAILED` (exit code 7); a Phase 13
  load error is retried <=3 times then closes the run `FAILED` (exit code 8).

## Data quality

### `src/data_quality.py` (Phase 14 - **delivered**)
- **Responsibility**: score the file across 7 dimensions - Completeness,
  Validity, Referential Integrity (reusing Phase 11's
  `ValidationResult.row_violations`, grouped by
  `src.validation.DQ_DIMENSION_BY_CATEGORY`), Uniqueness (full-row duplicates
  per the contract's declared `uniqueness: {row: all_columns}`), Consistency
  (`Revenue`/`Profit` re-derivation via `src.etl.recompute_revenue_profit`),
  Accuracy (`Order_Status`<->`Shipping_Days`), and Timeliness (`Order_Date`
  vs. the `sales_YYYY_MM_DD.csv` batch date, skipped for non-standard
  filenames) - then classifies the gate. The master spec names the
  dimensions/thresholds but not the formulas or weighting; this project's
  own operational definitions are documented in
  [`data-quality-engine.md`](data-quality-engine.md).
- **API**: `DIMENSIONS`, `DimensionResult`, `DqReport`, `thresholds()`,
  `score_file(csv_path, vres, contract=None) -> DqReport`. Pure - no
  database access.
- **Inputs**: the raw file (`data/raw/<name>.csv`) + the `ValidationResult`
  Phase 11 already computed for it.
- **Outputs**: one `data_quality_results` row per dimension plus one
  `'Overall'` row (8 total); `pipeline_runs.dq_score` and the run's terminal
  `status` (`>=95` PASS -> `SUCCESS`, `90-94.99` WARNING -> `WARNING`, `<90`
  REJECT -> `FAILED`).
- **Failure behaviour**: REJECT is **audit-only** - it marks the run
  `FAILED` (exit code 9) and never deletes or rolls back `fact_sales`/
  dimension rows Phase 13 already committed (`fact_sales` is insert-only per
  run; a correction is a new run).

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

### `sql/kpi_queries.sql` (Phase 15 - **delivered**)
- **Responsibility**: 10 standalone queries computing Revenue, Profit,
  Margin, Orders, Customers, Units, AOV, Return Rate, Average Discount,
  Shipping Time across the 6 grains FR-08 requires (day/month/region/
  category/product/customer), demonstrating every named advanced-SQL
  technique (JOIN, CASE, CTE, subqueries, window functions, RANK/DENSE_RANK,
  LAG/LEAD, rolling average, running total). Formulas are this project's own
  operational definition - the spec names the metrics/techniques but not the
  formulas (`docs/kpi-engine.md`). Each query is preceded by a
  `-- @query: <name>` marker so it can be run/parsed independently.
- **Inputs**: star schema (`fact_sales` + dims).
- **Outputs**: KPI result sets consumed by intelligence, reporting, BI, and
  Phase 16's views (below), which wrap the same calculations by grain.

### `sql/views.sql` (Phase 16 - **delivered**)
- **Responsibility**: PostgreSQL views `daily_kpis`, `monthly_kpis`,
  `regional_performance`, `category_performance`, `product_performance`,
  `customer_performance` - persistent, queryable, **complete** (unfiltered)
  wrappers around Phase 15's calculations. `CREATE OR REPLACE VIEW`
  throughout (no portable `IF NOT EXISTS` for views). Applied by
  `scripts/apply_schema.py` right after `sql/schema.sql`.
- **Inputs**: star schema.
- **Outputs**: views consumed by intelligence, reporting, BI.

### `src/change_detection.py` (Phase 17 - **delivered**)
- **Responsibility**: period comparison (day/week/month vs previous - the
  two most recent complete periods) and significant-movement detection
  across the 10 core KPIs, queried fresh from `fact_sales` per grain (not
  the Phase 16 views, to keep distinct-count metrics correct at the week
  grain).
- **API**: `compare_period(db, grain)`, `compare_all_periods(db)`,
  `build_change_records(...)` (pure core), `significant_change_threshold_pct()`.
- **Outputs**: `list[ChangeRecord]` (`metric`, `current`, `previous`,
  `pct_change`, `direction`, `magnitude`, `significant`) - **not persisted**
  (no schema table) and **not** wired into `src/orchestrator.py`'s per-file
  pipeline; a downstream/batch analytics module.
- **Failure behaviour**: fewer than 2 periods of data for a grain -> `[]`,
  not an error. A `0 -> non-zero` move is flagged `significant=True` with
  `pct_change=None` (undefined ratio, never silently dropped).

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
