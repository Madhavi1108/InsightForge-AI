"""Phase 11 (spec Phases 21-22) - data contract & schema validation.

The bulk of these tests need no database: the contract YAML, the validator
(structural + per-field + hierarchy), and the orchestrator's stage-3 wiring with
a stub ``Database``. The live checks are skipped unless
``INSIGHTFORGE_PG_INTEGRATION=1`` with a running PostgreSQL and the Phase 8
schema applied (development rule 1 - never fake).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.validation import validate_csv, validate_frame

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "config" / "data_contract.yaml"
_DIR_FIELDS = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase11_test__"

COLUMNS_22 = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]

# One valid row lifted from `generate_dataset.py --clean` output.
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
    for fn in (config.get_paths, config.get_settings, validation.get_contract):
        fn.cache_clear()
    yield
    for fn in (config.get_paths, config.get_settings, validation.get_contract):
        fn.cache_clear()


def _mut(**overrides):
    row = dict(GOOD_ROW)
    row.update(overrides)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=COLUMNS_22)


def _paths(tmp_path):
    from src.config import PipelinePaths
    p = PipelinePaths(**{f: tmp_path / f for f in _DIR_FIELDS})
    p.ensure()
    return p


class FakeDB:
    """Recording stand-in for src.database.Database (extends the Phase 10 stub)."""

    def __init__(self, dup_row=None, next_run_id=500):
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


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def test_phase11_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "validation.py",
        CONTRACT_PATH,
        PROJECT_ROOT / "docs" / "validation.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.validation"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# contract YAML
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def raw_contract():
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_yaml_shape(raw_contract):
    assert raw_contract["columns"] == COLUMNS_22
    assert set(raw_contract["mandatory"]) == set(COLUMNS_22) - {"Shipping_Days"}
    assert set(raw_contract["fields"]) == set(COLUMNS_22)
    assert raw_contract["allow_extra_columns"] is False
    assert set(raw_contract["hierarchies"]["category_subcategory"]) == {
        "Electronics", "Furniture", "Office Supplies", "Clothing", "Home & Kitchen",
    }
    assert set(raw_contract["hierarchies"]["region_state_city"]) == {
        "North", "South", "East", "West", "Central",
    }


def test_contract_matches_generator_constants(raw_contract):
    from scripts import generate_dataset as gd
    assert raw_contract["columns"] == gd.COLUMNS
    assert set(raw_contract["mandatory"]) == set(gd.MANDATORY_COLUMNS)
    rsc = raw_contract["hierarchies"]["region_state_city"]
    for region, states in gd.GEOGRAPHY.items():
        for state, cities in states.items():
            assert set(rsc[region][state]) == set(cities)
    cs = raw_contract["hierarchies"]["category_subcategory"]
    for category, subs in gd.CATALOG.items():
        assert set(cs[category]) == set(subs)


def test_get_contract_loads_and_caches():
    from src import validation
    validation.get_contract.cache_clear()
    c = validation.get_contract()
    assert len(c.columns) == 22
    assert len(c.mandatory) == 21
    assert c.allow_extra_columns is False
    assert "Electronics" in c.category_subcategory
    assert validation.get_contract() is c


def test_get_contract_honours_env(tmp_path, monkeypatch):
    from src import validation
    alt = tmp_path / "alt_contract.yaml"
    alt.write_text(CONTRACT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("DATA_CONTRACT_PATH", str(alt))
    validation.get_contract.cache_clear()
    assert len(validation.get_contract().columns) == 22


# --------------------------------------------------------------------------- #
# structural validation
# --------------------------------------------------------------------------- #
def test_structural_ok_for_exact_columns():
    res = validate_frame(_frame(GOOD_ROW))
    assert res.structural_ok and res.structural_errors == []
    assert res.ok


def test_structural_fail_missing_column():
    res = validate_frame(_frame(GOOD_ROW).drop(columns=["Discount"]))
    assert not res.structural_ok
    assert any("Discount" in e for e in res.structural_errors)
    assert res.row_violations == []


def test_structural_fail_extra_column():
    res = validate_frame(_frame(GOOD_ROW).assign(Extra="x"))
    assert not res.structural_ok
    assert any("Extra" in e for e in res.structural_errors)


def test_structural_fail_reordered_columns():
    cols = list(COLUMNS_22)
    cols[0], cols[1] = cols[1], cols[0]
    res = validate_frame(_frame(GOOD_ROW)[cols])
    assert not res.structural_ok
    assert any("order" in e.lower() for e in res.structural_errors)


# --------------------------------------------------------------------------- #
# per-field row validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides, column, category",
    [
        ({"Order_ID": "ORD-123"}, "Order_ID", "bad_pattern"),
        ({"Customer_ID": "cust-abc"}, "Customer_ID", "bad_pattern"),
        ({"Product_ID": "PROD-1"}, "Product_ID", "bad_pattern"),
        ({"Quantity": "0"}, "Quantity", "out_of_range"),
        ({"Quantity": "-1"}, "Quantity", "out_of_range"),
        ({"Quantity": "1.5"}, "Quantity", "wrong_type"),
        ({"Quantity": "abc"}, "Quantity", "wrong_type"),
        ({"Discount": "1.50"}, "Discount", "out_of_range"),
        ({"Discount": "-0.10"}, "Discount", "out_of_range"),
        ({"Unit_Price": "-500.0"}, "Unit_Price", "out_of_range"),
        ({"Unit_Price": "0.0"}, "Unit_Price", "out_of_range"),
        ({"Customer_Segment": "consumer"}, "Customer_Segment", "not_in_allowed_values"),
        ({"Customer_Segment": "CORPORATE "}, "Customer_Segment", "not_in_allowed_values"),
        ({"Category": "Gadgets"}, "Category", "not_in_allowed_values"),
        ({"Region": " West"}, "Region", "not_in_allowed_values"),
        ({"Payment_Method": "upi"}, "Payment_Method", "not_in_allowed_values"),
        ({"Order_Status": "done"}, "Order_Status", "not_in_allowed_values"),
        ({"Order_Date": "2027-06-01"}, "Order_Date", "out_of_range"),
        ({"Order_Date": "2025-12-31"}, "Order_Date", "out_of_range"),
        ({"Order_Date": "2026-13-40"}, "Order_Date", "wrong_type"),
        ({"Order_Date": "31/02/2026"}, "Order_Date", "wrong_type"),
        ({"Order_Date": ""}, "Order_Date", "missing_value"),
        ({"Customer_ID": ""}, "Customer_ID", "missing_value"),
        ({"Customer_Name": "x"}, "Customer_Name", "bad_length"),
        ({"Shipping_Days": "40"}, "Shipping_Days", "out_of_range"),
        ({"Shipping_Days": "-1"}, "Shipping_Days", "out_of_range"),
    ],
)
def test_field_violation(overrides, column, category):
    res = validate_frame(_frame(_mut(**overrides)))
    assert res.structural_ok
    got = {(v.column, v.category) for v in res.row_violations}
    assert (column, category) in got, got
    assert res.rows_rejected == 1 and not res.ok


def test_blank_shipping_days_is_allowed():
    assert validate_frame(_frame(_mut(Shipping_Days="", Order_Status="Pending"))).ok
    # Phase 11 does not enforce Order_Status <-> Shipping_Days consistency (Phase 14)
    assert validate_frame(_frame(_mut(Shipping_Days=""))).ok


def test_valid_high_shipping_days_from_anomaly_is_allowed():
    assert validate_frame(_frame(_mut(Shipping_Days="28"))).ok


# --------------------------------------------------------------------------- #
# hierarchy validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides, column",
    [
        ({"Category": "Electronics", "Sub_Category": "Chairs"}, "Sub_Category"),
        ({"Category": "Furniture", "Sub_Category": "Laptop"}, "Sub_Category"),
        ({"Region": "West", "State": "Kerala"}, "City"),
        ({"Region": "West", "State": "Maharashtra", "City": "Kochi"}, "City"),
    ],
)
def test_hierarchy_violation(overrides, column):
    res = validate_frame(_frame(_mut(**overrides)))
    assert res.structural_ok
    got = {(v.column, v.category) for v in res.row_violations}
    assert (column, "invalid_hierarchy") in got, got


def test_shared_subcategory_is_valid_under_its_own_category():
    assert validate_frame(_frame(_mut(Category="Furniture", Sub_Category="Storage"))).ok
    assert validate_frame(_frame(_mut(Category="Office Supplies", Sub_Category="Storage"))).ok


# --------------------------------------------------------------------------- #
# counting
# --------------------------------------------------------------------------- #
def test_row_counts_and_rejected_rows_map():
    rows = [GOOD_ROW, _mut(Quantity="0"), GOOD_ROW, _mut(Category="Gadgets"), GOOD_ROW]
    res = validate_frame(_frame(*rows))
    assert (res.rows_checked, res.rows_valid, res.rows_rejected) == (5, 3, 2)
    assert set(res.rejected_rows) == {2, 4}
    assert res.rejected_rows[2]["Quantity"] == "0"
    assert res.summary()["rows_rejected"] == 2


# --------------------------------------------------------------------------- #
# contract vs the real generator
# --------------------------------------------------------------------------- #
def test_clean_generator_output_passes_the_contract(tmp_path):
    from scripts import generate_dataset as gd
    csv = tmp_path / "clean.csv"
    gd.generate_dataset(rows=600, seed=5).to_csv(csv, index=False, date_format="%Y-%m-%d")
    res = validate_csv(csv)
    assert res.structural_ok
    assert res.row_violations == [], [
        (v.column, v.category, v.reason) for v in res.row_violations[:5]
    ]


def test_delivery_generator_output_is_structural_ok_but_has_row_defects(tmp_path):
    from scripts import generate_dataset as gd
    csv = tmp_path / "delivery.csv"
    gd.build_delivery_dataset(rows=3000, seed=5).delivery_df.to_csv(
        csv, index=False, date_format="%Y-%m-%d"
    )
    res = validate_csv(csv)
    assert res.structural_ok
    assert res.rows_rejected > 0
    cats = {v.category for v in res.row_violations}
    assert {"missing_value", "wrong_type", "out_of_range", "not_in_allowed_values"} & cats


# --------------------------------------------------------------------------- #
# orchestrator stage 3 (stub Database)
# --------------------------------------------------------------------------- #
def _run_file(monkeypatch, tmp_path, rows_or_df, name="sales_2026_02_24.csv"):
    from src import orchestrator
    p = _paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    fake = FakeDB()
    monkeypatch.setattr(orchestrator, "Database", lambda *a, **k: fake)
    df = rows_or_df if isinstance(rows_or_df, pd.DataFrame) else _frame(*rows_or_df)
    df.to_csv(p.incoming / name, index=False)
    code = orchestrator.run_file(p.incoming / name)
    return code, fake, p


def test_run_file_clean_closes_partial_with_validate_metrics(monkeypatch, tmp_path):
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW, GOOD_ROW])
    assert code == 0
    updates = [c[2]["sm"] for c in fake.calls
               if c[0] == "execute" and c[2] and "sm" in c[2]]
    assert updates and "validate" in updates[-1] and "ingest" in updates[-1]
    assert any("PARTIAL" in s for s in fake.sql_of("execute"))
    assert not any("rejected_records" in s for s in fake.sql_of("execute"))
    assert not any("rejected_records" in s for s in fake.sql_of("execute_many"))
    assert list(p.processed.glob("*.json"))


def test_run_file_structural_failure_returns_6(monkeypatch, tmp_path):
    df = _frame(GOOD_ROW).drop(columns=["Discount"])
    code, fake, _ = _run_file(monkeypatch, tmp_path, df, name="broken.csv")
    assert code == 6
    assert any("rejected_records" in s and "'schema'" in s for s in fake.sql_of("execute"))
    assert any("FAILED" in s for s in fake.sql_of("execute"))


def test_run_file_row_defects_persist_rejected_records(monkeypatch, tmp_path):
    rows = [GOOD_ROW, _mut(Quantity="0"), _mut(Category="Gadgets")]
    code, fake, _ = _run_file(monkeypatch, tmp_path, rows, name="mixed.csv")
    assert code == 0
    em = [c for c in fake.calls if c[0] == "execute_many" and "rejected_records" in c[1]]
    assert em and len(em[0][2]) == 2
    rj = [c[2]["rj"] for c in fake.calls if c[0] == "execute" and c[2] and "rj" in c[2]]
    assert rj and rj[-1] == 2


def test_clis_still_run():
    for cmd in (
        [sys.executable, "-m", "src.orchestrator", "--help"],
        [sys.executable, str(PROJECT_ROOT / "run_pipeline.py"), "--help"],
    ):
        r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr


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
def test_clean_file_closes_partial_with_no_rejects(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_clean.csv"
    gd.generate_dataset(rows=300, seed=5).to_csv(
        p.incoming / name, index=False, date_format="%Y-%m-%d"
    )
    db = Database()
    try:
        assert orchestrator.run_file(p.incoming / name) == 0
        row = db.fetch_one(
            "SELECT run_id, status, rows_rejected, stage_metrics FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        assert row["status"] == "PARTIAL"
        assert row["rows_rejected"] == 0
        assert row["stage_metrics"]["validate"]["rows_rejected"] == 0
        assert db.scalar(
            "SELECT count(*) FROM rejected_records WHERE run_id = :r", {"r": row["run_id"]}
        ) == 0
    finally:
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_structurally_broken_file_fails_with_schema_reject(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_broken.csv"
    _frame(GOOD_ROW).drop(columns=["Discount"]).to_csv(p.incoming / name, index=False)
    db = Database()
    try:
        assert orchestrator.run_file(p.incoming / name) == 6
        row = db.fetch_one(
            "SELECT run_id, status, error FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        assert row["status"] == "FAILED"
        assert "Discount" in row["error"]
        rr = db.fetch_all(
            "SELECT rejection_category FROM rejected_records WHERE run_id = :r",
            {"r": row["run_id"]},
        )
        assert len(rr) == 1 and rr[0]["rejection_category"] == "schema"
    finally:
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_defective_file_persists_one_rejected_record_per_bad_row(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_defects.csv"
    rows = [GOOD_ROW, _mut(Quantity="0"), GOOD_ROW,
            _mut(Category="Gadgets"), _mut(Order_Date="2027-01-01")]
    _frame(*rows).to_csv(p.incoming / name, index=False)
    db = Database()
    try:
        assert orchestrator.run_file(p.incoming / name) == 0
        row = db.fetch_one(
            "SELECT run_id, rows_valid, rows_rejected FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        assert (row["rows_valid"], row["rows_rejected"]) == (2, 3)
        assert db.scalar(
            "SELECT count(*) FROM rejected_records WHERE run_id = :r", {"r": row["run_id"]}
        ) == 3
        sample = db.fetch_one(
            "SELECT raw_record FROM rejected_records WHERE run_id = :r LIMIT 1",
            {"r": row["run_id"]},
        )
        assert isinstance(sample["raw_record"], dict) and "Order_ID" in sample["raw_record"]
    finally:
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
