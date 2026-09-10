# InsightForge AI - Schema & Contract Validation

> Phase 11 deliverable (spec Phases 21-22). `config/data_contract.yaml` +
> `src/validation.py` are pipeline **stage 3** (`data-flow.md` §3): the
> orchestrator runs them on the file in `data/raw/` immediately after ingestion
> (Phase 10) and before ETL (Phase 12+).

## 1. Scope

| In scope (Phase 11 / FR-03) | Out of scope (Phase 14 / FR-06, FR-07) |
|---|---|
| exact column set **and order** | the 7-dimension data-quality **score** |
| per-field type, pattern, numeric/date range | `data_quality_results`, the `≥95 PASS / <90 REJECT` gate |
| category membership, mandatory-field presence | Revenue/Profit **formula** consistency |
| `Category→Sub_Category` and `Region→State→City` hierarchies | full rejected-record lifecycle management |
| record every offending row in `rejected_records` | statistical outliers (e.g. an extreme but well-typed price) |

The `dq_dimension` this stage attaches to a `rejected_records` row
(`missing_value → Completeness`, `wrong_type`/`bad_pattern`/`out_of_range` →
`Validity`, `not_in_allowed_values`/`invalid_hierarchy` → `Referential
Integrity`) is a **provisional hint**; Phase 14 recomputes dimensions.

## 2. `config/data_contract.yaml`

Declarative, `yaml.safe_load`-able. It is the single source of truth for the
22-field CSV and is exactly satisfiable by `generate_dataset.py --clean` output
and the held-out demo day (`2026-09-09`).

| Key | Meaning |
|-----|---------|
| `columns` | the 22 names, in order; the ingested CSV header must match exactly |
| `allow_extra_columns` | `false` - unknown columns fail structural validation |
| `mandatory` | the 21 columns that must be present and non-empty on every row (all but `Shipping_Days`) |
| `fields.<Name>` | `type` (`string`/`integer`/`decimal`/`date`/`category`) plus, as needed, `pattern`, `min`/`max` (+ `exclusive_min`), `values`, `min_length`/`max_length`, `nullable`, `format` |
| `hierarchies.category_subcategory` | `{Category: [Sub_Category, …]}` (note `Storage`/`Accessories` are shared) |
| `hierarchies.region_state_city` | `{Region: {State: [City, …]}}` (5 / 23 / 74) |
| `uniqueness` | **declared** for completeness; a fully duplicated row is a Uniqueness defect **scored by Phase 14**, not rejected here |

`integer` accepts an integral value written as a float, so `Shipping_Days`
`"4.0"` and `Quantity` `"1"` both validate. `category` membership is
**exact** - `" West"`, `"south"`, `"CORPORATE "` all fail (the Phase 5
`inconsistent_text` defect).

## 3. `src/validation.py`

Pure, no database, import has no side effects.

| Symbol | Purpose |
|--------|---------|
| `DataContract` / `FieldRule` | parsed contract; `DataContract.from_yaml(path)` / `.from_dict(raw)` |
| `get_contract(path=None)` | cached loader; `DATA_CONTRACT_PATH` env overrides the default `config/data_contract.yaml` |
| `RowViolation` | `row_number` (1-based), `order_id`, `column`, `category`, `reason`, `.dq_dimension` |
| `ValidationResult` | `structural_ok`, `structural_errors`, `row_violations`, `rows_checked` / `rows_valid` / `rows_rejected`, `rejected_rows` (row → raw dict), `.ok`, `.summary()` |
| `validate_frame(df, contract=None)` | validate an in-memory frame (cells stringified) |
| `validate_csv(path, contract=None)` | read a CSV as raw strings and validate it |

Violation categories: `missing_value`, `wrong_type`, `bad_pattern`,
`out_of_range`, `not_in_allowed_values`, `invalid_hierarchy`, `bad_length`.

## 4. Orchestrator wiring (stage 3)

`src/orchestrator.py` `run_file()`, after ingestion:

1. `vres = validate_csv(result.raw_path)`.
2. **`not vres.structural_ok`** → write one `rejected_records` row
   (`rejection_category='schema'`, the joined structural errors as the reason),
   set the run `FAILED` (`error = "schema invalid: …"`, `stage_metrics.validate`
   recorded), print `[error] … schema invalid - …`, **return exit code 6**.
3. **`vres.row_violations`** → `db.execute_many` one `rejected_records` row per
   rejected data-row (`source_row_number`, `order_id` when it matches
   `^ORD-\d{8}$`, `rejection_category` = the row's single category or
   `multiple`, `rejection_reason` = joined messages, `dq_dimension`,
   `raw_record` = the full row as JSONB).
4. Close the run `PARTIAL` with `rows_valid` / `rows_rejected` /
   `stage_metrics = {"ingest": …, "validate": …}` and a
   `data/processed/<name>.json` summary.

A file with all 22 columns but bad **values** (e.g. `sales_invalid_date.csv`,
where every `Order_Date` is unparseable) is **not** a structural failure - it
closes `PARTIAL` with every row rejected on `Order_Date`.

New exit code: **`6` = file failed schema validation**. (`0` ok, `2` no mode,
`3` nothing to do, `4` DB unavailable, `5` ingestion failure.)

## 5. Configuration

`.env`: `DATA_CONTRACT_PATH=config/data_contract.yaml` (relative → project root).

## 6. Run it

```powershell
python scripts/postgres.py up
python scripts/apply_schema.py

python scripts/generate_dataset.py --clean          # pristine daily files
python run_pipeline.py --scan                       # every run closes PARTIAL, 0 rejects

python scripts/generate_dataset.py                  # delivery data with injected defects
python run_pipeline.py --scan                       # runs close PARTIAL with rows_rejected > 0
```

## 7. Verify

```powershell
pytest -q tests/test_phase11_schema_validation.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase11_schema_validation.py
```

## 8. Related documents

- [`dataset-design.md`](dataset-design.md) - the 22-field dictionary the contract encodes
- [`data-flow.md`](data-flow.md) §3 - stage 3
- [`database-schema.md`](database-schema.md) - `rejected_records`, `pipeline_runs`
- [`ingestion.md`](ingestion.md) - stages 1-2 that feed this one
- [`security.md`](security.md) §4-5 - input safety & sanitised errors
