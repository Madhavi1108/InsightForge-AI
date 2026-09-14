"""InsightForge AI - NL-to-SQL & AI security (Phase 29 / spec Phases
56-57, FR-21).

The spec (verbatim, ``docs/system-components.md``): "Question -> Intent ->
SQL -> Validation -> PostgreSQL -> Result -> Explanation. Read-only. Blocks
DROP/DELETE/UPDATE/ALTER/TRUNCATE/INSERT; table whitelist; statement
timeout; row limit; prompt-injection protection; error sanitization."
``docs/security.md`` section 3 (verbatim): "Read-only... table whitelist:
queries may reference only known analytical tables/views... statement
timeout and row limit are enforced on every execution... validation
happens before execution; a blocked query returns a safe message and is
logged."

**Validation is deterministic, never LLM-checked.** The LLM only ever
*proposes* SQL text; every safety property below is enforced by plain
Python string/regex logic that runs before any execution
(``docs/architecture.md``: "NL-to-SQL unsafe query -> Block, return a safe
error, never execute"). A rejected query is never sent to the database.

**Table whitelist** (this project's own documented, conservative reading
of "known analytical tables/views"): the 6 Phase-16 analytical views, plus
the Phase-8 star-schema tables they are built from (needed since not
every ad-hoc question fits a pre-aggregated view). Deliberately
**excludes** every operational/pipeline table (``pipeline_runs``,
``file_registry``, ``data_quality_results``, ``rejected_records``,
``anomalies``, ``recommendations``, ``forecast_results``,
``drift_results``) - pipeline-internal metadata, not the "analytical
tables/views" the spec names.

**No template fallback.** Unlike Phase 28 (AI Analyst), there is no safe
way to guess a SQL query without an LLM - :func:`generate_sql` honestly
reports ``None`` (unavailable) rather than fabricating one.

**"Question -> Intent" is folded into SQL generation.** Arbitrary NL
questions can't be safely bucketed into a handful of fixed intents the way
Phase 28's 6 named questions could; the LLM's translation of intent *into*
SQL, constrained by the fixed prompt/whitelist below, stands in for a
separate classification step.

**Explanation is deterministic, never a second LLM call.** It describes
the *shape* of the already-executed, real result set (row count, column
names) - nothing here can hallucinate, since nothing is generated.

**Prompt-injection protection**: the system prompt (``_SQL_PROMPT_TEMPLATE``)
is fixed, not user-editable; the question is interpolated as *data* inside
a fixed instruction, never concatenated as an instruction itself.

Not persisted, not wired into ``src/orchestrator.py`` - same reasoning as
Phases 22-28: an on-demand, interactive capability, not a per-file
pipeline step.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.config import LlmSettings, get_llm_settings
from src.database import Database

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Table whitelist + schema description (prompt context only - execution
# safety never depends on this column list being complete/accurate; an
# invalid column simply fails at PostgreSQL and is reported as a sanitized
# error, same as any other execution failure)
# --------------------------------------------------------------------------- #
TABLE_WHITELIST = (
    "daily_kpis", "monthly_kpis", "regional_performance", "category_performance",
    "product_performance", "customer_performance",
    "fact_sales", "dim_date", "dim_customer", "dim_product", "dim_region",
)

TABLE_SCHEMAS = {
    "daily_kpis": ("order_date", "day_name", "is_weekend", "revenue", "profit",
                   "margin_pct", "orders", "customers", "units", "aov",
                   "return_rate_pct", "avg_discount_pct", "avg_shipping_days"),
    "monthly_kpis": ("month_start", "year", "month", "month_name", "revenue",
                     "profit", "margin_pct", "orders", "customers", "units",
                     "aov", "return_rate_pct", "avg_discount_pct", "avg_shipping_days"),
    "regional_performance": ("region", "states_covered", "revenue", "profit",
                             "margin_pct", "orders", "customers", "units", "aov",
                             "return_rate_pct", "avg_discount_pct", "avg_shipping_days",
                             "above_avg_region_revenue"),
    "category_performance": ("category", "revenue", "profit", "margin_pct", "orders",
                             "customers", "units", "aov", "return_rate_pct",
                             "avg_discount_pct", "avg_shipping_days",
                             "rank_by_revenue", "dense_rank_by_profit"),
    "product_performance": ("product_id", "product_name", "category", "revenue",
                            "profit", "margin_pct", "orders", "customers", "units",
                            "aov", "return_rate_pct", "avg_discount_pct",
                            "avg_shipping_days", "rank_by_revenue", "above_avg_revenue"),
    "customer_performance": ("customer_id", "customer_name", "customer_segment",
                             "revenue", "profit", "margin_pct", "orders", "units",
                             "aov", "return_rate_pct", "avg_discount_pct",
                             "avg_shipping_days", "rank_by_revenue"),
    "fact_sales": ("order_id", "order_date", "customer_id", "product_id", "region",
                   "category", "sub_category", "customer_segment", "quantity",
                   "unit_price", "discount", "revenue", "cost", "profit",
                   "payment_method", "shipping_days", "order_status",
                   "return_status", "is_returned"),
    "dim_date": ("date_key", "year", "quarter", "month", "month_name", "day",
                "day_of_week", "day_name", "week_of_year", "is_weekend", "is_month_end"),
    "dim_customer": ("customer_key", "customer_id", "customer_name", "customer_segment"),
    "dim_product": ("product_key", "product_id", "product_name", "category", "sub_category"),
    "dim_region": ("region_key", "region", "state", "city"),
}

DEFAULT_ROW_LIMIT = 1000
DEFAULT_STATEMENT_TIMEOUT_MS = 5000


def nl_sql_row_limit() -> int:
    """``NL_SQL_ROW_LIMIT`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("NL_SQL_ROW_LIMIT", "").strip() or DEFAULT_ROW_LIMIT)
    except ValueError:
        return DEFAULT_ROW_LIMIT


def nl_sql_statement_timeout_ms() -> int:
    """``NL_SQL_STATEMENT_TIMEOUT_MS`` from the environment (fresh each call)."""
    try:
        return int(os.environ.get("NL_SQL_STATEMENT_TIMEOUT_MS", "").strip() or DEFAULT_STATEMENT_TIMEOUT_MS)
    except ValueError:
        return DEFAULT_STATEMENT_TIMEOUT_MS


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SqlValidationResult:
    is_safe: bool
    reason: str | None
    tables: tuple[str, ...]
    sql: str  # normalised (LIMIT applied); only meaningful when is_safe


@dataclass(frozen=True)
class NlSqlResult:
    question: str
    sql: str | None
    is_safe: bool
    blocked_reason: str | None
    rows: list[dict]
    row_count: int
    explanation: str
    engine: str  # "gemini" | "unavailable"
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Validation - pure, no database
# --------------------------------------------------------------------------- #
BANNED_KEYWORDS = (
    "DROP", "DELETE", "UPDATE", "ALTER", "TRUNCATE", "INSERT", "GRANT", "REVOKE",
    "CREATE", "EXEC", "EXECUTE", "CALL", "COPY", "MERGE", "VACUUM", "REINDEX",
)
_STATEMENT_START_RE = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE)
_BANNED_RE = re.compile(r"\b(" + "|".join(BANNED_KEYWORDS) + r")\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?", re.IGNORECASE)
_CTE_NAME_RE = re.compile(r"(?:\bWITH\b|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)


def _apply_row_limit(statement: str, row_limit: int) -> str:
    """Clamp an existing trailing ``LIMIT`` to ``row_limit``, or append
    one - every executed query is bounded, never "whatever the query
    returns"."""
    m = _LIMIT_RE.search(statement)
    if m and int(m.group(1)) <= row_limit:
        return statement
    if m:
        return statement[:m.start()].rstrip() + f" LIMIT {row_limit}"
    return f"{statement} LIMIT {row_limit}"


def validate_sql(sql: str, row_limit: int | None = None) -> SqlValidationResult:
    """Every safety property in one place, checked in order, first
    failure wins: non-empty -> single statement -> ``SELECT``/``WITH``-only
    -> no banned keyword -> every referenced table in
    :data:`TABLE_WHITELIST` (a ``WITH``-clause's own CTE names are
    recognised via :data:`_CTE_NAME_RE` and exempted from this check -
    they name a query-local result set, not a real table). Only on full
    success is the row limit applied and a normalised, executable SQL
    string returned."""
    row_limit = row_limit if row_limit is not None else nl_sql_row_limit()
    candidate = (sql or "").strip()
    if not candidate:
        return SqlValidationResult(False, "empty query", (), "")

    statements = [s.strip() for s in candidate.split(";") if s.strip()]
    if len(statements) != 1:
        return SqlValidationResult(False, "only a single SQL statement is allowed", (), "")
    statement = statements[0]

    if not _STATEMENT_START_RE.match(statement):
        return SqlValidationResult(
            False, "only SELECT (or WITH ... SELECT) queries are allowed", (), "",
        )

    banned = _BANNED_RE.search(statement)
    if banned:
        return SqlValidationResult(
            False, f"the keyword '{banned.group(1).upper()}' is not allowed", (), "",
        )

    cte_names = {m.lower() for m in _CTE_NAME_RE.findall(statement)}
    tables = tuple(dict.fromkeys(m.lower() for m in _TABLE_RE.findall(statement)))
    disallowed = [t for t in tables if t not in TABLE_WHITELIST and t not in cte_names]
    if disallowed:
        return SqlValidationResult(
            False, f"query references table(s) not in the whitelist: {', '.join(disallowed)}",
            tables, "",
        )

    return SqlValidationResult(True, None, tables, _apply_row_limit(statement, row_limit))


# --------------------------------------------------------------------------- #
# Gemini SQL generation - best-effort, mirrors src.ai_analyst._call_gemini
# (intentionally duplicated, not imported - keeps this module self-contained)
# --------------------------------------------------------------------------- #
_SQL_PROMPT_TEMPLATE = (
    "You translate a business question into exactly one read-only PostgreSQL "
    "SELECT statement (a WITH ... SELECT CTE is also allowed).\n"
    "Rules:\n"
    "- Use ONLY these tables/views, and only the columns listed for each:\n"
    "{schema}\n"
    "- Never use DROP, DELETE, UPDATE, ALTER, TRUNCATE, INSERT, GRANT, REVOKE, "
    "CREATE, EXEC, CALL, COPY, or MERGE.\n"
    "- Return exactly one statement, no trailing semicolon, no explanation, "
    "no markdown code fences - just the raw SQL.\n\n"
    "Question: {question}\n"
)


def _schema_description() -> str:
    return "\n".join(f"- {t}({', '.join(cols)})" for t, cols in TABLE_SCHEMAS.items())


def _call_gemini(prompt: str, settings: LlmSettings) -> str | None:
    """Raw Gemini text, or ``None`` on any failure (missing package, API
    error, timeout) - never raises."""
    try:
        import google.generativeai as genai
    except ImportError:
        return None
    try:
        genai.configure(api_key=settings.api_key)
        model = genai.GenerativeModel(settings.model)
        response = model.generate_content(
            prompt, request_options={"timeout": settings.timeout_seconds},
        )
        text_out = (response.text or "").strip()
        return text_out or None
    except Exception:  # noqa: BLE001 - any LLM error means "cannot generate SQL"
        return None


def _clean_sql_candidate(candidate: str) -> str:
    """Strip Markdown code fences a model might add despite instructions."""
    t = candidate.strip()
    t = re.sub(r"^```(?:sql)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"```\s*$", "", t).strip()
    return t


