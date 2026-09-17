# Chrome UI Validation — Dark Theme Redesign

## Environment

- Browser: real Google Chrome (`channel: 'chrome'`, `headless: false`), version 153.0.8010.48
- Playwright: 1.63.0
- OS: Windows 11
- Application URL: http://localhost:8501

## Pages tested (all 14, real Chrome, real Postgres data)

Screenshots at 1536×864 in `docs/screenshots/`: `overview.png`, `pipeline.png`, `quality.png`, `anomalies.png`, `rca.png`, `impact.png`, `forecast.png`, `customers.png`, `products.png`, `recommendations.png`, `ai-analyst.png`, `reports.png`, `alerts.png`, `audit.png`.

## Functional tests (via the existing 41-test Playwright E2E suite, rerun in full against the redesigned theme)

| Page/Area | Result |
|---|---|
| Full pipeline run, real Postgres cross-checks | PASS |
| All 14 pages load with real data / honest empty state | PASS |
| Database-unavailable graceful failure & recovery | PASS |
| Duplicate-file detection | PASS |
| Resilience (duplicate-click, refresh-mid-pipeline) | PASS |
| Report downloads (real XLSX/PDF validity) | PASS |
| NL-to-SQL (honest no-LLM path) | PASS |
| Prompt-injection probe | PASS |
| Console/network error tracking | PASS |
| Responsive (1366×768 / 1536×864 / 1280×720) | PASS |
| Anti-hardcoding data-variation check | PASS |
| **Accessibility (axe, wcag2a/wcag2aa)** | **PASS — and the previously-documented `color-contrast` finding on Overview is now gone (0 findings, confirmed via the `axe-color-contrast-findings` test attachment, down from 1 serious finding before this redesign)** |

**41/41 tests passed, 0 skipped, 0 flaky** — full run against the live, redesigned theme.

Also reran the pytest `AppTest` suite (`tests/test_phase30_streamlit_app.py`):
- No-live-DB tier (Postgres genuinely stopped for a clean run): **38/38 passed** — confirms `inject_theme_css()` inside `require_database()` doesn't interfere with the DB-unavailable guard path.
- Live-DB tier: found and fixed one real, pre-existing bug (`5_Business_Impact.py`'s date filter — see `docs/UI_UX_REDESIGN.md`), confirmed fixed after the change; one unrelated, pre-existing failure remains (`10_Logs` — this session's own repeated E2E testing left hundreds of legitimate `"duplicate of run X"` entries in `pipeline_runs`, which the Logs page correctly renders via real `st.error()` calls; a test written assuming a low-error-count dataset, not a redesign defect — see note below).

## Visual issues found and fixed

1. **KPI tile value truncation**: long currency values (e.g. `$12,624,062.06`) were cut off at a fixed metric-value width. Fixed with `white-space: normal; overflow-wrap: break-word` and a slightly smaller font-size on `[data-testid="stMetricValue"]`.
2. **Hero card not actually wrapping its contents**: the first implementation used two separate `st.markdown("<div>...")`/`"</div>"` calls to open/close a card around several widgets — this does not nest in Streamlit's render tree (each `st.*` call is an independent sibling block), so the "card" rendered as a stray unclosed tag rather than a visual container. Fixed using `st.container(key="insightforge-hero", border=True)` and styling its real, stable generated class (`.st-key-insightforge-hero`) instead.
3. **`.streamlit/config.toml` unsupported keys**: the first version included `baseRadius`, `borderColor`, `showWidgetBorder`, and `[theme.sidebar]`, none of which Streamlit 1.40.1 (this project's pinned version) actually supports — confirmed via real startup log warnings (`"... is not a valid config option"`). Removed; equivalent styling moved into CSS.

## Remaining known issues

- `tests/test_phase30_streamlit_app.py::test_page_renders_real_content_with_live_postgres[10_Logs]` fails against the *current* database state, purely because this session's own extensive E2E test runs generated hundreds of legitimate `pipeline_runs.error = "duplicate of run X"` rows that the Logs page correctly displays. Not a redesign defect; the test's assumption (a low/zero error count) doesn't hold against this session's accumulated test data.
- The Tier-2 scope items from `docs/UI_UX_REDESIGN.md` (Business Health score, custom RCA/anomaly/product visualizations, grouped sidebar sections) remain unimplemented, as scoped with the user.

## Console/network errors

Zero unexpected console errors or 5xx responses across the full 14-page tour, per `console-network.spec.ts`'s pass in the full E2E rerun.
