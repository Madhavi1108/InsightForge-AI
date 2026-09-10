#!/usr/bin/env python3
"""InsightForge AI - manual pipeline entry point.

This is the *controlled manual fallback* described in the specification. Normal
operation is automatic: a file watcher detects a new file in ``data/incoming/``
and invokes the orchestrator. This script invokes the **same** orchestration
code path for a single file, on demand.

Usage
-----
    python run_pipeline.py --file data/incoming/sales_2026_09_09.csv
    python run_pipeline.py --scan            # process every file in data/incoming/
    python run_pipeline.py --watch           # watch data/incoming/ for new files
    python run_pipeline.py --scheduler       # scheduled-mode loop (arrives in Phase 35)

Ingestion + SHA-256 fingerprinting (Phase 10) and schema/contract validation
(Phase 11) run for real; ETL and analytics arrive in Phase 12+. Scheduled mode
is wired in Phase 35.

Exit codes: 0 ok, 2 no mode, 3 nothing to do / not built, 4 database
unavailable, 5 file failed ingestion, 6 file failed schema validation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    """Load ``.env`` if python-dotenv and the file are both available."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        print("[warn] python-dotenv not installed; skipping .env load")
        return
    load_dotenv(env_path)
    print(f"[ok] loaded environment from {env_path.name}")


def _run_orchestrator(args: argparse.Namespace) -> int:
    """Delegate to the real orchestrator once it exists (Phase 10)."""
    try:
        from src.orchestrator import main as orchestrator_main  # type: ignore
    except ModuleNotFoundError:
        print(
            "[blocked] src/orchestrator.py is not implemented yet.\n"
            "          The pipeline orchestrator arrives in Phase 10. "
            "See docs/PHASE_STATUS.md for current progress."
        )
        return 3
    return int(orchestrator_main(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="InsightForge AI - manual pipeline entry point",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--file", type=str, help="process a single file")
    mode.add_argument(
        "--scan",
        action="store_true",
        help="process every file currently in data/incoming/",
    )
    mode.add_argument(
        "--watch",
        action="store_true",
        help="watch data/incoming/ and process new files as they arrive",
    )
    mode.add_argument(
        "--scheduler",
        action="store_true",
        help="run the scheduled-mode loop instead of a one-shot run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and log the plan without executing stages",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("InsightForge AI - pipeline entry point")
    print("=" * 40)
    _load_env()
    return _run_orchestrator(args)


if __name__ == "__main__":
    sys.exit(main())
