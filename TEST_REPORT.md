# INSIGHTFORGE AI
# REAL GOOGLE CHROME E2E TEST REPORT

## Environment

OS: Windows 11
Google Chrome: real, installed (`C:\Program Files\Google\Chrome\Application\chrome.exe`)
Chrome Version: 153.0.8010.48
Playwright: 1.63.0 (`@playwright/test`; `package.json` pins `^1.48.0`)
Python: (project `.venv`, used by Streamlit/backend/scripts)
Node: v22.14.0
PostgreSQL: 16-alpine, container `insightforge-postgres`, via `docker compose -f config/docker-compose.postgres.yml`
Application URL: http://localhost:8501

## Test Summary

Total: 41
Passed: 41
Failed: 0
Skipped: 0
Blocked: 0

Pass Percentage: 100%

Confirmed across two consecutive full clean runs at the suite's final configuration (see "Failures found and fixed" below for what it took to get there — nothing here is a first-try, unexamined pass).

## Browser Verification

Browser: Google Chrome
Headless: NO
Chrome Version: 153.0.8010.48 (read at runtime via `browser.version()`, launched with Playwright `channel: 'chrome'` — not bundled Chromium)

## Core Pipeline

Ingestion: PASS
SHA-256: PASS
Duplicate Detection: PASS
Schema Validation: PASS
Alteryx: PASS
Data Quality: PASS
PostgreSQL: PASS
SQL KPIs: PASS
Change Detection: PASS (via Overview's real day-over-day deltas)
Anomaly Detection: PASS (cross-checked via direct SQL against the planted West/Electronics/Laptop slice — see notes)
Drift: N/A — not exercised by this scope (not user-approved for this pass)
RCA: PASS (page renders without error; drill-down not independently numerically cross-checked in-browser)
Business Impact: PASS (page renders without error)
RFM: N/A — not exercised by this scope
Product Intelligence: N/A — not exercised by this scope
Forecast: PASS (page renders without error)
Recommendations: PASS (page renders without error; also has a dedicated Streamlit page tested)
AI Analyst: PASS (real evidence-grounded answer, template engine — no LLM key configured)
NL-to-SQL: PASS (scope adjusted — see Failures/Notes; the app's own no-LLM honest-unavailable path verified, not `validate_sql()`'s live blocking)
Security: PASS
Streamlit: PASS
Power BI Data: NOT EXECUTED — ENVIRONMENT LIMITATION (no `.pbix`/embed target exists by design)
PDF: PASS
Excel: PASS
Email: NOT EXECUTED — ENVIRONMENT LIMITATION (no SMTP catcher; only the app's own documented graceful no-send path verified)
Audit: PASS (`pipeline_runs`/Logs page)

## Streamlit Pages

Overview: PASS
Pipeline: PASS
Data Quality: PASS
Anomalies: PASS
Root Cause: PASS
Business Impact: PASS
Forecast: PASS
Customers: PASS
Products: PASS
AI Analyst: PASS
Logs: PASS
Recommendations: PASS (beyond the original 11-page spec; a real, existing page in this app)
Reports: PASS (beyond the original 11-page spec; a real, existing page in this app)
Alerts: PASS (beyond the original 11-page spec; a real, existing page in this app)

## Security

SQL Injection: PASS (app-level: the honest "AI query generation is unavailable" path is verified for every question when no LLM is configured; `validate_sql()`'s actual keyword/whitelist/multi-statement blocking is unit-tested directly in `tests/test_phase29_nl_to_sql.py`, not re-verified live in-browser — no `GEMINI_API_KEY` was available in this session)
Prompt Injection: PASS (documented probe — 3 adversarial questions produced the same honest template response, no credential-shaped text leaked; this is not a guarantee against a configured LLM being manipulated)
Secret Exposure: PASS (no `POSTGRES_PASSWORD`/`GEMINI_API_KEY`/`SMTP_PASSWORD` literal ever observed in any response)
Unsafe SQL: PASS (no fabricated SQL/result rows ever rendered)
Error Sanitization: PASS (`require_database()`'s friendly message renders on DB outage; no raw traceback ever observed)

## Performance

Startup: Docker + schema + Streamlit boot ~15-30s (not strictly benchmarked)
ETL: killer-test full pipeline-run-to-report-validation flow: 8-20s (varies duplicate vs. fresh path)
Database: not separately benchmarked
Analytics: not separately benchmarked
Reports: covered within the ETL figure above
Total Pipeline: full 41-test suite wall-clock: ~2.3-2.5 minutes at final configuration (`parallel` project capped at `workers: 2`)

Performance was not run as a dedicated benchmarking suite — explicitly out of the user-approved scope for this pass. Figures above are observed, not asserted pass/fail thresholds.

## Failures

Real failures encountered and fixed during this pass (root cause identified, application/test code fixed as appropriate, retested, confirmed via reruns) — see `docs/CHROME_E2E_AUDIT.md` §3 for full detail on each:

### Failure 1
Test: `db-unavailable.spec.ts` > stopping Postgres shows the friendly error
Expected: friendly error visible within 30s of stopping Postgres
Actual: intermittently not visible within 30s
Root Cause: `src/database.py`'s `ping()`→`scalar()` retries transient failures up to 3× with exponential backoff, each attempt up to `DB_CONNECT_TIMEOUT=10s` — real, deliberate app resilience, not a bug. Worst case can exceed 30s.
Affected File: `e2e/specs/db-unavailable.spec.ts`
Affected Line: the `toBeVisible` assertion's timeout
Fix: raised test timeout to 60s
Retest Result: PASS (confirmed across multiple subsequent runs)
Final Status: FIXED

### Failure 2
Test: `data-change.spec.ts` (original design)
Expected: ingesting a genuinely new future day succeeds and produces new `daily_kpis` data
Actual: `rows_valid=0`, run `FAILED`, DQ score 85.71
Root Cause: `config/data_contract.yaml`'s `Order_Date` rule enforces a hard upper bound of 2026-09-09 (the platform's fixed narrative present) — confirmed via two independent real attempts (`sales_2027_06_01.csv`, then a freshly-generated `sales_2026_09_10.csv`), both correctly rejected end-to-end. This is the app working correctly, not a bug.
Affected File: `e2e/specs/data-change.spec.ts`
Affected Line: the whole ingestion approach
Fix: rewrote the test as a real anti-hardcoding proof via cross-date `daily_kpis` value comparison instead of future-date ingestion
Retest Result: PASS
Final Status: FIXED (scope honestly adjusted, documented in `docs/CHROME_E2E_AUDIT.md` §6a)

### Failure 3
Test: `pg.Pool` process crash during `db-unavailable.spec.ts`
Expected: Node process survives stopping Postgres
Actual: unhandled `'error'` event crashed the entire test run
Root Cause: node-postgres's default behavior on an idle pooled connection's error event with no listener attached
Affected File: `e2e/helpers/db.ts`
Affected Line: `getPool()`
Fix: added `pool.on('error', () => {})`
Retest Result: PASS
Final Status: FIXED

### Failure 4
Test: accessibility axe scan on `/`
Expected: zero critical violations after excluding Streamlit chrome
Actual: `aria-allowed-attr`/`button-name` violations still reported on `#MainMenu`/sidebar-collapse elements
Root Cause: `AxeBuilder.exclude()` treats an array argument as one nested-frame selector chain, not independent excludes — a single `.exclude([a, b, c])` call silently excluded nothing useful
Affected File: `e2e/helpers/a11y.ts`
Affected Line: `runAxe()`
Fix: call `.exclude()` once per selector; expanded selector set to `[data-testid="stHeader"]`/`stSidebarCollapsedControl`/`stSidebar`
Retest Result: PASS
Final Status: FIXED

### Failure 5
Test: `full-pipeline.spec.ts` (Data Quality / Reports sections)
Expected: the original successful run's row is selectable on the Data Quality/Reports pages
Actual: `getByRole('option', ...)` / expander locator timed out — row not present
Root Cause: both pages cap their run list (`LIMIT 50`/`LIMIT 20`); as this suite's own repeated runs accumulated `SKIPPED_DUPLICATE` rows, the original run scrolled off both lists — correct product pagination, not a bug
Affected File: `e2e/specs/full-pipeline.spec.ts`
Affected Line: the Data Quality and Reports sections
Fix: validate DQ dimension counts and report-file validity directly against the database instead of depending on UI-list visibility for an old run
Retest Result: PASS
Final Status: FIXED

### Failure 6
Test: multiple, intermittent, under the `parallel` project's default worker count
Expected: pages render normally under concurrent test sessions
Actual: 3 specific tests (`data-change`, `reports`, `responsive @1366x768 /`) saw genuinely blank/unresponsive pages, reproducible across 2 separate full runs
Root Cause: Streamlit's single-process dev server has a real concurrency ceiling; several simultaneous worker sessions overloaded it
Affected File: `playwright.config.ts`
Affected Line: `parallel` project config
Fix: capped `parallel` project to `workers: 2`
Retest Result: PASS (2 consecutive clean 41/41 runs after the fix)
Final Status: FIXED

### Failure 7
Test: suite-wide, twice during this pass
Expected: `browserType.launch()` succeeds promptly
Actual: launch timed out (180s) once; a test context setup hung ~29 minutes once
Root Cause: accumulated stray `chrome.exe` processes (18 observed at worst) from repeated headed real-Chrome runs across this long session, exhausting local resources
Affected File: N/A (environment, not code)
Affected Line: N/A
Fix: killed stray `chrome.exe` processes before the run
Retest Result: PASS
Final Status: FIXED (documented as a recurring environment characteristic to watch for on reruns, not a one-time fluke)
