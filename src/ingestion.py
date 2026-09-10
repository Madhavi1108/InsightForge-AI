"""InsightForge AI - file ingestion & SHA-256 fingerprinting (Phase 10 / spec Phases 19-20).

Stage 1-2 of the pipeline (``docs/data-flow.md`` §3):

* **Detect / validate** - a path arrives from the watcher or ``run_pipeline.py``;
  the file is size- and type-checked before anything parses it.
* **Fingerprint & register** - a streamed SHA-256 hash is computed, checked
  against ``file_registry`` for a duplicate, a ``pipeline_runs`` row is minted,
  the file is **moved** ``incoming/ -> raw/`` and **copied** ``raw/ -> archive/``,
  and a ``file_registry`` row is written.

A duplicate hash produces a ``SKIPPED_DUPLICATE`` run and the re-dropped file is
moved to ``archive/`` (no warehouse change, no ``file_registry`` insert). An
unreadable / empty / non-CSV file raises :class:`IngestionError`; the caller
records the run as ``FAILED``.

All database access goes through :class:`src.database.Database`. Error text is
sanitised - it never carries an absolute path.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.database import Database, QueryExecutionError

logger = logging.getLogger(__name__)

_HASH_CHUNK = 1 << 20  # 1 MiB
_PARTIAL_SUFFIXES = {".tmp", ".part", ".crdownload", ".swp", ".partial"}
_UNIQUE_VIOLATION = "23505"  # PostgreSQL SQLSTATE for unique_violation


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


WATCH_SETTLE_SECONDS = _env_float("WATCH_SETTLE_SECONDS", 2.0)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class IngestionError(RuntimeError):
    """A file could not be ingested (missing / empty / unreadable / wrong type).

    ``str()`` and :attr:`reason` never contain an absolute filesystem path.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Fingerprint & metadata
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, *, chunk: int = _HASH_CHUNK) -> str:
    """Return the hex SHA-256 digest of ``path``, read in ``chunk``-sized blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _count_data_rows(path: Path) -> int:
    """Data-line count (total newline-terminated lines minus the header)."""
    with open(path, "rb") as fh:
        lines = sum(1 for _ in fh)
    return max(0, lines - 1)


@dataclass(frozen=True)
class FileMetadata:
    name: str
    path: Path
    size_bytes: int
    row_count: int
    modified_at: datetime
    sha256: str


def collect_metadata(path: Path) -> FileMetadata:
    """Validate ``path`` is a readable, non-empty ``.csv`` and describe it.

    Raises :class:`IngestionError` (with a name-only reason) on any problem.
    """
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"file not found: {path.name}")
    if not path.is_file():
        raise IngestionError(f"not a regular file: {path.name}")
    if path.suffix.lower() != ".csv":
        raise IngestionError(f"unsupported file type '{path.suffix}': {path.name}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise IngestionError(f"cannot stat {path.name}: {type(exc).__name__}") from None
    if size == 0:
        raise IngestionError(f"file is empty: {path.name}")
    try:
        row_count = _count_data_rows(path)
        digest = sha256_file(path)
    except OSError as exc:
        raise IngestionError(f"cannot read {path.name}: {type(exc).__name__}") from None
    return FileMetadata(
        name=path.name,
        path=path,
        size_bytes=size,
        row_count=row_count,
        modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        sha256=digest,
    )


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
@dataclass
class IngestionResult:
    run_id: int
    status: str  # 'RUNNING' | 'SKIPPED_DUPLICATE'
    file_hash: str
    file_name: str
    archive_path: Path
    metadata: FileMetadata
    raw_path: Path | None = None
    duplicate_of_run_id: int | None = None


def _archive_target(archive_dir: Path, name: str, run_id: int) -> Path:
    target = archive_dir / name
    if not target.exists():
        return target
    stem = Path(name)
    return archive_dir / f"{stem.stem}.dupe-{run_id}{stem.suffix}"


def _record_skip(db: Database, meta: FileMetadata, first_seen_run_id: int | None) -> int:
    note = (
        f"duplicate of run {first_seen_run_id}"
        if first_seen_run_id is not None
        else "duplicate file (hash already in file_registry)"
    )
    return int(db.insert_returning(
        "INSERT INTO pipeline_runs "
        "(file_name, file_hash, status, rows_received, finished_at, duration_s, error) "
        "VALUES (:n, :h, 'SKIPPED_DUPLICATE', :rr, now(), 0, :e) RETURNING run_id",
        {"n": meta.name, "h": meta.sha256, "rr": meta.row_count, "e": note},
    ))


def ingest_file(path: Path, db: Database, paths) -> IngestionResult:
    """Fingerprint, deduplicate, register and relocate one incoming file.

    ``paths`` is a :class:`src.config.PipelinePaths`. On a genuinely new file the
    returned run is left ``RUNNING`` for the downstream stages; the caller closes
    it. On a duplicate the run is already terminal (``SKIPPED_DUPLICATE``).
    """
    path = Path(path)
    meta = collect_metadata(path)
    paths.ensure()

    dup = db.fetch_one(
        "SELECT first_seen_run_id FROM file_registry WHERE file_hash = :h",
        {"h": meta.sha256},
    )
    if dup is not None:
        first_seen = dup.get("first_seen_run_id")
        run_id = _record_skip(db, meta, first_seen)
        target = _archive_target(paths.archive, meta.name, run_id)
        shutil.move(str(path), str(target))
        logger.info("run %s SKIPPED_DUPLICATE (%s); moved to archive", run_id, meta.name)
        return IngestionResult(
            run_id=run_id, status="SKIPPED_DUPLICATE", file_hash=meta.sha256,
            file_name=meta.name, archive_path=target, metadata=meta,
            raw_path=None, duplicate_of_run_id=first_seen,
        )

    run_id = int(db.insert_returning(
        "INSERT INTO pipeline_runs (file_name, file_hash, status, rows_received) "
        "VALUES (:n, :h, 'RUNNING', :rr) RETURNING run_id",
        {"n": meta.name, "h": meta.sha256, "rr": meta.row_count},
    ))

    try:
        raw_path = Path(shutil.move(str(path), str(paths.raw / meta.name)))
        archive_path = Path(shutil.copy2(str(raw_path), str(paths.archive / meta.name)))
    except OSError as exc:
        db.execute(
            "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), error=:e "
            "WHERE run_id=:r",
            {"e": f"relocation failed: {type(exc).__name__}", "r": run_id},
        )
        raise IngestionError(f"could not relocate {meta.name}: {type(exc).__name__}") from None

    try:
        db.execute(
            "INSERT INTO file_registry "
            "(file_name, file_hash, file_size_bytes, row_count, first_seen_run_id) "
            "VALUES (:n, :h, :sz, :rc, :r)",
            {"n": meta.name, "h": meta.sha256, "sz": meta.size_bytes,
             "rc": meta.row_count, "r": run_id},
        )
    except QueryExecutionError as exc:
        if getattr(exc, "sqlstate", None) != _UNIQUE_VIOLATION:
            db.execute(
                "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), error=:e "
                "WHERE run_id=:r",
                {"e": f"file_registry insert failed: {exc.cause_type or 'error'}", "r": run_id},
            )
            raise
        # A concurrent ingest registered this hash first - treat as a duplicate.
        db.execute(
            "UPDATE pipeline_runs SET status='SKIPPED_DUPLICATE', finished_at=now(), "
            "duration_s=0, error='duplicate (lost registration race)' WHERE run_id=:r",
            {"r": run_id},
        )
        logger.info("run %s SKIPPED_DUPLICATE (registration race, %s)", run_id, meta.name)
        return IngestionResult(
            run_id=run_id, status="SKIPPED_DUPLICATE", file_hash=meta.sha256,
            file_name=meta.name, archive_path=archive_path, metadata=meta,
            raw_path=raw_path, duplicate_of_run_id=None,
        )

    logger.info("run %s ingested %s (%d rows, %s)", run_id, meta.name,
                meta.row_count, meta.sha256[:12])
    return IngestionResult(
        run_id=run_id, status="RUNNING", file_hash=meta.sha256,
        file_name=meta.name, archive_path=archive_path, metadata=meta,
        raw_path=raw_path, duplicate_of_run_id=None,
    )


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #
def _is_candidate(path: Path) -> bool:
    return (
        path.suffix.lower() == ".csv"
        and not path.name.startswith(".")
        and path.suffix.lower() not in _PARTIAL_SUFFIXES
    )


def wait_until_stable(path: Path, *, settle: float = 1.0, timeout: float = 120.0) -> bool:
    """Block until ``path``'s size is unchanged across two ``settle``-spaced reads.

    Returns ``True`` when stable, ``False`` if the file vanished or ``timeout``
    elapsed first.
    """
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last:
            return True
        last = size
        time.sleep(max(0.0, settle))
    return False


class _IncomingHandler(FileSystemEventHandler):
    """Dispatch each settled ``.csv`` that lands in the watched directory."""

    def __init__(self, dispatch: Callable[[Path], object], settle: float) -> None:
        self._dispatch = dispatch
        self._settle = settle

    def _handle(self, raw_path: str, is_directory: bool) -> None:
        if is_directory:
            return
        path = Path(raw_path)
        if not _is_candidate(path):
            return
        if not wait_until_stable(path, settle=self._settle):
            logger.warning("watcher: %s never settled; skipping", path.name)
            return
        try:
            self._dispatch(path)
        except Exception:  # noqa: BLE001 - a bad file must not kill the observer
            logger.exception("watcher: dispatch failed for %s", path.name)

    def on_created(self, event) -> None:
        self._handle(event.src_path, event.is_directory)

    def on_moved(self, event) -> None:
        self._handle(event.dest_path, event.is_directory)


def watch(incoming_dir: Path, dispatch: Callable[[Path], object], *,
          block: bool = True, settle: float | None = None) -> Observer:
    """Start a watchdog observer on ``incoming_dir``; call ``dispatch(path)`` per file.

    With ``block=True`` (the CLI default) this runs until ``KeyboardInterrupt``.
    With ``block=False`` it returns the live :class:`Observer` for the caller to
    ``stop()`` / ``join()`` (used by tests).
    """
    incoming_dir = Path(incoming_dir)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    settle = WATCH_SETTLE_SECONDS if settle is None else settle

    observer = Observer()
    observer.schedule(_IncomingHandler(dispatch, settle), str(incoming_dir), recursive=False)
    observer.start()
    logger.info("watching %s (settle=%.1fs)", incoming_dir, settle)

    if not block:
        return observer
    try:
        while observer.is_alive():
            observer.join(1)
    except KeyboardInterrupt:
        logger.info("watcher: stopping on interrupt")
    finally:
        observer.stop()
        observer.join()
    return observer
