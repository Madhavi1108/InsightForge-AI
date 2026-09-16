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
| `status` | `RUNNING` -> `SUCCESS` / `WARNING` / `PARTIAL` / `FAILED` / `SKIPPED_DUPLICATE` |
| `started_at`, `finished_at`, `duration_s` | timing |
| `rows_received`, `rows_valid`, `rows_rejected` | counts |
| `dq_score` | data quality score (%) |
| `stage_metrics` | per-stage duration / outcome (JSON) |
| `error` | sanitized failure message, if any |

Every insight row (`anomalies`, `recommendations`, `forecast_results`,
`rejected_records`, `data_quality_results`) carries the `run_id` -> full lineage
from insight back to source file.

## 3. Stage-by-stage flow

Stages 1-2 are implemented in `src/ingestion.py` (Phase 10; see
[`ingestion.md`](ingestion.md)); stage 3 in `src/validation.py` (Phase 11; see
[`validation.md`](validation.md)); stage 4 in `src/alteryx.py` (Phase 12; see
[`alteryx-workflows.md`](alteryx-workflows.md)); stage 5 in `src/etl.py`
(Phase 13; see [`star-schema-etl.md`](star-schema-etl.md)); stage 6 in
`src/data_quality.py` (Phase 14; see
[`data-quality-engine.md`](data-quality-engine.md)).

| # | Stage | Reads | Writes | Notes |
|--:|-------|-------|--------|-------|
| 1 | **Detect** | `data/incoming/` | - | watchdog event or `--scan` |
| 2 | **Fingerprint & register** | file bytes, `file_registry` | `file_registry`, `pipeline_runs` (RUNNING), moves file to `raw/` + `archive/` | SHA-256; duplicate hash -> `SKIPPED_DUPLICATE`, re-drop moved to `archive/`, stop |
| 3 | **Schema validation** | `raw/` file, `config/data_contract.yaml` | `rejected_records` (structural + per-row), `pipeline_runs` (`rows_valid` / `rows_rejected`, `stage_metrics.validate`) | missing/extra/reordered columns -> reject file, `FAILED`, exit 6; bad rows -> one `rejected_records` row each, valid rows proceed. DQ *scoring* is stage 6 (Phase 14). |
| 4 | **Alteryx ingestion & DQ workflows** | `raw/` file | `pipeline_runs.stage_metrics.alteryx_ingestion` / `.alteryx_dq`, `data/processed/<name>.json` | Phase 12 - `.yxmd` workflow when Alteryx is configured (retried <=3), else Python fallback (`verified=False`, never faked); unexpected failure -> `FAILED`, exit 7 |
| 5 | **Alteryx / Python ETL load** | contract-valid rows | `fact_sales`, `dim_date`, `dim_customer`, `dim_product`, `dim_region`, `pipeline_runs.stage_metrics.etl` | Phase 13 - re-derives `Revenue`/`Profit` (FR-05); upserts dimensions, bulk-loads `fact_sales` in one transaction; load error -> retry <=3 then `FAILED`, exit 8. Does **not** set the terminal status - stage 6 does. |
| 6 | **Data quality engine & gate** | raw file + the stage-3 `ValidationResult` | `data_quality_results` (7 dimensions + `Overall`), `pipeline_runs.dq_score` / `.status` / `stage_metrics.dq` | Phase 14 - completeness, validity, uniqueness, consistency, accuracy, timeliness, referential integrity; overall = unweighted mean; `>=95` PASS -> `SUCCESS`, `90-94.99` WARNING -> `WARNING` (run still completed), `<90` REJECT -> `FAILED` + exit 9. **REJECT is audit-only** - it never deletes or rolls back the `fact_sales` rows stage 5 already loaded (insert-only per run; a correction is a new run). |
| 7 | **SQL KPI engine** | star schema | KPI result sets (in-memory) + analytical views | Revenue, Profit, Margin, Orders, Customers, Units, AOV, Return Rate, Avg Discount, Shipping Time |
| 8 | **Change detection** | views / KPI history | change records | day/week/month vs previous, % deltas |
| 9 | **Anomaly detection & fusion** | KPI time series | `anomalies` | Z-score + IQR + rolling baseline + Isolation Forest (Phases 18-19, pure, not persisted) -> fused result -> severity (Phase 20, `src/anomaly_fusion.py`, only for the just-ingested file's dates). This write never changes `pipeline_runs.status` - advisory analytics, not a gate. |
| 10 | **Drift detection** | `fact_sales` current vs baseline windows | `drift_results` | Phase 21, `src/drift_detection.py` - PSI per price/quantity/discount/shipping/category mix/region mix; wired into the orchestrator (stage 8) like anomaly fusion, never changes `pipeline_runs.status` |
| 11 | **Root cause & contribution** | `fact_sales` + dims | RCA results (metric, driver, evidence, contribution, confidence) - no dedicated table (see `docs/root-cause-analysis.md`) | Region -> Category -> Subcategory -> Product -> Segment; still a pure on-demand function, but no longer *disconnected* - stage 14 (`src/recommendations.py`) calls `analyze_root_cause` live as part of every file's run |
| 12 | **Business impact** | RCA + KPIs | impact figures - no dedicated table (see `docs/business-impact-engine.md`) | expected vs actual revenue/profit, gaps, at-risk, customers/orders affected; same as stage 11 - invoked live by stage 14, not persisted separately |
| 13 | **Forecast** | KPI history | `forecast_results`, `pipeline_runs.stage_metrics.forecast` | exponential smoothing, 7 & 30 days; MAE/RMSE/MAPE. Wired into the orchestrator (stage 9, `src/forecasting.py::generate_and_persist_forecast`) - advisory, never changes `pipeline_runs.status` |
| 14 | **Recommendations & priority** | change + RCA + impact + forecast | `recommendations`, `pipeline_runs.stage_metrics.recommendations` | transparent rules; priority = severity x impact x confidence (0-100). Wired into the orchestrator (stage 10, `src/recommendations.py::generate_and_persist_recommendations`) - internally calls stages 11/12/13 live to enrich each recommendation's rationale/priority; advisory, never changes `pipeline_runs.status` |
| 15 | **AI evidence + explanation** | `anomalies`, `recommendations`, RCA, impact | evidence package (in-memory only, consumed live by the AI Analyst) + `pipeline_runs.stage_metrics.ai_evidence` (count/significant, not the packages themselves) | LLM consumes verified evidence only; template fallback if no LLM. Wired into the orchestrator (stage 11, `src/ai_evidence.py::assemble_all_evidence`) - advisory, never changes `pipeline_runs.status` |
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
  transactions, retries) - the `Database` facade, contract in
  [`database-layer.md`](database-layer.md).
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
