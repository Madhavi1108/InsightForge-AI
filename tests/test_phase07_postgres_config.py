"""Phase 7 (spec Phase 14) - PostgreSQL installation & configuration.

Everything here runs with **no Docker and no database**. The single live
connection check is skipped unless ``INSIGHTFORGE_PG_INTEGRATION=1`` and a
reachable PostgreSQL (development rule 1 - never fake).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy.engine import make_url

from src import config as cfg
from src.config import PostgresSettings, get_settings, is_placeholder, load_env

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = PROJECT_ROOT / "config" / "docker-compose.postgres.yml"
INITDB_PATH = PROJECT_ROOT / "config" / "postgres" / "initdb" / "01_bootstrap.sql"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# src/config.py - PostgresSettings
# --------------------------------------------------------------------------- #
def test_from_env_reads_discrete_parts(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "forge")
    monkeypatch.setenv("POSTGRES_USER", "forge_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")

    s = PostgresSettings.from_env()

    assert (s.host, s.port, s.db, s.user, s.password) == (
        "db.example", 6543, "forge", "forge_user", "s3cret",
    )
    assert isinstance(s.port, int)


def test_from_env_accepts_explicit_mapping():
    s = PostgresSettings.from_env(
        {
            "POSTGRES_HOST": "h", "POSTGRES_PORT": "5432", "POSTGRES_DB": "d",
            "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p",
        }
    )
    assert s.host == "h" and s.db == "d" and s.user == "u"


def test_url_is_valid_and_round_trips():
    s = PostgresSettings("localhost", 5432, "insightforge", "insightforge", "pw")
    parsed = make_url(s.url)
    assert parsed.drivername == "postgresql+psycopg2"
    assert parsed.host == "localhost"
    assert parsed.port == 5432
    assert parsed.database == "insightforge"
    assert parsed.username == "insightforge"
    assert parsed.password == "pw"


def test_url_encodes_special_characters_and_hides_nothing():
    raw = "p@ss/w:rd#1"
    s = PostgresSettings("localhost", 5432, "db", "user", raw)
    # special chars are percent-encoded, so the raw password never appears verbatim
    assert raw not in s.url
    assert "%40" in s.url  # '@'
    # but the URL still decodes back to the true password
    assert make_url(s.url).password == raw


def test_safe_url_masks_the_password():
    s = PostgresSettings("localhost", 5432, "db", "user", "supersecret")
    assert "supersecret" not in s.safe_url
    assert "***" in s.safe_url
    assert s.safe_url.startswith("postgresql+psycopg2://user:")


def test_libpq_dsn_has_all_keywords():
    s = PostgresSettings("h", 5432, "d", "u", "p")
    dsn = s.libpq_dsn
    for kw in ("host=h", "port=5432", "dbname=d", "user=u", "password=p"):
        assert kw in dsn


def test_database_url_override_wins(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://ovr_user:ovr_pw@ovr_host:7777/ovr_db",
    )
    monkeypatch.setenv("POSTGRES_HOST", "ignored")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored")

    s = PostgresSettings.from_env()

    assert (s.host, s.port, s.db, s.user, s.password) == (
        "ovr_host", 7777, "ovr_db", "ovr_user", "ovr_pw",
    )


# --------------------------------------------------------------------------- #
# placeholder handling
# --------------------------------------------------------------------------- #
def test_is_placeholder():
    tmpl = PostgresSettings("h", 5432, "d", "u", cfg.PLACEHOLDER_PASSWORD)
    real = PostgresSettings("h", 5432, "d", "u", "an-actual-password")
    assert is_placeholder(tmpl) is True
    assert is_placeholder(real) is False


def test_get_settings_raises_on_placeholder_in_production(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", cfg.PLACEHOLDER_PASSWORD)
    monkeypatch.setenv("INSIGHTFORGE_ENV", "production")

    with pytest.raises(RuntimeError) as excinfo:
        get_settings()
    # the message must not leak the password value
    assert cfg.PLACEHOLDER_PASSWORD not in str(excinfo.value)


def test_get_settings_only_warns_on_placeholder_in_development(monkeypatch, caplog):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", cfg.PLACEHOLDER_PASSWORD)
    monkeypatch.setenv("INSIGHTFORGE_ENV", "development")

    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = get_settings()

    assert is_placeholder(s)
    assert any("placeholder" in r.message for r in caplog.records)


def test_load_env_missing_file_is_safe(tmp_path):
    assert load_env(tmp_path / "nope.env") is False


# --------------------------------------------------------------------------- #
# config/docker-compose.postgres.yml
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_phase07_files_exist():
    for p in (
        COMPOSE_PATH,
        INITDB_PATH,
        PROJECT_ROOT / "src" / "config.py",
        PROJECT_ROOT / "scripts" / "postgres.py",
        PROJECT_ROOT / "docs" / "database-setup.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_compose_postgres_service_uses_pinned_16_image(compose):
    svc = compose["services"]["postgres"]
    assert svc["image"].startswith("postgres:16")


def test_compose_has_no_literal_secrets():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "change_me" not in text
    # credentials must be interpolated, never written inline
    svc = yaml.safe_load(text)["services"]["postgres"]["environment"]
    for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        val = svc[key]
        assert val.startswith("${") and val.endswith("}"), f"{key} is not interpolated"


def test_compose_persists_data_in_named_volume(compose):
    svc = compose["services"]["postgres"]
    mounts = svc["volumes"]
    assert any(m.endswith(":/var/lib/postgresql/data") for m in mounts)
    data_vol = next(m.split(":")[0] for m in mounts if m.endswith(":/var/lib/postgresql/data"))
    assert data_vol in compose["volumes"]


def test_compose_mounts_initdb_scripts(compose):
    mounts = compose["services"]["postgres"]["volumes"]
    assert any("/docker-entrypoint-initdb.d" in m for m in mounts)


def test_compose_healthcheck_uses_pg_isready(compose):
    test = compose["services"]["postgres"]["healthcheck"]["test"]
    assert any("pg_isready" in part for part in test)


def test_compose_port_is_env_driven(compose):
    ports = compose["services"]["postgres"]["ports"]
    assert any("${POSTGRES_PORT" in str(p) for p in ports)


def test_compose_pgadmin_is_behind_tools_profile(compose):
    pgadmin = compose["services"]["pgadmin"]
    assert pgadmin["profiles"] == ["tools"]


# --------------------------------------------------------------------------- #
# initdb bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_sets_utc_and_is_minimal():
    sql = INITDB_PATH.read_text(encoding="utf-8").lower()
    assert "set timezone to 'utc'" in sql
    # schema/tables are Phase 8 - the bootstrap must not create them
    assert "create table" not in sql


# --------------------------------------------------------------------------- #
# optional live connection check
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
def test_live_connection_and_utc():
    psycopg2 = pytest.importorskip("psycopg2")
    s = PostgresSettings.from_env()
    conn = psycopg2.connect(s.libpq_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
            cur.execute("SHOW timezone")
            assert cur.fetchone()[0] == "UTC"
    finally:
        conn.close()
