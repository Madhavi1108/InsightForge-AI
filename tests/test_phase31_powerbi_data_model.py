"""Phase 31 (spec Phase 60, part of FR-23) - Power BI data model.

No Python module, no live Postgres, no Power BI in this environment - so
every check here is **static**: the hand-authored `dashboard/data_model.pq`
(Power Query M) and `dashboard/measures.dax` (DAX) files are real,
syntactically balanced text, and every table/column they reference is
cross-checked against the real `CREATE TABLE` definitions in
`sql/schema.sql` - the same "parses as valid, never executed here" shape
`tests/test_phase12_alteryx_workflows.py` uses for the hand-authored
`.yxmd` Alteryx workflows.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PQ_PATH = PROJECT_ROOT / "dashboard" / "data_model.pq"
DAX_PATH = PROJECT_ROOT / "dashboard" / "measures.dax"
DOC_PATH = PROJECT_ROOT / "docs" / "powerbi-data-model.md"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"

IMPORTED_TABLES = (
    "dim_date", "dim_customer", "dim_product", "dim_region", "fact_sales",
    "pipeline_runs", "data_quality_results", "anomalies", "drift_results",
    "forecast_results", "recommendations",
)

KPI_MEASURES = (
    "Revenue", "Profit", "Margin %", "Orders", "Customers", "Units", "AOV",
    "Return Rate %", "Avg Discount %", "Avg Shipping Days",
)


def test_phase31_files_exist():
    for p in (PQ_PATH, DAX_PATH, DOC_PATH):
        assert p.is_file(), f"missing {p}"


# --------------------------------------------------------------------------- #
# schema.sql cross-reference helpers
# --------------------------------------------------------------------------- #
def _schema_text() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _table_exists(table: str) -> bool:
    return re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\(", _schema_text()) is not None


def _fact_sales_columns() -> set[str]:
    text = _schema_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS fact_sales\s*\((.*?)\n\);", text, re.DOTALL)
    assert match, "could not locate fact_sales definition in sql/schema.sql"
    body = match.group(1)
    columns = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        col_match = re.match(r"([a-z_][a-z0-9_]*)\s+[A-Z]", line)
        if col_match:
            columns.add(col_match.group(1))
    return columns


def test_every_imported_table_exists_in_schema():
    for table in IMPORTED_TABLES:
        assert _table_exists(table), f"{table} has no CREATE TABLE in sql/schema.sql"


# --------------------------------------------------------------------------- #
# dashboard/data_model.pq
# --------------------------------------------------------------------------- #
def test_pq_every_imported_table_is_queried():
    text = PQ_PATH.read_text(encoding="utf-8")
    for table in IMPORTED_TABLES:
        assert f'Item="{table}"' in text, f"data_model.pq never queries {table}"


def test_pq_let_in_blocks_are_balanced():
    text = PQ_PATH.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
    let_count = len(re.findall(r"^\s*let\s*$", code, re.MULTILINE))
    in_count = len(re.findall(r"^\s*in\s*$", code, re.MULTILINE))
    assert let_count == len(IMPORTED_TABLES)
    assert let_count == in_count


def test_pq_declares_parameters_not_hardcoded_credentials():
    text = PQ_PATH.read_text(encoding="utf-8")
    assert "PGServer" in text and "PGDatabase" in text
    for leak in ("Password=", "pwd=", "PASSWORD ="):
        assert leak not in text, f"data_model.pq appears to hardcode a credential ({leak!r})"


def test_pq_uses_postgresql_database_connector():
    text = PQ_PATH.read_text(encoding="utf-8")
    assert text.count("PostgreSQL.Database(PGServer, PGDatabase)") == len(IMPORTED_TABLES)


# --------------------------------------------------------------------------- #
# dashboard/measures.dax
# --------------------------------------------------------------------------- #
def test_dax_every_kpi_measure_present():
    text = DAX_PATH.read_text(encoding="utf-8")
    for measure in KPI_MEASURES:
        assert re.search(rf"^{re.escape(measure)} =", text, re.MULTILINE), \
            f"measures.dax is missing the {measure!r} measure"


def test_dax_parentheses_are_balanced():
    text = DAX_PATH.read_text(encoding="utf-8")
    # strip // line comments before counting, so a comment mentioning "(" can't skew the count
    code = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    assert code.count("(") == code.count(")")


def test_dax_fact_sales_column_references_are_real_columns():
    text = DAX_PATH.read_text(encoding="utf-8")
    referenced = set(re.findall(r"fact_sales\[([a-z_][a-z0-9_]*)\]", text))
    assert referenced, "no fact_sales[...] column references found"
    real_columns = _fact_sales_columns()
    missing = referenced - real_columns
    assert not missing, f"measures.dax references non-existent fact_sales columns: {missing}"


def test_dax_uses_divide_not_raw_division_for_ratio_measures():
    text = DAX_PATH.read_text(encoding="utf-8")
    for measure in ("Margin %", "AOV", "Return Rate %", "Pipeline Success Rate %"):
        block_match = re.search(rf"^{re.escape(measure)} =(.*?)(?=\n\n|\Z)", text, re.MULTILINE | re.DOTALL)
        assert block_match, f"could not isolate the {measure!r} measure body"
        assert "DIVIDE(" in block_match.group(1)


# --------------------------------------------------------------------------- #
# docs/powerbi-data-model.md - consistency with the two artifact files
# --------------------------------------------------------------------------- #
def test_doc_mentions_every_imported_table():
    text = DOC_PATH.read_text(encoding="utf-8")
    for table in IMPORTED_TABLES:
        assert table in text, f"docs/powerbi-data-model.md never mentions {table}"


def test_doc_mentions_every_kpi_measure():
    text = DOC_PATH.read_text(encoding="utf-8")
    for measure in KPI_MEASURES:
        assert measure in text, f"docs/powerbi-data-model.md never mentions the {measure!r} measure"


def test_doc_explains_absence_of_pbix():
    text = DOC_PATH.read_text(encoding="utf-8").lower()
    assert ".pbix" in text