def generate_sql(question: str, settings: LlmSettings | None = None) -> str | None:
    """``None`` when the LLM isn't configured or the call fails - see
    module docstring: no template fallback here."""
    settings = settings or get_llm_settings()
    if not settings.is_configured():
        return None
    prompt = _SQL_PROMPT_TEMPLATE.format(schema=_schema_description(), question=question)
    raw = _call_gemini(prompt, settings)
    return _clean_sql_candidate(raw) if raw else None


# --------------------------------------------------------------------------- #
# Execution - read-only connection, per-query statement timeout
# --------------------------------------------------------------------------- #
def _execute_readonly(db: Database, sql: str, timeout_ms: int) -> tuple[list[dict] | None, str | None]:
    """``(rows, error)``. ``error`` is a sanitized message - no raw
    driver/SQL detail surfaced (``docs/security.md`` section 5); the full
    exception type is logged server-side only."""
    try:
        with db.engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            result = conn.execute(text(sql))
            return [dict(row) for row in result.mappings().all()], None
    except SQLAlchemyError as exc:
        logger.warning("NL-to-SQL query failed: %s", type(exc).__name__)
        return None, "the query could not be executed (it may have timed out or been invalid)"


def _explain_result(rows: list[dict]) -> str:
    """Describes the shape of real, already-executed results - never a
    second LLM pass, so there is nothing here to hallucinate."""
    if not rows:
        return "The query returned no matching rows."
    cols = ", ".join(rows[0].keys())
    return f"The query returned {len(rows)} row(s) with columns: {cols}."


