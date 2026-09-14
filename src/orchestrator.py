"""InsightForge AI - pipeline orchestrator (Phase 10 / spec Phase 19).

The single component that sequences the pipeline (``docs/architecture.md`` §4).
Both entry points - the ``watchdog`` watcher and ``run_pipeline.py`` - call
:func:`main` with the same parsed arguments.

Working stages: **ingestion** (``src/ingestion.py``), **schema validation**
(``src/validation.py``, Phase 11), the **Alteryx ingestion / data-quality
workflows** (``src/alteryx.py``, Phase 12), the **star-schema load**
(``src/etl.py``, Phase 13), the **data quality engine & gate**
(``src/data_quality.py``, Phase 14), **anomaly fusion**
(``src/anomaly_fusion.py``, Phase 20), **drift detection**
(``src/drift_detection.py``, Phase 21), **automated reporting**
(``src/reporting.py``, Phase 33), and the **alert & email engine**
(``src/alerts.py``, Phase 34). The DQ gate gives the run its
terminal status: ``>=95`` score -> ``SUCCESS``, ``90-94.99`` -> ``WARNING``
(the run still completed), ``<90`` -> ``FAILED`` - an audit-only REJECT that
never deletes or rolls back rows Phase 13 already loaded into
``fact_sales``. Anomaly fusion, drift detection, reporting, and alerting all
run after the gate and never change the terminal status - each is advisory
analytics (a failure in any of them is logged into ``stage_metrics``, never
fails the run).

**Observability & failure recovery (Phase 35, ``src/observability.py``)**:
structured JSON logs land in ``logs/pipeline.log`` tagged with each run's
``run_id`` (:func:`~src.observability.get_run_logger`); every stage from
Alteryx onward records its own ``"seconds"`` duration into
``stage_metrics``; the stages whose persistence can't duplicate a side
effect on replay (Alteryx, anomaly fusion, drift detection, reporting) are
retried up to 3x on failure (:func:`~src.observability.retry_stage`) -
alerts are deliberately **not** retried (a retry could resend a real
email); and one outer recovery net around the whole stage sequence closes
any otherwise-uncaught failure as ``FAILED``
(:func:`~src.observability.mark_run_failed`) rather than leaving a run
stuck at ``RUNNING``. Scheduled-mode runs (``--scheduler``,
``src/scheduler.py``) wrap this same ``run_file``/`scan` path in an
APScheduler cron loop, gated by ``SCHEDULER_ENABLED``.

Exit codes: ``0`` ok (includes a DQ ``WARNING``), ``2`` no mode chosen,
``3`` nothing to do / mode not built yet / scheduler disabled or
unavailable, ``4`` database unavailable, ``5`` a file failed ingestion,
``6`` a file failed schema validation (rejected), ``7`` the Alteryx/DQ
workflow stage raised unexpectedly (after retries), ``8`` the star-schema
load failed after retries, ``9`` the data-quality gate rejected the run
(score < ``DQ_WARN_THRESHOLD``), ``10`` an otherwise-uncaught failure was
caught by the recovery net and the run was marked ``FAILED``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from dataclasses import asdict

import pandas as pd

from src import ingestion
from src.alerts import dispatch_alerts
from src.alteryx import run_data_quality_workflow, run_ingestion_workflow
from src.anomaly_fusion import detect_and_persist_anomalies
from src.config import get_alteryx_settings, get_paths
from src.data_quality import DIMENSIONS, DqReport, score_file, thresholds
from src.database import Database, DatabaseError
from src.drift_detection import detect_and_persist_drift
from src.etl import EtlLoadError, run_sales_etl_workflow
from src.ingestion import IngestionError, IngestionResult, ingest_file
from src.observability import Stopwatch, configure_logging, get_run_logger, mark_run_failed, retry_stage
from src.reporting import generate_reports
from src.validation import ValidationResult, validate_csv

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level_num = getattr(logging, level, logging.INFO)
    logging.basicConfig(level=level_num, format="%(levelname)s %(name)s: %(message)s")
    configure_logging(get_paths().logs, level=level_num)


def _write_processed_summary(processed_dir: Path, result: IngestionResult,
                             vres: ValidationResult, stage_metrics: dict,
                             status: str) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": result.run_id,
        "file_name": result.file_name,
        "file_hash": result.file_hash,
        "rows_received": result.metadata.row_count,
        "rows_valid": vres.rows_valid,
        "rows_rejected": vres.rows_rejected,
        "alteryx_ingestion": stage_metrics.get("alteryx_ingestion"),
        "alteryx_dq": stage_metrics.get("alteryx_dq"),
        "etl": stage_metrics.get("etl"),
        "dq": stage_metrics.get("dq"),
        "anomalies": stage_metrics.get("anomalies"),
        "drift": stage_metrics.get("drift"),
        "status": status,
    }
    out = processed_dir / f"{Path(result.file_name).stem}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _dq_result_params(run_id: int, report: DqReport) -> list[dict]:
    params = [
        {"run": run_id, "dim": d.dimension, "score": d.score, "passed": d.passed,
         "checked": d.records_checked, "failed": d.records_failed,
         "detail": json.dumps(d.detail, ensure_ascii=False)}
        for d in report.dimensions
    ]
    params.append({
        "run": run_id, "dim": "Overall", "score": report.overall_score,
        "passed": report.gate != "REJECT", "checked": report.rows_checked,
        "failed": sum(d.records_failed for d in report.dimensions),
        "detail": json.dumps({"gate": report.gate, "dimensions": list(DIMENSIONS)},
                             ensure_ascii=False),
    })
    return params


def _reject_structural(db: Database, run_id: int, reason: str) -> None:
    db.execute(
        "INSERT INTO rejected_records "
        "(run_id, rejection_category, rejection_reason, dq_dimension) "
        "VALUES (:run, 'schema', :reason, 'Validity')",
        {"run": run_id, "reason": reason[:1000]},
    )


def _reject_rows(db: Database, run_id: int, vres: ValidationResult) -> int:
    by_row: dict[int, list] = {}
    for v in vres.row_violations:
        by_row.setdefault(v.row_number, []).append(v)
    params = []
    for n, violations in sorted(by_row.items()):
        categories = {v.category for v in violations}
        category = next(iter(categories)) if len(categories) == 1 else "multiple"
        dimension = violations[0].dq_dimension if len(categories) == 1 else "multiple"
        reason = "; ".join(v.reason for v in violations)[:500]
        params.append({
            "run": run_id, "n": n, "oid": violations[0].order_id,
            "cat": category, "reason": reason, "dim": dimension,
            "raw": json.dumps(vres.rejected_rows[n], ensure_ascii=False),
        })
    if params:
        db.execute_many(
            "INSERT INTO rejected_records "
            "(run_id, source_row_number, order_id, rejection_category, "
            "rejection_reason, dq_dimension, raw_record) "
            "VALUES (:run, :n, :oid, :cat, :reason, :dim, CAST(:raw AS JSONB))",
            params,
        )
    return len(params)


def run_file(path: Path, *, dry_run: bool = False) -> int:
    """Ingest one file end-to-end (as far as the pipeline is currently built)."""
    paths = get_paths()
    path = Path(path)

    if dry_run:
        print(f"[dry-run] would ingest {path.name}")
        print(f"          move  -> {paths.raw / path.name}")
        print(f"          copy  -> {paths.archive / path.name}")
        return 0

    try:
        db = Database()
        db.healthcheck()
    except DatabaseError as exc:
        print(f"[error] database unavailable: {exc}")
        return 4
    except Exception as exc:  # noqa: BLE001 - e.g. driver missing / bad config
        print(f"[error] database unavailable: {type(exc).__name__}")
        return 4

    started = time.monotonic()
    try:
        result = ingest_file(path, db, paths)
    except IngestionError as exc:
        print(f"[error] {exc.reason}")
        return 5

    if result.status == "SKIPPED_DUPLICATE":
        print(f"[skip] {result.file_name}: SKIPPED_DUPLICATE (run {result.run_id})")
        return 0

    run_logger = get_run_logger(result.run_id)
    ingest_seconds = round(time.monotonic() - started, 3)
    ingest_metrics = {
        "seconds": ingest_seconds,
        "rows_received": result.metadata.row_count,
        "size_bytes": result.metadata.size_bytes,
        "sha256": result.file_hash,
    }
    run_logger.info("ingested %s (%s rows) in %ss", result.file_name,
                    result.metadata.row_count, ingest_seconds)

    # Everything from here on is wrapped in one recovery net: any exception
    # that escapes an inner stage's own try/except (a bug, or a transient
    # DatabaseError not already handled) still closes this run as FAILED
    # rather than leaving it stuck at RUNNING (docs/architecture.md: "After
    # retries, run FAILED, state persisted for recovery, alert").
    try:
        # Stage 3 - schema / contract validation (Phase 11)
        v_started = time.monotonic()
        vres = validate_csv(result.raw_path)
        validate_metrics = {**vres.summary(), "seconds": round(time.monotonic() - v_started, 3)}

        if not vres.structural_ok:
            reason = "; ".join(vres.structural_errors)[:500]
            _reject_structural(db, result.run_id, reason)
            db.execute(
                "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), "
                "duration_s=:d, rows_received=:rr, rows_valid=0, rows_rejected=0, "
                "error=:e, stage_metrics=CAST(:sm AS JSONB) WHERE run_id=:r",
                {"d": round(time.monotonic() - started, 3), "rr": result.metadata.row_count,
                 "e": f"schema invalid: {reason}",
                 "sm": json.dumps({"ingest": ingest_metrics, "validate": validate_metrics}),
                 "r": result.run_id},
            )
            run_logger.warning("schema invalid: %s", reason)
            print(f"[error] {result.file_name}: schema invalid - {reason}")
            return 6

        if vres.row_violations:
            _reject_rows(db, result.run_id, vres)

        # Stage 4 - Alteryx ingestion & data-quality workflows (Phase 12) -
        # pure compute, no DB writes -> safe to retry up to 3x.
        alteryx_settings = get_alteryx_settings()
        try:
            with Stopwatch() as stage4_timer:
                def _run_alteryx_workflows():
                    ing = run_ingestion_workflow(result.raw_path, alteryx_settings)
                    dq = run_data_quality_workflow(result.raw_path, settings=alteryx_settings)
                    return ing, dq
                ing_wf, dq_wf = retry_stage(
                    _run_alteryx_workflows, what="alteryx workflows", stage_logger=run_logger,
                )
        except Exception as exc:  # noqa: BLE001 - never let a workflow crash the run silently
            elapsed = round(time.monotonic() - started, 3)
            db.execute(
                "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), "
                "duration_s=:d, rows_received=:rr, rows_valid=:rv, rows_rejected=:rj, "
                "error=:e, stage_metrics=CAST(:sm AS JSONB) WHERE run_id=:r",
                {"d": elapsed, "rr": result.metadata.row_count, "rv": vres.rows_valid,
                 "rj": vres.rows_rejected, "e": f"alteryx/dq workflow failed: {type(exc).__name__}",
                 "sm": json.dumps({"ingest": ingest_metrics, "validate": validate_metrics}),
                 "r": result.run_id},
            )
            run_logger.error("alteryx/dq workflow stage failed: %s", type(exc).__name__)
            print(f"[error] {result.file_name}: alteryx/dq workflow stage failed - {type(exc).__name__}")
            return 7

        alteryx_metrics = {"alteryx_ingestion": asdict(ing_wf), "alteryx_dq": asdict(dq_wf),
                           "alteryx": {"seconds": stage4_timer.seconds}}

        # Stage 5 - star-schema load (Phase 13) - already retries internally
        # (src/etl.py), not double-wrapped here.
        with Stopwatch() as stage5_timer:
            try:
                etl_result = run_sales_etl_workflow(result.raw_path, result.run_id, db, vres,
                                                    alteryx_settings)
            except EtlLoadError as exc:
                elapsed = round(time.monotonic() - started, 3)
                db.execute(
                    "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), "
                    "duration_s=:d, rows_received=:rr, rows_valid=:rv, rows_rejected=:rj, "
                    "error=:e, stage_metrics=CAST(:sm AS JSONB) WHERE run_id=:r",
                    {"d": elapsed, "rr": result.metadata.row_count, "rv": vres.rows_valid,
                     "rj": vres.rows_rejected, "e": f"star-schema load failed: {exc}"[:1000],
                     "sm": json.dumps({"ingest": ingest_metrics, "validate": validate_metrics,
                                       **alteryx_metrics}),
                     "r": result.run_id},
                )
                run_logger.error("star-schema load failed: %s", exc)
                print(f"[error] {result.file_name}: star-schema load failed - {exc}")
                return 8

        etl_metrics = asdict(etl_result)
        etl_metrics["seconds"] = stage5_timer.seconds
        stage_metrics = {**alteryx_metrics, "etl": etl_metrics}

        # Stage 6 - data quality engine, score & gate (Phase 14)
        with Stopwatch() as stage6_timer:
            dq_report = score_file(result.raw_path, vres)
        stage_metrics["dq"] = {
            "overall_score": dq_report.overall_score, "gate": dq_report.gate,
            "dimensions": {d.dimension: d.score for d in dq_report.dimensions},
            "seconds": stage6_timer.seconds,
        }
        status = {"PASS": "SUCCESS", "WARNING": "WARNING", "REJECT": "FAILED"}[dq_report.gate]
        run_logger.info("DQ gate %s (score %s)", dq_report.gate, dq_report.overall_score)

        # Stage 7 - anomaly fusion (Phase 20) - advisory only, never affects
        # status. A single atomic execute_many -> safe to retry up to 3x.
        file_dates = None
        try:
            with Stopwatch() as stage7_timer:
                file_dates = set(pd.read_csv(result.raw_path, usecols=["Order_Date"])["Order_Date"])
                fused = retry_stage(
                    lambda: detect_and_persist_anomalies(db, result.run_id, dates=file_dates),
                    what="anomaly fusion", stage_logger=run_logger,
                )
            stage_metrics["anomalies"] = {
                "count": len(fused),
                "by_severity": {sev: sum(1 for a in fused if a.severity == sev)
                               for sev in ("LOW", "MEDIUM", "HIGH", "CRITICAL")},
                "seconds": stage7_timer.seconds,
            }
        except Exception as exc:  # noqa: BLE001 - advisory analytics, never fails the run
            stage_metrics["anomalies"] = {"error": f"anomaly fusion failed: {type(exc).__name__}"}
            run_logger.warning("anomaly fusion failed: %s", type(exc).__name__)

        # Stage 8 - drift detection & reporting (Phase 21) - advisory only,
        # never affects status. Same atomic-insert safety as Stage 7.
        try:
            with Stopwatch() as stage8_timer:
                if not file_dates:
                    file_dates = set(
                        pd.read_csv(result.raw_path, usecols=["Order_Date"])["Order_Date"])
                current_end_date = max(file_dates)
                drift_results = retry_stage(
                    lambda: detect_and_persist_drift(db, result.run_id, current_end_date),
                    what="drift detection", stage_logger=run_logger,
                )
            stage_metrics["drift"] = {
                "count": len(drift_results),
                "by_status": {s: sum(1 for d in drift_results if d.status == s)
                             for s in ("Normal", "Warning", "Drift Detected")},
                "seconds": stage8_timer.seconds,
            }
        except Exception as exc:  # noqa: BLE001 - advisory analytics, never fails the run
            stage_metrics["drift"] = {"error": f"drift detection failed: {type(exc).__name__}"}
            run_logger.warning("drift detection failed: %s", type(exc).__name__)

        # Stage 9 - automated Excel & PDF reports (Phase 33) - advisory
        # only, never affects status. Overwrites deterministic filenames
        # -> safe to retry up to 3x.
        excel_path: Path | None = None
        pdf_path: Path | None = None
        try:
            with Stopwatch() as stage9_timer:
                excel_path, pdf_path = retry_stage(
                    lambda: generate_reports(
                        db, result.run_id, dq_report=dq_report, reports_dir=paths.reports,
                    ),
                    what="report generation", stage_logger=run_logger,
                )
            stage_metrics["report"] = {"excel": str(excel_path), "pdf": str(pdf_path),
                                       "seconds": stage9_timer.seconds}
        except Exception as exc:  # noqa: BLE001 - advisory analytics, never fails the run
            stage_metrics["report"] = {"error": f"report generation failed: {type(exc).__name__}"}
            run_logger.warning("report generation failed: %s", type(exc).__name__)

        # Stage 10 - severity-routed alerts & email engine (Phase 34) -
        # advisory only, never affects status. NOT retried: a retry after a
        # partially-successful attempt could resend a real email.
        try:
            with Stopwatch() as stage10_timer:
                alerts = dispatch_alerts(db, excel_path=excel_path, pdf_path=pdf_path)
            stage_metrics["alert"] = {
                "count": len(alerts),
                "emails_sent": sum(1 for a in alerts if a["email_sent"]),
                "by_route": {route: sum(1 for a in alerts if a["route"] == route)
                            for route in ("dashboard", "streamlit", "email", "immediate_email")},
                "seconds": stage10_timer.seconds,
            }
        except Exception as exc:  # noqa: BLE001 - advisory analytics, never fails the run
            stage_metrics["alert"] = {"error": f"alert dispatch failed: {type(exc).__name__}"}
            run_logger.warning("alert dispatch failed: %s", type(exc).__name__)

        error = (f"data quality gate rejected the run: overall score "
                 f"{dq_report.overall_score} < {thresholds()[1]}" if dq_report.gate == "REJECT"
                 else None)

        elapsed = round(time.monotonic() - started, 3)
        db.execute_many(
            "INSERT INTO data_quality_results "
            "(run_id, dimension, score, passed, records_checked, records_failed, detail) "
            "VALUES (:run, :dim, :score, :passed, :checked, :failed, CAST(:detail AS JSONB))",
            _dq_result_params(result.run_id, dq_report),
        )
        db.execute(
            "UPDATE pipeline_runs SET status=:status, finished_at=now(), "
            "duration_s=:d, rows_received=:rr, rows_valid=:rv, rows_rejected=:rj, "
            "dq_score=:dq, error=:e, stage_metrics=CAST(:sm AS JSONB) WHERE run_id=:r",
            {"status": status, "d": elapsed, "rr": result.metadata.row_count,
             "rv": vres.rows_valid, "rj": vres.rows_rejected, "dq": dq_report.overall_score,
             "e": error,
             "sm": json.dumps({"ingest": ingest_metrics, "validate": validate_metrics,
                               **stage_metrics}),
             "r": result.run_id},
        )
        _write_processed_summary(paths.processed, result, vres, stage_metrics, status)
        run_logger.info("run finished: status=%s duration=%ss", status, elapsed)
        print(f"[{'error' if dq_report.gate == 'REJECT' else 'ok'}] {result.file_name}: "
              f"run {result.run_id} - {vres.rows_valid} valid / {vres.rows_rejected} rejected - "
              f"alteryx {ing_wf.engine}/{dq_wf.engine}, etl {etl_result.engine} "
              f"({etl_result.summary.get('rows_loaded', 0)} rows loaded) - "
              f"DQ {dq_report.overall_score} ({dq_report.gate}) - "
              f"anomalies {stage_metrics['anomalies'].get('count', 'n/a')} - "
              f"drift {stage_metrics['drift'].get('count', 'n/a')} - "
              f"report {'ok' if 'error' not in stage_metrics['report'] else 'failed'} - "
              f"alerts {stage_metrics['alert'].get('count', 'n/a')} - {status}")
        return 9 if dq_report.gate == "REJECT" else 0
    except Exception as exc:  # noqa: BLE001 - the recovery net: see comment above
        mark_run_failed(db, result.run_id, exc, run_logger)
        print(f"[error] {result.file_name}: run {result.run_id} - unexpected failure "
              f"({type(exc).__name__}), marked FAILED")
        return 10


def scan(*, dry_run: bool = False) -> int:
    """Process every ``*.csv`` currently in ``data/incoming/``."""
    incoming = get_paths().incoming
    files = sorted(p for p in incoming.glob("*.csv") if p.is_file())
    if not files:
        print(f"[ok] no files in {incoming}")
        return 3
    worst = 0
    for f in files:
        code = run_file(f, dry_run=dry_run)
        worst = worst or code
    return worst


def watch_mode() -> int:
    """Block on the ``data/incoming/`` watcher, ingesting files as they land."""
    paths = get_paths()
    paths.ensure()
    print(f"[ok] watching {paths.incoming} - drop .csv files to ingest (Ctrl+C to stop)")
    ingestion.watch(paths.incoming, dispatch=lambda p: run_file(p))
    return 0


def main(args: argparse.Namespace) -> int:
    _configure_logging()

    if getattr(args, "scheduler", False):
        from src.scheduler import run_scheduler
        return run_scheduler()
    if getattr(args, "watch", False):
        return watch_mode()

    dry = getattr(args, "dry_run", False)
    if getattr(args, "file", None):
        return run_file(Path(args.file), dry_run=dry)
    if getattr(args, "scan", False):
        return scan(dry_run=dry)

    print("[error] choose one of --file / --scan / --watch / --scheduler")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="src.orchestrator",
        description="InsightForge AI - pipeline orchestrator",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--file", type=str, help="process a single file")
    mode.add_argument("--scan", action="store_true",
                      help="process every file currently in data/incoming/")
    mode.add_argument("--watch", action="store_true",
                      help="watch data/incoming/ and process new files as they arrive")
    mode.add_argument("--scheduler", action="store_true",
                      help="run the APScheduler cron loop (SCHEDULER_ENABLED/SCHEDULER_CRON in .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and log the plan without executing stages")
    return parser


if __name__ == "__main__":
    sys.exit(main(build_parser().parse_args()))
