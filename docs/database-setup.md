# InsightForge AI - Database Setup

> Phase 7 deliverable (spec Phase 14 - *PostgreSQL installation & configuration*).
> Provisions the PostgreSQL 16 instance that is the source of numerical truth for
> the whole platform. The **star schema** (`fact_sales` + dimensions) is created
> in Phase 8; the **connection layer** (`src/database.py`) in Phase 9. This phase
> only stands the server up and wires configuration through `.env`.

## 1. What Phase 7 provides

| Artifact | Purpose |
|----------|---------|
| `config/docker-compose.postgres.yml` | PostgreSQL 16 container (+ optional pgAdmin), all credentials from `.env` |
| `config/postgres/initdb/01_bootstrap.sql` | first-boot SQL: sets the `insightforge` DB to UTC, adds a description |
| `src/config.py` | reads `.env` and assembles the SQLAlchemy connection URL at runtime |
| `scripts/postgres.py` | `up` / `down` / `status` / `logs` / `wait` lifecycle helper |

## 2. Prerequisites

- **Docker Desktop** running (`docker version` succeeds). Docker is optional for
  the project as a whole (NFR-08) but required to run PostgreSQL locally.
- A populated **`.env`** file:

  ```powershell
  copy .env.example .env
  # then edit .env and set a real POSTGRES_PASSWORD (and PGADMIN_DEFAULT_PASSWORD
  # if you plan to use pgAdmin)
  ```

  `src/config.py` refuses to run with the placeholder password when
  `INSIGHTFORGE_ENV=production`; in development it warns and continues.

## 3. Connection parameters

All read from `.env` (`.env.example` carries the defaults):

| `.env` key | Default | Meaning |
|------------|---------|---------|
| `POSTGRES_HOST` | `localhost` | host the app connects to |
| `POSTGRES_PORT` | `5432` | published container port |
| `POSTGRES_DB` | `insightforge` | database name (auto-created by the image) |
| `POSTGRES_USER` | `insightforge` | role name (auto-created by the image) |
| `POSTGRES_PASSWORD` | *(placeholder)* | role password - **set a real value** |
| `DATABASE_URL` | *(unset)* | optional full SQLAlchemy URL; overrides the parts above |

The application never hard-codes these. `src/config.py`:

```python
from src.config import get_settings

s = get_settings()
s.url        # postgresql+psycopg2://insightforge:***@localhost:5432/insightforge  (real password)
s.safe_url   # same, password shown as *** - safe to log
s.libpq_dsn  # host=... port=... dbname=... user=... password=...
```

## 4. Bring the database up

```powershell
# start PostgreSQL (detached)
python scripts/postgres.py up

# wait until it accepts connections (exits non-zero on timeout)
python scripts/postgres.py wait --timeout 60

# show container status + the configured (masked) URL
python scripts/postgres.py status
```

Equivalent raw Docker Compose commands:

```powershell
docker compose -f config/docker-compose.postgres.yml up -d
docker compose -f config/docker-compose.postgres.yml ps
```

## 5. Connect

**psql inside the container:**

```powershell
docker compose -f config/docker-compose.postgres.yml exec postgres `
  psql -U insightforge -d insightforge -c "show timezone;"
# -> UTC
```

**Any local client** (DBeaver, psql, SQLAlchemy) uses the `.env` parameters
above, e.g. `postgresql://insightforge:<password>@localhost:5432/insightforge`.

**pgAdmin** (optional, not started by default):

```powershell
python scripts/postgres.py up --tools
# open http://localhost:5050  (login = PGADMIN_DEFAULT_EMAIL / PGADMIN_DEFAULT_PASSWORD)
# register a server -> host: postgres, port: 5432, db/user/password from .env
```

## 6. Data persistence & teardown

Data lives in the named volume **`insightforge_pgdata`** and survives
`down` / restarts.

```powershell
python scripts/postgres.py down              # stop containers, KEEP the data
python scripts/postgres.py down --volumes    # stop containers, DELETE all data
```

Deleting the volume causes `config/postgres/initdb/01_bootstrap.sql` to run again
on the next `up`.

## 7. Verify (no Docker required)

```powershell
pytest -q tests/test_phase07_postgres_config.py
```

The optional live-connection check is skipped unless Docker is up and you opt in:

```powershell
$env:INSIGHTFORGE_PG_INTEGRATION = "1"
pytest -q tests/test_phase07_postgres_config.py
```

## 8. Related documents

- [`architecture.md`](architecture.md) §6 - runtime dependencies
- [`system-components.md`](system-components.md) - "Storage" component contract
- [`data-flow.md`](data-flow.md) - which tables each pipeline stage touches
- [`security.md`](security.md) §1-2 - secrets handling & database safety
- [`PHASE_STATUS.md`](PHASE_STATUS.md) - build progress
