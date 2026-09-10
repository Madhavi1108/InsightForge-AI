# InsightForge AI

**Autonomous Business Analytics & Decision Intelligence Platform**

InsightForge AI automatically converts raw retail business data into validated,
actionable intelligence. Drop a sales file into `data/incoming/` and the platform
detects it, validates it, cleans and transforms it, loads it into PostgreSQL,
recalculates KPIs, detects meaningful changes / anomalies / drift, finds likely
root causes, quantifies business impact, forecasts future performance, generates
evidence-based recommendations, explains the results with an evidence-grounded AI
Analyst, and communicates everything through dashboards, reports, and alerts.

```
DATA -> DETECT -> VALIDATE -> CLEAN -> TRANSFORM -> STORE -> ANALYZE ->
DETECT CHANGE -> DETECT ANOMALY -> DETECT DRIFT -> FIND ROOT CAUSE ->
QUANTIFY IMPACT -> FORECAST -> RECOMMEND -> EXPLAIN WITH AI -> VISUALIZE ->
REPORT -> ALERT -> AUDIT
```

Intelligence philosophy: **DETECT - EXPLAIN - QUANTIFY - PREDICT - RECOMMEND - COMMUNICATE**

---

## Status

Built incrementally against the 69-phase master specification
(`INSIGHTFORGE AI.pdf`), consolidated into **36 build phases** (see
[`docs/PHASE_MAP.md`](docs/PHASE_MAP.md) for the 69 -> 36 crosswalk - no scope is
dropped). Current progress is tracked in
[`docs/PHASE_STATUS.md`](docs/PHASE_STATUS.md).

| Wave | Phases | Theme | State |
|------|--------|-------|-------|
| 0 | 0 | Project initialization | **Complete** |
| 1 | 1-6 | Architecture, requirements, dataset, generation | **Complete** |
| 2 | 7-14 | PostgreSQL, ingestion, validation, Alteryx, data quality | Phases 7-11 complete |
| 3 | 15-17 | SQL / KPIs / views / change detection | Not started |
| 4 | 18-23 | Anomaly / drift / RCA / impact | Not started |
| 5 | 24-26 | RFM / product intel / forecast / recommendations | Not started |
| 6 | 27-35 | AI Analyst / NL-to-SQL / Streamlit / Power BI / reporting / alerts / observability | Not started |
| 7 | 36 | Security / testing / performance / final demo | Not started |

---

## Technology stack

| Layer | Tools |
|-------|-------|
| Data | Python, Pandas, NumPy, Alteryx |
| Database | PostgreSQL, pgAdmin |
| Analytics | SQL, Pandas, SciPy, scikit-learn |
| ML | Z-score, IQR, rolling baseline, Isolation Forest, exponential smoothing |
| BI | Power BI, DAX |
| Application | Streamlit |
| AI | Gemini LLM API, evidence-grounded AI Analyst, NL-to-SQL |
| Reporting | ReportLab, openpyxl |
| Automation | watchdog file watcher, APScheduler |
| Version control | Git, GitHub |

Domain: **E-commerce / Retail** (Indian geography). Optional components
(Docker, Airflow) never block the core system.

---

## Repository layout

```
data/         raw + pipeline working directories (git-ignored contents)
src/          application source (orchestrator, engines, connectors)
sql/          schema, KPI queries, analytical views
alteryx/      Alteryx .yxmd workflows + integration boundary docs
dashboard/    Power BI model and report pages
streamlit/    Streamlit application
reports/      generated PDF / Excel reports (git-ignored contents)
logs/         structured pipeline logs (git-ignored contents)
tests/        pytest suite
config/       data contract, settings, docker-compose for PostgreSQL
scripts/      dataset generation and one-off utilities
docs/         architecture, requirements, security, testing, PHASE_STATUS
models/       trained ML artifacts (git-ignored contents)
```

---

## Quick start

### 1. Prerequisites

- Python 3.12
- Docker (for the PostgreSQL container) — added in Phase 7
- Power BI Desktop (for the BI model) — added in Phase 31
- Alteryx Designer *(optional)* — workflows and a Python ETL fallback are both provided

### 2. Environment

```powershell
# from the project root
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# configure secrets
copy .env.example .env
# then edit .env with real values
```

### 3. Run the pipeline

```powershell
# start PostgreSQL and create the schema (Phases 7-8)
python scripts/postgres.py up
python scripts/apply_schema.py

# ingest files (Phase 10): one file, a sweep, or watch the drop zone
python run_pipeline.py --file data/incoming/sales_2026_08_01.csv
python run_pipeline.py --scan
python run_pipeline.py --watch        # Ctrl+C to stop
```

Ingestion + SHA-256 fingerprinting ([`docs/ingestion.md`](docs/ingestion.md))
and schema/contract validation ([`docs/validation.md`](docs/validation.md)) run
for real today; ETL and analytics arrive in Phase 12+. Scheduled-mode
(`--scheduler`) wiring is added in Phase 35.

---

## Development rules (from the specification)

1. Never fake functionality.
2. Never hard-code analytical results.
3. PostgreSQL and deterministic analytics are the source of truth.
4. The LLM is an explanation / interface layer, not the source of numerical truth.
5. Raw data must remain immutable.
6. Invalid data must be traceable.
7. Every important pipeline stage must be logged.
8. Every important feature must have tests.
9. Never commit credentials.
10. Do not add complexity just to look advanced.
11. Finish P0 features before P1 / P2.
12. Do not proceed to the next phase while the current phase has unresolved critical failures.

---

## License

Internal portfolio project.
