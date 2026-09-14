# InsightForge AI - Observability & Failure Recovery

> Phase 35 deliverable (spec Phase 67, FR-26). `src/observability.py` adds
> structured logging, per-stage execution duration, safe stage retries, and
> a recovery net to the orchestrator; `src/scheduler.py` wires a real
> (opt-in) APScheduler cron loop behind `--scheduler`.

## 1. What the spec says and what it leaves open

The spec (verbatim): "Implement: structured logging, pipeline IDs,
execution duration, retries, failure states, error messages, recovery
states. Retry failed components up to three times where safe." No format,
storage location, or retry policy is specified beyond that -
`docs/system-components.md` narrows it to a named module
(`src/observability.py`, "structured logging to `logs/`, pipeline run IDs,
execution durations, retry counts, failure states, sanitized error
messages, recovery states, audit trail") and a separate scheduler
requirement (`SCHEDULER_ENABLED`/`SCHEDULER_CRON`, APScheduler). Everything
below that isn't quoted above is this project's own documented operational
choice.

## 2. Pipeline IDs already exist - this phase makes them visible

`pipeline_runs.run_id` (Phase 10) is minted once per file and threaded
through every stage already. `get_run_logger(run_id)` wraps the module
logger in a `logging.LoggerAdapter` that stamps `run_id` onto every log
record's extra fields, so a run's full story can be grepped/filtered out of
`logs/pipeline.log` by that one ID - no second identifier invented.

## 3. Structured logging

`configure_logging(logs_dir, level=None)` adds a `RotatingFileHandler`
(5&nbsp;MB &times; 3 backups) to the root logger, formatted as one JSON
object per line (`_JsonFormatter`): `timestamp`, `level`, `logger`,
`message`, plus any extra fields a caller attached (`run_id`, and anything
else passed via `extra=`). It's additive - the existing console
`logging.basicConfig` (`src/orchestrator.py::_configure_logging`, plain
text) and the human-readable `print(...)` summary line both stay; this adds
a **second**, machine-parseable destination on disk. Idempotent (guarded by
a module-level flag) so a long-lived `--scan`/`--watch`/scheduler process
doesn't stack a new handler per file processed.

## 4. Execution duration

`Stopwatch` is a small context manager (`with Stopwatch() as sw: ...` then
`sw.seconds`). Before this phase, only ingestion and validation had a
per-stage `"seconds"` key in `stage_metrics`; every stage from Alteryx
onward (`alteryx`, `etl`, `dq`, `anomalies`, `drift`, `report`, `alert`) now
gets one too - satisfying `docs/requirements.md` NFR-05's "per-run
measurement of ... ingestion, ETL, DB load, SQL, analytics, report
generation, and total pipeline time; recorded in
`pipeline_runs.stage_metrics`" for real, not just the overall `duration_s`.

## 5. Retries - "where safe" is a real constraint

`retry_stage(fn, attempts=3, base_delay=0.5, what=..., stage_logger=None)`
is a generic helper (built on `tenacity`, the same library
`src/database.py` already uses for connection-level retries) - it does not
decide *what* is safe to retry; `src/orchestrator.py` does, per stage,
based on whether a repeat call can duplicate a side effect:

| Stage | Persistence shape | Safe to retry? |
|---|---|---|
| 4 - Alteryx workflows | pure compute, no DB write | **yes** |
| 5 - ETL load | its own internal retry already exists (Phase 13, `src/etl.py`) | not double-wrapped |
| 7 - anomaly fusion | one atomic `execute_many` | **yes** - either 0 rows or the call already returned |
| 8 - drift detection | one atomic `execute_many` | **yes** - same reasoning |
| 9 - report generation | overwrites deterministic filenames | **yes** - re-running just re-overwrites |
| 10 - alert/email dispatch | sends real email via SMTP | **no** - a retry after a partially-successful attempt could resend a real message |

This table is the concrete answer to "where safe": every retried stage's
persistence is a single all-or-nothing operation or has no persistence at
all; the one stage with an irreversible external side effect (email) stays
single-attempt, explicitly and by design, not by omission.

Connection-level transient failures (a dropped connection mid-query) are
still retried separately and first, inside `src/database.py`
(`DB_MAX_RETRIES`, Phase 9) - `retry_stage` retries the *whole stage
function* on top of that, for failures that aren't a transient connection
blip (a bad row, a computation error, a brief external dependency hiccup).

## 6. Failure states & recovery states

Every stage already had its own `try/except` closing the run as `FAILED`
on a *known* failure mode (schema invalid, Alteryx/DQ workflow error,
ETL load error) or logging-and-continuing for an *advisory* one (anomalies,
drift, report, alerts). What was missing, per `docs/architecture.md`'s
failure table ("Database unavailable -> After retries, run FAILED, state
persisted for recovery, alert"): a safety net for anything that slips past
all of those - most notably a `DatabaseConnectionError` raised from a call
with no dedicated handler (the final `INSERT`/`UPDATE`, `score_file`,
`_reject_rows`, ...).

`run_file()` now wraps its entire stage sequence (from the point `run_id`
exists) in one outer `try/except Exception`, calling
`mark_run_failed(db, run_id, exc, run_logger)` and returning a new exit
code **10**. `mark_run_failed` is deliberately narrow and defensive:

```sql
UPDATE pipeline_runs SET status='FAILED', finished_at=now(), error=:e
WHERE run_id=:r AND status NOT IN ('SUCCESS', 'WARNING', 'FAILED')
```

- the `status NOT IN (...)` guard means it never overwrites a run that
  already reached a terminal state through its normal path;
- the error message is `f"unexpected failure: {type(exc).__name__}"` -
  sanitised, no raw exception text (`docs/security.md` section 5);
- the `UPDATE` call itself is wrapped in `try/except` - a failure while
  recovering from a failure is logged, never raised again.

## 7. Scheduler (`src/scheduler.py`)

`APScheduler` is already pinned in `requirements.txt` but was never
imported anywhere; `--scheduler` was a pure stub (`"arrives in Phase 35"`,
exit code 3). It's real now, gated by `SchedulerSettings`
(`SCHEDULER_ENABLED`, default `false`; `SCHEDULER_CRON`, default
`"0 * * * *"`, already scaffolded in `.env.example` as `0 8 * * *` -
daily 08:00):

- `run_scheduler()` returns exit code 3 immediately (no APScheduler import
  attempted) when `SCHEDULER_ENABLED` is false - the same "nothing to do"
  family `scan()` already uses for an empty `data/incoming/`, not a silent
  background loop nobody opted into.
- Otherwise, `build_scheduler()` imports `apscheduler` **lazily, inside the
  function** - never at module import time - so `import src.scheduler`
  always succeeds even when the package isn't installed (confirmed: it
  isn't, in this project's own sandbox). A missing dependency prints a
  clear message and returns exit code 3, the same "never fake it"
  convention already used for Alteryx's engine and the Gemini SDK.
  Otherwise it starts a `BlockingScheduler` with one `CronTrigger
  .from_crontab(cron)` job calling `src.orchestrator.scan` (the default
  `dispatch`), returning 0 on a clean `Ctrl+C`.

## 8. API

| Symbol | Purpose |
|---|---|
| `configure_logging(logs_dir, level=None)` | adds the JSON-lines file handler; idempotent |
| `get_run_logger(run_id, base_logger=None)` | a `LoggerAdapter` stamping `run_id` onto every record |
| `Stopwatch` | context manager; `.seconds` after the block |
| `retry_stage(fn, attempts=3, base_delay=0.5, what=..., stage_logger=None)` | generic retry, `tenacity`-backed |
| `mark_run_failed(db, run_id, exc, stage_logger=None)` | the recovery net; never raises |
| `SchedulerSettings.from_env()` / `get_scheduler_settings()` | `SCHEDULER_ENABLED`/`SCHEDULER_CRON` |
| `build_scheduler(dispatch=None, settings=None)` | one-job `BlockingScheduler`; lazy `apscheduler` import |
| `run_scheduler(settings=None)` | the `--scheduler` entry point |

## 9. Configuration

Already scaffolded in `.env.example` (no change needed):

```
SCHEDULER_ENABLED=false
SCHEDULER_CRON=0 8 * * *
```

## 10. Verify

```powershell
pytest -q tests/test_phase35_observability.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase35_observability.py
python run_pipeline.py --scheduler   # exits 3, "scheduler disabled"
```

```sql
SELECT run_id, status, error, stage_metrics FROM pipeline_runs ORDER BY run_id DESC LIMIT 5;
```

## 11. Related documents

- [`architecture.md`](architecture.md) - the failure-mode table this phase closes the last gap in
- [`security.md`](security.md) - sanitised error message convention
- [`database-layer.md`](database-layer.md) - the connection-level retry `retry_stage` builds on top of
- [`ingestion.md`](ingestion.md) - `pipeline_runs.run_id`'s origin
