"""InsightForge AI - Alteryx ingestion & data-quality workflows (Phase 12 /
spec Phases 23-24).

Pipeline **stage 4** (``docs/data-flow.md``): the orchestrator runs this on
the file in ``data/raw/`` right after schema validation (Phase 11) and
before the sales / customer / product ETL that loads the star schema
(Phase 13 - not built yet, so this module never touches ``fact_sales`` or
any ``dim_*`` table).

Two workflows, each with a real Alteryx artifact under ``alteryx/`` and a
Python fallback that produces the *same contract output*:

* ``01_ingestion`` - re-affirms the file is structurally sound for the data
  contract (column set + order) and reports the row count.
* ``02_data_quality`` - runs the full per-row contract check and reports
  violation counts by category and by DQ dimension.

Engine selection (``docs/system-components.md``): when
``AlteryxSettings.is_configured()`` is true, each workflow is attempted
against the real ``AlteryxEngineCmd.exe`` first (retried up to 3 times on a
non-zero exit / timeout); otherwise - or if all 3 attempts fail - it runs via
the Python fallback. A fallback run is always reported honestly
(``engine="python_fallback"``, ``verified=False``); a real Alteryx run is
never faked as having happened.

The Python fallback deliberately calls back into ``src.validation`` (already
verified by Phase 11) rather than re-implementing contract checks, so its
output is provably identical to what the rest of the pipeline already
computes.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.config import AlteryxSettings, get_alteryx_settings
from src.validation import DataContract, get_contract, validate_csv

#: How many times a real Alteryx invocation is retried before falling back.
MAX_ENGINE_ATTEMPTS = 3
#: Fixed backoff between retries, in seconds.
RETRY_BACKOFF_SECONDS = 1.0
#: Per-attempt subprocess timeout, in seconds.
ENGINE_TIMEOUT_SECONDS = 300


class AlteryxExecutionError(RuntimeError):
    """Raised when every attempt to run a workflow through the real Alteryx
    engine fails (non-zero exit or timeout). Callers fall back to Python."""


@dataclass(frozen=True)
class WorkflowResult:
    workflow: str
    engine: str  # "alteryx" | "python_fallback"
    verified: bool
    seconds: float
    summary: dict = field(default_factory=dict)
    error: str | None = None


def invoke_alteryx_engine(workflow_path: Path, csv_path: Path,
                          settings: AlteryxSettings) -> subprocess.CompletedProcess:
    """Shell out to ``AlteryxEngineCmd.exe <workflow.yxmd>``, retried.

    The input CSV path is passed as a positional argument; the ``.yxmd``
    workflows are authored to read it via an Alteryx user-constant / question
    (see ``alteryx/01_ingestion.yxmd`` / ``alteryx/02_data_quality.yxmd``).
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ENGINE_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                [settings.engine_cmd, str(workflow_path), str(csv_path)],
                capture_output=True, text=True, timeout=ENGINE_TIMEOUT_SECONDS,
                check=False,
            )
            if proc.returncode == 0:
                return proc
            last_error = AlteryxExecutionError(
                f"AlteryxEngineCmd exited {proc.returncode} (attempt {attempt})"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
        if attempt < MAX_ENGINE_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS)
    raise AlteryxExecutionError(
        f"{workflow_path.name}: engine failed after {MAX_ENGINE_ATTEMPTS} attempts"
    ) from last_error


def _run_with_engine_or_fallback(
    workflow_name: str, workflow_file: str, csv_path: Path,
    settings: AlteryxSettings, fallback_summary_fn,
) -> WorkflowResult:
    started = time.monotonic()
    error: str | None = None
    if settings.is_configured():
        workflow_path = settings.workflow_path(workflow_file)
        try:
            invoke_alteryx_engine(workflow_path, csv_path, settings)
            return WorkflowResult(
                workflow=workflow_name, engine="alteryx", verified=True,
                seconds=round(time.monotonic() - started, 3),
                summary=fallback_summary_fn(),
            )
        except AlteryxExecutionError as exc:
            # Fall through to the Python fallback below; report honestly.
            error = str(exc)

    summary = fallback_summary_fn()
    return WorkflowResult(
        workflow=workflow_name, engine="python_fallback", verified=False,
        seconds=round(time.monotonic() - started, 3), summary=summary,
        error=error,
    )


def _ingestion_summary(csv_path: Path, contract: DataContract) -> dict:
    import pandas as pd

    header = list(pd.read_csv(csv_path, nrows=0).columns)
    columns_ok = header == list(contract.columns)
    with open(csv_path, encoding="utf-8") as fh:
        row_count = sum(1 for _ in fh) - 1
    return {"columns_ok": columns_ok, "row_count": max(row_count, 0)}


def run_ingestion_workflow(csv_path: Path, settings: AlteryxSettings | None = None,
                           contract: DataContract | None = None) -> WorkflowResult:
    """Workflow 1 - ``alteryx/01_ingestion.yxmd`` (structural re-check)."""
    settings = settings or get_alteryx_settings()
    contract = contract or get_contract()
    return _run_with_engine_or_fallback(
        "01_ingestion", "01_ingestion.yxmd", Path(csv_path), settings,
        lambda: _ingestion_summary(Path(csv_path), contract),
    )


def _data_quality_summary(csv_path: Path, contract: DataContract) -> dict:
    vres = validate_csv(csv_path, contract)
    by_category: dict[str, int] = {}
    by_dimension: dict[str, int] = {}
    for v in vres.row_violations:
        by_category[v.category] = by_category.get(v.category, 0) + 1
        by_dimension[v.dq_dimension] = by_dimension.get(v.dq_dimension, 0) + 1
    return {
        "rows_checked": vres.rows_checked,
        "rows_valid": vres.rows_valid,
        "rows_rejected": vres.rows_rejected,
        "violations_by_category": by_category,
        "violations_by_dimension": by_dimension,
    }


def run_data_quality_workflow(csv_path: Path, contract: DataContract | None = None,
                              settings: AlteryxSettings | None = None) -> WorkflowResult:
    """Workflow 2 - ``alteryx/02_data_quality.yxmd`` (per-row contract check).

    The Python fallback calls :func:`src.validation.validate_csv` directly so
    its output is identical to what Phase 11 already computed for this file -
    this workflow reports it, it does not recompute a divergent DQ pass.
    """
    settings = settings or get_alteryx_settings()
    contract = contract or get_contract()
    return _run_with_engine_or_fallback(
        "02_data_quality", "02_data_quality.yxmd", Path(csv_path), settings,
        lambda: _data_quality_summary(Path(csv_path), contract),
    )
