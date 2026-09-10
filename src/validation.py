"""InsightForge AI - schema / contract validation (Phase 11 / spec Phases 21-22).

Pipeline **stage 3** (``docs/data-flow.md`` §3): the orchestrator runs this on the
file in ``data/raw/`` right after ingestion. It enforces
``config/data_contract.yaml``:

* **structural** - exact column set and order; a mismatch rejects the whole file
  (run ``FAILED``);
* **per-row contract** - type, pattern, numeric / date range, category
  membership, mandatory-field presence, and the declared
  ``Category -> Sub_Category`` / ``Region -> State -> City`` hierarchies. Offending
  rows are reported for ``rejected_records``; valid rows proceed.

Out of scope (Phase 14): the 7-dimension data-quality *score*,
``data_quality_results``, the PASS/WARN/REJECT gate, and Revenue/Profit formula
consistency. The ``dq_dimension`` this module attaches to a violation is a
best-effort hint; Phase 14 recomputes dimensions authoritatively.

Pure and side-effect-free: no database access, importing the module opens
nothing.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from src.config import PROJECT_ROOT, load_env

DEFAULT_CONTRACT_PATH = PROJECT_ROOT / "config" / "data_contract.yaml"

VIOLATION_CATEGORIES = (
    "missing_value", "wrong_type", "bad_pattern", "out_of_range",
    "not_in_allowed_values", "invalid_hierarchy", "bad_length",
)

#: Provisional mapping of a violation category to a DQ dimension. Phase 14 owns
#: the authoritative per-dimension scoring.
DQ_DIMENSION_BY_CATEGORY = {
    "missing_value": "Completeness",
    "wrong_type": "Validity",
    "bad_pattern": "Validity",
    "out_of_range": "Validity",
    "bad_length": "Validity",
    "not_in_allowed_values": "Referential Integrity",
    "invalid_hierarchy": "Referential Integrity",
}

_ORDER_ID_RE = re.compile(r"^ORD-\d{8}$")


def dq_dimension_for(category: str) -> str:
    return DQ_DIMENSION_BY_CATEGORY.get(category, "Validity")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_date(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), fmt).date()
    except (TypeError, ValueError):
        return None


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value)


# --------------------------------------------------------------------------- #
# Contract model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldRule:
    name: str
    type: str  # string | integer | decimal | date | category
    pattern: re.Pattern | None = None
    min: object = None
    max: object = None
    exclusive_min: bool = False
    exclusive_max: bool = False
    values: frozenset[str] | None = None
    min_length: int | None = None
    max_length: int | None = None
    nullable: bool = False
    date_format: str = "%Y-%m-%d"

    @classmethod
    def from_spec(cls, name: str, spec: dict) -> "FieldRule":
        typ = spec.get("type", "string")
        date_fmt = spec.get("format", "%Y-%m-%d")
        lo, hi = spec.get("min"), spec.get("max")
        if typ == "date":
            lo = _parse_date(lo, date_fmt) if lo is not None else None
            hi = _parse_date(hi, date_fmt) if hi is not None else None
        pat = spec.get("pattern")
        return cls(
            name=name,
            type=typ,
            pattern=re.compile(pat) if pat else None,
            min=lo,
            max=hi,
            exclusive_min=bool(spec.get("exclusive_min", False)),
            exclusive_max=bool(spec.get("exclusive_max", False)),
            values=frozenset(spec["values"]) if "values" in spec else None,
            min_length=spec.get("min_length"),
            max_length=spec.get("max_length"),
            nullable=bool(spec.get("nullable", False)),
            date_format=date_fmt,
        )


@dataclass(frozen=True)
class DataContract:
    version: int
    columns: tuple[str, ...]
    allow_extra_columns: bool
    mandatory: frozenset[str]
    fields: dict[str, FieldRule]
    category_subcategory: dict[str, frozenset[str]]
    region_state_city: dict[str, dict[str, frozenset[str]]]

    @classmethod
    def from_dict(cls, raw: dict) -> "DataContract":
        cols = tuple(raw["columns"])
        fields = {
            name: FieldRule.from_spec(name, spec)
            for name, spec in (raw.get("fields") or {}).items()
        }
        h = raw.get("hierarchies") or {}
        cat_sub = {
            k: frozenset(v)
            for k, v in (h.get("category_subcategory") or {}).items()
        }
        rsc = {
            region: {state: frozenset(cities) for state, cities in states.items()}
            for region, states in (h.get("region_state_city") or {}).items()
        }
        return cls(
            version=int(raw.get("version", 1)),
            columns=cols,
            allow_extra_columns=bool(raw.get("allow_extra_columns", False)),
            mandatory=frozenset(raw.get("mandatory", [])),
            fields=fields,
            category_subcategory=cat_sub,
            region_state_city=rsc,
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> "DataContract":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)


@lru_cache(maxsize=4)
def get_contract(path: str | None = None) -> DataContract:
    """Load the data contract (cached). ``DATA_CONTRACT_PATH`` env overrides."""
    load_env()
    if path is None:
        path = os.environ.get("DATA_CONTRACT_PATH", "").strip() or None
    if path is None:
        resolved: Path = DEFAULT_CONTRACT_PATH
    else:
        p = Path(path).expanduser()
        resolved = p if p.is_absolute() else (PROJECT_ROOT / p)
    return DataContract.from_yaml(resolved)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RowViolation:
    row_number: int  # 1-based, excludes the header
    order_id: str | None
    column: str | None
    category: str
    reason: str

    @property
    def dq_dimension(self) -> str:
        return dq_dimension_for(self.category)


@dataclass(frozen=True)
class ValidationResult:
    structural_ok: bool
    structural_errors: list[str]
    row_violations: list[RowViolation]
    rows_checked: int
    rows_valid: int
    rows_rejected: int
    rejected_rows: dict[int, dict[str, str]]  # row_number -> raw row (for rejected_records.raw_record)

    @property
    def ok(self) -> bool:
        """Structurally valid AND every row satisfies the contract."""
        return self.structural_ok and not self.row_violations

    def summary(self) -> dict:
        return {
            "structural_ok": self.structural_ok,
            "structural_errors": self.structural_errors,
            "rows_checked": self.rows_checked,
            "rows_valid": self.rows_valid,
            "rows_rejected": self.rows_rejected,
        }


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _structural_errors(actual_columns, contract: DataContract) -> list[str]:
    actual = list(actual_columns)
    expected = list(contract.columns)
    if actual == expected:
        return []
    errs: list[str] = []
    missing = [c for c in expected if c not in actual]
    extra = [c for c in actual if c not in expected]
    if missing:
        errs.append(f"missing column(s): {', '.join(missing)}")
    if extra and not contract.allow_extra_columns:
        errs.append(f"unexpected column(s): {', '.join(extra)}")
    if not missing and not extra:
        errs.append("column order does not match the contract")
    return errs


def _range_problems(rule: FieldRule, num: float, raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if rule.min is not None:
        if rule.exclusive_min and num <= rule.min:
            out.append(("out_of_range", f"{rule.name}={raw} must be > {rule.min}"))
        elif not rule.exclusive_min and num < rule.min:
            out.append(("out_of_range", f"{rule.name}={raw} must be >= {rule.min}"))
    if rule.max is not None:
        if rule.exclusive_max and num >= rule.max:
            out.append(("out_of_range", f"{rule.name}={raw} must be < {rule.max}"))
        elif not rule.exclusive_max and num > rule.max:
            out.append(("out_of_range", f"{rule.name}={raw} must be <= {rule.max}"))
    return out


def _date_range_problems(rule: FieldRule, value: date, raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if rule.min is not None and value < rule.min:
        out.append(("out_of_range", f"{rule.name}={raw} is before {rule.min.isoformat()}"))
    if rule.max is not None and value > rule.max:
        out.append(("out_of_range", f"{rule.name}={raw} is after {rule.max.isoformat()}"))
    return out


def _check_cell(rule: FieldRule, raw_value: str, mandatory: frozenset[str]) -> list[tuple[str, str]]:
    raw = raw_value
    if raw.strip() == "":
        if rule.name in mandatory:
            return [("missing_value", f"{rule.name} is required but empty")]
        return []  # blank permitted (Shipping_Days)

    problems: list[tuple[str, str]] = []
    if rule.type == "integer":
        num = _parse_number(raw.strip())
        if num is None or not float(num).is_integer():
            problems.append(("wrong_type", f"{rule.name}={raw!r} is not an integer"))
        else:
            problems += _range_problems(rule, num, raw)
    elif rule.type == "decimal":
        num = _parse_number(raw.strip())
        if num is None:
            problems.append(("wrong_type", f"{rule.name}={raw!r} is not a number"))
        else:
            problems += _range_problems(rule, num, raw)
    elif rule.type == "date":
        parsed = _parse_date(raw, rule.date_format)
        if parsed is None:
            problems.append(("wrong_type", f"{rule.name}={raw!r} is not a {rule.date_format} date"))
        else:
            problems += _date_range_problems(rule, parsed, raw)
    elif rule.type == "category":
        if rule.values is not None and raw not in rule.values:
            problems.append(("not_in_allowed_values",
                             f"{rule.name}={raw!r} is not an accepted value"))

    if rule.pattern is not None and not rule.pattern.match(raw):
        problems.append(("bad_pattern", f"{rule.name}={raw!r} does not match the required format"))
    if rule.min_length is not None and len(raw) < rule.min_length:
        problems.append(("bad_length", f"{rule.name} is shorter than {rule.min_length} characters"))
    if rule.max_length is not None and len(raw) > rule.max_length:
        problems.append(("bad_length", f"{rule.name} is longer than {rule.max_length} characters"))
    return problems


def _hierarchy_problems(raw: dict[str, str], contract: DataContract) -> list[tuple[str, str, str]]:
    """Returns (column, category, reason) triples."""
    out: list[tuple[str, str, str]] = []
    cat, sub = raw.get("Category", ""), raw.get("Sub_Category", "")
    if cat and sub and cat in contract.category_subcategory:
        if sub not in contract.category_subcategory[cat]:
            out.append(("Sub_Category", "invalid_hierarchy",
                        f"Sub_Category={sub!r} is not a child of Category={cat!r}"))
    region, state, city = raw.get("Region", ""), raw.get("State", ""), raw.get("City", "")
    rsc = contract.region_state_city
    if region and state and city and region in rsc:
        states = rsc[region]
        if state not in states or city not in states.get(state, frozenset()):
            out.append(("City", "invalid_hierarchy",
                        f"Region/State/City = {region}/{state}/{city} is not a valid combination"))
    return out


def validate_frame(df: pd.DataFrame, contract: DataContract | None = None) -> ValidationResult:
    """Validate an in-memory frame. Cells are stringified before checking."""
    contract = contract or get_contract()

    structural = _structural_errors(df.columns, contract)
    if structural:
        return ValidationResult(
            structural_ok=False, structural_errors=structural, row_violations=[],
            rows_checked=int(len(df)), rows_valid=0, rows_rejected=0, rejected_rows={},
        )

    violations: list[RowViolation] = []
    rejected_rows: dict[int, dict[str, str]] = {}
    records = df.to_dict("records")
    for i, rec in enumerate(records, start=1):
        raw = {col: _stringify(rec.get(col)) for col in contract.columns}
        oid = raw["Order_ID"] if _ORDER_ID_RE.match(raw["Order_ID"]) else None
        row_problems: list[RowViolation] = []
        for col in contract.columns:
            rule = contract.fields.get(col)
            if rule is None:
                continue
            for cat, reason in _check_cell(rule, raw[col], contract.mandatory):
                row_problems.append(RowViolation(i, oid, col, cat, reason))
        for col, cat, reason in _hierarchy_problems(raw, contract):
            row_problems.append(RowViolation(i, oid, col, cat, reason))
        if row_problems:
            violations.extend(row_problems)
            rejected_rows[i] = raw

    rejected = len(rejected_rows)
    return ValidationResult(
        structural_ok=True, structural_errors=[], row_violations=violations,
        rows_checked=int(len(df)), rows_valid=int(len(df)) - rejected,
        rows_rejected=rejected, rejected_rows=rejected_rows,
    )


def validate_csv(path: Path | str, contract: DataContract | None = None) -> ValidationResult:
    """Read a CSV as raw strings and validate it against the contract."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[])
    return validate_frame(df, contract)
