"""Phase 13 (spec Phases 25-26) - Alteryx sales / customer / product ETL.

Pure helpers (date-dimension math, Revenue/Profit re-derivation, the valid-
rows filter) and the ``.yxmd`` files need no database. The real loader
(``src.etl.load_valid_rows``) needs a genuine SQLAlchemy connection
(``db.transaction()``) to enforce dimension-before-fact FK ordering, so it is
covered by the ``INSIGHTFORGE_PG_INTEGRATION=1``-gated tests only - a stub
``Database`` cannot fake relational integrity (development rule 1: never
fake functionality). Orchestrator control-flow (stub DB) tests monkeypatch
``run_sales_etl_workflow`` itself, the same technique
``tests/test_phase12_alteryx_workflows.py`` uses for its crash test.
"""
from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.etl import (
    EtlLoadError,
    EtlResult,
    date_dim_row,
    recompute_revenue_profit,
    run_sales_etl_workflow,
    valid_rows_frame,
)
from src.validation import validate_frame

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALTERYX_DIR = PROJECT_ROOT / "alteryx"
_DIR_FIELDS = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase13_test__"

COLUMNS_22 = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]

GOOD_ROW = {
    "Order_ID": "ORD-00000001", "Order_Date": "2026-02-24",
    "Customer_ID": "CUST-000173", "Customer_Name": "Neha Rao",
    "Customer_Segment": "Corporate", "Product_ID": "PROD-00011",
    "Product_Name": "Camera Max 882", "Category": "Electronics",
    "Sub_Category": "Camera", "Region": "West", "State": "Maharashtra",
    "City": "Mumbai", "Quantity": "1", "Unit_Price": "126482.21",
    "Discount": "0.2", "Revenue": "999999.99", "Cost": "111418.92",
    "Profit": "999999.99", "Payment_Method": "Credit Card",
    "Shipping_Days": "4.0", "Order_Status": "Completed",
    "Return_Status": "Not Returned",
}


def _mut(**overrides):
    row = dict(GOOD_ROW)
    row.update(overrides)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=COLUMNS_22)


@pytest.fixture(autouse=True)
def _reset_caches():
    from src import config, validation
    for fn in (config.get_paths, config.get_settings, config.get_alteryx_settings,
              validation.get_contract):
        fn.cache_clear()
    yield
    for fn in (config.get_paths, config.get_settings, config.get_alteryx_settings,
              validation.get_contract):
        fn.cache_clear()


# --------------------------------------------------------------------------- #
# files exist / parse
# --------------------------------------------------------------------------- #
def test_phase13_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "etl.py",
        ALTERYX_DIR / "03_sales_etl.yxmd",
        ALTERYX_DIR / "04_customer_product_etl.yxmd",
        PROJECT_ROOT / "docs" / "star-schema-etl.md",
    ):
        assert p.is_file(), f"missing {p}"


@pytest.mark.parametrize("name", ["03_sales_etl.yxmd", "04_customer_product_etl.yxmd"])
def test_yxmd_is_well_formed_xml_with_expected_tools(name):
    tree = ET.parse(ALTERYX_DIR / name)
    root = tree.getroot()
    assert root.tag == "AlteryxDocument"
    nodes = root.findall(".//Node")
    assert len(nodes) >= 3
    plugins = {n.find(".//GuiSettings").get("Plugin") for n in nodes}
    assert any("DbFileInput" in p for p in plugins)
    assert any("DbFileOutput" in p for p in plugins)


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.etl"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# date_dim_row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "d, quarter, day_of_week, is_weekend, is_month_end",
    [
        (date(2026, 1, 1), 1, 4, False, False),    # Thursday
        (date(2026, 2, 28), 1, 6, True, True),     # Saturday, Feb has 28 days in 2026
        (date(2026, 3, 31), 1, 2, False, True),    # Tuesday, month end
        (date(2026, 4, 1), 2, 3, False, False),    # Wednesday, Q2 starts
        (date(2026, 9, 6), 3, 7, True, False),     # Sunday
    ],
)
def test_date_dim_row_fields(d, quarter, day_of_week, is_weekend, is_month_end):
    row = date_dim_row(d)
    assert row["date_key"] == d
    assert row["year"] == d.year
    assert row["quarter"] == quarter
    assert row["month"] == d.month
    assert row["month_name"] == d.strftime("%B")
    assert row["day"] == d.day
    assert row["day_of_week"] == day_of_week
    assert row["day_name"] == d.strftime("%A")
    assert row["week_of_year"] == d.isocalendar()[1]
    assert row["is_weekend"] is is_weekend
    assert row["is_month_end"] is is_month_end


# --------------------------------------------------------------------------- #
# recompute_revenue_profit (FR-05 - never trust the CSV)
# --------------------------------------------------------------------------- #
def test_recompute_revenue_profit_matches_formula():
    revenue, profit = recompute_revenue_profit("2", "100.00", "0.10", "150.00")
    assert revenue == Decimal("180.00")  # 2 * 100 * 0.9
    assert profit == Decimal("30.00")    # 180 - 150


