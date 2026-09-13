# InsightForge AI - Data Quality Engine, Score & Rejected-Record Management

> Phase 14 deliverable (spec Phases 27-29, FR-06/FR-07). `src/data_quality.py`
> is pipeline **stage 6** (`data-flow.md`): the orchestrator runs it on the
> file in `data/raw/` right after the Phase 13 star-schema load, scores the
> file across 7 dimensions, and gates the run's terminal status.

## 1. What the spec says (verbatim) and what it leaves open

The master spec (`INSIGHTFORGE AI.pdf`, phases 27-29) names the 7 dimensions
and the three gate thresholds, and requires rejected records to be stored,
classified, traceable, and linked to `run_id` - but gives **no per-dimension
formula**, **no dimension weighting** for the overall score, and **says
nothing about what happens to already-loaded `fact_sales` rows on REJECT**.
Those three things are this project's own resolved design, documented here:

1. **Formulas** (below) are deterministic and built entirely from data the
   pipeline already has - no invented numbers, no LLM judgement.
2. **Overall score** = unweighted mean of the 7 dimension scores (the spec
   gives no weights to use instead).
3. **REJECT is audit-only.** It marks `pipeline_runs.status = 'FAILED'`
   (triggering alerting, Phase 34) but **never deletes or rolls back**
   `fact_sales`/dimension rows Phase 13 already committed for that run.
   `fact_sales` is insert-only per run; a correction is a new run, not an
   in-place edit or deletion (`docs/data-flow.md` §5).

## 2. The 7 dimensions and their formulas

Every dimension: `score = 100 * (records_checked - records_failed) / records_checked` (100.0 when the file has 0 rows). `passed = score >= DQ_WARN_THRESHOLD`.

| Dimension | `records_checked` | A row fails when... |
|---|---|---|
| **Completeness** | all rows in the file | it has >=1 `missing_value` violation (`ValidationResult.row_violations`, Phase 11) |
| **Validity** | all rows | it has >=1 `wrong_type`/`bad_pattern`/`out_of_range`/`bad_length` violation |
| **Referential Integrity** | all rows | it has >=1 `not_in_allowed_values`/`invalid_hierarchy` violation |
| **Uniqueness** | all rows | it's a full-row duplicate of an earlier row (`config/data_contract.yaml`'s declared `uniqueness: {row: all_columns}`) |
| **Consistency** | all rows | its own `Revenue`/`Profit` don't match `src.etl.recompute_revenue_profit(Quantity, Unit_Price, Discount, Cost)` within 0.01 |
| **Accuracy** | all rows | `Shipping_Days` isn't blank for `Pending`/`Cancelled`, or is blank for `Completed`/`Returned` |
| **Timeliness** | all rows (skipped/scores 100 if the filename isn't `sales_YYYY_MM_DD.csv`) | its `Order_Date` doesn't match the date encoded in the filename |

Completeness/Validity/Referential Integrity **reuse** Phase 11's
`ValidationResult.row_violations` (grouped by
`src.validation.DQ_DIMENSION_BY_CATEGORY`) rather than re-deriving separate
rule logic - `src/validation.py` already states this is Phase 14's job
("Phase 14 recomputes dimensions authoritatively").

**Timeliness caveat**: the check only fires when the filename follows the
daily-batch convention `scripts/generate_dataset.py` produces
(`sales_YYYY_MM_DD.csv`). An ad hoc upload or test fixture with a different
name has no declared "expected day" to check rows against, so the dimension
scores 100 rather than penalising a file this check can't meaningfully judge
(it does **not** fall back to guessing a date from the data itself - a
multi-day dump would otherwise be unfairly scored as if it were a
single-day batch).

## 3. The gate

`DQ_PASS_THRESHOLD` (default 95.0) / `DQ_WARN_THRESHOLD` (default 90.0),
already in `.env.example`:

| Overall score | Gate | `pipeline_runs.status` | Exit code |
|---|---|---|---|
| `>= 95` | PASS | `SUCCESS` | 0 |
| `90 - 94.99` | WARNING | `WARNING` (new status value - see §4) | 0 (the run completed; WARNING is a flag) |
| `< 90` | REJECT | `FAILED` | **9** (new) |

## 4. `sql/schema.sql` - the `WARNING` status

`pipeline_runs.status`'s CHECK constraint didn't include `WARNING` before
this phase (only `RUNNING/SUCCESS/PARTIAL/FAILED/SKIPPED_DUPLICATE`). The
schema file now declares `WARNING` inline **and** widens an already-existing
table via an idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD
CONSTRAINT ...` block, so re-running `scripts/apply_schema.py` against a
database created before this phase still picks it up.

## 5. `src/data_quality.py`

| Symbol | Purpose |
|---|---|
| `DIMENSIONS` | the 7 names, in the canonical order used everywhere |
| `DimensionResult` | `dimension`, `score`, `passed`, `records_checked`, `records_failed`, `detail` |
| `DqReport` | `dimensions` (7), `overall_score`, `gate`, `rows_checked` |
| `thresholds()` | `(DQ_PASS_THRESHOLD, DQ_WARN_THRESHOLD)`, read fresh from the environment each call |
| `score_file(csv_path, vres, contract=None)` | the entry point - reads the raw CSV, scores all 7 dimensions, classifies the gate |

Pure - no database access, importing the module opens nothing.

## 6. Orchestrator wiring (stage 6)

`src/orchestrator.py::run_file()`, after the Phase 13 ETL stage:

```python
dq_report = score_file(result.raw_path, vres)
```

One `data_quality_results` row is inserted per dimension **plus one
`'Overall'` row** (`dimension='Overall'`, matching
`docs/database-schema.md`'s documented "7 dimensions + an `Overall` row"
convention). The single terminal `UPDATE pipeline_runs` sets `status` from
the gate, `dq_score = overall_score`, and `stage_metrics.dq`. `error` is set
only on REJECT (a sanitised summary of the gate failure); PASS/WARNING leave
`error` NULL. `_write_processed_summary`'s `data/processed/<name>.json` gains
a `"dq"` block and reflects the real gated `status`.

Rejected-record management (FR-07) doesn't change in this phase: Phase 11's
existing `rejected_records` inserts (one row per invalid row, or one
`'schema'` row for a structural failure) already satisfy "every invalid row
stored, classified, traceable, linked to `run_id`, never silently dropped" -
this phase's scoring reads that same data, it doesn't duplicate the storage.

## 7. Configuration

Same env keys as before this phase - no new variables:
```
DQ_PASS_THRESHOLD=95.0
DQ_WARN_THRESHOLD=90.0
```

## 8. Verify

```powershell
python scripts/postgres.py up
python scripts/apply_schema.py   # widens the WARNING constraint if the DB predates this phase

pytest -q tests/test_phase14_data_quality_engine.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase14_data_quality_engine.py

python run_pipeline.py --scan
# SELECT status, dq_score, stage_metrics->'dq' FROM pipeline_runs ORDER BY run_id DESC LIMIT 1;
# SELECT dimension, score, passed FROM data_quality_results WHERE run_id = ...;
```

## 9. Related documents

- [`star-schema-etl.md`](star-schema-etl.md) - stage 5, the load this stage scores after
- [`validation.md`](validation.md) - stage 3, source of the reused `ValidationResult`
- [`database-schema.md`](database-schema.md) - `data_quality_results`/`pipeline_runs` full column reference
- [`data-flow.md`](data-flow.md) - full pipeline stage list
