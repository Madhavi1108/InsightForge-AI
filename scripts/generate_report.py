#!/usr/bin/env python3
"""InsightForge AI - generate the Excel & PDF reports on demand (Phase 33 /
spec Phases 64-65, FR-24).

``src/reporting.py`` is wired into ``src/orchestrator.py`` as an advisory
per-run stage, so a normal pipeline run already produces both files. This
script exists to (re)generate them **without** re-running ingestion - useful
for a demo, a manual re-run after a formula change, or the Streamlit app.

Usage
-----
    python scripts/generate_report.py                # latest pipeline_runs row
    python scripts/generate_report.py --run-id 42     # a specific run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/generate_report.py",
        description="Generate InsightForge AI's Excel & PDF reports on demand",
    )
    parser.add_argument(
        "--run-id", type=int, default=None,
        help="pipeline_runs.run_id to report on (default: the latest run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from src.database import Database, DatabaseError
    from src.reporting import generate_reports

    db = Database()
    try:
        if args.run_id is not None:
            run_id = args.run_id
        else:
            row = db.fetch_one("SELECT run_id FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
            if row is None:
                print("[error] no pipeline_runs rows exist yet - run the pipeline first")
                return 2
            run_id = row["run_id"]
    except DatabaseError as exc:
        print(f"[error] could not reach the database: {exc.__class__.__name__}")
        return 3

    print(f"[run] generating reports for run_id={run_id}")
    try:
        excel_path, pdf_path = generate_reports(db, run_id)
    except Exception as exc:  # noqa: BLE001 - surface a sanitised message only
        print(f"[error] report generation failed: {exc.__class__.__name__}")
        return 4

    print(f"[ok] {excel_path}")
    print(f"[ok] {pdf_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
