"""Phase 12 (spec Phases 23-24) - Alteryx ingestion & data-quality workflows.

No test here needs a real Alteryx install: `AlteryxSettings.is_configured()`
only turns true for a genuine executable path, so every unit test below
exercises the Python fallback - and asserts it reports itself honestly as
unverified, never faked. Orchestrator wiring uses a stub ``Database``
(``FakeDB``), matching ``tests/test_phase11_schema_validation.py``.
"""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import pytest

from src.alteryx import (
    AlteryxExecutionError,
    run_data_quality_workflow,
    run_ingestion_workflow,
)
from src.config import AlteryxSettings
from src.validation import validate_csv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALTERYX_DIR = PROJECT_ROOT / "alteryx"
_DIR_FIELDS = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")

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
    "Discount": "0.2", "Revenue": "101185.77", "Cost": "111418.92",
    "Profit": "-10233.15", "Payment_Method": "Credit Card",
    "Shipping_Days": "4.0", "Order_Status": "Completed",
    "Return_Status": "Not Returned",
}


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


def _mut(**overrides):
    row = dict(GOOD_ROW)
    row.update(overrides)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=COLUMNS_22)


def _write_csv(tmp_path, rows, name="sample.csv"):
    p = tmp_path / name
    _frame(*rows).to_csv(p, index=False)
    return p


# --------------------------------------------------------------------------- #
# files exist / parse
# --------------------------------------------------------------------------- #
def test_phase12_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "alteryx.py",
        ALTERYX_DIR / "01_ingestion.yxmd",
        ALTERYX_DIR / "02_data_quality.yxmd",
        PROJECT_ROOT / "docs" / "alteryx-workflows.md",
    ):
        assert p.is_file(), f"missing {p}"


@pytest.mark.parametrize("name", ["01_ingestion.yxmd", "02_data_quality.yxmd"])
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
        [__import__("sys").executable, "-c", "import src.alteryx"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# AlteryxSettings
# --------------------------------------------------------------------------- #
def test_settings_default_is_unconfigured(monkeypatch):
    monkeypatch.delenv("ALTERYX_ENGINE_CMD", raising=False)
    s = AlteryxSettings.from_env()
    assert s.engine_cmd is None
    assert not s.is_configured()


def test_settings_blank_env_is_unconfigured(monkeypatch):
    monkeypatch.setenv("ALTERYX_ENGINE_CMD", "   ")
    s = AlteryxSettings.from_env()
    assert s.engine_cmd is None
    assert not s.is_configured()


def test_settings_nonexistent_path_is_unconfigured(monkeypatch, tmp_path):
    fake_exe = tmp_path / "AlteryxEngineCmd.exe"  # never created
    monkeypatch.setenv("ALTERYX_ENGINE_CMD", str(fake_exe))
    s = AlteryxSettings.from_env()
    assert s.engine_cmd == str(fake_exe)
    assert not s.is_configured()


def test_settings_real_file_path_is_configured(monkeypatch, tmp_path):
    fake_exe = tmp_path / "AlteryxEngineCmd.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("ALTERYX_ENGINE_CMD", str(fake_exe))
    s = AlteryxSettings.from_env()
    assert s.is_configured()


def test_settings_workflow_dir_defaults_to_alteryx(monkeypatch):
    monkeypatch.delenv("ALTERYX_WORKFLOW_DIR", raising=False)
    s = AlteryxSettings.from_env()
    assert s.workflow_dir == ALTERYX_DIR
    assert s.workflow_path("01_ingestion.yxmd") == ALTERYX_DIR / "01_ingestion.yxmd"


# --------------------------------------------------------------------------- #
# Python fallback - ingestion workflow
# --------------------------------------------------------------------------- #
def test_ingestion_workflow_fallback_on_clean_file(tmp_path):
    csv = _write_csv(tmp_path, [GOOD_ROW, GOOD_ROW])
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)
    result = run_ingestion_workflow(csv, settings)
    assert result.workflow == "01_ingestion"
    assert result.engine == "python_fallback"
    assert result.verified is False
    assert result.error is None
    assert result.summary == {"columns_ok": True, "row_count": 2}


def test_ingestion_workflow_fallback_flags_bad_columns(tmp_path):
    df = _frame(GOOD_ROW).drop(columns=["Discount"])
    csv = tmp_path / "broken.csv"
    df.to_csv(csv, index=False)
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)
    result = run_ingestion_workflow(csv, settings)
    assert result.summary["columns_ok"] is False


# --------------------------------------------------------------------------- #
# Python fallback - data-quality workflow (identical to Phase 11 output)
# --------------------------------------------------------------------------- #
def test_dq_workflow_fallback_matches_validate_csv_directly(tmp_path):
    rows = [GOOD_ROW, _mut(Quantity="0"), GOOD_ROW, _mut(Category="Gadgets"), GOOD_ROW]
    csv = _write_csv(tmp_path, rows)
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)

    result = run_data_quality_workflow(csv, settings=settings)
    direct = validate_csv(csv)

    assert result.engine == "python_fallback"
    assert result.verified is False
    assert result.summary["rows_checked"] == direct.rows_checked
    assert result.summary["rows_valid"] == direct.rows_valid
    assert result.summary["rows_rejected"] == direct.rows_rejected
    assert result.summary["rows_rejected"] == 2
    assert result.summary["violations_by_category"].get("out_of_range") == 1
    assert result.summary["violations_by_category"].get("not_in_allowed_values") == 1


