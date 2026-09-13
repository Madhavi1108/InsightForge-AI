# InsightForge AI - Streamlit Application & Pipeline Control

> Phase 30 deliverable (spec Phases 58-59, FR-22). `streamlit_app/` is the
> first Presentation-layer phase: all 11 spec-named pages, plus controlled
> pipeline actions.

## 1. What the spec says

`docs/system-components.md`: "Pages: Overview, Pipeline, Data Quality,
Anomalies, Root Cause, Business Impact, Forecast, Customers, Products, AI
Analyst, Logs." "Pipeline control: status, last run, duration, records,
quality, anomalies, failures; controlled actions (run pipeline, inspect
run, inspect errors) - no arbitrary execution." `docs/requirements.md`
FR-22: "Streamlit app with all 11 pages and controlled pipeline actions."

## 2. Directory naming - `streamlit_app/`, not `streamlit/`

The spec names the directory `streamlit/`. This project's `pytest.ini`
sets `pythonpath = .` (project root on `sys.path` for every test run - the
same mechanism that makes `from src.x import y` work everywhere else). A
project-root directory literally named `streamlit/` would collide with
the **installed** `streamlit` package: `import streamlit` and `from
streamlit.common import ...` would resolve unpredictably (or fail
outright) once the real `streamlit` package - a regular package, found
later on `sys.path` - wins name resolution over a same-named local
directory. `streamlit_app/` sidesteps this entirely; a deliberate,
necessary deviation from the literal directory name, functionally
identical to the spec's intent.

## 3. How to run

```powershell
streamlit run streamlit_app/Overview.py
```

Needs `.env` configured and PostgreSQL up (`docs/database-setup.md`).
Every page/`common.py` opens with a small `sys.path` bootstrap so the app
works whether launched this way (Streamlit only puts the script's own
directory on `sys.path`, not the project root) or under pytest (where
`pythonpath = .` already covers it).

## 4. Pages

| # | Page | Reuses |
|---|---|---|
| 1 | `Overview.py` | `src.change_detection.compare_period`, `src.recommendations.generate_recommendations`, `daily_kpis` view |
| 2 | `pages/1_Pipeline.py` | `pipeline_runs`/`rejected_records` tables, controlled `run_pipeline.py` subprocess |
| 3 | `pages/2_Data_Quality.py` | `data_quality_results`/`drift_results` tables |
| 4 | `pages/3_Anomalies.py` | `anomalies` table (Phase 20 fusion output) |
| 5 | `pages/4_Root_Cause.py` | `src.root_cause.analyze_root_cause` |
| 6 | `pages/5_Business_Impact.py` | `src.impact_analysis.assess_business_impact_series` |
| 7 | `pages/6_Forecast.py` | `src.forecasting.forecast_metric` |
| 8 | `pages/7_Customers.py` | `src.rfm.analyze_customer_rfm` |
| 9 | `pages/8_Products.py` | `src.product_intelligence.analyze_product_intelligence` |
| 10 | `pages/9_AI_Analyst.py` | `src.ai_analyst.answer_question` (6 quick questions), `src.nl_to_sql.ask` (ad-hoc tab) |
| 11 | `pages/10_Logs.py` | `pipeline_runs` full history + `data/processed/*.json` summaries |

Streamlit auto-orders `pages/` by filename; the `N_Name.py` prefixes
reproduce the spec's own page order, with `Overview.py` as the entry
script (nav item 1 by construction).

`pages/4_Root_Cause.py` and `pages/6_Forecast.py` call an analytics
function on user input inside `st.spinner` + `try/except`, rendering
`st.warning(str(exc))` instead of crashing - e.g. `analyze_root_cause`
raises `ValueError` for an unsupported metric, already a clean message,
never a raw stack trace (`docs/security.md`'s sanitized-error rule applied
at the UI layer for the first time).

## 5. Controlled actions - no arbitrary execution

`streamlit_app/common.py`'s `run_pipeline_subprocess(args)` is the **only**
code-execution path in the entire app: a fixed argv list
(`[sys.executable, "run_pipeline.py", *args]`), never `shell=True`, never a
user-typed command string. The Pipeline page offers exactly two modes -
"scan `data/incoming/`" (`--scan`) or "run one file", where the file is
chosen from a `st.selectbox` populated by `incoming_files()` (a real
directory listing of `data/incoming/*.csv`) - never a free-text path, so
"run this file" can never become an arbitrary path or command. "Inspect
run" and "inspect errors" are read-only queries against `pipeline_runs`/
`rejected_records`.

## 6. `require_database()` - the safe-failure guard

Every page's first line. `Database.ping()` (already "never raises... a
boolean probe", Phase 9) gates the page; a failure renders one honest
`st.error` ("The database is unavailable right now...") and `st.stop()`s -
matching `docs/security.md` section 5's sanitized-error convention rather
than leaking a driver traceback into the browser.

## 7. Testing approach

Streamlit 1.40.1 (already pinned) ships `streamlit.testing.v1.AppTest`,
which runs a page script headlessly (in-process) and inspects its
rendered element tree - no browser needed. Every page gets:

- an unconditional check that it never raises an unhandled exception;
- (no live Postgres) a check that it renders the `require_database()`
  failure message - proving the failure path actually works, not just
  that a crash is silently swallowed;
- an `INSIGHTFORGE_PG_INTEGRATION=1`-gated check that it renders real
  content with no exception and no `st.error`, against live data.

Full browser-driven interaction testing (clicking buttons, filling forms
visually) is a manual follow-up outside this phase's automated suite -
this dev environment has no browser session wired to a running Streamlit
server; `AppTest` covers render-correctness and safe-failure, which is
what this phase's automated tests assert.

## 8. Logs page scope

Structured, file-based logging (`docs/security.md` section 5: "Structured
logs in `logs/`") is a Phase 35 concern, not yet built. The Logs page
therefore surfaces what already exists: the full `pipeline_runs` audit
trail (status, duration, `stage_metrics`, sanitized `error`) plus each
run's `data/processed/<file>.json` summary (Phase 10) - an honest scope
boundary, not a placeholder.

## 9. Related documents

- [`security.md`](security.md) - sanitized-error convention, section 5
- [`database-setup.md`](database-setup.md) - PostgreSQL setup this app needs
- [`recommendations.md`](recommendations.md), [`root-cause-analysis.md`](root-cause-analysis.md),
  [`business-impact-engine.md`](business-impact-engine.md), [`forecasting.md`](forecasting.md),
  [`customer-rfm-analysis.md`](customer-rfm-analysis.md), [`product-intelligence.md`](product-intelligence.md),
  [`ai-analyst.md`](ai-analyst.md), [`nl-to-sql.md`](nl-to-sql.md) - every module a page calls

## 10. Verify

```powershell
pytest -q tests/test_phase30_streamlit_app.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase30_streamlit_app.py
streamlit run streamlit_app/Overview.py
```
