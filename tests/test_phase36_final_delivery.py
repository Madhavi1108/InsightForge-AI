"""Phase 36 (spec Phases 68-69, FR-27/FR-28, NFR-03/NFR-05) - security,
testing, performance & final end-to-end delivery. The last phase.

By this point every one of the spec's Phase 68 checks already has *some*
coverage scattered across 35 prior phases' test files (mapped in
``SCENARIO_COVERAGE`` below) - this file adds only what was genuinely
missing: a repo-wide (not just ``.env``) secret scan, two literally-named
scenario tests the spec calls out by name, a machine-checked cross
-reference proving every scenario in the spec's own list is actually
covered somewhere, a performance-budget assertion against
``docs/requirements.md``'s table, and an automated analogue of the spec's
"killer demo" (Phase 69). The demo/performance tests need a real
PostgreSQL, so - same convention as every ``INSIGHTFORGE_PG_INTEGRATION``
-gated test since Phase 9 - they are gated and did not execute in this
sandbox (no Docker/Postgres available here); the secret scan, named tests,
and coverage cross-reference run unconditionally.
"""
from __future__ import annotations

import importlib
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.validation import validate_frame

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase36_test__"


def test_phase36_files_exist():
    for p in (
        PROJECT_ROOT / "scripts" / "run_final_demo.py",
        PROJECT_ROOT / "docs" / "final-delivery.md",
    ):
        assert p.is_file(), f"missing {p}"


# --------------------------------------------------------------------------- #
# Security - repo-wide secret scan (new; Phase 0 only checked .env itself)
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{12,}"),
    "openai_style_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "pem_private_key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
# Files allowed to mention a pattern's *name* (docs explaining the
# convention) without it being a real match - none currently need this,
# kept for future-proofing rather than silently widening the scan.
_ALLOWLIST_SUBSTRINGS: tuple[str, ...] = ()


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        p = PROJECT_ROOT / line
        if not p.is_file():
            continue
        # Skip binary-ish files a text scan can't meaningfully cover.
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".pyc", ".xlsx", ".ico"}:
            continue
        paths.append(p)
    return paths


def test_no_leaked_secrets_in_tracked_files():
    """Every file git actually tracks (not just ``.env``) is scanned for
    common leaked-secret shapes. `.env` itself is untracked (enforced by
    `tests/test_phase00_scaffold.py::test_env_file_is_not_committed`), so
    this only ever sees `.env.example`'s placeholders and source/docs."""
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001 - unreadable file, not this test's concern
            continue
        if any(sub in text for sub in _ALLOWLIST_SUBSTRINGS):
            continue
        for name, pattern in _SECRET_PATTERNS.items():
            if pattern.search(text):
                offenders.append((str(path.relative_to(PROJECT_ROOT)), name))
    assert not offenders, f"possible leaked secret(s) found: {offenders}"


# --------------------------------------------------------------------------- #
# Testing - two of the spec's scenarios called out by their literal names
# --------------------------------------------------------------------------- #
_COLUMNS_22 = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]
_GOOD_ROW = {
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


def _frame(**overrides) -> pd.DataFrame:
    row = {**_GOOD_ROW, **overrides}
    return pd.DataFrame([row], columns=_COLUMNS_22)


def test_negative_quantity_rejected():
    res = validate_frame(_frame(Quantity="-1"))
    assert res.structural_ok
    assert any(v.column == "Quantity" and v.category == "out_of_range"
               for v in res.row_violations)


def test_unknown_category_rejected():
    res = validate_frame(_frame(Category="Gadgets"))
    assert res.structural_ok
    assert any(v.column == "Category" and v.category == "not_in_allowed_values"
               for v in res.row_violations)


# --------------------------------------------------------------------------- #
# Testing - machine-checked traceability for the spec's full scenario list
# --------------------------------------------------------------------------- #
#: spec scenario name -> ("tests.module", "test_function_name") already
#: covering it. Kept in one place so docs/final-delivery.md's own table
#: can quote this exact mapping rather than a hand-copied one that could drift.
SCENARIO_COVERAGE = {
    "Valid file": ("tests.test_phase13_star_schema_etl", "test_clean_file_loads_fact_sales_and_closes_success"),
    "Missing columns": ("tests.test_phase11_schema_validation", "test_structural_fail_missing_column"),
    "Extra columns": ("tests.test_phase11_schema_validation", "test_structural_fail_extra_column"),
    "Duplicate file": ("tests.test_phase10_file_ingestion", "test_ingest_duplicate_skips_and_archives"),
    "Duplicate rows": ("tests.test_phase14_data_quality_engine", "test_uniqueness_flags_full_row_duplicates"),
    "NULLs": ("tests.test_phase02_generator", "test_mandatory_columns_non_null"),
    "Invalid date": ("tests.test_phase11_schema_validation", "test_field_violation"),
    "Negative quantity": ("tests.test_phase36_final_delivery", "test_negative_quantity_rejected"),
    "Invalid discount": ("tests.test_phase05_dataquality_anomaly", "test_invalid_discounts_present"),
    "Unknown category": ("tests.test_phase36_final_delivery", "test_unknown_category_rejected"),
    "Empty file": ("tests.test_phase10_file_ingestion", "test_ingest_empty_file_raises_without_touching_db_or_file"),
    "Database unavailable": ("tests.test_phase10_file_ingestion", "test_orchestrator_run_file_returns_4_when_db_unavailable"),
    "Email failure": ("tests.test_phase34_alerts", "test_send_alert_email_smtp_failure_returns_false_not_raise"),
    "Anomaly": ("tests.test_phase20_anomaly_fusion", "test_obvious_spike_is_fused_with_all_flags"),
    "No anomaly": ("tests.test_phase20_anomaly_fusion", "test_flat_series_produces_no_anomalies"),
    "Drift": ("tests.test_phase21_drift_detection", "test_detect_drift_returns_result_with_enough_rows"),
    "RCA": ("tests.test_phase22_root_cause", "test_analyze_root_cause_assembles_flat_and_tiered_result"),
    "Forecast": ("tests.test_phase25_forecasting", "test_forecast_metric_happy_path_produces_horizon_rows"),
    "AI": ("tests.test_phase28_ai_analyst", "test_answer_question_routes_all_six_spec_questions"),
    "NL-to-SQL": ("tests.test_phase29_nl_to_sql", "test_ask_blocks_unsafe_generated_sql"),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIO_COVERAGE))
