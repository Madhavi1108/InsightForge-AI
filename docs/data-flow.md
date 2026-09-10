# InsightForge AI - Data Flow

> Phase 1 deliverable. How data moves from a raw file to an audited insight, which
> directories and PostgreSQL tables are touched at each stage, and the rules that
> keep raw data immutable and invalid data traceable.

## 1. Directory lifecycle

```
data/
  incoming/    drop zone - the file watcher polls here
  raw/         the file, moved here the moment ingestion accepts it
  processed/   marker/summary for files that completed the pipeline
  rejected/    files (or extracts) that failed structural validation
  archive/     immutable copy of every raw file, kept forever
```

Rules:

- A file is **moved** `incoming -> raw` before any parsing, and **copied**
  `raw -> archive` in the same step.
- The archived copy is **never modified or deleted**. Re-processing always reads a
  fresh copy from `archive/`.
- A file that fails structural validation goes to `rejected/`; a file whose rows
  are partially bad still completes, with the bad rows captured in the
  `rejected_records` table (not on disk).
- On success a small summary is written to `processed/` (run ID, counts, quality
  score); the heavy results live in PostgreSQL.

## 2. Pipeline run identity

Every execution (watcher-triggered or `run_pipeline.py`) creates one
`pipeline_runs` row **before** stage 1:

| Column | Meaning |
|--------|---------|
| `run_id` | primary key, referenced by every downstream row |
| `file_name`, `file_hash` | the file being processed |
| `status` | `RUNNING` -> `SUCCESS` / `PARTIAL` / `FAILED` / `SKIPPED_DUPLICATE` |
| `started_at`, `finished_at`, `duration_s` | timing |
| `rows_received`, `rows_valid`, `rows_rejected` | counts |
| `dq_score` | data quality score (%) |
| `stage_metrics` | per-stage duration / outcome (JSON) |
| `error` | sanitized failure message, if any |

Every insight row (`anomalies`, `recommendations`, `forecast_results`,
`rejected_records`, `data_quality_results`) carries the `run_id` -> full lineage
from insight back to source file.

## 3. Stage-by-stage flow

| # | Stage | Reads | Writes | Notes |
|--:|-------|-------|--------|-------|
| 1 | **Detect** | `data/incoming/` | - | watchdog event or `--scan` |
| 2 | **Fingerprint & register** | file bytes, `file_registry` | `file_registry`, `pipeline_runs` (RUNNING), moves file to `raw/` + `archive/` | SHA-256; duplicate hash -> `SKIPPED_DUPLICATE`, stop |
| 3 | **Schema validation** | `raw/` file, `config/data_contract.yaml` | `rejected_records` (structural), `pipeline_runs` | missing/extra columns, wrong types, missing mandatory fields -> reject file, `FAILED` |
| 4 | **Alteryx / Python ETL** | validated rows | `fact_sales`, `dim_date`, `dim_customer`, `dim_product`, `dim_region` | derives `Revenue`, `Profit`; prepares dimensions |
| 5 | **Data quality engine** | loaded rows / staging | `data_quality_results`, `rejected_records` (row-level), `pipeline_runs.dq_score` | completeness, validity, uniqueness, consistency, accuracy, timeliness, referential integrity |
| 6 | **Quality gate** | `dq_score` | `pipeline_runs.status` | `>=95` PASS, `90-94.99` WARNING (loads), `<90` REJECT (`FAILED`, alert) |
| 7 | **SQL KPI engine** | star schema | KPI result sets (in-memory) + analytical views | Revenue, Profit, Margin, Orders, Customers, Units, AOV, Return Rate, Avg Discount, Shipping Time |
| 8 | **Change detection** | views / KPI history | change records | day/week/month vs previous, % deltas |
| 9 | **Anomaly detection & fusion** | KPI time series | `anomalies` | Z-score + IQR + rolling baseline + Isolation Forest -> fused result -> severity |
| 10 | **Drift detection** | current vs baseline distributions | drift history | price/quantity/discount/shipping/category mix/region mix |
| 11 | **Root cause & contribution** | `fact_sales` + dims | RCA results (metric, driver, evidence, contribution, confidence) | Region -> Category -> Subcategory -> Product -> Segment |
| 12 | **Business impact** | RCA + KPIs | impact figures | expected vs actual revenue/profit, gaps, at-risk, customers/orders affected |
| 13 | **Forecast** | KPI history | `forecast_results` | exponential smoothing, 7 & 30 days; MAE/RMSE/MAPE |
| 14 | **Recommendations & priority** | change + RCA + impact + forecast | `recommendations` | transparent rules; priority = severity x impact x confidence (0-100) |
| 15 | **AI evidence + explanation** | `anomalies`, `recommendations`, RCA, impact | evidence package -> AI Analyst text | LLM consumes verified evidence only; template fallback if no LLM |
| 16 | **Visualize** | PostgreSQL + views | Streamlit pages, Power BI dataset | Power BI refresh is a documented manual/gateway step |
| 17 | **Report** | all of the above | `reports/InsightForge_Report_DATE.xlsx`, `reports/InsightForge_Executive_Report_DATE.pdf` | - |
| 18 | **Alert** | severity | email / Streamlit / dashboard | LOW->dashboard, MEDIUM->Streamlit, HIGH->email, CRITICAL->immediate email; SMTP failure is non-fatal |
| 19 | **Audit** | `pipeline_runs`, all run-scoped rows | `pipeline_runs` (final status, duration), `processed/` summary | closes the run |

## 4. Communication between stages

- Stages exchange data through **PostgreSQL tables** and **typed Python return
  values** from the orchestrator - never through undocumented temp files.
- The orchestrator (`src/orchestrator.py`, new Phase 10) owns sequencing; a stage
  never calls the next stage directly.
- All database access goes through `src/database.py` (parameterised queries,
  transactions, retries).
- The tables above are defined in `sql/schema.sql` (Phase 8); see
  [`database-schema.md`](database-schema.md) for columns, relationships and
  indexes.

## 5. Immutability & traceability guarantees

1. Raw files in `data/archive/` are write-once.
2. `fact_sales` rows are insert-only per run; corrections are new runs, not
   in-place edits.
3. No invalid record is ever discarded silently - it lands in `rejected_records`
   with a category and the `run_id`.
4. Given any number on a dashboard or report, you can trace: value -> query ->
   `fact_sales` rows -> `run_id` -> `file_hash` -> archived file.
