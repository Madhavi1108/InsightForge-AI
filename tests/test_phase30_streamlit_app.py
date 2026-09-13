"""Phase 30 (spec Phases 58-59, FR-22) - Streamlit application.

Every page gets an ``AppTest`` smoke check: it must never raise an
unhandled exception. With no PostgreSQL running in this dev environment,
that means every page's ``require_database()`` guard fires and renders a
friendly ``st.error`` - asserted explicitly, so this suite proves the
*failure path* actually works, not just that failure is silently
swallowed. A second, ``INSIGHTFORGE_PG_INTEGRATION=1``-gated pass asserts
real rendered content once a database is available (development rule 1:
never fake functionality).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from streamlit_app.common import fmt_currency, fmt_pct, incoming_files

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_DIR = PROJECT_ROOT / "streamlit_app"

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
no_live_db = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") == "1",
    reason="a live database is configured for this run",
)

ALL_PAGES = [STREAMLIT_DIR / "Overview.py", *sorted((STREAMLIT_DIR / "pages").glob("*.py"))]


def test_all_11_pages_exist():
    assert len(ALL_PAGES) == 11, [p.name for p in ALL_PAGES]


def test_common_module_files_exist():
    for p in (STREAMLIT_DIR / "common.py", PROJECT_ROOT / "docs" / "streamlit-app.md"):
        assert p.is_file(), f"missing {p}"


# --------------------------------------------------------------------------- #
# common.py - pure helpers
# --------------------------------------------------------------------------- #
def test_fmt_currency():
    assert fmt_currency(1234.5) == "$1,234.50"
    assert fmt_currency(None) == "-"


def test_fmt_pct():
    assert fmt_pct(12.345) == "12.35%"
    assert fmt_pct(None) == "-"


def test_incoming_files_empty_when_dir_missing(tmp_path, monkeypatch):
    import streamlit_app.common as common

    class _FakePaths:
        incoming = tmp_path / "does_not_exist"

    monkeypatch.setattr(common, "get_paths", lambda: _FakePaths())
    assert incoming_files() == []


def test_incoming_files_lists_csvs_only(tmp_path, monkeypatch):
    import streamlit_app.common as common

    (tmp_path / "a.csv").write_text("x")
    (tmp_path / "b.csv").write_text("x")
    (tmp_path / "notes.txt").write_text("x")

    class _FakePaths:
        incoming = tmp_path

    monkeypatch.setattr(common, "get_paths", lambda: _FakePaths())
    assert incoming_files() == ["a.csv", "b.csv"]


# --------------------------------------------------------------------------- #
# Every page - smoke tests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p.stem)
def test_page_renders_without_unhandled_exception(page):
    at = AppTest.from_file(str(page), default_timeout=30).run()
    assert not at.exception, [str(e) for e in at.exception]


@no_live_db
@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p.stem)
def test_page_shows_db_unavailable_error_without_live_postgres(page):
    at = AppTest.from_file(str(page), default_timeout=30).run()
    assert any("database is unavailable" in e.value for e in at.error), \
        [e.value for e in at.error]


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
@pg_integration
@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p.stem)
def test_page_renders_real_content_with_live_postgres(page):
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception
    assert not at.error
