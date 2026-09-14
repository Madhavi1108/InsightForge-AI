"""InsightForge AI - observability & failure recovery (Phase 35 / spec
Phase 67, FR-26).

The spec (verbatim): "Implement: structured logging, pipeline IDs,
execution duration, retries, failure states, error messages, recovery
states. Retry failed components up to three times where safe." No format,
storage location, or retry policy is specified beyond that - the rest below
is this project's own documented operational choice.

**Pipeline IDs** already exist: ``pipeline_runs.run_id`` (Phase 10) is
minted once per file and threaded through every stage. This module doesn't
invent a second identifier - :func:`get_run_logger` just makes that ID show
up in every log line for the run.

**"Where safe" is a real constraint, not a blanket retry wrapper.**
:func:`retry_stage` is a generic helper; `src/orchestrator.py` decides,
stage by stage, whether retrying is safe (documented there): a stage whose
persistence is a single atomic ``execute_many`` (anomaly fusion, drift) or
that only overwrites deterministically-named files (reporting) can't
duplicate side effects on retry; a stage with an external side effect that
a first attempt may have already partially completed (sending an email)
cannot be retried blindly and stays single-attempt.

**Recovery**: :func:`mark_run_failed` is the safety net for anything that
slips past every stage's own ``try/except`` - it closes a ``pipeline_runs``
row as ``FAILED`` with a sanitised error message (no raw exception detail,
``docs/security.md`` section 5) and is itself best-effort: a failure while
recovering from a failure must never raise again.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from types import TracebackType

from tenacity import Retrying, stop_after_attempt, wait_fixed

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 0.5

_LOG_FILENAME = "pipeline.log"
_configured = False


# --------------------------------------------------------------------------- #
# Structured logging to logs/
# --------------------------------------------------------------------------- #
class _JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message, and any
    extra fields a caller attached (notably ``run_id``)."""

    _RESERVED = set(logging.LogRecord(
        "", 0, "", 0, "", (), None,
    ).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(logs_dir: Path, level: int | None = None) -> None:
    """Add a JSON-lines file handler writing to ``logs_dir/pipeline.log``.

    Idempotent - safe to call once per process (or once per ``run_file``
    call in a long-lived ``--scan``/``--watch``/scheduler loop) without
    stacking duplicate handlers.
    """
    global _configured
    if _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / _LOG_FILENAME, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    if level is not None:
        root.setLevel(level)
    _configured = True


def get_run_logger(run_id: int, base_logger: logging.Logger | None = None) -> logging.LoggerAdapter:
    """A logger that injects ``run_id`` into every record's extra fields -
    the spec's "pipeline IDs" requirement, reusing the ID
    ``pipeline_runs.run_id`` already mints rather than a second one."""
    return logging.LoggerAdapter(base_logger or logger, {"run_id": run_id})


# --------------------------------------------------------------------------- #
# Execution duration
# --------------------------------------------------------------------------- #
class Stopwatch:
    """``with Stopwatch() as sw: ...`` then ``sw.seconds`` - per-stage
    execution duration, the spec's own wording."""

    def __enter__(self) -> "Stopwatch":
        self._start = time.monotonic()
        self.seconds: float | None = None
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> bool:
        self.seconds = round(time.monotonic() - self._start, 3)
        return False


# --------------------------------------------------------------------------- #
# Retries - "up to three times where safe"
# --------------------------------------------------------------------------- #
def retry_stage(
    fn, *, attempts: int = DEFAULT_RETRY_ATTEMPTS, base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    what: str = "stage", stage_logger: logging.LoggerAdapter | logging.Logger | None = None,
):
    """Call ``fn()``, retrying up to ``attempts`` times total on any
    exception, with a fixed short delay between attempts.

    Only call this for a stage the caller has verified is safe to repeat
    (idempotent persistence, or no side effect at all) - see the module
    docstring. Re-raises the last exception after exhausting ``attempts``;
    every retry (not just the last) is logged at ``WARNING``.
    """
    log = stage_logger or logger
    retryer = Retrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_fixed(base_delay),
        reraise=True,
    )
    attempt_count = 0

    def _tracked():
        nonlocal attempt_count
        attempt_count += 1
        try:
            return fn()
        except Exception as exc:
            if attempt_count < attempts:
                log.warning("retrying %s (attempt %d/%d) after %s",
                            what, attempt_count, attempts, type(exc).__name__)
            raise
    return retryer(_tracked)


# --------------------------------------------------------------------------- #
# Failure & recovery states
# --------------------------------------------------------------------------- #
def mark_run_failed(db, run_id: int, exc: BaseException,
                    stage_logger: logging.LoggerAdapter | logging.Logger | None = None) -> None:
    """Best-effort: close ``pipeline_runs`` as ``FAILED`` with a sanitised
    error message. Never raises - a failure while recovering from a
    failure must not crash the process; it is only logged.
    """
    log = stage_logger or logger
    message = f"unexpected failure: {type(exc).__name__}"
    try:
        db.execute(
            "UPDATE pipeline_runs SET status='FAILED', finished_at=now(), "
            "error=:e WHERE run_id=:r AND status NOT IN ('SUCCESS', 'WARNING', 'FAILED')",
            {"e": message, "r": run_id},
        )
        log.error("run %s marked FAILED (recovery net): %s", run_id, message)
    except Exception as recovery_exc:  # noqa: BLE001 - recovery itself must never raise
        log.error("run %s: recovery UPDATE also failed: %s",
                  run_id, type(recovery_exc).__name__)
