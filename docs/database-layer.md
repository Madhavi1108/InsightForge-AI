# InsightForge AI - Database Connection Layer

> Phase 9 deliverable (spec Phase 18). `src/database.py` is the **only** module
> the platform uses to talk to PostgreSQL. Provisioning is Phase 7
> ([`database-setup.md`](database-setup.md)); the schema is Phase 8
> ([`database-schema.md`](database-schema.md)); the ETL that fills the tables is
> Phases 12-13.

## 1. What it provides

| Concern | Implementation |
|---------|----------------|
| Connection management + **pooling** | one lazily-created SQLAlchemy `Engine` (`get_engine()`), `QueuePool` with `pool_pre_ping`, sized from env |
| **Transactions** | `Database.transaction()` context manager - BEGIN / COMMIT / ROLLBACK; the write helpers each run in their own transaction |
| **Retries** | transient connection failures retried up to `DB_MAX_RETRIES` (default 3) with exponential backoff via `tenacity`; non-transient errors are never retried |
| **Error handling** | typed, **sanitised** `DatabaseError` hierarchy - no SQL, bind values, DSN, host or driver stack trace reaches a caller or log line |
| **Safe query execution** | every statement goes through `sqlalchemy.text()`; every value is a bound `:name` parameter |

Importing `src.database` opens **no** connection. `create_engine` is only called
on the first `get_engine()`.

## 2. Public API

```python
from src.database import Database, get_engine, dispose_engine
from src.database import DatabaseError, DatabaseConnectionError, QueryExecutionError

db = Database()                      # uses the shared engine

# reads
db.fetch_all("SELECT * FROM pipeline_runs WHERE status = :s", {"s": "RUNNING"})
db.fetch_one("SELECT * FROM pipeline_runs WHERE run_id = :r", {"r": 42})
db.scalar("SELECT count(*) FROM fact_sales WHERE run_id = :r", {"r": 42})

# writes (each in its own transaction, retried on transient connection loss)
db.execute("UPDATE pipeline_runs SET status = :s WHERE run_id = :r",
           {"s": "SUCCESS", "r": 42})
db.execute_many("INSERT INTO fact_sales (...) VALUES (...)", list_of_row_dicts)
run_id = db.insert_returning(
    "INSERT INTO pipeline_runs (file_name, file_hash) VALUES (:n, :h) RETURNING run_id",
    {"n": name, "h": sha256},
)

# multi-statement unit of work
with db.transaction() as conn:
    conn.execute(text("INSERT INTO file_registry (...) VALUES (...)"), params)
    conn.execute(text("UPDATE pipeline_runs SET ... WHERE run_id = :r"), {"r": run_id})
# COMMIT here; any exception -> ROLLBACK and re-raise

# health
db.ping()          # -> bool, never raises
db.healthcheck()   # -> None, raises DatabaseConnectionError if unreachable
```

`fetch_all` / `fetch_one` return plain `list[dict]` / `dict` - callers never hold
driver objects.

**Rule:** never build SQL with f-strings, `.format`, or `%` and user / file
input. Pass values through `params`.

## 3. Error taxonomy

```
DatabaseError(RuntimeError)
├── DatabaseConnectionError   # database unreachable, retries exhausted
└── QueryExecutionError       # non-transient failure (integrity, syntax, type, ...)
```

Each carries `.sqlstate` (PostgreSQL `SQLSTATE`, when known) and `.cause_type`
(driver exception class name). `str(err)` is a fixed shape, e.g.
`"execute failed: IntegrityError [SQLSTATE 23505] (UniqueViolation)"` - safe to
log or surface. `ping()` swallows everything and returns a bool.

## 4. Retry policy

`_is_transient(exc)` is `True` for `sqlalchemy.exc.OperationalError`,
`InterfaceError`, and any `DBAPIError` whose connection was invalidated (server
down, dropped socket, restart). Those are retried `DB_MAX_RETRIES` times with
`wait_exponential(DB_RETRY_BASE_DELAY .. DB_RETRY_MAX_DELAY)`; exhaustion raises
`DatabaseConnectionError`. `IntegrityError`, `ProgrammingError`, `DataError` are
**not** transient - they raise `QueryExecutionError` on the first failure. Each
retry logs one WARNING line on logger `src.database` (attempt count + sanitised
cause).

The orchestrator (Phase 10) still owns **stage**-level retries; this layer only
retries the database round-trip.

## 5. Configuration (env, all optional)

| Variable | Default | Meaning |
|----------|--------:|---------|
| `DB_POOL_SIZE` | 5 | persistent pooled connections |
| `DB_POOL_MAX_OVERFLOW` | 5 | extra connections under load |
| `DB_POOL_TIMEOUT` | 30 | seconds to wait for a pooled connection |
| `DB_POOL_RECYCLE` | 1800 | recycle a connection after N seconds |
| `DB_CONNECT_TIMEOUT` | 10 | TCP connect timeout (seconds) |
| `DB_STATEMENT_TIMEOUT_MS` | 0 | server `statement_timeout`; 0 = server default (Phase 29 sets its own) |
| `DB_MAX_RETRIES` | 3 | retries after the initial attempt, for a transient failure |
| `DB_RETRY_BASE_DELAY` | 0.5 | backoff multiplier (seconds) |
| `DB_RETRY_MAX_DELAY` | 5.0 | backoff cap (seconds) |

Connection parameters themselves come from `src.config` (i.e. `.env`) - this
module never hard-codes a DSN.

## 6. Self-check

```powershell
python -m src.database
# target : postgresql+psycopg2://insightforge:***@localhost:5432/insightforge
# [ok] ping ok            (exit 0)   |   [error] ...   (exit 1, no traceback)
```

`scripts/postgres.py wait` checks `pg_isready`; this exercises the real pool +
retry path + `SELECT 1`.

## 7. Verify

```powershell
pytest -q tests/test_phase09_database.py
python scripts/postgres.py up; python scripts/apply_schema.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase09_database.py
```

## 8. Related documents

- [`database-setup.md`](database-setup.md) - PostgreSQL provisioning
- [`database-schema.md`](database-schema.md) - the tables this layer reads/writes
- [`security.md`](security.md) §2 - database safety requirements
- [`system-components.md`](system-components.md) - "Storage" component contract
