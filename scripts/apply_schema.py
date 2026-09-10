#!/usr/bin/env python3
"""InsightForge AI - apply the database schema (Phase 8 / spec Phases 15-17).

Runs ``sql/schema.sql`` (optionally ``sql/drop_schema.sql`` first) against the
PostgreSQL instance configured in ``.env``. Connection parameters come from
``src.config.PostgresSettings`` - nothing is hard-coded.

Usage
-----
    python scripts/apply_schema.py                 # create/verify all objects
    python scripts/apply_schema.py --drop          # drop then recreate (DEV ONLY)
    python scripts/apply_schema.py --dry-run       # print the plan, touch nothing

The schema is idempotent (every object uses ``IF NOT EXISTS``), so a plain run on
an already-provisioned database is a safe no-op. The pooled connection layer
(``src/database.py``) arrives in Phase 9; this one-shot DDL tool talks to
``psycopg2`` directly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = PROJECT_ROOT / "sql" / "schema.sql"
DROP_SQL = PROJECT_ROOT / "sql" / "drop_schema.sql"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _expected_objects() -> tuple[list[str], list[str]]:
    """(tables, indexes) the schema is expected to define - for the summary."""
    tables = [
        "pipeline_runs", "file_registry", "data_quality_results",
        "rejected_records", "anomalies", "recommendations", "forecast_results",
        "dim_date", "dim_customer", "dim_product", "dim_region", "fact_sales",
    ]
    return tables, []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/apply_schema.py",
        description="Apply the InsightForge AI database schema",
    )
    parser.add_argument(
        "--drop", action="store_true",
        help="run sql/drop_schema.sql before sql/schema.sql (destroys all data)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the target and the SQL that would run, then exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for path in (SCHEMA_SQL, DROP_SQL):
        if not path.is_file():
            print(f"[error] missing SQL file: {path}")
            return 2

    from src.config import get_settings

    settings = get_settings()
    schema_sql = SCHEMA_SQL.read_text(encoding="utf-8")
    drop_sql = DROP_SQL.read_text(encoding="utf-8")

    print("InsightForge AI - apply schema")
    print("=" * 40)
    print(f"target : {settings.safe_url}")
    print(f"drop   : {args.drop}")

    if args.dry_run:
        if args.drop:
            print("\n--- sql/drop_schema.sql ---\n" + drop_sql)
        print("\n--- sql/schema.sql ---\n" + schema_sql)
        print("[dry-run] nothing was executed")
        return 0

    try:
        import psycopg2
    except ModuleNotFoundError:
        print("[error] psycopg2 is not installed; cannot connect. "
              "Install requirements.txt into the project venv.")
        return 3

    try:
        conn = psycopg2.connect(settings.libpq_dsn)
    except Exception as exc:  # noqa: BLE001 - surface a sanitised message only
        print(f"[error] could not connect to PostgreSQL: {exc.__class__.__name__}")
        return 4

    tables, _ = _expected_objects()
    try:
        conn.autocommit = False
        with conn, conn.cursor() as cur:
            if args.drop:
                print("\n[run] sql/drop_schema.sql")
                cur.execute(drop_sql)
            print("[run] sql/schema.sql")
            cur.execute(schema_sql)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            present = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'"
            )
            index_count = cur.fetchone()[0]
    finally:
        conn.close()

    missing = [t for t in tables if t not in present]
    print(f"\ntables present : {len(tables) - len(missing)}/{len(tables)}")
    print(f"indexes present: {index_count}")
    if missing:
        print(f"[error] missing tables after apply: {missing}")
        return 5
    print("[ok] schema applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
