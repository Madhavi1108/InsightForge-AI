# InsightForge AI - File Ingestion & Fingerprinting

> Phase 10 deliverable (spec Phases 19-20). `src/ingestion.py` is pipeline
> stages 1-2 (`data-flow.md` §3); `src/orchestrator.py` is the single sequencer
> both entry points call. Provisioning is Phase 7, the schema Phase 8, the
> connection layer ([`database-layer.md`](database-layer.md)) Phase 9. Validation
> / ETL / analytics arrive in Phase 11+.

## 1. Flow

```
data/incoming/<file>.csv
        │  collect_metadata()      size- and type-check BEFORE parsing
        │                          (.csv, non-empty, readable)
        ▼
   sha256_file()                   streamed 1 MiB blocks
        │
        ▼
   file_registry lookup by file_hash
        │
   ┌────┴─────────────────────────────┐
   │ hash seen before                 │ new hash
   ▼                                  ▼
 pipeline_runs row               pipeline_runs row (status RUNNING,
 (SKIPPED_DUPLICATE,              rows_received set)
  finished, error=                     │
  "duplicate of run N")                ├─ move  data/incoming → data/raw
   │                                   ├─ copy  data/raw     → data/archive
   ▼                                   ├─ file_registry row (hash, size, rows,
 move file → data/archive/            │                     first_seen_run_id)
 (suffix .dupe-<run_id> on            ▼
  name collision)                orchestrator closes the run PARTIAL
                                 (+ data/processed/<name>.json summary)
```

## 2. `src/ingestion.py`

| Symbol | Purpose |
|--------|---------|
| `sha256_file(path, *, chunk=1MiB) -> str` | streamed hex SHA-256 |
| `collect_metadata(path) -> FileMetadata` | validate + describe (`name`, `size_bytes`, `row_count`, `modified_at`, `sha256`); raises `IngestionError` |
| `IngestionError` | missing / empty / unreadable / non-`.csv`; `.reason` never contains an absolute path |
| `ingest_file(path, db, paths) -> IngestionResult` | the stage 1-2 body; `paths` is a `src.config.PipelinePaths` |
| `IngestionResult` | `run_id`, `status` (`RUNNING` \| `SKIPPED_DUPLICATE`), `file_hash`, `file_name`, `raw_path`, `archive_path`, `metadata`, `duplicate_of_run_id` |
| `watch(incoming_dir, dispatch, *, block=True, settle=None) -> Observer` | `watchdog` observer; calls `dispatch(path)` for each settled `.csv` |
| `wait_until_stable(path, *, settle, timeout)` | block until the file's size stops changing |

`ingest_file` writes to the database only through `src.database.Database`
(`insert_returning`, `execute`, `fetch_one`). A lost registration race
(concurrent `file_registry` unique-violation, `SQLSTATE 23505`) degrades to
`SKIPPED_DUPLICATE` rather than failing.

### `pipeline_runs` lifecycle in this phase

| Outcome | `status` | set here |
|---------|----------|----------|
| New file, ingested | `RUNNING` → **`PARTIAL`** (orchestrator) | `file_name`, `file_hash`, `rows_received`, then `finished_at`, `duration_s`, `stage_metrics.ingest`, `error` = *"stages after ingestion not implemented yet (Phase 11+)"* |
| Duplicate hash | `SKIPPED_DUPLICATE` | `file_name`, `file_hash`, `rows_received`, `finished_at`, `duration_s=0`, `error` = *"duplicate of run N"* |
| Unreadable / empty / relocation failure | `FAILED` | `finished_at`, sanitised `error` (no path / stack trace) |

`PARTIAL` is a placeholder: once validation + ETL exist, an ingested run closes
`SUCCESS`.

## 3. `src/orchestrator.py`

`main(args)` (called by `run_pipeline.py` with the parsed `argparse.Namespace`)
dispatches:

| Mode | Behaviour | Exit |
|------|-----------|-----:|
| `--file <path>` | `run_file` - healthcheck, `ingest_file`, close `PARTIAL`, write `processed/` summary | 0 / 4 / 5 |
| `--scan` | every `data/incoming/*.csv`, sorted; empty dir prints "no files" | 0 / 3 / 4 / 5 |
| `--watch` | block on the `data/incoming/` watcher, ingesting arrivals | (Ctrl+C) |
| `--scheduler` | not built - APScheduler wiring is Phase 35 | 3 |
| `--dry-run` (+`--file`/`--scan`) | print the plan, touch nothing | 0 |

Exit codes: `0` ok · `2` no mode · `3` nothing to do / not built · `4` database
unavailable (sanitised message, no traceback) · `5` a file failed ingestion.

## 4. File lifecycle & immutability

- A file is **moved** `incoming → raw` **before any parsing**, then **copied**
  `raw → archive` in the same step (`data-flow.md` §1).
- `data/archive/` is write-once. A re-dropped duplicate is still archived (as
  another received file) under `<stem>.dupe-<run_id><suffix>` if the name
  collides - the first archived copy is never touched.
- Nothing is deleted. A duplicate is retained in `archive/`, not removed.

## 5. Configuration

`src/config.py` `PipelinePaths` / `get_paths()` resolve the working directories
from `.env` (`DATA_INCOMING_DIR`, `DATA_RAW_DIR`, `DATA_PROCESSED_DIR`,
`DATA_REJECTED_DIR`, `DATA_ARCHIVE_DIR`, `LOGS_DIR`, `REPORTS_DIR`); a relative
value is resolved against the project root. `WATCH_SETTLE_SECONDS` (default `2`)
is how long a new file's size must be stable before the watcher processes it.

Structured file logging to `logs/` (each line carrying the `run_id`) is Phase 35
(`src/observability.py`); for now the orchestrator logs to the console.

## 6. Run it

```powershell
python scripts/postgres.py up
python scripts/apply_schema.py

python run_pipeline.py --file data/incoming/sales_2026_08_01.csv
python run_pipeline.py --scan
python run_pipeline.py --watch          # Ctrl+C to stop
python run_pipeline.py --scan --dry-run
```

## 7. Verify

```powershell
pytest -q tests/test_phase10_file_ingestion.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase10_file_ingestion.py
```

## 8. Related documents

- [`data-flow.md`](data-flow.md) §1-3 - directory lifecycle, run identity, stages
- [`database-schema.md`](database-schema.md) - `pipeline_runs`, `file_registry`
- [`database-layer.md`](database-layer.md) - the `Database` facade used here
- [`architecture.md`](architecture.md) §4-5 - control flow & failure paths
- [`security.md`](security.md) §4-5 - input safety & sanitised errors