def test_dq_workflow_fallback_clean_file_has_no_violations(tmp_path):
    csv = _write_csv(tmp_path, [GOOD_ROW, GOOD_ROW, GOOD_ROW])
    settings = AlteryxSettings(engine_cmd=None, workflow_dir=ALTERYX_DIR)
    result = run_data_quality_workflow(csv, settings=settings)
    assert result.summary["rows_rejected"] == 0
    assert result.summary["violations_by_category"] == {}
    assert result.summary["violations_by_dimension"] == {}


# --------------------------------------------------------------------------- #
# Engine failure -> fallback (never faked)
# --------------------------------------------------------------------------- #
def test_engine_invocation_failure_falls_back_and_reports_error(tmp_path, monkeypatch):
    fake_exe = tmp_path / "AlteryxEngineCmd.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    settings = AlteryxSettings(engine_cmd=str(fake_exe), workflow_dir=ALTERYX_DIR)
    csv = _write_csv(tmp_path, [GOOD_ROW])

    def _boom(*a, **k):
        raise subprocess.SubprocessError("engine crashed")

    import src.alteryx as alteryx_mod
    monkeypatch.setattr(alteryx_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subprocess, "run", _boom)

    result = run_ingestion_workflow(csv, settings)
    assert result.engine == "python_fallback"
    assert result.verified is False
    assert result.error is not None
    assert "01_ingestion.yxmd" in result.error


def test_engine_success_is_reported_as_verified(tmp_path, monkeypatch):
    fake_exe = tmp_path / "AlteryxEngineCmd.exe"
    fake_exe.write_text("stub", encoding="utf-8")
    settings = AlteryxSettings(engine_cmd=str(fake_exe), workflow_dir=ALTERYX_DIR)
    csv = _write_csv(tmp_path, [GOOD_ROW])

    def _ok(*a, **k):
        return subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _ok)

    result = run_ingestion_workflow(csv, settings)
    assert result.engine == "alteryx"
    assert result.verified is True
    assert result.error is None


# --------------------------------------------------------------------------- #
# orchestrator integration (stub Database, matches Phase 11's FakeDB pattern)
# --------------------------------------------------------------------------- #
class FakeDB:
    def __init__(self, dup_row=None, next_run_id=700):
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


def _stub_etl_result():
    from src.etl import EtlResult
    return EtlResult(engine="python_fallback", verified=False, seconds=0.0,
                     summary={"rows_loaded": 0, "customers_upserted": 0,
                              "products_upserted": 0, "regions_upserted": 0,
                              "dates_upserted": 0})


def _stub_dq_report(rows):
    from src.data_quality import DIMENSIONS, DqReport, DimensionResult
    return DqReport(
        dimensions=tuple(DimensionResult(dimension=d, score=100.0, passed=True,
                                         records_checked=len(rows), records_failed=0)
                         for d in DIMENSIONS),
        overall_score=100.0, gate="PASS", rows_checked=len(rows),
    )


def _run_file(monkeypatch, tmp_path, rows, name="sales_2026_02_24.csv"):
    from src import orchestrator
    p = _paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    fake = FakeDB()
    monkeypatch.setattr(orchestrator, "Database", lambda *a, **k: fake)
    # Phase 13's real loader needs a genuine DB connection (db.transaction());
    # these tests exercise only the Phase 12 Alteryx stage against a stub DB.
    monkeypatch.setattr(orchestrator, "run_sales_etl_workflow",
                        lambda *a, **k: _stub_etl_result())
    # Phase 14's DQ engine lands after the ETL stage; these fixtures reuse
    # GOOD_ROW verbatim (a genuine Uniqueness violation to a real DQ engine),
    # so give it a canned PASS report to isolate the Alteryx stage.
    monkeypatch.setattr(orchestrator, "score_file", lambda *a, **k: _stub_dq_report(rows))
    _frame(*rows).to_csv(p.incoming / name, index=False)
    code = orchestrator.run_file(p.incoming / name)
    return code, fake, p


def test_run_file_adds_alteryx_stage_metrics_and_succeeds(monkeypatch, tmp_path):
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW, GOOD_ROW])
    assert code == 0
    updates = [c[2]["sm"] for c in fake.calls if c[0] == "execute" and c[2] and "sm" in c[2]]
    assert updates
    last = updates[-1]
    assert "alteryx_ingestion" in last and "alteryx_dq" in last
    assert '"engine": "python_fallback"' in last
    assert any(c[2].get("status") == "SUCCESS" for c in fake.calls
              if c[0] == "execute" and c[2])


def test_run_file_processed_summary_includes_alteryx_keys(monkeypatch, tmp_path):
    import json
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW])
    assert code == 0
    files = list(p.processed.glob("*.json"))
    assert files
    summary = json.loads(files[0].read_text(encoding="utf-8"))
    assert summary["alteryx_ingestion"]["engine"] == "python_fallback"
    assert summary["alteryx_dq"]["engine"] == "python_fallback"


def test_run_file_workflow_crash_closes_failed_with_exit_7(monkeypatch, tmp_path):
    from src import orchestrator

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "run_ingestion_workflow", _boom)
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW])
    assert code == 7
    assert any("FAILED" in s for s in fake.sql_of("execute"))
