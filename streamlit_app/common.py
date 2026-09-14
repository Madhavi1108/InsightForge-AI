"""InsightForge AI - Streamlit shared helpers (Phase 30 / spec Phases
58-59, FR-22).

DB access, formatting, and the safe-failure guard every page opens with.

**Directory naming note**: this app lives in ``streamlit_app/``, not the
spec's literal ``streamlit/`` (``docs/system-components.md``). This
project's ``pytest.ini`` sets ``pythonpath = .`` (project root on
``sys.path`` for every test run - the same mechanism that makes ``from
src.x import y`` work everywhere else). A project-root directory literally
named ``streamlit/`` would collide with the **installed** ``streamlit``
package: ``import streamlit`` and ``from streamlit.common import ...``
would resolve unpredictably (or fail outright) once the real ``streamlit``
package - a regular package, found later on ``sys.path`` - wins name
resolution over a same-named local directory. ``streamlit_app/`` sidesteps
this entirely; functionally identical to the spec's intent (one app, 11
pages), documented in ``docs/streamlit-app.md``.

Every page starts with :func:`require_database`, which never lets a page
crash on a DB outage: :meth:`~src.database.Database.ping` (already "never
raises... a boolean probe") gates every page, and a failure renders one
honest ``st.error`` and ``st.stop()``s - matching this codebase's
sanitized-error convention (``docs/security.md`` section 5) applied at the
UI layer for the first time, rather than leaking a driver traceback into
the browser.

**Controlled execution**: :func:`run_pipeline_subprocess` is the *only*
way this app ever executes code - a fixed argv list against
``run_pipeline.py``, never ``shell=True``, never a user-typed command
string (``docs/system-components.md``: "controlled actions... no
arbitrary execution"). :func:`incoming_files` is the only source of
filenames a user may pick for a ``--file`` run, so "run this file" can
never become an arbitrary path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Bootstrap: make the project root importable regardless of how this app
# is launched. `streamlit run streamlit_app/Overview.py` (or any page
# file directly) only puts that script's own directory on sys.path, not
# the project root - without this, `from src...`/`from streamlit_app...`
# would fail outside of pytest (where `pythonpath = .` already covers it).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from src.config import get_paths
from src.database import Database

PROJECT_ROOT = _PROJECT_ROOT


@st.cache_resource
def get_database() -> Database:
    return Database()


def require_database() -> Database:
    """The cached :class:`Database`, or a friendly ``st.error`` +
    ``st.stop()`` when it can't be reached - every page's first line."""
    db = get_database()
    if not db.ping():
        st.error(
            "The database is unavailable right now. Start PostgreSQL "
            "(see docs/database-setup.md) and reload this page."
        )
        st.stop()
    return db


def rows_to_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def fmt_currency(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}%"


def run_pipeline_subprocess(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """The only code-execution path in this app: a fixed argv list
    against ``run_pipeline.py``."""
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run_pipeline.py"), *args],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout, check=False,
    )


def incoming_files() -> list[str]:
    """Real filenames currently in ``data/incoming/`` - the only choices a
    user can pick for a ``--file`` run."""
    paths = get_paths()
    if not paths.incoming.is_dir():
        return []
    return sorted(p.name for p in paths.incoming.glob("*.csv"))
