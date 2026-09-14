"""Phase 35 (spec Phase 67, FR-26) - observability & failure recovery.

Stopwatch, retry_stage, mark_run_failed, structured logging, and
SchedulerSettings are pure/unit-testable without a database and get direct
tests. `build_scheduler`'s happy path needs `apscheduler` installed
(`pytest.importorskip`) - this sandbox doesn't have it, mirroring
`src/scheduler.py`'s own "optional dependency, imported lazily" design; its
missing-dependency path is exercised for real (not simulated) since
`apscheduler` genuinely isn't installed here. The real per-stage retry/
timing wiring and the recovery net inside `src/orchestrator.py::run_file`
need real data, so those are `INSIGHTFORGE_PG_INTEGRATION=1`-gated, same
convention as Phases 20/21/26/33/34.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import src.observability as obs
from src.config import SchedulerSettings
from src.observability import Stopwatch, mark_run_failed, retry_stage

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase35_test__"


def test_phase35_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "observability.py",
        PROJECT_ROOT / "src" / "scheduler.py",
        PROJECT_ROOT / "docs" / "observability.md",
    ):
        assert p.is_file(), f"missing {p}"


@pytest.mark.parametrize("module", ["src.observability", "src.scheduler"])
def test_import_has_no_side_effects(module):
    r = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# Stopwatch - pure
# --------------------------------------------------------------------------- #
def test_stopwatch_measures_elapsed_seconds():
    with Stopwatch() as sw:
        time.sleep(0.02)
    assert sw.seconds is not None
    assert sw.seconds >= 0.02


def test_stopwatch_records_time_even_on_exception():
    sw = Stopwatch()
    with pytest.raises(RuntimeError):
        with sw:
            raise RuntimeError("boom")
    assert sw.seconds is not None


# --------------------------------------------------------------------------- #
# retry_stage - pure
# --------------------------------------------------------------------------- #
def test_retry_stage_succeeds_on_first_attempt():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert retry_stage(fn, what="test") == "ok"
    assert len(calls) == 1


def test_retry_stage_succeeds_after_two_failures():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("transient")
        return "recovered"

    assert retry_stage(fn, attempts=3, base_delay=0.01, what="test") == "recovered"
    assert len(calls) == 3


def test_retry_stage_exhausts_and_reraises():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        retry_stage(fn, attempts=3, base_delay=0.01, what="test")
    assert len(calls) == 3


def test_retry_stage_logs_a_warning_per_retry():
    class _CapturingLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, *args, **kwargs):
            self.warnings.append(args)

    log = _CapturingLogger()
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        retry_stage(fn, attempts=3, base_delay=0.01, what="test", stage_logger=log)
    assert len(log.warnings) == 2  # logged before attempts 2 and 3, not after the final raise


# --------------------------------------------------------------------------- #
# Structured logging - JSON lines with run_id
# --------------------------------------------------------------------------- #
def test_configure_logging_writes_json_lines_with_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_configured", False)
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    try:
        obs.configure_logging(tmp_path, level=logging.INFO)
        run_logger = obs.get_run_logger(99)
        run_logger.info("hello world")
        for h in root.handlers:
            h.flush()

        log_file = tmp_path / "pipeline.log"
        assert log_file.is_file()
        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines
        record = json.loads(lines[-1])
        assert record["run_id"] == 99
        assert record["message"] == "hello world"
        assert record["level"] == "INFO"
        assert "timestamp" in record
    finally:
        for h in list(root.handlers):
            if h not in before_handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(before_level)
        monkeypatch.setattr(obs, "_configured", False)


def test_configure_logging_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_configured", False)
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    try:
        obs.configure_logging(tmp_path)
        obs.configure_logging(tmp_path)  # second call must not add a second handler
        new_handlers = [h for h in root.handlers if h not in before_handlers]
        assert len(new_handlers) == 1
    finally:
        for h in list(root.handlers):
            if h not in before_handlers:
                root.removeHandler(h)
                h.close()
        monkeypatch.setattr(obs, "_configured", False)


# --------------------------------------------------------------------------- #
# mark_run_failed - stub Database, no real connection
# --------------------------------------------------------------------------- #
class _StubDatabase:
    def __init__(self, raise_on_execute=False):
        self.calls = []
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._raise:
            raise RuntimeError("db unreachable")
        return 1


def test_mark_run_failed_writes_sanitised_update():
    db = _StubDatabase()
    mark_run_failed(db, 42, ValueError("raw sensitive detail"))
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "UPDATE pipeline_runs SET status='FAILED'" in sql
    assert params["r"] == 42
    assert params["e"] == "unexpected failure: ValueError"
    assert "raw sensitive detail" not in params["e"]


def test_mark_run_failed_never_raises_even_if_recovery_write_fails():
    db = _StubDatabase(raise_on_execute=True)
    mark_run_failed(db, 42, ValueError("boom"))  # must not raise


# --------------------------------------------------------------------------- #
# SchedulerSettings - pure
# --------------------------------------------------------------------------- #
def test_scheduler_settings_defaults_to_disabled():
    settings = SchedulerSettings.from_env({})
    assert settings.enabled is False
    assert settings.cron == "0 * * * *"


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_scheduler_settings_enabled_values(value):
    settings = SchedulerSettings.from_env({"SCHEDULER_ENABLED": value})
    assert settings.enabled is True


def test_scheduler_settings_reads_cron_override():
    settings = SchedulerSettings.from_env({"SCHEDULER_CRON": "0 8 * * *"})
    assert settings.cron == "0 8 * * *"


# --------------------------------------------------------------------------- #
# Scheduler build/run - apscheduler optional
# --------------------------------------------------------------------------- #
def test_build_scheduler_registers_one_cron_job():
    pytest.importorskip("apscheduler")
    from src.scheduler import build_scheduler

    calls = []
    settings = SchedulerSettings(enabled=True, cron="*/5 * * * *")
    scheduler = build_scheduler(dispatch=lambda: calls.append(1), settings=settings)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "insightforge_scan"


def test_run_scheduler_disabled_returns_exit_3(capsys):
    from src.scheduler import run_scheduler
    code = run_scheduler(SchedulerSettings(enabled=False, cron="0 * * * *"))
    assert code == 3
    assert "disabled" in capsys.readouterr().out


def test_run_scheduler_missing_dependency_message(capsys, monkeypatch):
    """This sandbox genuinely has no ``apscheduler`` installed, so calling
    ``run_scheduler`` with it enabled exercises the real missing-dependency
    path end to end (not simulated) - the ``monkeypatch`` below only forces
    the same ``ModuleNotFoundError`` path deterministically regardless of
    what happens to be installed wherever this test runs."""
    import src.scheduler as sched_module

    def _raise(*a, **k):
        raise ModuleNotFoundError("no module named 'apscheduler'")
    monkeypatch.setattr(sched_module, "build_scheduler", _raise)
    code = sched_module.run_scheduler(SchedulerSettings(enabled=True, cron="0 * * * *"))
    assert code == 3
    assert "apscheduler" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL + orchestrator wiring
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pg_integration
def test_orchestrator_records_per_stage_seconds_and_json_logs(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    monkeypatch.setattr(obs, "_configured", False)
    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=1500, seed=35)
    d = sorted(df["Order_Date"].unique())[0]
    day_df = df[df["Order_Date"] == d].copy()
    name = f"{SENTINEL}_a.csv"
    day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")

    code = orchestrator.run_file(p.incoming / name)
    assert code in (0, 9)

    row = db.fetch_one(
        "SELECT run_id, stage_metrics FROM pipeline_runs WHERE file_name = :n", {"n": name},
    )
    metrics = row["stage_metrics"]
    for stage in ("alteryx", "etl", "dq", "anomalies", "drift", "report", "alert"):
        assert "seconds" in metrics[stage], f"stage {stage} missing seconds"

    log_file = p.logs / "pipeline.log"
    assert log_file.is_file()
    lines = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(rec.get("run_id") == row["run_id"] for rec in lines)


@pg_integration
def test_recovery_net_marks_run_failed_on_unexpected_exception(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    monkeypatch.setattr(obs, "_configured", False)
    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected failure")
    monkeypatch.setattr(orchestrator, "score_file", _boom)

    df = gd.generate_dataset(rows=1200, seed=36)
    d = sorted(df["Order_Date"].unique())[0]
    day_df = df[df["Order_Date"] == d].copy()
    name = f"{SENTINEL}_b.csv"
    day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")

    code = orchestrator.run_file(p.incoming / name)
    assert code == 10

    row = db.fetch_one(
        "SELECT status, error FROM pipeline_runs WHERE file_name = :n", {"n": name},
    )
    assert row["status"] == "FAILED"
    assert row["error"] == "unexpected failure: RuntimeError"
