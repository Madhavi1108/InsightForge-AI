# InsightForge AI - Architecture

> Phase 1 deliverable (spec Phases 1-4 consolidated). This document describes the
> whole system at a level where a reader can understand it **without opening
> `src/`**. Component-by-component detail is in
> [`system-components.md`](system-components.md); the data lifecycle is in
> [`data-flow.md`](data-flow.md); the threat model is in [`security.md`](security.md).

## 1. What the system does

InsightForge AI is an **autonomous business analytics and decision-intelligence
platform** for e-commerce / retail. A sales file lands in `data/incoming/` and the
platform runs the full lifecycle without a human executing individual steps:

```
DATA -> DETECT -> VALIDATE -> CLEAN -> TRANSFORM -> STORE -> ANALYZE ->
DETECT CHANGE -> DETECT ANOMALY -> DETECT DRIFT -> FIND ROOT CAUSE ->
QUANTIFY IMPACT -> FORECAST -> RECOMMEND -> EXPLAIN WITH AI -> VISUALIZE ->
REPORT -> ALERT -> AUDIT
```

Intelligence philosophy: **DETECT -> EXPLAIN -> QUANTIFY -> PREDICT -> RECOMMEND
-> COMMUNICATE**.

Target users: Business Manager, Sales Manager, Operations Manager, Finance
Manager, Data Analyst.

## 2. Design principles

1. **Never fake functionality.** Every core feature actually runs.
2. **Deterministic analytics are the source of truth.** PostgreSQL + SQL + Python
   produce every number.
3. **The LLM is an explanation / interface layer only.** It consumes verified
   evidence packages and never invents figures.
4. **Raw data is immutable.** Incoming files are hashed and archived untouched.
5. **Invalid data is traceable, never silently dropped.** Rejected records are
   stored and linked to the pipeline run that rejected them.
6. **Every important stage is logged** with a pipeline run ID.
7. **Every important feature has tests.**
8. **No complexity for its own sake.** Optional components (Docker, Airflow) never
   block the core pipeline.
9. **Finish P0 before P1 before P2** (see [`PHASE_MAP.md`](PHASE_MAP.md)).

## 3. Layered architecture

| Layer | Responsibility | Key components |
|-------|----------------|----------------|
| **Data generation** | Produce a realistic 150k+ row retail dataset with controlled bad data and one known anomaly; split into daily files | `scripts/generate_dataset.py` |
| **Ingestion** | Detect new files, fingerprint them (SHA-256), reject duplicates, register metadata, mint a pipeline run ID | `src/ingestion.py`, `file_registry`, `pipeline_runs` |
| **Validation** | Enforce the data contract: columns, types, ranges, categories, nullability, uniqueness | `config/data_contract.yaml`, `src/validation.py` |
| **ETL** | Clean, transform, derive fields, prepare dimensions, load PostgreSQL | Alteryx `.yxmd` workflows + Python ETL fallback (identical contract) |
| **Storage** | Star schema + operational/audit tables + indexes | PostgreSQL `insightforge` database, `sql/schema.sql` |
| **Analytics** | KPIs, advanced SQL, analytical views, period comparison | `sql/kpi_queries.sql`, `sql/views.sql`, `src/change_detection.py` |
| **Intelligence** | Change / anomaly / drift detection, anomaly fusion, severity, root cause, contribution, business impact, RFM, product intelligence, forecasting, recommendations, priority | `src/anomaly_*.py`, `src/drift_detection.py`, `src/root_cause.py`, `src/impact_analysis.py`, `src/forecasting.py`, `src/recommendations.py` |
| **AI** | Build evidence packages, AI Analyst Q&A, structured explanations, NL-to-SQL with safety | `src/ai_evidence.py`, `src/ai_analyst.py`, `src/nl_to_sql.py` |
| **Presentation** | Streamlit operational app; Power BI executive/intelligence dashboards | `streamlit/`, `dashboard/` |
| **Automation & reporting** | Excel + PDF reports, severity-based email alerts, scheduler | `src/reporting.py`, `src/alerts.py` |
| **Observability** | Structured logs, pipeline IDs, durations, retries, failure & recovery states, audit trail | `src/observability.py`, `logs/`, `pipeline_runs` |

## 4. Control flow

