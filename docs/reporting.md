# InsightForge AI - Automated Excel & PDF Reports

> Phase 33 deliverable (spec Phases 64-65, FR-24). `src/reporting.py`
> assembles every prior engine's latest output into a dated 12-sheet Excel
> workbook and a management-friendly PDF summary, written into `reports/`
> once per pipeline run.

## 1. What the spec says and what it leaves open

The spec (verbatim) is two short lists:

- **Phase 64**: create `src/reporting.py`; generate
  `InsightForge_Report_DATE.xlsx` with 12 sheets - Executive Summary, KPIs,
  Regions, Categories, Products, Customers, Anomalies, Root Causes, Impact,
  Recommendations, Forecast, Data Quality.
- **Phase 65**: generate `InsightForge_Executive_Report_DATE.pdf` covering
  executive summary, KPI changes, major anomalies, root causes, impact,
  forecast, and recommendations - "keep it management-friendly."

No sheet layout, trigger mechanism, or date format is specified beyond that.
`docs/requirements.md` FR-24 adds one constraint the spec itself doesn't
state explicitly: reports are "generated **per run**" - and
`docs/data-flow.md` places Report as pipeline stage 17. Everything below
that isn't quoted above is this project's own documented operational
choice, the same situation as every prior spec-silent phase.

## 2. Trigger: wired into the orchestrator, unlike its own inputs

Phases 22-26 (root cause, business impact, RFM/product intelligence,
forecasting, recommendations) are deliberately **not** wired into
`src/orchestrator.py` - they need a period comparison, not a single
just-ingested file's dates. Reporting is different: FR-24's "per run"
requirement and `docs/data-flow.md`'s stage list both call for automatic,
per-run generation, so `src/reporting.py` **is** wired into
`src/orchestrator.py::run_file()` as a new advisory stage (Stage 9, right
after drift detection) - same convention as anomaly fusion (Phase 20) and
drift detection (Phase 21): wrapped in `try/except`, logged into
`stage_metrics["report"]`, never changes `pipeline_runs.status` or the exit
code.

## 3. Data assembly is computed fresh, not read back by `run_id`

Because RCA/impact/forecast/recommendations aren't persisted per `run_id`,
`generate_report_data(db, run_id, dq_report=None)` calls each engine
directly for the latest available period rather than querying tables that
wouldn't have rows for this run yet - the same best-effort pattern
`src.recommendations` already uses for its own RCA/impact/forecast
enrichment (`docs/recommendations.md` section 6). Every section is
independently wrapped in `try/except`: a failed section becomes an empty
table (Excel) or a one-line note (PDF) plus an entry in
`ReportData.errors`, never a raised exception - reporting must never fail
the pipeline run, and no raw error detail is ever surfaced
(`docs/security.md` section 5).