def test_recompute_revenue_profit_ignores_bad_csv_values():
    # GOOD_ROW's own Revenue/Profit columns are deliberately wrong (999999.99);
    # the recomputed values must ignore them entirely.
    revenue, profit = recompute_revenue_profit(
        GOOD_ROW["Quantity"], GOOD_ROW["Unit_Price"], GOOD_ROW["Discount"], GOOD_ROW["Cost"])
    assert revenue != Decimal(GOOD_ROW["Revenue"])
    assert profit != Decimal(GOOD_ROW["Profit"])
    assert revenue == Decimal("101185.77")  # 1 * 126482.21 * 0.8, rounded
    assert profit == revenue - Decimal(GOOD_ROW["Cost"])


# --------------------------------------------------------------------------- #
# valid_rows_frame
# --------------------------------------------------------------------------- #
def test_valid_rows_frame_drops_only_rejected_rows(tmp_path):
    rows = [GOOD_ROW, _mut(Quantity="0"), GOOD_ROW, _mut(Category="Gadgets"), GOOD_ROW]
    csv = tmp_path / "mixed.csv"
    _frame(*rows).to_csv(csv, index=False)
    vres = validate_frame(_frame(*rows))
    assert vres.rows_rejected == 2

    df = valid_rows_frame(csv, vres)
    assert len(df) == 3
    assert set(df["Order_ID"]) == {GOOD_ROW["Order_ID"]}


def test_valid_rows_frame_all_valid_returns_full_frame(tmp_path):
    rows = [GOOD_ROW, GOOD_ROW, GOOD_ROW]
    csv = tmp_path / "clean.csv"
    _frame(*rows).to_csv(csv, index=False)
    vres = validate_frame(_frame(*rows))
    df = valid_rows_frame(csv, vres)
    assert len(df) == 3


# --------------------------------------------------------------------------- #
# run_sales_etl_workflow - engine selection / retry-then-fail (no real DB)
# --------------------------------------------------------------------------- #
def test_python_fallback_success_reports_engine_and_verified(tmp_path, monkeypatch):
    from src import etl as etl_mod
    from src.config import AlteryxSettings

    csv = tmp_path / "clean.csv"
    _frame(GOOD_ROW).to_csv(csv, index=False)
    vres = validate_frame(_frame(GOOD_ROW))
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)

    monkeypatch.setattr(etl_mod, "load_valid_rows",
                        lambda *a, **k: {"rows_loaded": 1, "customers_upserted": 1,
                                         "products_upserted": 1, "regions_upserted": 1,
                                         "dates_upserted": 1})

    result = run_sales_etl_workflow(csv, run_id=1, db=object(), vres=vres, settings=settings)
    assert isinstance(result, EtlResult)
    assert result.engine == "python_fallback"
    assert result.verified is False
    assert result.summary["rows_loaded"] == 1
    assert result.error is None


def test_load_failure_retries_then_raises_etl_load_error(tmp_path, monkeypatch):
    from src import etl as etl_mod
    from src.config import AlteryxSettings

    csv = tmp_path / "clean.csv"
    _frame(GOOD_ROW).to_csv(csv, index=False)
    vres = validate_frame(_frame(GOOD_ROW))
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)

    attempts = []

    def _boom(*a, **k):
        attempts.append(1)
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(etl_mod, "load_valid_rows", _boom)
    monkeypatch.setattr(etl_mod.time, "sleep", lambda *_: None)

    with pytest.raises(EtlLoadError):
        run_sales_etl_workflow(csv, run_id=1, db=object(), vres=vres, settings=settings)
    assert len(attempts) == etl_mod.MAX_LOAD_ATTEMPTS