The **orchestrator** (`src/orchestrator.py`, arrives in new Phase 10) is the only
component that sequences the pipeline. Two entry points invoke the *same*
orchestration code:

- **Automatic** - a `watchdog` file watcher fires when a file appears in
  `data/incoming/`.
- **Manual fallback** - `run_pipeline.py --file <path>` / `--scan` / `--scheduler`
  (the controlled manual path described in the spec).

Each run:

1. Creates a `pipeline_runs` row (status `RUNNING`, start time, run ID).
2. Executes stages in order (ingest -> validate -> ETL -> load -> analyze ->
   detect -> RCA -> impact -> forecast -> recommend -> explain -> report ->
   alert).
3. Writes stage outcomes, durations, and record counts back to `pipeline_runs`.
4. Closes the run as `SUCCESS`, `PARTIAL`, or `FAILED`, then writes the audit
   trail.

Stages communicate through **PostgreSQL tables and typed Python objects**, not ad
hoc files. A stage reads its inputs from the database (or the previous stage's
return value) and writes its outputs to the database before the next stage runs.

## 5. Failure paths

| Failure | Detection | Response |
|---------|-----------|----------|
| Duplicate file (same SHA-256) | `file_registry` lookup | Skip processing, mark run `SKIPPED_DUPLICATE`, log, no DB mutation |
| Schema invalid (missing/extra columns, wrong types) | `src/validation.py` | Reject file, write reason to `rejected_records`, run `FAILED`, alert |
| Some rows invalid | Data quality engine | Valid rows proceed; invalid rows -> `rejected_records` with a classification and the run ID |
| Data quality score < 90% | `src/data_quality.py` (thresholds in `.env`) | Reject load, run `FAILED`, alert; 90-94.99% loads with a `WARNING` |
| Database unavailable | Connection layer ret/retry (up to 3, backoff) | After retries, run `FAILED`, state persisted for recovery, alert |
| ETL / analytics stage error | Orchestrator try/except per stage | Retry safe stages up to 3x; otherwise mark stage `FAILED`, continue to reporting where possible, run `PARTIAL` |
| Email send failure | `src/alerts.py` | Log, keep the report artifacts, do not fail the whole run |
| LLM unavailable / no API key | `src/ai_analyst.py` | Fall back to a deterministic template explanation built from the evidence package |
| NL-to-SQL unsafe query | `src/nl_to_sql.py` validator | Block, return a safe error, never execute |

Recovery: a `FAILED` or `PARTIAL` run leaves enough state in `pipeline_runs` and
`rejected_records` to re-run the file after the underlying issue is fixed. Raw
data is always still in `data/archive/`.

## 6. Dependencies

- **Runtime**: Python 3.12, PostgreSQL 16 (Docker container, new Phase 7).
- **Python**: pandas, numpy, scipy, scikit-learn, SQLAlchemy + psycopg2,
  watchdog, APScheduler, ReportLab, openpyxl, streamlit, google-generativeai,
  pyyaml, python-dotenv (see `requirements.txt`).
- **External tools**: Alteryx Designer (optional - Python ETL fallback provides an
  identical contract), Power BI Desktop (dataset refresh is a documented manual /
  gateway step).
- **Configuration**: all secrets and tunables via `.env` (template in
  `.env.example`); no credentials in source or Git.

## 7. Related documents

- [`system-components.md`](system-components.md) - per-component contracts
- [`data-flow.md`](data-flow.md) - end-to-end data lifecycle and DB tables
- [`database-setup.md`](database-setup.md) - PostgreSQL provisioning (Docker) & `.env` wiring
- [`database-schema.md`](database-schema.md) - star schema, operational tables, indexes & lineage
- [`database-layer.md`](database-layer.md) - `src/database.py` API, pooling, retry policy & error taxonomy
- [`security.md`](security.md) - secrets, SQL safety, AI safety, auditability
- [`requirements.md`](requirements.md) - functional & non-functional requirements
- [`business-questions.md`](business-questions.md) - questions the system answers
- [`dataset-design.md`](dataset-design.md) - the 22-field retail dataset
- [`PHASE_MAP.md`](PHASE_MAP.md) - 69 -> 36 phase consolidation
- [`PHASE_STATUS.md`](PHASE_STATUS.md) - live build progress
