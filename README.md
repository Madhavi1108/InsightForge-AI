# InsightForge AI

**Autonomous business analytics and decision intelligence for retail.**

InsightForge turns incoming sales data into governed, explainable decisions. It
ingests files, validates them against a data contract, loads a PostgreSQL star
schema, computes business KPIs, detects change, anomalies, and drift, identifies
likely drivers, estimates impact, produces forecasts and recommendations, and
delivers the result through dashboards, reports, alerts, and an evidence-grounded
AI Analyst.

The platform is designed around one operating principle:

> **Detect -> Explain -> Quantify -> Predict -> Recommend -> Communicate**

## Why InsightForge

Most analytics workflows stop at a dashboard or a model score. InsightForge
connects the full decision loop:

- **Trusted data:** immutable raw files, SHA-256 fingerprinting, schema validation,
  rejected-record lineage, and seven-dimension data-quality scoring.
- **Decision intelligence:** KPI computation, period comparison, anomaly fusion,
  PSI-based drift detection, root-cause contribution analysis, business impact,
  customer RFM, product intelligence, and forecasting.
- **Actionable outputs:** transparent recommendations with severity, confidence,
  and impact; Excel and PDF reports; severity-routed alerts; Streamlit and Power BI
  experiences.
- **Grounded AI:** the AI Analyst explains verified evidence. Deterministic
  analytics and PostgreSQL remain the source of numerical truth, with a template
  fallback when Gemini is unavailable.
- **Operational control:** one orchestrator, run-scoped audit records, structured
  logs, retries for safe stages, failure recovery, and a controlled natural-language
  to SQL safety boundary.

## End-to-end workflow

```text
Incoming CSV
    -> Detect and fingerprint
    -> Validate against the data contract
    -> Clean and transform
    -> Load PostgreSQL star schema
    -> Compute KPIs and analytical views
    -> Detect changes, anomalies, and drift
    -> Explain drivers and quantify impact
    -> Forecast and recommend
    -> Generate evidence-grounded explanations
    -> Publish dashboards, reports, alerts, and audit records
```

The same orchestration path supports a one-off file, a directory scan, a watched
drop zone, and scheduled execution.

## Architecture at a glance

| Layer | Responsibility | Primary implementation |
| --- | --- | --- |
| Ingestion | Detect files, fingerprint content, deduplicate, archive | `src/ingestion.py` |
| Contract and quality | Validate structure, types, ranges, relationships, and quality | `config/data_contract.yaml`, `src/validation.py`, `src/data_quality.py` |
| ETL and storage | Build dimensions and facts in an auditable star schema | `src/etl.py`, `sql/schema.sql`, PostgreSQL |
| Analytics | KPIs, views, comparisons, anomalies, drift, RCA, impact | `sql/`, `src/change_detection.py`, `src/anomaly_*.py` |
| Decision engines | RFM, product intelligence, forecasts, recommendations | `src/rfm.py`, `src/product_intelligence.py`, `src/forecasting.py`, `src/recommendations.py` |
| AI and safety | Evidence packages, grounded answers, read-only NL-to-SQL | `src/ai_evidence.py`, `src/ai_analyst.py`, `src/nl_to_sql.py` |
| Delivery | Streamlit, Power BI model, PDF/Excel, email alerts | `streamlit_app/`, `dashboard/`, `src/reporting.py`, `src/alerts.py` |
| Operations | Orchestration, scheduling, retries, structured logs, audit trail | `src/orchestrator.py`, `src/scheduler.py`, `src/observability.py` |

## Technology stack

- **Runtime:** Python 3.12, pandas, NumPy, SciPy, scikit-learn
- **Data platform:** PostgreSQL 16, SQLAlchemy, psycopg2
- **Pipeline:** watchdog, APScheduler, Alteryx workflows with a Python fallback
- **Intelligence:** SQL, deterministic Python analytics, Holt trend smoothing,
  Isolation Forest, PSI, Gemini (optional)
- **Delivery:** Streamlit, Power BI / DAX, ReportLab, XlsxWriter, openpyxl
- **Testing:** pytest and pytest-cov
- **Domain:** e-commerce and retail analytics using Indian geography

Docker, Alteryx Designer, Power BI Desktop, and Gemini are optional at different
boundaries. The core Python pipeline remains the execution fallback where those
tools are unavailable.

## Quick start

### Prerequisites

- Python 3.12
- Docker Desktop with Docker Compose
- Power BI Desktop for the Power BI model (optional)
- Alteryx Designer for native workflow execution (optional)

### 1. Create the environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with local PostgreSQL credentials and any optional Gemini or SMTP
settings. Never commit `.env` or credentials.

### 2. Start PostgreSQL and apply the schema

```powershell
docker compose -f config/docker-compose.postgres.yml up -d
python scripts/apply_schema.py
```

