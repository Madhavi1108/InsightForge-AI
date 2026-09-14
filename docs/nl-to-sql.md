# InsightForge AI - NL-to-SQL & AI Security

> Phase 29 deliverable (spec Phases 56-57, FR-21). `src/nl_to_sql.py` lets a
> user ask an arbitrary ad-hoc question in plain English; an LLM proposes
> SQL, which is validated read-only against a whitelist **before** it is
> ever executed.

## 1. What the spec says

`docs/system-components.md` (verbatim): "Question -> Intent -> SQL ->
Validation -> PostgreSQL -> Result -> Explanation. Read-only. Blocks
DROP/DELETE/UPDATE/ALTER/TRUNCATE/INSERT; table whitelist; statement
timeout; row limit; prompt-injection protection; error sanitization."
`docs/security.md` section 3 ("NL-to-SQL"): read-only; a table whitelist;
`NL_SQL_STATEMENT_TIMEOUT_MS` and `NL_SQL_ROW_LIMIT` enforced on every
execution; validation before execution; a blocked query returns a safe
message and is logged.

## 2. The pipeline

| Spec stage | Function |
|---|---|
| Question -> Intent -> SQL | `generate_sql(question, settings)` - folds "intent" into the LLM's translation, constrained by a fixed prompt (section 5) |
| Validation | `validate_sql(sql, row_limit)` - pure, deterministic (section 4) |
| PostgreSQL | `_execute_readonly(db, sql, timeout_ms)` |
| Result -> Explanation | `_explain_result(rows)` (section 6) |

`ask(db, question, settings=None, row_limit=None, timeout_ms=None)` is the
single entry point chaining all four; each stage short-circuits safely.

## 3. Table whitelist

```
daily_kpis, monthly_kpis, regional_performance, category_performance,
product_performance, customer_performance,          # Phase 16 views
fact_sales, dim_date, dim_customer, dim_product, dim_region  # Phase 8 star schema
```

This project's own documented, conservative reading of "known analytical
tables/views": the 6 Phase-16 analytical views, plus the star-schema
tables they're built from (needed since not every ad-hoc question fits a
pre-aggregated view - e.g. a specific-date, specific-city question).
Deliberately **excludes** every operational/pipeline table
(`pipeline_runs`, `file_registry`, `data_quality_results`,
`rejected_records`, `anomalies`, `recommendations`, `forecast_results`,
`drift_results`) - pipeline-internal metadata, not the "analytical
tables/views" the spec names. A narrower, safer starting whitelist than
"every table"; expandable later if a real need arises.

## 4. Validation rules

| Rule | Enforced by |
|---|---|
| Non-empty | `validate_sql` |
| Exactly one statement (splits on `;`) | `validate_sql` - blocks the classic multi-statement injection a single `cursor.execute()` call could otherwise run |
| Must start with `SELECT` or `WITH` | `_STATEMENT_START_RE` |
| No `DROP`/`DELETE`/`UPDATE`/`ALTER`/`TRUNCATE`/`INSERT`/`GRANT`/`REVOKE`/`CREATE`/`EXEC`/`EXECUTE`/`CALL`/`COPY`/`MERGE`/`VACUUM`/`REINDEX` | `_BANNED_RE` (`BANNED_KEYWORDS`) |
| Every `FROM`/`JOIN` table in `TABLE_WHITELIST` (a `WITH`-clause's own CTE names are recognised and exempted - they name a query-local result set, not a real table) | `_TABLE_RE`/`_CTE_NAME_RE` + membership check |
| Bounded row count | `_apply_row_limit` - clamps an existing `LIMIT` above `row_limit`, appends one if absent |

Checked in this order, first failure wins. A rejected query is **never**
sent to the database.

## 5. Gemini SQL generation - no template fallback

Unlike Phase 28 (AI Analyst), there is no safe way to guess a SQL query
without an LLM - `generate_sql` returns `None` (honestly "unavailable")
rather than fabricating one when `LlmSettings.is_configured()` is `False`
or the call fails. The fixed prompt (`_SQL_PROMPT_TEMPLATE`) lists every
whitelisted table and its columns (`TABLE_SCHEMAS`, for prompt context
only - not itself a safety boundary; an invalid column simply fails at
PostgreSQL and is reported as a sanitized error) and repeats the banned
keywords. The question is interpolated as *data* inside this fixed
instruction, never concatenated as an instruction itself
(prompt-injection protection). `_call_gemini` mirrors
`src.ai_analyst._call_gemini` (intentionally duplicated, not imported -
keeps this module self-contained).

## 6. Execution & explanation

- `_execute_readonly` opens its own connection, issues `SET LOCAL
  statement_timeout = <NL_SQL_STATEMENT_TIMEOUT_MS>` in the same
  transaction, then runs the validated (and row-limited) query. Any
  `SQLAlchemyError` (including a timeout) is caught and reported as a
  fixed, sanitized string - no raw driver/SQL detail ever reaches the
  caller (`docs/security.md` section 5); the exception *type* is logged
  server-side.
- `_explain_result` describes the *shape* of the real, already-executed
  result set (row count, column names) - never a second LLM pass, so
  there is nothing left to hallucinate. This is a deliberately narrower
  "explanation" than Phase 28's business-event narration: a result set's
  explanation is naturally its shape.

## 7. Logging

A blocked query is logged via `_log_blocked_query` (`logger.warning`,
question/SQL/reason - no secrets) before `ask` returns - satisfying
`docs/security.md`'s "a blocked query returns a safe message and is
logged."

## 8. Wiring

Not persisted (no table stores an `NlSqlResult`), not wired into
`src/orchestrator.py` - same reasoning as Phases 22-28: an on-demand,
interactive capability, not a per-file pipeline step. Consumed directly by
its caller (a Streamlit AI Analyst / ad-hoc query page, Phase 30).

## 9. API

| Symbol | Purpose |
|---|---|
| `TABLE_WHITELIST` / `TABLE_SCHEMAS` | section 3 |
| `nl_sql_row_limit()` / `nl_sql_statement_timeout_ms()` | fresh env reads |
| `validate_sql(sql, row_limit=None)` | pure - section 4 |
| `generate_sql(question, settings=None)` | section 5 |
| `ask(db, question, settings=None, row_limit=None, timeout_ms=None)` | the pipeline entry point |
| `NlSqlResult` | the full pipeline outcome (`to_dict()` for JSON) |
| `SqlValidationResult` | `validate_sql`'s return type |

## 10. Configuration

No new environment variables - `NL_SQL_ROW_LIMIT`/`NL_SQL_STATEMENT_TIMEOUT_MS`
were already provisioned in `.env.example` (Phase 27's note). Reuses Phase
28's `LlmSettings`/`get_llm_settings()` unchanged.

## 11. Verify

```powershell
pytest -q tests/test_phase29_nl_to_sql.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase29_nl_to_sql.py
```

## 12. Related documents

- [`security.md`](security.md) - section 3, AI/LLM safety
- [`ai-analyst.md`](ai-analyst.md) - the sibling AI Analyst (Phase 28), its `_call_gemini` template
- [`database-schema.md`](database-schema.md) - `fact_sales`/`dim_*` columns
- [`analytical-views.md`](analytical-views.md) - the 6 whitelisted views
