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


def inject_theme_css() -> None:
    """The dark navy/electric-blue/AI-indigo design system
    (``docs/UI_UX_REDESIGN.md``) - one ``<style>`` block, injected here so
    every page gets it for free via :func:`require_database` without
    editing any of the 14 page files individually. Pure CSS on top of
    ``.streamlit/config.toml``'s base theme - no new widgets, nothing that
    could change page structure or break the existing AppTest/Playwright
    suites, which assert on content and behavior, never on style."""
    st.markdown(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
    --background: #07111F;
    --surface: #111E30;
    --surface-elevated: #142238;
    --border: #1B2942;
    --primary: #3B82F6;
    --secondary: #22D3EE;
    --accent-ai: #8B5CF6;
    --success: #22C55E;
    --warning: #F59E0B;
    --danger: #EF4444;
    --text-primary: #E7ECF5;
    --text-secondary: #A9B4C6;
    --muted: #6B7A93;
}

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* Subtle layered background - no animation. */
[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(ellipse 80% 50% at 15% -10%, rgba(59, 130, 246, 0.10), transparent),
        radial-gradient(ellipse 60% 40% at 90% 0%, rgba(139, 92, 246, 0.08), transparent),
        var(--background);
}

/* Sidebar surface - Streamlit 1.40.1 has no [theme.sidebar] config key,
   so this is CSS-only. */
[data-testid="stSidebar"] {
    background: #0B1626;
    border-right: 1px solid var(--border);
}
[data-testid="stSidebarNav"] a[aria-current="page"] {
    background: rgba(59, 130, 246, 0.15);
    border-radius: 6px;
}

/* KPI tiles (st.metric) as cards. */
[data-testid="stMetric"] {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.1rem;
}
[data-testid="stMetricLabel"] { color: var(--text-secondary); }
[data-testid="stMetricValue"] {
    color: var(--text-primary);
    font-weight: 700;
    font-size: 1.5rem;
    white-space: normal;
    overflow-wrap: break-word;
}

/* Expanders / dataframes as consistent cards. */
[data-testid="stExpander"] {
    background: var(--surface);
    border: 1px solid var(--border) !important;
    border-radius: 10px;
}
[data-testid="stDataFrame"] {
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
}

/* Buttons - primary electric-blue action. */
[data-testid="stButton"] button {
    border-radius: 8px;
    font-weight: 600;
    border: 1px solid var(--border);
}
[data-testid="stButton"] button[kind="primary"] {
    background: var(--primary);
    border: none;
}

/* Status alerts - color is never the only signal; Streamlit already
   pairs each with an icon, this just keeps the left-border consistent
   with the token palette above. */
[data-testid="stAlert"] { border-radius: 8px; border-left: 4px solid var(--muted); }
[data-testid="stAlert"][data-baseweb="notification"] p { color: var(--text-primary); }

/* st.caption / small text - the color-contrast fix. Streamlit's default
   (#83858c on white) measured 3.68:1, below the 4.5:1 WCAG AA minimum
   (docs/CHROME_E2E_AUDIT.md sec 6). --text-secondary on the dark
   background here clears that bar with real margin. */
[data-testid="stCaptionContainer"], small, .stCaption { color: var(--text-secondary) !important; }

/* AI-flavored pages opt in via st.container(key="ai-page-accent") (see
   9_AI_Analyst.py / 11_Recommendations.py). */
.st-key-ai-page-accent {
    border-left: 3px solid var(--accent-ai);
    padding-left: 0.75rem;
}

/* Overview's "Insight Pulse" hero card - st.container(key="insightforge-hero",
   border=True) gives its wrapper a stable "st-key-insightforge-hero" class. */
.st-key-insightforge-hero {
    background: linear-gradient(135deg, rgba(59, 130, 246, 0.10), rgba(139, 92, 246, 0.08));
    border-radius: 12px;
}
</style>
""",
        unsafe_allow_html=True,
    )


def require_database() -> Database:
    """The cached :class:`Database`, or a friendly ``st.error`` +
    ``st.stop()`` when it can't be reached - every page's first line."""
    inject_theme_css()
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


def valid_report_path(path_str: str | None) -> Path | None:
    """The resolved :class:`Path` for a non-empty file, or ``None``.

    Used by the Reports page to decide whether to render an
    ``st.download_button`` - never for a report that wasn't actually
    generated (a missing key, a relative path that no longer resolves, or a
    zero-byte file all read as "not available" rather than a broken
    button)."""
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return path


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