def _log_blocked_query(question: str, sql: str, reason: str) -> None:
    logger.warning("NL-to-SQL blocked query - reason=%r question=%r sql=%r", reason, question, sql)


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def ask(
    db: Database, question: str, settings: LlmSettings | None = None,
    row_limit: int | None = None, timeout_ms: int | None = None,
) -> NlSqlResult:
    """Question -> Intent (folded into SQL generation - see module
    docstring) -> SQL (:func:`generate_sql`) -> Validation
    (:func:`validate_sql`) -> PostgreSQL (:func:`_execute_readonly`) ->
    Result -> Explanation (:func:`_explain_result`). Every stage
    short-circuits safely; nothing after a failed stage runs."""
    settings = settings or get_llm_settings()
    row_limit = row_limit if row_limit is not None else nl_sql_row_limit()
    timeout_ms = timeout_ms if timeout_ms is not None else nl_sql_statement_timeout_ms()

    candidate_sql = generate_sql(question, settings)
    if candidate_sql is None:
        return NlSqlResult(
            question=question, sql=None, is_safe=False,
            blocked_reason="AI query generation is unavailable (no LLM configured, or the request failed).",
            rows=[], row_count=0, explanation="", engine="unavailable",
        )

    validation = validate_sql(candidate_sql, row_limit)
    if not validation.is_safe:
        _log_blocked_query(question, candidate_sql, validation.reason)
        return NlSqlResult(
            question=question, sql=candidate_sql, is_safe=False,
            blocked_reason=validation.reason, rows=[], row_count=0,
            explanation="", engine="gemini",
        )

    rows, error = _execute_readonly(db, validation.sql, timeout_ms)
    if error is not None:
        return NlSqlResult(
            question=question, sql=validation.sql, is_safe=True, blocked_reason=None,
            rows=[], row_count=0, explanation="", engine="gemini", error=error,
        )

    return NlSqlResult(
        question=question, sql=validation.sql, is_safe=True, blocked_reason=None,
        rows=rows, row_count=len(rows), explanation=_explain_result(rows),
        engine="gemini",
    )