| Sheet | Source |
|---|---|
| Executive Summary | latest `daily_kpis` row, significant-move count, this run's anomaly count/severity mix, DQ gate/score, top recommendation |
| KPIs | `daily_kpis` view (Phase 16), full history |
| Regions | `regional_performance` view, ranked by revenue |
| Categories | `category_performance` view, ranked by revenue |
| Products | `product_performance` view, ranked by revenue |
| Customers | `customer_performance` view, ranked by revenue |
| Anomalies | `anomalies` table `WHERE run_id = :run_id` (Phase 20's fusion output for this run) |
| Root Causes | `src.root_cause.analyze_root_cause` for each `RCA_SUPPORTED_METRICS` metric with a significant day-over-day move |
| Impact | `src.impact_analysis.assess_business_impact(db)` for the latest date |
| Recommendations | `src.recommendations.generate_recommendations(db)` |
| Forecast | `src.forecasting.forecast_metric(db, metric, horizon=7)` for each `FORECAST_METRICS` metric |
| Data Quality | the orchestrator's in-memory `DqReport` (avoids a redundant query and a race against its own not-yet-committed insert); a standalone caller with no `DqReport` falls back to the persisted `data_quality_results` rows for that `run_id` |

## 4. Output convention

```
reports/InsightForge_Report_<date>.xlsx
reports/InsightForge_Executive_Report_<date>.pdf
```

`<date>` is the latest date the KPI data itself covers (`daily_kpis.order_date`
max), not wall-clock "today" - a report generated for an older backfilled
file is dated by its own data. `reports/` resolves through
`src.config.get_paths().reports` (`REPORTS_DIR`, unchanged since Phase 10).
This filename/location convention is a stable contract: Phase 34's alert
engine (`src/alerts.py`, not yet built) will attach these same files to
HIGH/CRITICAL emails.

## 5. Excel workbook (`write_excel_report`)

Built with `pandas.ExcelWriter(engine="xlsxwriter")` - one sheet per
`SHEET_NAMES` entry, always in that order, always present even when a
section is empty or failed (an empty/failed sheet renders a single `note`
column explaining why, rather than being omitted - every spec-named sheet
exists in every workbook). Headers are bold with a shared header colour;
column widths auto-fit to content.

## 6. PDF summary (`write_pdf_report`)

Built with `reportlab.platypus` (`SimpleDocTemplate` + `Table`/`Paragraph`
flowables) - a single-column flowing document, not a fixed-page layout,
matching the spec's "keep it management-friendly" instruction with the
Phase 65 section order: Executive Summary, KPI Changes, Major Anomalies (top
10 by confidence), Root Causes, Business Impact, Forecast (7-day), Recommendations
(top 10 by priority). A section with nothing to report renders one
plain-language sentence instead of an empty table.

## 7. Why reporting doesn't need new persistence

Unlike recommendations/forecasts/anomalies, reports are files, not rows -
there is no `reports` table. Traceability instead comes from
`pipeline_runs.stage_metrics->'report'`, which records the two output paths
(or a failure note) for every run, and from the files themselves living in
`reports/` (excluded from git per `docs/security.md`).

## 8. API

| Symbol | Purpose |
|---|---|
| `ReportData` | everything both outputs render from; `errors` holds per-section failure notes |
| `generate_report_data(db, run_id, dq_report=None)` | fetch every section, best-effort |
| `write_excel_report(data, path)` | the 12-sheet workbook |
| `write_pdf_report(data, path)` | the management-friendly PDF |
| `generate_reports(db, run_id, dq_report=None, reports_dir=None)` | assemble + write both; returns `(excel_path, pdf_path)` |

`reports_dir` is accepted explicitly (defaulting to `get_paths().reports`)
so a caller that already resolved `PipelinePaths` for this run -
`src.orchestrator`, or a test with a monkeypatched `get_paths` - writes to
the same directory without a second, independently-monkeypatched lookup.

## 9. Standalone use

`scripts/generate_report.py [--run-id N]` (re)generates both files without
re-running ingestion - defaults to the latest `pipeline_runs` row.

## 10. Configuration

No new environment variables - filenames are date-derived, and the output
directory reuses `REPORTS_DIR` (already resolved since Phase 10).

## 11. Verify

```powershell
pytest -q tests/test_phase33_reports.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase33_reports.py
python scripts/generate_report.py
```

```sql
SELECT run_id, stage_metrics->'report' FROM pipeline_runs ORDER BY run_id DESC LIMIT 5;
```

## 12. Related documents

- [`kpi-engine.md`](kpi-engine.md) / [`analytical-views.md`](analytical-views.md) - the KPI/Regions/Categories/Products/Customers sheet sources
- [`anomaly-fusion.md`](anomaly-fusion.md) - the Anomalies sheet source
- [`root-cause-analysis.md`](root-cause-analysis.md) - the Root Causes sheet source
- [`business-impact-engine.md`](business-impact-engine.md) - the Impact sheet source
- [`forecasting.md`](forecasting.md) - the Forecast sheet source
- [`recommendations.md`](recommendations.md) - the Recommendations sheet source
- [`data-quality-engine.md`](data-quality-engine.md) - the Data Quality sheet source
