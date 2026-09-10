"""Phase 10 (spec Phases 19-20) - file ingestion & SHA-256 fingerprinting.

Almost everything here runs with no database: the fingerprint, metadata
validation, the duplicate/relocate logic (with a stub ``Database``), the watcher
handler and the orchestrator's non-DB paths. The live checks are skipped unless
``INSIGHTFORGE_PG_INTEGRATION=1`` with a running PostgreSQL and the Phase 8 schema
applied (development rule 1 - never fake).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from src import ingestion as ing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DIR_FIELDS = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase10_test__"


@pytest.fixture(autouse=True)
def _reset_caches():
    from src import config
    config.get_paths.cache_clear()
    config.get_settings.cache_clear()
    yield
    config.get_paths.cache_clear()
    config.get_settings.cache_clear()


@pytest.fixture
def paths(tmp_path):
    from src.config import PipelinePaths
    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    return p


class FakeDB:
    """Recording stand-in for src.database.Database."""

    def __init__(self, dup_row=None, next_run_id=101):
        self.dup_row = dup_row
        self.next_run_id = next_run_id
        self.calls: list[tuple[str, str, dict | None]] = []

    def healthcheck(self):
        self.calls.append(("healthcheck", "", None))

    def fetch_one(self, sql, params=None):
        self.calls.append(("fetch_one", sql, params))
        return self.dup_row

    def insert_returning(self, sql, params=None):
        self.calls.append(("insert_returning", sql, params))
        rid = self.next_run_id
        self.next_run_id += 1
        return rid

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))
        return 1

    def sql_of(self, method):
        return [c[1] for c in self.calls if c[0] == method]


def _incoming(paths, name="sales_2026_09_09.csv", body=b"a,b\n1,2\n3,4\n"):
    f = paths.incoming / name
    f.write_bytes(body)
    return f


def _ns(**kw):
    base = dict(file=None, scan=False, watch=False, scheduler=False, dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def test_phase10_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "ingestion.py",
        PROJECT_ROOT / "src" / "orchestrator.py",
        PROJECT_ROOT / "docs" / "ingestion.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.ingestion, src.orchestrator"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# sha256_file
# --------------------------------------------------------------------------- #
def test_sha256_matches_hashlib(tmp_path):
    data = b"Order_ID,Order_Date\nORD-1,2026-01-01\n"
    f = tmp_path / "a.csv"
    f.write_bytes(data)
    assert ing.sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_sha256_streams_large_file(tmp_path):
    data = os.urandom(5_000_000)
    f = tmp_path / "big.csv"
    f.write_bytes(data)
    assert ing.sha256_file(f, chunk=4096) == hashlib.sha256(data).hexdigest()


def test_sha256_identical_equal_changed_differ(tmp_path):
    a, b, c = (tmp_path / n for n in ("a.csv", "b.csv", "c.csv"))
    a.write_bytes(b"x,y\n1,2\n")
    b.write_bytes(b"x,y\n1,2\n")
    c.write_bytes(b"x,y\n1,3\n")
    assert ing.sha256_file(a) == ing.sha256_file(b)
    assert ing.sha256_file(a) != ing.sha256_file(c)


# --------------------------------------------------------------------------- #
# collect_metadata
# --------------------------------------------------------------------------- #
def test_collect_metadata_describes_a_good_csv(tmp_path):
    f = tmp_path / "sales.csv"
    f.write_bytes(b"h1,h2\n1,2\n3,4\n5,6\n")
    m = ing.collect_metadata(f)
    assert m.name == "sales.csv"
    assert m.size_bytes == f.stat().st_size
    assert m.row_count == 3
    assert m.sha256 == hashlib.sha256(f.read_bytes()).hexdigest()
    assert m.modified_at.tzinfo is not None


def test_collect_metadata_rejects_empty(tmp_path):
    f = tmp_path / "e.csv"
    f.write_bytes(b"")
    with pytest.raises(ing.IngestionError) as e:
        ing.collect_metadata(f)
    assert "empty" in e.value.reason


def test_collect_metadata_rejects_missing(tmp_path):
    with pytest.raises(ing.IngestionError):
        ing.collect_metadata(tmp_path / "nope.csv")


def test_collect_metadata_rejects_non_csv(tmp_path):
    f = tmp_path / "data.txt"
    f.write_bytes(b"hi")
    with pytest.raises(ing.IngestionError) as e:
        ing.collect_metadata(f)
    assert "type" in e.value.reason.lower()


def test_collect_metadata_rejects_directory(tmp_path):
    with pytest.raises(ing.IngestionError):
        ing.collect_metadata(tmp_path)


def test_ingestion_error_reason_has_no_absolute_path(tmp_path):
    f = tmp_path / "e.csv"
    f.write_bytes(b"")
    with pytest.raises(ing.IngestionError) as e:
        ing.collect_metadata(f)
    assert str(tmp_path) not in e.value.reason
    assert "e.csv" in e.value.reason


# --------------------------------------------------------------------------- #
# src.config.PipelinePaths
# --------------------------------------------------------------------------- #
def test_pipeline_paths_relative_vs_absolute(tmp_path):
    from src.config import PROJECT_ROOT as ROOT, PipelinePaths
    p = PipelinePaths.from_env({
        "DATA_INCOMING_DIR": "data/incoming",
        "DATA_RAW_DIR": str(tmp_path / "raw"),
    })
    assert p.incoming == ROOT / "data" / "incoming"
    assert p.raw == tmp_path / "raw"


def test_pipeline_paths_defaults_match_env_example():
    from src.config import PROJECT_ROOT as ROOT, PipelinePaths
    p = PipelinePaths.from_env({})
    assert p.processed == ROOT / "data" / "processed"
    assert p.archive == ROOT / "data" / "archive"
    assert p.reports == ROOT / "reports"
    assert p.logs == ROOT / "logs"


def test_pipeline_paths_ensure_creates_all_seven(tmp_path):
    from src.config import PipelinePaths
    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    for f in _DIR_FIELDS:
        assert (tmp_path / f).is_dir()


def test_get_paths_is_cached():
    from src import config
    config.get_paths.cache_clear()
    assert config.get_paths() is config.get_paths()


# --------------------------------------------------------------------------- #
# ingest_file  (stub Database)
# --------------------------------------------------------------------------- #
def test_ingest_new_file_moves_registers_and_stays_running(paths):
    f = _incoming(paths)
    db = FakeDB(dup_row=None, next_run_id=55)

    res = ing.ingest_file(f, db, paths)

    assert res.status == "RUNNING"
    assert res.run_id == 55
    assert res.file_hash == hashlib.sha256((paths.raw / f.name).read_bytes()).hexdigest()
    assert not f.exists()
    assert (paths.raw / f.name).is_file()
    assert (paths.archive / f.name).is_file()
    assert any("pipeline_runs" in s and "RUNNING" in s for s in db.sql_of("insert_returning"))
    assert any("file_registry" in s for s in db.sql_of("execute"))


def test_ingest_duplicate_skips_and_archives(paths):
    f = _incoming(paths)
    db = FakeDB(dup_row={"first_seen_run_id": 7}, next_run_id=88)

    res = ing.ingest_file(f, db, paths)

    assert res.status == "SKIPPED_DUPLICATE"
    assert res.duplicate_of_run_id == 7
    assert res.run_id == 88
    assert not f.exists()
    assert not (paths.raw / f.name).exists()
    assert any(p.name.startswith("sales_2026_09_09") for p in paths.archive.iterdir())
    assert db.sql_of("insert_returning") and "SKIPPED_DUPLICATE" in db.sql_of("insert_returning")[0]
    assert not any("file_registry" in s for s in db.sql_of("execute"))


def test_ingest_empty_file_raises_without_touching_db_or_file(paths):
    f = _incoming(paths, body=b"")
    db = FakeDB()

    with pytest.raises(ing.IngestionError):
        ing.ingest_file(f, db, paths)

    assert db.calls == []
    assert f.exists()
    assert not (paths.raw / f.name).exists()


# --------------------------------------------------------------------------- #
# watcher
# --------------------------------------------------------------------------- #
def test_watch_handler_dispatches_settled_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(ing, "wait_until_stable", lambda *a, **k: True)
    got: list[Path] = []
    h = ing._IncomingHandler(lambda p: got.append(p), settle=0)
    h.on_created(types.SimpleNamespace(src_path=str(tmp_path / "x.csv"), is_directory=False))
    assert got == [tmp_path / "x.csv"]


@pytest.mark.parametrize("name", ["x.tmp", "x.crdownload", ".hidden.csv", "notes.txt"])
def test_watch_handler_ignores_non_candidates(monkeypatch, tmp_path, name):
    monkeypatch.setattr(ing, "wait_until_stable", lambda *a, **k: True)
    got: list[Path] = []
    h = ing._IncomingHandler(lambda p: got.append(p), settle=0)
    h.on_created(types.SimpleNamespace(src_path=str(tmp_path / name), is_directory=False))
    assert got == []


def test_watch_handler_ignores_directory_events(monkeypatch, tmp_path):
    monkeypatch.setattr(ing, "wait_until_stable", lambda *a, **k: True)
    got: list[Path] = []
    h = ing._IncomingHandler(lambda p: got.append(p), settle=0)
    h.on_created(types.SimpleNamespace(src_path=str(tmp_path / "sub"), is_directory=True))
    assert got == []


def test_watch_handler_swallows_dispatch_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ing, "wait_until_stable", lambda *a, **k: True)

    def boom(_p):
        raise RuntimeError("bad file")

    h = ing._IncomingHandler(boom, settle=0)
    h.on_created(types.SimpleNamespace(src_path=str(tmp_path / "x.csv"), is_directory=False))
    # no exception propagated


def test_wait_until_stable_returns_false_for_missing_file(tmp_path):
    assert ing.wait_until_stable(tmp_path / "ghost.csv", settle=0, timeout=1) is False


def test_watch_real_filesystem_smoke(tmp_path):
    got: list[Path] = []
    observer = ing.watch(tmp_path, dispatch=lambda p: got.append(Path(p)),
                         block=False, settle=0.1)
    try:
        time.sleep(0.3)
        (tmp_path / "drop.csv").write_bytes(b"a,b\n1,2\n")
        deadline = time.monotonic() + 10
        while not got and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        observer.stop()
        observer.join()
    assert any(p.name == "drop.csv" for p in got)


# --------------------------------------------------------------------------- #
# orchestrator (non-DB paths)
# --------------------------------------------------------------------------- #
def _point_orchestrator_paths(monkeypatch, root):
    from src.config import PipelinePaths
    from src import orchestrator
    p = PipelinePaths(**{f: root / f for f in _DIR_FIELDS})
    p.ensure()
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    return p


def test_orchestrator_scan_empty_returns_3(monkeypatch, tmp_path, capsys):
    from src import orchestrator
    _point_orchestrator_paths(monkeypatch, tmp_path)
    assert orchestrator.main(_ns(scan=True)) == 3
    assert "no files" in capsys.readouterr().out


def test_orchestrator_scheduler_returns_3_and_mentions_phase_35(capsys):
    from src import orchestrator
    assert orchestrator.main(_ns(scheduler=True)) == 3
    assert "Phase 35" in capsys.readouterr().out


def test_orchestrator_no_mode_returns_2(capsys):
    from src import orchestrator
    assert orchestrator.main(_ns()) == 2


def test_orchestrator_dry_run_file_touches_no_db(monkeypatch, tmp_path, capsys):
    from src import orchestrator
    _point_orchestrator_paths(monkeypatch, tmp_path)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("Database constructed during --dry-run")

    monkeypatch.setattr(orchestrator, "Database", _Boom)
    assert orchestrator.main(_ns(file="whatever.csv", dry_run=True)) == 0
    assert "dry-run" in capsys.readouterr().out


def test_orchestrator_run_file_returns_4_when_db_unavailable(monkeypatch, tmp_path, capsys):
    from src import orchestrator
    p = _point_orchestrator_paths(monkeypatch, tmp_path)
    (p.incoming / "sales_x.csv").write_bytes(b"Order_ID\nORD-1\n")

    class _DownDB:
        def __init__(self, *a, **k):
            raise RuntimeError("driver missing / server down")

    monkeypatch.setattr(orchestrator, "Database", _DownDB)
    assert orchestrator.run_file(p.incoming / "sales_x.csv") == 4
    out = capsys.readouterr().out
    assert "database unavailable" in out
    assert "Traceback" not in out


def test_orchestrator_module_help_exits_zero():
    r = subprocess.run(
        [sys.executable, "-m", "src.orchestrator", "--help"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0


def test_run_pipeline_help_still_names_the_project():
    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run_pipeline.py"), "--help"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    assert "InsightForge AI" in r.stdout


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL + filesystem
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _rows(body_lines=5):
    head = b"Order_ID,Order_Date\n"
    return head + b"".join(f"ORD-{i},2026-09-09\n".encode() for i in range(body_lines))


@pg_integration
def test_ingest_registers_hash_and_dedupes(tmp_path):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src.config import PipelinePaths
    from src.database import Database

    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    db = Database()
    body = _rows()
    a, b = f"{SENTINEL}_a.csv", f"{SENTINEL}_b.csv"
    (p.incoming / a).write_bytes(body)
    (p.incoming / b).write_bytes(body)
    try:
        r1 = ing.ingest_file(p.incoming / a, db, p)
        assert r1.status == "RUNNING"
        reg = db.fetch_all(
            "SELECT file_hash, row_count FROM file_registry WHERE file_name = :n", {"n": a}
        )
        assert len(reg) == 1 and reg[0]["file_hash"] == r1.file_hash
        assert (p.raw / a).is_file() and (p.archive / a).is_file()

        r2 = ing.ingest_file(p.incoming / b, db, p)
        assert r2.status == "SKIPPED_DUPLICATE"
        assert r2.duplicate_of_run_id == r1.run_id
        assert db.scalar(
            "SELECT count(*) FROM file_registry WHERE file_hash = :h", {"h": r1.file_hash}
        ) == 1
        assert not (p.raw / b).exists()
        assert any(SENTINEL in x.name and "_b" in x.name for x in p.archive.iterdir())
    finally:
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_run_file_closes_run_as_partial(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.config import PipelinePaths
    from src.database import Database

    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_c.csv"
    (p.incoming / name).write_bytes(_rows())
    db = Database()
    try:
        assert orchestrator.run_file(p.incoming / name) == 0
        row = db.fetch_one(
            "SELECT status, error, stage_metrics FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        assert row["status"] == "PARTIAL"
        assert "not implemented yet" in row["error"]
        assert "ingest" in json.dumps(row["stage_metrics"])
        assert (p.processed / f"{SENTINEL}_c.json").is_file()
    finally:
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
