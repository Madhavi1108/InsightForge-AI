# InsightForge AI - Security

> Phase 1 deliverable (spec Phase 1 `docs/security.md`, extended with the AI-safety
> requirements from spec Phases 56-57 and 68). Covers secrets, database safety,
> AI/LLM safety, logging, and auditability.

## 1. Secrets & configuration

- All secrets live in `.env` (PostgreSQL credentials, `GEMINI_API_KEY`, SMTP
  credentials). `.env` is **git-ignored** and must never be committed.
- `.env.example` is the committed template - placeholders only, no real values.
  A CI/scaffold test asserts it contains `GEMINI_API_KEY=` but not a populated
  key.
- `.gitignore` excludes `.env`, `*.key`, `*.pem`, `models/`, `logs/`, `reports/`,
  and data working directories.
- Credentials are **never hard-coded** in source, SQL, notebooks, or Alteryx
  workflows. The database URL is assembled at runtime from `.env` parts.
- No secret is ever written to a log, a report, an error message, or an LLM
  prompt.

## 2. Database safety

- Every query runs through `src/database.py` using **parameterised statements** -
  no string-formatted SQL with user or file input.
- The application database role has only the privileges it needs (DDL at
  migration time; DML on its own tables at runtime).
- Transactions wrap each load; a failed load rolls back cleanly.
- Connection failures retry up to 3 times with backoff, then raise a typed error
  (no raw driver stack trace surfaced to users).

## 3. AI / LLM safety

### NL-to-SQL (new Phase 29)
- **Read-only.** The generated SQL is rejected if it contains `DROP`, `DELETE`,
  `UPDATE`, `ALTER`, `TRUNCATE`, `INSERT`, or multiple statements.
- **Table whitelist**: queries may reference only known analytical tables/views.
- **Statement timeout** (`NL_SQL_STATEMENT_TIMEOUT_MS`) and **row limit**
  (`NL_SQL_ROW_LIMIT`) are enforced on every execution.
- Validation happens **before** execution; a blocked query returns a safe message
  and is logged.

### AI Analyst (new Phase 27-28)
- The LLM receives a **structured evidence package** built from verified
  analytical results - never raw user text concatenated into an instruction.
- **Prompt-injection protection**: file contents, row values, and user questions
  are passed as data, not as instructions; system prompt is fixed and not
  user-editable.
- The LLM **must not invent numbers** - every figure in an explanation traces to
  the evidence package. If the LLM is unavailable, a deterministic template
  explanation is produced from the same evidence.
- LLM errors are caught; the platform degrades to the template path rather than
  failing the run.

## 4. Input / file safety

- Incoming files are size- and type-checked before parsing.
- SHA-256 fingerprinting prevents duplicate/replayed files from mutating the
  warehouse.
- Structural validation rejects malformed schemas before any load.

## 5. Logging & error handling

- Structured logs in `logs/`, each line carrying the pipeline `run_id`.
- Error messages shown to users / emailed are **sanitized** - no credentials, no
  connection strings, no internal file-system paths, no raw stack traces.
- Full diagnostic detail stays in the server-side log.

## 6. Auditability

- `pipeline_runs` records who/what/when for every execution.
- Every insight row references its `run_id`; every `run_id` references a
  `file_hash`; every `file_hash` maps to an immutable archived file.
- `rejected_records` guarantees invalid data is retained and explainable, never
  silently dropped.

## 7. Checklist enforced in later phases

| Item | Enforced in |
|------|-------------|
| `.env` secrets, no keys in Git | scaffold test (Phase 0) + Phase 36 |
| SQL injection protection | Phase 9, 15, 29 |
| Safe LLM queries / table whitelist | Phase 29 |
| Prompt-injection protection | Phase 27-29 |
| Sanitized logs & errors | Phase 35 |
| Query timeout & result limits | Phase 29 |
