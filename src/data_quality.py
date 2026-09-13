"""InsightForge AI - data quality engine, score & rejected-record management
(Phase 14 / spec Phases 27-29).

Pipeline **stage 6** (``docs/data-flow.md``): the orchestrator runs this on
the file in ``data/raw/`` right after the Phase 13 star-schema load, scoring
the file across the spec's 7 named dimensions - Completeness, Validity,
Uniqueness, Consistency, Accuracy, Timeliness, Referential Integrity - and
gating the run: ``>=95`` PASS, ``90-94.99`` WARNING, ``<90`` REJECT
(``DQ_PASS_THRESHOLD`` / ``DQ_WARN_THRESHOLD`` in ``.env``).

The master spec names the dimensions and thresholds but not the per-dimension
formulas; the ones below are this project's own operational definition -
documented in ``docs/data-quality-engine.md``, not verbatim spec text.
Completeness/Validity/Referential Integrity reuse Phase 11's already-computed
``ValidationResult.row_violations`` (grouped by
``src.validation.DQ_DIMENSION_BY_CATEGORY``) rather than re-deriving separate
rule logic. **REJECT is an audit-status gate only**: it marks the run
``FAILED`` for alerting but never deletes or rolls back rows Phase 13 already
committed to ``fact_sales`` (``fact_sales`` is insert-only per run;
corrections are new runs, per ``docs/data-flow.md`` §5).

Pure and side-effect-free: no database access, importing the module opens
nothing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from src.etl import recompute_revenue_profit
from src.validation import DataContract, ValidationResult, get_contract

DIMENSIONS = (
    "Completeness", "Validity", "Uniqueness", "Consistency", "Accuracy",
    "Timeliness", "Referential Integrity",
)

_TOLERANCE = Decimal("0.01")
_FILENAME_DATE_RE = re.compile(r"(\d{4})_(\d{2})_(\d{2})")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def thresholds() -> tuple[float, float]:
    """``(pass_threshold, warn_threshold)`` from the environment (fresh each call)."""
    return _env_float("DQ_PASS_THRESHOLD", 95.0), _env_float("DQ_WARN_THRESHOLD", 90.0)


@dataclass(frozen=True)
class DimensionResult:
    dimension: str
    score: float
    passed: bool
    records_checked: int
    records_failed: int
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DqReport:
    dimensions: tuple[DimensionResult, ...]
    overall_score: float
    gate: str  # "PASS" | "WARNING" | "REJECT"
    rows_checked: int


def _score(records_checked: int, records_failed: int) -> float:
    if records_checked == 0:
        return 100.0
    return round(100.0 * (records_checked - records_failed) / records_checked, 2)


def _classify(overall_score: float) -> str:
    pass_threshold, warn_threshold = thresholds()
    if overall_score >= pass_threshold:
        return "PASS"
    if overall_score >= warn_threshold:
        return "WARNING"
    return "REJECT"


def _result(dimension: str, records_checked: int, records_failed: int,
           warn_threshold: float, detail: dict) -> DimensionResult:
    score = _score(records_checked, records_failed)
    return DimensionResult(
        dimension=dimension, score=score, passed=score >= warn_threshold,
        records_checked=records_checked, records_failed=records_failed, detail=detail,
    )


# --------------------------------------------------------------------------- #
# Completeness / Validity / Referential Integrity - reuse Phase 11's ValidationResult
# --------------------------------------------------------------------------- #
def _violation_dimension_result(vres: ValidationResult, dimension: str,
                                warn_threshold: float) -> DimensionResult:
    failed_rows = {v.row_number for v in vres.row_violations if v.dq_dimension == dimension}
    categories = sorted({v.category for v in vres.row_violations if v.dq_dimension == dimension})
    return _result(dimension, vres.rows_checked, len(failed_rows), warn_threshold,
                   {"source": "Phase 11 ValidationResult", "categories": categories})


# --------------------------------------------------------------------------- #
# Uniqueness - full-row duplicates (config/data_contract.yaml: uniqueness.row = all_columns)
# --------------------------------------------------------------------------- #
def _uniqueness_result(df: pd.DataFrame, warn_threshold: float) -> DimensionResult:
    duplicate_count = int(df.duplicated(keep="first").sum())
    return _result("Uniqueness", len(df), duplicate_count, warn_threshold,
                   {"rule": "full-row duplicate (all columns)"})


# --------------------------------------------------------------------------- #
# Consistency - Revenue/Profit formula consistency against the raw CSV's own values
# --------------------------------------------------------------------------- #
def _row_is_formula_consistent(row: pd.Series) -> bool:
    try:
        expected_revenue, expected_profit = recompute_revenue_profit(
            row["Quantity"], row["Unit_Price"], row["Discount"], row["Cost"])
        actual_revenue = Decimal(row["Revenue"])
        actual_profit = Decimal(row["Profit"])
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        return False
    return (abs(expected_revenue - actual_revenue) <= _TOLERANCE
            and abs(expected_profit - actual_profit) <= _TOLERANCE)


def _consistency_result(df: pd.DataFrame, warn_threshold: float) -> DimensionResult:
    consistent = df.apply(_row_is_formula_consistent, axis=1)
    failed = int((~consistent).sum())
    return _result("Consistency", len(df), failed, warn_threshold,
                   {"rule": "Revenue/Profit re-derivation matches the CSV's own values (tolerance 0.01)"})


# --------------------------------------------------------------------------- #
# Accuracy - Order_Status <-> Shipping_Days business rule
# --------------------------------------------------------------------------- #
_SHIPPING_BLANK_STATUSES = frozenset({"Pending", "Cancelled"})
_SHIPPING_PRESENT_STATUSES = frozenset({"Completed", "Returned"})


def _row_is_accurate(row: pd.Series) -> bool:
    status = row["Order_Status"]
    blank = row["Shipping_Days"].strip() == ""
    if status in _SHIPPING_BLANK_STATUSES:
        return blank
    if status in _SHIPPING_PRESENT_STATUSES:
        return not blank
    return False  # unrecognised status - can't confirm the rule holds


def _accuracy_result(df: pd.DataFrame, warn_threshold: float) -> DimensionResult:
    accurate = df.apply(_row_is_accurate, axis=1)
    failed = int((~accurate).sum())
    return _result("Accuracy", len(df), failed, warn_threshold,
                   {"rule": "Shipping_Days blank iff Order_Status in {Pending, Cancelled}"})


# --------------------------------------------------------------------------- #
# Timeliness - rows whose Order_Date doesn't match the file's own batch date
# --------------------------------------------------------------------------- #
def _expected_batch_date(filename: str) -> date | None:
    """The batch date encoded in a ``sales_YYYY_MM_DD.csv``-style filename.

    Only applies when the filename actually follows that convention - an ad
    hoc / non-standard filename (e.g. a test fixture) has no declared batch
    date to check rows against, so this dimension is skipped (not penalised)
    rather than guessed at from the data itself.
    """
    m = _FILENAME_DATE_RE.search(filename)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _timeliness_result(df: pd.DataFrame, filename: str, warn_threshold: float) -> DimensionResult:
    expected = _expected_batch_date(filename)
    if expected is None:
        return _result("Timeliness", len(df), 0, warn_threshold,
                       {"rule": "Order_Date matches the file's batch date",
                        "expected_date": None,
                        "note": "filename does not follow sales_YYYY_MM_DD.csv; dimension not applicable"})

    def _matches(value: str) -> bool:
        try:
            return date.fromisoformat(value) == expected
        except ValueError:
            return False

    matches = df["Order_Date"].apply(_matches)
    failed = int((~matches).sum())
    return _result("Timeliness", len(df), failed, warn_threshold,
                   {"rule": "Order_Date matches the file's batch date",
                    "expected_date": expected.isoformat()})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def score_file(csv_path: Path | str, vres: ValidationResult,
              contract: DataContract | None = None) -> DqReport:
    """Score ``csv_path`` across all 7 dimensions and classify the gate."""
    contract = contract or get_contract()
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, na_values=[])
    _, warn_threshold = thresholds()

    by_name = {
        "Completeness": _violation_dimension_result(vres, "Completeness", warn_threshold),
        "Validity": _violation_dimension_result(vres, "Validity", warn_threshold),
        "Referential Integrity": _violation_dimension_result(
            vres, "Referential Integrity", warn_threshold),
        "Uniqueness": _uniqueness_result(df, warn_threshold),
        "Consistency": _consistency_result(df, warn_threshold),
        "Accuracy": _accuracy_result(df, warn_threshold),
        "Timeliness": _timeliness_result(df, csv_path.name, warn_threshold),
    }
    ordered = tuple(by_name[name] for name in DIMENSIONS)
    overall = round(sum(d.score for d in ordered) / len(ordered), 2)
    gate = _classify(overall)
    return DqReport(dimensions=ordered, overall_score=overall, gate=gate,
                    rows_checked=len(df))
