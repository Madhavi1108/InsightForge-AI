"""InsightForge AI - pipeline orchestrator (Phase 10 / spec Phase 19).

The single component that sequences the pipeline (``docs/architecture.md`` §4).
Both entry points - the ``watchdog`` watcher and ``run_pipeline.py`` - call
:func:`main` with the same parsed arguments.

Today the pipeline has one working stage: **ingestion** (``src/ingestion.py``).
Validation / ETL / analytics arrive in Phase 11+. Until then an ingested file's
run is closed as ``PARTIAL`` with an explanatory note; later phases will flip it
to ``SUCCESS``.

Exit codes: ``0`` ok, ``2`` no mode chosen, ``3`` nothing to do / mode not built
yet, ``4`` database unavailable, ``5`` a file failed ingestion.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from src import ingestion
from src.config import get_paths
from src.database import Database, DatabaseError
from src.ingestion import IngestionError, IngestionResult, ingest_file

logger = logging.getLogger(__name__)

_PARTIAL_NOTE = "stages after ingestion not implemented yet (Phase 11+)"


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )


def _write_processed_summary(processed_dir: Path, result: IngestionResult) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": result.run_id,
        "file_name": result.file_name,
        "file_hash": result.file_hash,
        "rows_received": result.metadata.row_count,
        "status": "PARTIAL",
        "note": _PARTIAL_NOTE,
    }
    out = processed_dir / f"{Path(result.file_name).stem}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")


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

    elapsed = round(time.monotonic() - started, 3)
    stage_metrics = json.dumps({
        "ingest": {
            "seconds": elapsed,
            "rows_received": result.metadata.row_count,
            "size_bytes": result.metadata.size_bytes,
            "sha256": result.file_hash,
        }
    })
    db.execute(
        "UPDATE pipeline_runs SET status='PARTIAL', finished_at=now(), "
        "duration_s=:d, error=:note, stage_metrics=CAST(:sm AS JSONB) WHERE run_id=:r",
        {"d": elapsed, "note": _PARTIAL_NOTE, "sm": stage_metrics, "r": result.run_id},
    )
    _write_processed_summary(paths.processed, result)
    print(f"[ok] {result.file_name}: ingested as run {result.run_id} "
          f"(PARTIAL - {_PARTIAL_NOTE})")
    return 0


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
        print("[blocked] scheduled mode arrives in Phase 35 (APScheduler wiring). "
              "Use --file / --scan / --watch for now.")
        return 3
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
                      help="run the scheduled-mode loop (arrives in Phase 35)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and log the plan without executing stages")
    return parser


if __name__ == "__main__":
    sys.exit(main(build_parser().parse_args()))