def test_engine_invocation_failure_degrades_to_python_fallback(tmp_path, monkeypatch):
    from src import etl as etl_mod
    from src.config import AlteryxSettings

    fake_exe = tmp_path / "AlteryxEngineCmd.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    settings = AlteryxSettings(engine_cmd=str(fake_exe), workflow_dir=ALTERYX_DIR)
    csv = tmp_path / "clean.csv"
    _frame(GOOD_ROW).to_csv(csv, index=False)
    vres = validate_frame(_frame(GOOD_ROW))

    def _boom(*a, **k):
        raise subprocess.SubprocessError("engine crashed")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(etl_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(etl_mod, "load_valid_rows",
                        lambda *a, **k: {"rows_loaded": 1, "customers_upserted": 1,
                                         "products_upserted": 1, "regions_upserted": 1,
                                         "dates_upserted": 1})

    result = run_sales_etl_workflow(csv, run_id=1, db=object(), vres=vres, settings=settings)
    assert result.engine == "python_fallback"
    assert result.verified is False
    assert result.error is not None


# --------------------------------------------------------------------------- #
# orchestrator control flow (stub Database - never exercises the real loader)
# --------------------------------------------------------------------------- #
class FakeDB:
    def __init__(self, dup_row=None, next_run_id=900):
        self.dup_row = dup_row
        self.next_run_id = next_run_id
        self.calls: list[tuple] = []

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

    def execute_many(self, sql, seq):
        seq = list(seq)
        self.calls.append(("execute_many", sql, seq))
        return len(seq)

    def sql_of(self, method):
        return [c[1] for c in self.calls if c[0] == method]

    def params_of(self, method):
        return [c[2] for c in self.calls if c[0] == method]


def _paths(tmp_path):
    from src.config import PipelinePaths
    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    return p


def _run_file(monkeypatch, tmp_path, rows, name="sales_2026_02_24.csv", etl_outcome=None):
    from src import orchestrator
    p = _paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    fake = FakeDB()
    monkeypatch.setattr(orchestrator, "Database", lambda *a, **k: fake)

    if etl_outcome is None:
        etl_outcome = EtlResult(engine="python_fallback", verified=False, seconds=0.01,
                                summary={"rows_loaded": len(rows), "customers_upserted": 1,
                                         "products_upserted": 1, "regions_upserted": 1,
                                         "dates_upserted": 1})

    def _etl_stub(*a, **k):
        if isinstance(etl_outcome, Exception):
            raise etl_outcome
        return etl_outcome

    monkeypatch.setattr(orchestrator, "run_sales_etl_workflow", _etl_stub)
    # Phase 14's DQ engine lands after the ETL stage; these tests exercise
    # only stages 1-5, so give it a canned PASS report (GOOD_ROW here has
    # deliberately-wrong Revenue/Profit for Phase 13's own recompute test,
    # which would otherwise tank the real DQ score).
    from src.data_quality import DIMENSIONS, DqReport, DimensionResult
    pass_report = DqReport(
        dimensions=tuple(DimensionResult(dimension=d, score=100.0, passed=True,
                                         records_checked=len(rows), records_failed=0)
                         for d in DIMENSIONS),
        overall_score=100.0, gate="PASS", rows_checked=len(rows),
    )
    monkeypatch.setattr(orchestrator, "score_file", lambda *a, **k: pass_report)
    _frame(*rows).to_csv(p.incoming / name, index=False)
    code = orchestrator.run_file(p.incoming / name)
    return code, fake, p


def test_run_file_success_closes_success_with_etl_metrics(monkeypatch, tmp_path):
    import json
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW, GOOD_ROW])
    assert code == 0
    updates = [c[2]["sm"] for c in fake.calls if c[0] == "execute" and c[2] and "sm" in c[2]]
    assert updates and "etl" in updates[-1] and "rows_loaded" in updates[-1]
    assert any(c[2].get("status") == "SUCCESS" for c in fake.calls
              if c[0] == "execute" and c[2])
    assert not any("PARTIAL" in s for s in fake.sql_of("execute"))

    files = list(p.processed.glob("*.json"))
    assert files
    summary = json.loads(files[0].read_text(encoding="utf-8"))
    assert summary["status"] == "SUCCESS"
    assert summary["etl"]["engine"] == "python_fallback"


def test_run_file_etl_load_error_closes_failed_with_exit_8(monkeypatch, tmp_path):
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW],
                              etl_outcome=EtlLoadError("boom"))
    assert code == 8
    assert any("FAILED" in s for s in fake.sql_of("execute"))


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    return p


@pg_integration
def test_clean_file_loads_fact_sales_and_closes_success(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_clean.csv"
    gd.generate_dataset(rows=50, seed=5).to_csv(
        p.incoming / name, index=False, date_format="%Y-%m-%d"
    )
    db = Database()
    try:
        assert orchestrator.run_file(p.incoming / name) == 0
        row = db.fetch_one(
            "SELECT run_id, status, stage_metrics FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        # SUCCESS or WARNING - both mean the load succeeded; this test is
        # about the ETL load, not Phase 14's data quality scoring.
        assert row["status"] in ("SUCCESS", "WARNING")
        assert "etl" in row["stage_metrics"]
        loaded = db.scalar(
            "SELECT count(*) FROM fact_sales WHERE run_id = :r", {"r": row["run_id"]}
        )
        assert loaded == 50
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_shared_dimensions_are_not_duplicated_across_files(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()
    names = [f"{SENTINEL}_a.csv", f"{SENTINEL}_b.csv"]
    try:
        df = gd.generate_dataset(rows=80, seed=7)
        half = len(df) // 2
        df.iloc[:half].to_csv(p.incoming / names[0], index=False, date_format="%Y-%m-%d")
        for n in names[:1]:
            assert orchestrator.run_file(p.incoming / n) == 0
        before = db.scalar("SELECT count(*) FROM dim_customer")
        df.iloc[:half].to_csv(p.incoming / names[1], index=False, date_format="%Y-%m-%d")
        # same rows again (different file/hash) - dimensions must not duplicate
        assert orchestrator.run_file(p.incoming / names[1]) == 0
        after = db.scalar("SELECT count(*) FROM dim_customer")
        assert after == before
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
