"""Phase 32 (spec Phases 61-63, part of FR-23) - Power BI executive,
intelligence & forecast/pipeline pages.

No Python module, no live Postgres, no Power BI in this environment - every
check here is static, the same shape as
`tests/test_phase31_powerbi_data_model.py`: the hand-authored
`dashboard/page_measures.dax` is real, syntactically balanced DAX, every
column it references is cross-checked against `sql/schema.sql`, and
`docs/powerbi-pages.md` is checked for consistency with both.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAX_PATH = PROJECT_ROOT / "dashboard" / "page_measures.dax"
DOC_PATH = PROJECT_ROOT / "docs" / "powerbi-pages.md"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"

PAGES = (
    "Executive Overview", "Business Drivers", "Customer Intelligence",
    "Product Intelligence", "Risk & Anomalies", "Forecast", "Pipeline Health",
)

NEW_MEASURES = (
    "Revenue Prev Day", "Revenue % Change (Day)",
    "Profit Prev Day", "Profit % Change (Day)",
    "Orders Prev Day", "Orders % Change (Day)",
    "Revenue Prev Month", "Revenue % Change (Month)",
    "Dataset Max Date", "Customer Recency Days",
    "Forecast Value", "Forecast Lower Bound", "Forecast Upper Bound",
    "Drift Features Flagged",
)

NON_PERSISTED_MODULES = ("src/root_cause.py", "src/rfm.py", "src/product_intelligence.py")


def test_phase32_files_exist():
    for p in (DAX_PATH, DOC_PATH):
        assert p.is_file(), f"missing {p}"


# --------------------------------------------------------------------------- #
# schema.sql cross-reference helper (generic over table name)
# --------------------------------------------------------------------------- #
def _table_columns(table: str) -> set[str]:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);", text, re.DOTALL,
    )
    assert match, f"could not locate {table} definition in sql/schema.sql"
    columns = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        col_match = re.match(r"([a-z_][a-z0-9_]*)\s+[A-Z]", line)
        if col_match:
            columns.add(col_match.group(1))
    return columns


# --------------------------------------------------------------------------- #
# dashboard/page_measures.dax
# --------------------------------------------------------------------------- #
def test_dax_every_new_measure_present():
    text = DAX_PATH.read_text(encoding="utf-8")
    for measure in NEW_MEASURES:
        assert re.search(rf"^{re.escape(measure)} =", text, re.MULTILINE), \
            f"page_measures.dax is missing the {measure!r} measure"


def test_dax_parentheses_are_balanced():
    text = DAX_PATH.read_text(encoding="utf-8")
    code = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    assert code.count("(") == code.count(")")


def test_dax_column_references_are_real_columns():
    text = DAX_PATH.read_text(encoding="utf-8")
    refs = re.findall(r"([a-z_][a-z0-9_]*)\[([a-z_][a-z0-9_]*)\]", text)
    assert refs, "no <table>[<column>] references found"
    columns_by_table: dict[str, set[str]] = {}
    missing = []
    for table, column in refs:
        if table not in columns_by_table:
            columns_by_table[table] = _table_columns(table)
        if column not in columns_by_table[table]:
            missing.append(f"{table}[{column}]")
    assert not missing, f"page_measures.dax references non-existent columns: {missing}"


def test_dax_time_intelligence_uses_dateadd_on_marked_date_table():
    text = DAX_PATH.read_text(encoding="utf-8")
    assert "DATEADD(dim_date[date_key]" in text


# --------------------------------------------------------------------------- #
# docs/powerbi-pages.md - consistency with the artifact files
# --------------------------------------------------------------------------- #
def test_doc_mentions_every_page():
    text = DOC_PATH.read_text(encoding="utf-8")
    for page in PAGES:
        assert page in text, f"docs/powerbi-pages.md never mentions the {page!r} page"


def test_doc_mentions_every_new_measure():
    text = DOC_PATH.read_text(encoding="utf-8")
    for measure in NEW_MEASURES:
        assert measure in text, f"docs/powerbi-pages.md never mentions the {measure!r} measure"


def test_doc_documents_non_persisted_module_limitation():
    text = DOC_PATH.read_text(encoding="utf-8")
    for module in NON_PERSISTED_MODULES:
        assert module in text, f"docs/powerbi-pages.md never explains the {module} limitation"
    assert "no .pbix" in text.lower() or "no `.pbix`" in text.lower()
