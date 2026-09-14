#!/usr/bin/env python3
"""InsightForge AI - the final "killer demo" (Phase 36 / spec Phase 69).

Phase 69, verbatim: "This is the final acceptance phase. Run the entire
system using a NEW dataset file. Place: sales_2026_09_09.csv into:
data/incoming/." `docs/PHASE_MAP.md`'s "Final killer demo" note adds:
"...then ask the AI Analyst 'Why did revenue decrease?'"

``data/sales_2026_09_09.csv`` already exists (Phase 6's generator writes it
there deliberately, to be dropped in for this exact demo -
``docs/dataset-design.md``). This script does the dropping and the asking:
copy it into ``data/incoming/`` (copy, not move, so re-running the demo
doesn't need to regenerate the dataset), run it through the orchestrator
end to end (ingest -> validate -> Alteryx -> ETL -> DQ -> anomalies ->
drift -> report -> alert - every stage FR-28 requires with **no manual
stage execution**), then ask the AI Analyst the demo's own question.

Needs a running PostgreSQL (``python scripts/apply_schema.py`` first if the
schema isn't applied yet). Usage::

    docker compose -f config/docker-compose.postgres.yml up -d
    python scripts/apply_schema.py
    python scripts/run_final_demo.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEMO_FILE = PROJECT_ROOT / "data" / "sales_2026_09_09.csv"
DEMO_QUESTION = "Why did revenue decrease?"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/run_final_demo.py",
        description="Drop sales_2026_09_09.csv into data/incoming/, run the "
                    "pipeline end to end, then ask the AI Analyst why revenue changed",
    )
    parser.add_argument(
        "--question", default=DEMO_QUESTION,
        help=f"question to ask the AI Analyst afterward (default: {DEMO_QUESTION!r})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not DEMO_FILE.is_file():
        print(f"[error] expected {DEMO_FILE} - run scripts/generate_dataset.py first")
        return 2

    from src.ai_analyst import answer_question
    from src.config import get_paths
    from src.database import Database, DatabaseError
    from src.orchestrator import run_file

    paths = get_paths()
    paths.ensure()
    target = paths.incoming / DEMO_FILE.name
    target.write_bytes(DEMO_FILE.read_bytes())
    print(f"[ok] copied {DEMO_FILE.name} -> {target}")

    print("[run] processing through the full pipeline (no manual stage execution)...")
    code = run_file(target)
    if code not in (0, 9):
        print(f"[error] pipeline run did not complete cleanly (exit code {code})")
        return code

    try:
        db = Database()
    except DatabaseError as exc:
        print(f"[error] could not reach the database to ask the AI Analyst: "
              f"{exc.__class__.__name__}")
        return 4

    print(f"\n[ask] AI Analyst: {args.question}")
    answer = answer_question(db, args.question)
    print(f"\nSummary ({answer.engine}, confidence {answer.confidence_band}):")
    print(f"  {answer.summary}")
    if answer.root_cause:
        print(f"\nRoot cause:\n  {answer.root_cause}")
    if answer.impact:
        print(f"\nImpact:\n  {answer.impact}")
    if answer.recommendation:
        print(f"\nRecommendation:\n  {answer.recommendation}")
    print("\nEvidence:")
    for line in answer.evidence:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
