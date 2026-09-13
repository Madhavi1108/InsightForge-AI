# InsightForge AI - Alteryx Sales / Customer / Product ETL (Star-Schema Load)

> Phase 13 deliverable (spec Phases 25-26). `alteryx/03_sales_etl.yxmd`,
> `alteryx/04_customer_product_etl.yxmd`, and `src/etl.py` are pipeline
> **stage 5** (`data-flow.md`): the orchestrator runs them on the file in
> `data/raw/` right after the Phase 12 ingestion/data-quality workflows,
> loading only the rows that passed Phase 11 validation. This is the phase
> that finally flips a run from `PARTIAL` to **`SUCCESS`**.

## 1. Scope

| In scope (Phase 13) | Out of scope |
|---|---|
| upsert `dim_customer` / `dim_product` / `dim_region` / `dim_date` | the 7-dimension DQ score/gate (Phase 14) |
| re-derive `Revenue`/`Profit` (FR-05) - never trust the CSV | analytical views, KPI SQL (Phase 15-16) |
| bulk-load `fact_sales` for every contract-valid row | anomaly detection, RCA, forecasting (Phase 18+) |
| flip the run to `SUCCESS` | - |

Only rows that passed Phase 11's per-row contract check ever reach
`fact_sales` (`docs/database-schema.md`): "Only valid rows reach `fact_sales`;
invalid rows go to `rejected_records`."

## 2. Environment reality

Same caveat as Phase 12: Alteryx isn't installed here, so
`alteryx/03_sales_etl.yxmd` / `alteryx/04_customer_product_etl.yxmd` are
hand-authored, valid Alteryx XML that have **not** been executed against a
real engine. Every load in this codebase goes through the Python fallback
(`src/etl.py::load_valid_rows`) and is reported as such
(`engine="python_fallback"`, `verified=False`).

## 3. `src/etl.py`

| Symbol | Purpose |
|---|---|
| `EtlResult` | `engine`, `verified`, `seconds`, `summary`, `error` |
| `EtlLoadError` | raised after the **load** (not just an engine invocation) fails `MAX_LOAD_ATTEMPTS` (3) times |
| `date_dim_row(d)` | pure `dim_date` row builder (`quarter`, ISO `day_of_week`, `week_of_year`, `is_month_end`, ...) |
| `recompute_revenue_profit(quantity, unit_price, discount, cost)` | `Revenue = round(qty * price * (1 - discount), 2)`, `Profit = round(Revenue - cost, 2)` - **FR-05**, the CSV's own `Revenue`/`Profit` are ignored |
| `valid_rows_frame(csv_path, vres)` | re-reads the CSV the way `validate_csv` does and drops every `vres.rejected_rows` row number |
| `load_valid_rows(csv_path, run_id, db, vres, contract=None)` | the real (Python) loader - see §4 |
| `run_sales_etl_workflow(csv_path, run_id, db, vres, settings=None, contract=None)` | engine selection + retry (see §5) |

## 4. `load_valid_rows` - the Python loader

Runs inside **one** `db.transaction()` (a genuine SQLAlchemy `Connection` -
`src.database.Database.transaction()`), so dimension rows always exist before
the fact rows referencing them commit:

1. Build distinct dimension frames from the valid rows: customers
   (`Customer_ID`/`Customer_Name`/`Customer_Segment`), products
   (`Product_ID`/`Product_Name`/`Category`/`Sub_Category`), regions
   (`Region`/`State`/`City`), and order dates.
2. `INSERT ... ON CONFLICT (<key>) DO NOTHING` into `dim_customer`
   (`customer_id`), `dim_product` (`product_id`), `dim_region`
   (`region, state, city`), and `dim_date` (`date_key`) - a dimension's
   attributes are fixed the first time a business key is seen
   (`first_seen_run_id`); a later file never overwrites them.
3. Look up the surrogate keys for exactly the distinct values this file
   touched (one `SELECT` per distinct value - daily files are small enough
   that this beats depending on any `IN (:list)` expansion behaviour).
4. Bulk-insert `fact_sales` in the same transaction, with the four surrogate
   FKs, the denormalised business attributes, and the **recomputed**
   `revenue`/`profit`.

Returns `{"rows_loaded", "customers_upserted", "products_upserted",
"regions_upserted", "dates_upserted"}` (the `_upserted` counts are rows
**actually inserted** this run - `ON CONFLICT DO NOTHING` rows don't count).

## 5. Engine selection + retry-then-fail

Same `AlteryxSettings.is_configured()` gate as Phase 12
(`src/config.py`, `ALTERYX_ENGINE_CMD` blank/missing -> not configured):

- **Configured**: attempt `03_sales_etl.yxmd` then `04_customer_product_etl.yxmd`
  via `src.alteryx.invoke_alteryx_engine` (each retried up to 3 times
  internally on a non-zero exit/timeout); on success, **verify** by
  re-querying `fact_sales` row count for the run - anything short of the
  expected row count is treated as a load failure, not trusted from the
  engine's exit code alone.
- **Not configured** (the case in this environment): call `load_valid_rows`
  directly.
- Either way, the **load** itself is retried up to `MAX_LOAD_ATTEMPTS` (3)
  times with a short fixed backoff. An `AlteryxExecutionError` from the
  engine degrades the remaining attempts to the Python path (logged in
  `EtlResult.error`, never faked as having run for real); any other
  exception during the load is retried as-is. Exhausting all attempts raises
  `EtlLoadError` (sanitised message only - no SQL/params, `docs/security.md`)
  and the orchestrator marks the run `FAILED`.

## 6. Orchestrator wiring (stage 5)

`src/orchestrator.py::run_file()`, after the Phase 12 stage:

```python
etl_result = run_sales_etl_workflow(result.raw_path, result.run_id, db, vres, alteryx_settings)
```

`stage_metrics["etl"]` gets `asdict(etl_result)`; the final
`UPDATE pipeline_runs` sets **`status='SUCCESS'`**, `error=NULL` (an `error`
column value now always means a genuine failure, never a routine
placeholder). An `EtlLoadError` instead closes the run **`FAILED`** and
returns **exit code 8** - new in this phase (`0` ok, `2` no mode, `3` nothing
to do, `4` DB unavailable, `5` ingestion failure, `6` schema validation
failure, `7` Alteryx/DQ workflow failure, `8` star-schema load failure).

## 7. Configuration

Same `.env` keys as Phase 12 - `ALTERYX_ENGINE_CMD` / `ALTERYX_WORKFLOW_DIR`.
No new environment variables.

## 8. Verify

```powershell
python -c "import xml.etree.ElementTree as ET; ET.parse('alteryx/03_sales_etl.yxmd'); ET.parse('alteryx/04_customer_product_etl.yxmd')"
pytest -q tests/test_phase13_star_schema_etl.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase13_star_schema_etl.py

python scripts/postgres.py up
python scripts/apply_schema.py
python scripts/generate_dataset.py --clean
python run_pipeline.py --scan
# then: SELECT status, stage_metrics FROM pipeline_runs ORDER BY run_id DESC LIMIT 1;
#       SELECT count(*) FROM fact_sales;
```

## 9. Related documents

- [`alteryx-workflows.md`](alteryx-workflows.md) - stage 4, the ingestion/DQ workflows that precede this one
- [`validation.md`](validation.md) - stage 3, the source of `vres.rejected_rows`
- [`database-schema.md`](database-schema.md) - the star schema, FR-05, and full lineage chain
- [`data-flow.md`](data-flow.md) - full pipeline stage list
