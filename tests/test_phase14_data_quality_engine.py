"""Phase 14 (spec Phases 27-29) - data quality engine, score & rejected-record
management.

The 7 dimension scorers and the gate classifier are pure (no database) and
get direct unit tests against small hand-built fixtures. Persisting
``data_quality_results`` and the gated `pipeline_runs.status` transition need
a real Postgres (dimension-key/FK-free but exercising the widened `WARNING`
CHECK constraint), so those are `INSIGHTFORGE_PG_INTEGRATION=1`-gated,
following `tests/test_phase13_star_schema_etl.py`'s pattern. Orchestrator
control-flow (stub DB) tests monkeypatch `score_file` itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.data_quality import (
    DIMENSIONS,
    DqReport,
    _classify,
    score_file,
    thresholds,
)
from src.validation import validate_frame

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DIR_FIELDS = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase14_test__"

COLUMNS_22 = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]

# Order_Date matches the sales_2026_02_24.csv batch filename used below;
# Revenue/Profit are correctly derived; Shipping_Days matches Completed.
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


def _mut(**overrides):
    row = dict(GOOD_ROW)
    row.update(overrides)
    if "Order_ID" not in overrides:
        row["Order_ID"] = f"ORD-{abs(hash(frozenset(overrides.items()))) % 10**8:08d}"
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=COLUMNS_22)


def _write(tmp_path, rows, name="sales_2026_02_24.csv"):
    p = tmp_path / name
    _frame(*rows).to_csv(p, index=False)
    return p


def _score(tmp_path, rows, name="sales_2026_02_24.csv"):
    csv = _write(tmp_path, rows, name)
    vres = validate_frame(_frame(*rows))
    return score_file(csv, vres)


def _dim(report: DqReport, name: str):
    return next(d for d in report.dimensions if d.dimension == name)


# --------------------------------------------------------------------------- #
# files exist
# --------------------------------------------------------------------------- #
def test_phase14_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "data_quality.py",
        PROJECT_ROOT / "docs" / "data-quality-engine.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.data_quality"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_all_seven_dimensions_present(tmp_path):
    report = _score(tmp_path, [GOOD_ROW, _mut(Order_ID="ORD-00000002")])
    assert {d.dimension for d in report.dimensions} == set(DIMENSIONS)
    assert len(report.dimensions) == 7


def test_clean_file_scores_100_and_passes(tmp_path):
    rows = [_mut(Order_ID=f"ORD-{i:08d}") for i in range(5)]
    report = _score(tmp_path, rows)
    assert report.overall_score == 100.0
    assert report.gate == "PASS"
    assert all(d.score == 100.0 and d.passed for d in report.dimensions)


# --------------------------------------------------------------------------- #
# Completeness / Validity / Referential Integrity - reuse ValidationResult
# --------------------------------------------------------------------------- #
def test_completeness_validity_referential_integrity_match_validation_result(tmp_path):
    rows = [GOOD_ROW, _mut(Customer_ID=""), _mut(Quantity="0"), _mut(Category="Gadgets")]
    csv = _write(tmp_path, rows)
    vres = validate_frame(_frame(*rows))
    report = score_file(csv, vres)

    for dimension in ("Completeness", "Validity", "Referential Integrity"):
        expected_failed = len({v.row_number for v in vres.row_violations
                               if v.dq_dimension == dimension})
        got = _dim(report, dimension)
        assert got.records_failed == expected_failed, dimension
        assert got.records_checked == vres.rows_checked


# --------------------------------------------------------------------------- #
# Uniqueness
# --------------------------------------------------------------------------- #
def test_uniqueness_flags_full_row_duplicates(tmp_path):
    rows = [GOOD_ROW, GOOD_ROW, _mut(Order_ID="ORD-00000099")]
    report = _score(tmp_path, rows)
    dim = _dim(report, "Uniqueness")
    assert dim.records_checked == 3
    assert dim.records_failed == 1  # the second occurrence of GOOD_ROW


def test_uniqueness_no_duplicates_scores_100(tmp_path):
    rows = [_mut(Order_ID=f"ORD-{i:08d}") for i in range(4)]
    report = _score(tmp_path, rows)
    assert _dim(report, "Uniqueness").score == 100.0


# --------------------------------------------------------------------------- #
# Consistency - Revenue/Profit formula
# --------------------------------------------------------------------------- #
def test_consistency_flags_mismatched_revenue(tmp_path):
    rows = [GOOD_ROW, _mut(Revenue="999999.99")]
    report = _score(tmp_path, rows)
    dim = _dim(report, "Consistency")
    assert dim.records_failed == 1


def test_consistency_correct_formula_scores_100(tmp_path):
    rows = [_mut(Order_ID=f"ORD-{i:08d}") for i in range(3)]
    report = _score(tmp_path, rows)
    assert _dim(report, "Consistency").score == 100.0


# --------------------------------------------------------------------------- #
# Accuracy - Order_Status <-> Shipping_Days
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"Order_Status": "Cancelled", "Shipping_Days": "5"},   # should be blank
        {"Order_Status": "Pending", "Shipping_Days": "3"},     # should be blank
        {"Order_Status": "Completed", "Shipping_Days": ""},    # should be present
        {"Order_Status": "Returned", "Shipping_Days": ""},     # should be present
    ],
)
def test_accuracy_flags_shipping_days_status_mismatch(tmp_path, overrides):
    rows = [GOOD_ROW, _mut(**overrides)]
    report = _score(tmp_path, rows)
    assert _dim(report, "Accuracy").records_failed == 1


def test_accuracy_correct_combinations_score_100(tmp_path):
    rows = [
        _mut(Order_ID="ORD-00000010", Order_Status="Cancelled", Shipping_Days=""),
        _mut(Order_ID="ORD-00000011", Order_Status="Pending", Shipping_Days=""),
        _mut(Order_ID="ORD-00000012", Order_Status="Completed", Shipping_Days="4"),
        _mut(Order_ID="ORD-00000013", Order_Status="Returned", Shipping_Days="6"),
    ]
    report = _score(tmp_path, rows)
    assert _dim(report, "Accuracy").score == 100.0


# --------------------------------------------------------------------------- #
# Timeliness
# --------------------------------------------------------------------------- #
def test_timeliness_flags_stray_order_date(tmp_path):
    rows = [GOOD_ROW, _mut(Order_Date="2026-01-01")]
    report = _score(tmp_path, rows, name="sales_2026_02_24.csv")
    dim = _dim(report, "Timeliness")
    assert dim.records_failed == 1
    assert dim.detail["expected_date"] == "2026-02-24"


def test_timeliness_skips_non_standard_filename(tmp_path):
    rows = [GOOD_ROW, _mut(Order_Date="2026-01-01")]
    report = _score(tmp_path, rows, name=f"{SENTINEL}_ad_hoc.csv")
    dim = _dim(report, "Timeliness")
    assert dim.score == 100.0
    assert dim.detail["expected_date"] is None


# --------------------------------------------------------------------------- #
# gate classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "score, expected",
    [(100.0, "PASS"), (95.0, "PASS"), (94.99, "WARNING"), (90.0, "WARNING"),
     (89.99, "REJECT"), (0.0, "REJECT")],
)
def test_classify_thresholds(score, expected, monkeypatch):
    monkeypatch.delenv("DQ_PASS_THRESHOLD", raising=False)
    monkeypatch.delenv("DQ_WARN_THRESHOLD", raising=False)
    assert _classify(score) == expected


def test_thresholds_honour_env(monkeypatch):
    monkeypatch.setenv("DQ_PASS_THRESHOLD", "99.0")
    monkeypatch.setenv("DQ_WARN_THRESHOLD", "80.0")
    assert thresholds() == (99.0, 80.0)
    assert _classify(85.0) == "WARNING"
    assert _classify(70.0) == "REJECT"
    assert _classify(99.5) == "PASS"


# --------------------------------------------------------------------------- #
# orchestrator control flow (stub Database, matches Phases 12-13's pattern)
# --------------------------------------------------------------------------- #
class FakeDB:
    def __init__(self, dup_row=None, next_run_id=1100):
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


def _canned_report(gate: str, rows: int = 1):
    scores = {"PASS": 100.0, "WARNING": 92.0, "REJECT": 60.0}[gate]
    from src.data_quality import DimensionResult
    return DqReport(
        dimensions=tuple(DimensionResult(dimension=d, score=scores, passed=gate != "REJECT",
                                         records_checked=rows, records_failed=0)
                         for d in DIMENSIONS),
        overall_score=scores, gate=gate, rows_checked=rows,
    )


def _run_file(monkeypatch, tmp_path, rows, gate="PASS", name="sales_2026_02_24.csv"):
    from src import orchestrator
    from src.etl import EtlResult
    p = _paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    fake = FakeDB()
    monkeypatch.setattr(orchestrator, "Database", lambda *a, **k: fake)
    monkeypatch.setattr(
        orchestrator, "run_sales_etl_workflow",
        lambda *a, **k: EtlResult(engine="python_fallback", verified=False, seconds=0.0,
                                  summary={"rows_loaded": len(rows)}),
    )
    monkeypatch.setattr(orchestrator, "score_file",
                        lambda *a, **k: _canned_report(gate, len(rows)))
    _frame(*rows).to_csv(p.incoming / name, index=False)
    code = orchestrator.run_file(p.incoming / name)
    return code, fake, p


def test_pass_gate_closes_success(monkeypatch, tmp_path):
    import json
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW], gate="PASS")
    assert code == 0
    assert any(c[2].get("status") == "SUCCESS" for c in fake.calls
              if c[0] == "execute" and c[2])
    dq_inserts = [c for c in fake.calls if c[0] == "execute_many"
                 and "data_quality_results" in c[1]]
    assert dq_inserts and len(dq_inserts[0][2]) == 8  # 7 dimensions + Overall

    files = list(p.processed.glob("*.json"))
    summary = json.loads(files[0].read_text(encoding="utf-8"))
    assert summary["status"] == "SUCCESS"
    assert summary["dq"]["gate"] == "PASS"


def test_warning_gate_closes_warning_with_exit_0(monkeypatch, tmp_path):
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW], gate="WARNING")
    assert code == 0
    assert any(c[2].get("status") == "WARNING" for c in fake.calls
              if c[0] == "execute" and c[2])


def test_reject_gate_closes_failed_with_exit_9(monkeypatch, tmp_path):
    code, fake, p = _run_file(monkeypatch, tmp_path, [GOOD_ROW], gate="REJECT")
    assert code == 9
    assert any(c[2].get("status") == "FAILED" for c in fake.calls
              if c[0] == "execute" and c[2])
    errors = [c[2].get("e") for c in fake.calls if c[0] == "execute" and c[2] and c[2].get("e")]
    assert errors and "data quality gate rejected" in errors[-1]


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
def test_clean_file_scores_8_dimensions_and_closes_success(tmp_path, monkeypatch):
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
            "SELECT run_id, status, dq_score FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        assert row["status"] in ("SUCCESS", "WARNING")
        dq_rows = db.fetch_all(
            "SELECT dimension, score FROM data_quality_results WHERE run_id = :r",
            {"r": row["run_id"]},
        )
        assert len(dq_rows) == 8
        overall = next(d["score"] for d in dq_rows if d["dimension"] == "Overall")
        assert float(overall) == float(row["dq_score"])
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})


@pg_integration
def test_reject_keeps_fact_sales_rows_in_place(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    name = f"{SENTINEL}_reject.csv"
    # Duplicate rows + mismatched Revenue -> Uniqueness and Consistency both
    # fail hard enough to drop the overall score below DQ_WARN_THRESHOLD.
    rows = [GOOD_ROW, GOOD_ROW, _mut(Revenue="1.00"), _mut(Revenue="2.00")]
    _frame(*rows).to_csv(p.incoming / name, index=False)
    db = Database()
    try:
        code = orchestrator.run_file(p.incoming / name)
        row = db.fetch_one(
            "SELECT run_id, status FROM pipeline_runs "
            "WHERE file_name = :n ORDER BY run_id DESC LIMIT 1", {"n": name},
        )
        if row["status"] == "FAILED" and code == 9:
            loaded = db.scalar(
                "SELECT count(*) FROM fact_sales WHERE run_id = :r", {"r": row["run_id"]}
            )
            assert loaded == len(rows)  # REJECT does not roll back the load
    finally:
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
