#!/usr/bin/env python3
"""InsightForge AI - PostgreSQL container lifecycle helper (Phase 7 / spec Phase 14).

A thin wrapper around ``docker compose -f config/docker-compose.postgres.yml``.
Every action shells out to the real Docker CLI - nothing is simulated.

Usage
-----
    python scripts/postgres.py up [--tools]
    python scripts/postgres.py wait [--timeout 60]
    python scripts/postgres.py status
    python scripts/postgres.py logs [--follow]
    python scripts/postgres.py down [--volumes]

``wait`` polls ``pg_isready`` inside the container until it succeeds or the
timeout elapses; it exits non-zero on timeout so CI / the orchestrator (Phase 10)
can gate on database readiness. See ``docs/database-setup.md``.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "config" / "docker-compose.postgres.yml"
SERVICE = "postgres"


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=PROJECT_ROOT, text=True,
        capture_output=capture, check=False,
    )


def _settings():
    """Best-effort load of PostgresSettings for display; never fatal."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.config import get_settings

        return get_settings()
    except Exception as exc:  # pragma: no cover - display convenience only
        print(f"[warn] could not resolve src.config settings: {exc}")
        return None


def cmd_up(args: argparse.Namespace) -> int:
    compose_args = ["up", "-d"]
    if args.tools:
        compose_args = ["--profile", "tools", *compose_args]
    return _run(_compose(*compose_args)).returncode


def cmd_down(args: argparse.Namespace) -> int:
    compose_args = ["--profile", "tools", "down"]
    if args.volumes:
        compose_args.append("--volumes")
    return _run(_compose(*compose_args)).returncode


def cmd_status(_args: argparse.Namespace) -> int:
    rc = _run(_compose("ps")).returncode
    settings = _settings()
    if settings is not None:
        print(f"\nconfigured URL: {settings.safe_url}")
    return rc


def cmd_logs(args: argparse.Namespace) -> int:
    compose_args = ["logs", SERVICE]
    if args.follow:
        compose_args.insert(1, "--follow")
    return _run(_compose(*compose_args)).returncode


def cmd_wait(args: argparse.Namespace) -> int:
    settings = _settings()
    user = settings.user if settings else "insightforge"
    db = settings.db if settings else "insightforge"
    probe = _compose(
        "exec", "-T", SERVICE,
        "pg_isready", "-U", user, "-d", db,
    )
    deadline = time.monotonic() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        result = _run(probe, capture=True)
        if result.returncode == 0:
            print(f"[ok] postgres accepting connections (after {attempt} probe(s))")
            return 0
        if time.monotonic() >= deadline:
            print(
                f"[error] postgres not ready after {args.timeout}s "
                f"({attempt} probe(s)): {(result.stdout or result.stderr or '').strip()}"
            )
            return 1
        time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/postgres.py",
        description="InsightForge AI - PostgreSQL container lifecycle helper",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="start the postgres container (detached)")
    p_up.add_argument("--tools", action="store_true", help="also start pgAdmin")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop and remove containers")
    p_down.add_argument("--volumes", action="store_true", help="also delete the data volume")
    p_down.set_defaults(func=cmd_down)

    p_status = sub.add_parser("status", help="show container status + configured URL")
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="show postgres logs")
    p_logs.add_argument("--follow", action="store_true", help="stream logs")
    p_logs.set_defaults(func=cmd_logs)

    p_wait = sub.add_parser("wait", help="block until postgres accepts connections")
    p_wait.add_argument("--timeout", type=int, default=60, help="seconds (default 60)")
    p_wait.set_defaults(func=cmd_wait)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not COMPOSE_FILE.is_file():
        print(f"[error] compose file not found: {COMPOSE_FILE}")
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
