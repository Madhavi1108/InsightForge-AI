# InsightForge AI - Alert & Email Engine

> Phase 34 deliverable (spec Phase 66, FR-25). `src/alerts.py` routes every
> current recommendation by severity - `LOW -> Dashboard`, `MEDIUM ->
> Streamlit`, `HIGH -> Email`, `CRITICAL -> Immediate Email` - and emails
> HIGH/CRITICAL alerts with the four spec-required sections, attaching the
> run's Excel/PDF reports.

## 1. What the spec says and what it leaves open

The spec (verbatim): create `src/alerts.py`; route by severity per the table
above; "Email must contain: issue, evidence, impact, recommendation. Attach
PDF/Excel where appropriate." No trigger source, routing implementation, or
email format is specified beyond that - everything below that isn't quoted
above is this project's own documented operational choice, the same
situation as every prior spec-silent phase.

## 2. Trigger: fresh recommendations, same shape as reporting

`docs/data-flow.md`'s stage list runs `... -> RECOMMEND -> EXPLAIN -> REPORT
-> ALERT -> AUDIT` - Alert consumes the same recommendations Report already
surfaces. `src.recommendations.Recommendation` (Phase 26) already carries
the spec's four required fields in different words:

| Spec field | `Recommendation` source |
|---|---|
| issue | synthesised from `metric`/`direction`/`period_key`/`rule_id` |
| evidence | `rationale` (already includes any RCA/forecast corroboration notes, `docs/recommendations.md` section 6) |
| impact | `impact_value`, formatted as a dollar figure or "not quantified" |
| recommendation | `title` |

Reusing `Recommendation` directly means one rule/severity/impact model
everywhere, not a second one invented for alerting.

## 3. "Dashboard" and "Streamlit" routes need no code

Both already display severity-tagged data straight from Postgres:
`anomalies`/`recommendations` rows are queried live by the Streamlit pages
(Phase 30) and by Power BI's Risk & Anomalies page (Phase 32). Routing a
LOW/MEDIUM alert therefore means classifying it and stopping there -
`dispatch_alerts` still returns a summary row for every severity (so
`stage_metrics` records the full breakdown), but only `HIGH`/`CRITICAL` do
real work: send via SMTP.

## 4. Wired into the orchestrator, like reporting

Same reasoning as Phase 33 (FR-24/FR-25 both name "per run" behaviour):
`src/orchestrator.py::run_file()` calls `dispatch_alerts` as a new advisory
stage (Stage 10, right after the Phase 33 report stage), wrapped in
`try/except` and logged into `stage_metrics["alert"]` - never changes
`pipeline_runs.status` or the exit code. An SMTP failure specifically is
logged and the run's report artifacts are kept
(`docs/architecture.md`: "Email send failure -> Log, keep the report
artifacts, do not fail the whole run").

`dispatch_alerts` computes recommendations fresh via
`generate_recommendations(db)` rather than depending on the report stage's
in-memory result - keeps this stage independently retriable/testable,
matching every other advisory orchestrator stage (anomaly fusion and drift
detection don't share state with each other either). The report stage's
`excel_path`/`pdf_path` (`None` on report failure) are passed through so
HIGH/CRITICAL emails can attach them.

## 5. Unconfigured SMTP never fakes a send

Same "configured means a real attempt, else a deterministic, honest
fallback" convention as `AlteryxSettings`/`LlmSettings`
(`docs/system-components.md`): a blank `SMTP_HOST` (`.env.example` ships it
blank) means every HIGH/CRITICAL alert is logged instead of sent, and
`email_sent=False` is reported rather than a fabricated delivery. No secret
(`SMTP_PASSWORD`) is ever written to a log line (`docs/security.md`
section 5).

## 6. Email content

Plain-text body, four labelled sections (`Issue:`/`Evidence:`/`Impact:`/
`Recommendation:`), subject line `[InsightForge ALERT] <issue>` for `HIGH`
or `[InsightForge URGENT] <issue>` for `CRITICAL` - the "immediate" distinction
the spec draws between the two email routes, expressed as urgency rather
than a queue (this pipeline is already synchronous, so both send
immediately). Attachments: the run's `InsightForge_Report_<date>.xlsx` and
`InsightForge_Executive_Report_<date>.pdf` (Phase 33) when they exist;
"attach where appropriate" is read as "attach whatever this run actually
produced" - a missing report attaches nothing rather than failing the send.

## 7. Why alerting doesn't need new persistence

Like reporting, alerts have no dedicated table - `anomalies`/`recommendations`
already carry the severity that drives routing, and `pipeline_runs
.stage_metrics->'alert'` records the per-run routing breakdown
(`count`, `emails_sent`, `by_route`) for audit.

## 8. API

| Symbol | Purpose |
|---|---|
| `ALERT_ROUTES` | the spec's severity -> route table |
| `route_for_severity(severity)` | pure; unknown severities default to `"dashboard"` |
| `Alert` | one routed alert - the four required fields plus routing context |
| `build_alert(rec)` | `Recommendation` -> `Alert` |
| `email_subject(alert)` / `email_body(alert)` | pure formatting |
| `build_email(alert, settings, attachments=None)` | assembles the `EmailMessage`, attachments included |
| `send_alert_email(alert, settings=None, attachments=None)` | the only network I/O; `True` iff actually sent |
| `dispatch_alerts(db, excel_path=None, pdf_path=None, settings=None)` | the on-demand/orchestrator entry point; returns one summary dict per alert |

## 9. Configuration

Already scaffolded in `.env.example` (Phase 34 section), no code change
needed to add them - `src.config.AlertSettings`/`get_alert_settings()` is
the new piece that reads them:

```
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_USE_TLS=true
ALERT_EMAIL_FROM=insightforge@example.com
ALERT_EMAIL_TO=manager@example.com
```

## 10. Verify

```powershell
pytest -q tests/test_phase34_alerts.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase34_alerts.py
```

```sql
SELECT run_id, stage_metrics->'alert' FROM pipeline_runs ORDER BY run_id DESC LIMIT 5;
```

## 11. Related documents

- [`recommendations.md`](recommendations.md) - the source of every alert's four fields
- [`reporting.md`](reporting.md) - the attached Excel/PDF files
- [`anomaly-fusion.md`](anomaly-fusion.md) - the severity scale (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`) alerts route on
- [`security.md`](security.md) - secret handling for `SMTP_PASSWORD` and sanitized logging