The database setup runbook, environment variables, and troubleshooting guidance
are in [`docs/database-setup.md`](docs/database-setup.md).

### 3. Run the pipeline

Place a CSV matching the contract in `data/incoming/`, then choose an entry point:

```powershell
# Process one file
python run_pipeline.py --file data/incoming/sales_2026_09_09.csv

# Process all files currently in the drop zone
python run_pipeline.py --scan

# Watch for new CSV files until interrupted
python run_pipeline.py --watch

# Run the scheduled mode configured in .env
python run_pipeline.py --scheduler
```

The pipeline records each run, stage outcome, duration, data-quality result, and
failure state. Generated reports are written to `reports/`; structured logs are
written to `logs/`.

### 4. Open the operational application

```powershell
streamlit run streamlit_app/Overview.py
```

The Streamlit application includes pipeline control, data quality, anomalies,
root cause, business impact, forecasting, customer and product intelligence, AI
Analyst, and logs pages.

### 5. Run the end-to-end demonstration

```powershell
python scripts/run_final_demo.py
```

This processes the held-out `data/sales_2026_09_09.csv` file and asks the AI
Analyst, "Why did revenue decrease?" The demo requires the database and schema to
be available.

## Verification

Run the full local test suite:

```powershell
pytest -q
```

Tests that require a live PostgreSQL instance are opt-in:

```powershell
$env:INSIGHTFORGE_PG_INTEGRATION = "1"
pytest -q
```

The final acceptance scenarios, security checks, performance budgets, and
integration-test boundaries are documented in
[`docs/final-delivery.md`](docs/final-delivery.md).

## Data contract and lifecycle

The default retail dataset contains 22 fields covering orders, customers,
products, geography, pricing, fulfillment, returns, revenue, and profit. The
contract is defined in [`config/data_contract.yaml`](config/data_contract.yaml).

Incoming files move through an auditable lifecycle:

```text
data/incoming/ -> data/raw/ -> data/processed/
                         \-> data/archive/
                         \-> data/rejected/
```

Raw inputs are preserved. Invalid rows are classified and retained with their
pipeline run ID rather than silently discarded. Generated datasets and runtime
artifacts are excluded from version control.

## Security and trust model

- Secrets are loaded from environment variables and excluded from Git.
- Database statements use bound parameters; errors and logs are sanitized.
- Natural-language SQL is restricted to read-only queries over an explicit table
  and view allowlist, with row limits and statement timeouts.
- The AI Analyst receives verified evidence packages, not unrestricted database
  access or raw untrusted prompts.
- Every important pipeline stage is run-scoped and auditable.
- Deterministic database and Python calculations are authoritative; the LLM is an
  explanation and interface layer only.

See [`docs/security.md`](docs/security.md) and
[`docs/ai-evidence.md`](docs/ai-evidence.md) for the detailed controls.

## Repository layout

```text
src/             Pipeline, analytics, intelligence, AI, reporting, and operations
sql/             PostgreSQL schema, KPI queries, and analytical views
config/          Data contract, Docker Compose, and database bootstrap files
scripts/         Dataset, database, report, and demonstration utilities
streamlit_app/   Streamlit operational application
dashboard/       Power BI Power Query and DAX assets
alteryx/         Alteryx ingestion, quality, and ETL workflows
tests/           Unit, contract, integration-gated, and UI tests
docs/            Architecture, runbooks, requirements, and feature documentation
data/            Local input, archive, processing, rejection, and demo data paths
reports/         Generated Excel and PDF outputs
logs/            Structured pipeline logs
models/          Local model artifacts
```

## Documentation

- [Architecture](docs/architecture.md) - system boundaries, control flow, and failure handling
- [Data flow](docs/data-flow.md) - lifecycle, lineage, and database handoffs
- [Database schema](docs/database-schema.md) - star schema and operational tables
- [Data quality](docs/data-quality-engine.md) - scoring dimensions and quality gates
- [Anomaly and drift detection](docs/anomaly-fusion.md) - detection, fusion, and PSI drift
- [Business impact and root cause](docs/business-impact-engine.md) - explanation methodology
- [Forecasting and recommendations](docs/forecasting.md) - prediction and action engines
- [AI Analyst](docs/ai-analyst.md) - grounding and deterministic fallbacks
- [Streamlit application](docs/streamlit-app.md) - application pages and controls
- [Power BI model](docs/powerbi-data-model.md) - data model, measures, and refresh boundaries
- [Phase status](docs/PHASE_STATUS.md) - authoritative implementation status

## Project status

The consolidated 36-phase implementation is complete. The authoritative phase
matrix and environment-specific verification notes live in
[`docs/PHASE_STATUS.md`](docs/PHASE_STATUS.md). Native Alteryx execution and
Power BI refresh remain dependent on their desktop/gateway environments; Python
fallbacks and documented integration boundaries are provided where appropriate.

## License

Internal portfolio project.