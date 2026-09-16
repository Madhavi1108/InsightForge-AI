# Frontend Implementation Report

## 1. Summary

Gap-filling pass over `streamlit_app/` per `docs/FRONTEND_AUDIT.md`: 3 new
pages, Overview KPI/freshness enrichment, one filter addition. All 11
pre-existing pages were left unmodified except `Overview.py` and
`pages/5_Business_Impact.py`, per the findings table.

## 2. New files

| File | Responsibility |
|---|---|
| `streamlit_app/pages/11_Recommendations.py` | Full recommendation list with severity/priority-band filter |
| `streamlit_app/pages/12_Reports.py` | Excel/PDF report downloads per run, gated on the file existing on disk and being non-empty |
| `streamlit_app/pages/13_Alerts.py` | Severity-routed alert review (display only) |
| `docs/FRONTEND_AUDIT.md` | Gap analysis behind this pass |
| `docs/FRONTEND_IMPLEMENTATION_REPORT.md` | This file |

## 3. Changed files

| File | Change |
|---|---|
| `streamlit_app/common.py` | Added `valid_report_path()` - pure helper, resolves a report path and returns `None` unless the file exists and is non-empty |
| `streamlit_app/Overview.py` | Added 5 KPI tiles (AOV, margin, customers, avg discount, avg shipping days) + day-over-day `delta` on every tile (reusing the existing `compare_period` call, no new query); added a data-freshness caption (last run, data date range) |
| `streamlit_app/pages/5_Business_Impact.py` | Added an in-memory date-range filter over the already-fetched impact series |
| `tests/test_phase30_streamlit_app.py` | Page-count assertion updated 11 → 14; added 4 unit tests for `valid_report_path` |
| `docs/streamlit-app.md` | Page table extended to 14 rows; header note on the gap-filling pass |

## 4. Reused vs. new logic

**Zero new `src/` logic was written.** Every new page/tile is a UI-only
composition of existing, already-tested functions:
`src.recommendations.generate_recommendations`, `src.alerts.build_alert`,
`src.alerts.route_for_severity`, the report paths `src.reporting.generate_reports`
already writes into `pipeline_runs.stage_metrics`, and
`src.change_detection.compare_period` (already imported by Overview.py before
this pass).

## 5. Safety notes

- **Alerts page never sends email.** It calls only the pure `build_alert()`
  per current recommendation - never `src.alerts.dispatch_alerts()` or
  `send_alert_email()`, both of which send real SMTP mail for HIGH/CRITICAL
  severity. Verified: `grep -n "dispatch_alerts\|send_alert_email" streamlit_app/pages/13_Alerts.py` returns nothing.
- **Reports page never shows a dead download button.** `valid_report_path()`
  checks `.is_file()` and `.stat().st_size > 0` before any `st.download_button`
  is rendered; a run with no report (or a report whose stage failed) shows
  "Report not available for this run." instead.

## 6. Test coverage

- `pytest tests/test_phase30_streamlit_app.py` (no live Postgres, this
  sandbox): **38 passed, 14 skipped** (the `pg_integration`-gated tier).
  All 3 new pages pass the existing 3-tier `AppTest` pattern (no-exception /
  DB-unavailable-error / — the live-content tier is the 14 skips) automatically,
  since `ALL_PAGES` is built from a glob over `pages/*.py`.
- 4 new direct unit tests for `valid_report_path` (missing input, nonexistent
  file, zero-byte file, real file) - all passing.

## 7. Known limitations

- Overview's "data covers X to Y" freshness line is the overall
  `daily_kpis` min/max, not scoped to the latest run's own file - an
  intentional simplification (`pipeline_runs` has no per-run date-range
  column), not a bug.
- **The `pg_integration`-gated test tier (14 tests) and the manual browser
  walkthrough were not run in this session.** Docker Desktop is installed
  (`docker --version` succeeds) but its daemon is not running in this
  sandbox (`docker compose ... ps` fails: "failed to connect to the docker
  API ... dockerDesktopLinuxEngine"), so PostgreSQL could not be started
  here. This is stated plainly rather than fabricating a passing run,
  consistent with this project's "never fake functionality" rule and the
  same honest gap `docs/final-delivery.md` records for Phase 36's own
  sandbox. To complete verification:

  ```powershell
  docker compose -f config/docker-compose.postgres.yml up -d
  python scripts/apply_schema.py
  $env:INSIGHTFORGE_PG_INTEGRATION = "1"
  pytest -q tests/test_phase30_streamlit_app.py
  streamlit run streamlit_app/Overview.py
  ```

  Then verify by hand: Overview's 9 tiles + deltas + freshness line;
  Business Impact's date filter narrows the chart; Recommendations page
  filters correctly; Reports page shows a working download only where a
  report file exists on disk (run the Pipeline page first if `reports/` is
  empty) and "not available" otherwise; Alerts page groups correctly.