def test_scenario_coverage_reference_exists(scenario):
    """Every spec Phase 68 scenario points at a test function that still
    exists - catches silent drift if a referenced test is renamed/removed."""
    module_name, func_name = SCENARIO_COVERAGE[scenario]
    module = importlib.import_module(module_name)
    obj = getattr(module, func_name, None)
    assert obj is not None, f"{scenario}: {module_name}.{func_name} no longer exists"
    assert inspect.isfunction(obj), f"{scenario}: {module_name}.{func_name} is not a test function"


def test_scenario_coverage_lists_every_spec_scenario():
    spec_scenarios = {
        "Valid file", "Missing columns", "Extra columns", "Duplicate file",
        "Duplicate rows", "NULLs", "Invalid date", "Negative quantity",
        "Invalid discount", "Unknown category", "Empty file",
        "Database unavailable", "Email failure", "Anomaly", "No anomaly",
        "Drift", "RCA", "Forecast", "AI", "NL-to-SQL",
    }
    assert set(SCENARIO_COVERAGE) == spec_scenarios


# --------------------------------------------------------------------------- #
# Performance - stage_metrics vs docs/requirements.md's budget table
# --------------------------------------------------------------------------- #
#: (stage_metrics path, budget seconds) - only stages the orchestrator
#: times directly per-file; "SQL KPIs+views" and "Analytics+intelligence"
#: in the requirements table span on-demand modules (RCA/impact/forecast/
#: recommendations, Phases 22-26) that aren't per-file orchestrator stages,
#: so they aren't asserted here - see docs/final-delivery.md.
BUDGETS = {
    "ingest": 2.0,
    "etl": 20.0,
    "report": 15.0,
}
TOTAL_BUDGET_SECONDS = 90.0


def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pg_integration
def test_stage_durations_within_documented_budget(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=1500, seed=360)
    d = sorted(df["Order_Date"].unique())[0]
    day_df = df[df["Order_Date"] == d].copy()
    name = f"{SENTINEL}_perf.csv"
    day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")

    code = orchestrator.run_file(p.incoming / name)
    assert code in (0, 9)

    row = db.fetch_one(
        "SELECT duration_s, stage_metrics FROM pipeline_runs WHERE file_name = :n", {"n": name},
    )
    metrics = row["stage_metrics"]
    for stage, budget in BUDGETS.items():
        seconds = metrics.get(stage, {}).get("seconds")
        assert seconds is not None, f"stage {stage} did not record a duration"
        assert seconds < budget, f"stage {stage} took {seconds}s, budget is {budget}s"
    assert float(row["duration_s"]) < TOTAL_BUDGET_SECONDS


# --------------------------------------------------------------------------- #
# Final end-to-end delivery - the automated analogue of the killer demo
# --------------------------------------------------------------------------- #
@pg_integration
def test_final_demo_dataset_runs_end_to_end_and_ai_analyst_answers(tmp_path, monkeypatch):
    """Copies the real ``data/sales_2026_09_09.csv`` into a *temporary*
    incoming directory (never the real ``data/incoming/``, so this test
    can't mutate repo state), runs it through the orchestrator, then asks
    the AI Analyst the spec's own killer-demo question."""
    pytest.importorskip("psycopg2")
    demo_file = PROJECT_ROOT / "data" / "sales_2026_09_09.csv"
    assert demo_file.is_file(), "expected data/sales_2026_09_09.csv (Phase 6's generator output)"

    _apply_schema()
    from src import orchestrator
    from src.ai_analyst import answer_question
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    target = p.incoming / demo_file.name
    target.write_bytes(demo_file.read_bytes())

    code = orchestrator.run_file(target)
    assert code in (0, 9)

    answer = answer_question(db, "Why did revenue decrease?")
    assert answer.summary
    assert answer.confidence_band in ("HIGH", "MEDIUM", "LOW")
    assert answer.engine in ("gemini", "template")
