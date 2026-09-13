# InsightForge AI - Alteryx Ingestion & Data-Quality Workflows

> Phase 12 deliverable (spec Phases 23-24). `alteryx/01_ingestion.yxmd`,
> `alteryx/02_data_quality.yxmd`, and `src/alteryx.py` are pipeline **stage 4**
> (`data-flow.md`): the orchestrator runs them on the file in `data/raw/`
> right after schema validation (Phase 11) and before the sales / customer /
> product ETL that loads the star schema (Phase 13 - not built yet).

## 1. Scope

| In scope (Phase 12) | Out of scope |
|---|---|
| `01_ingestion.yxmd` - re-affirm column set + order, report row count | loading `fact_sales` / `dim_*` (Phase 13's `03_sales_etl.yxmd` / `04_customer_product_etl.yxmd`) |
| `02_data_quality.yxmd` - per-row contract checks, valid vs. violating split | the 7-dimension DQ **score** and PASS/WARN/REJECT gate (Phase 14) |
| honest engine selection (real Alteryx vs. Python fallback) | flipping `pipeline_runs.status` to `SUCCESS` (Phase 13) |

## 2. Environment reality

Alteryx Designer/Engine is **not installed** in this environment
(`docs/PHASE_STATUS.md`). The two `.yxmd` files are hand-authored, valid
Alteryx workflow XML (parseable, correct tool chain), but they have **not**
been executed against a real Alteryx engine as part of this phase - that
would require a Windows install of `AlteryxEngineCmd.exe`, which this
environment doesn't have. Every workflow run in this codebase, in tests and
in the pipeline, goes through the Python fallback and is reported as such
(`engine="python_fallback"`, `verified=False`). This matches the project's
existing rule: automated Alteryx execution is documented as unverified,
never faked.

## 3. Engine selection (`src/config.py::AlteryxSettings`)

| Field | Source | Meaning |
|---|---|---|
| `engine_cmd` | `ALTERYX_ENGINE_CMD` | path to `AlteryxEngineCmd.exe`; blank/unset -> `None` |
| `workflow_dir` | `ALTERYX_WORKFLOW_DIR` (default `alteryx`) | where the `.yxmd` files live |

`AlteryxSettings.is_configured()` is `True` only when `engine_cmd` is set
**and** the path actually exists on disk - a stale/wrong path degrades to the
fallback instead of raising.

## 4. `src/alteryx.py`

| Symbol | Purpose |
|---|---|
| `WorkflowResult` | `workflow`, `engine` (`"alteryx"` \| `"python_fallback"`), `verified`, `seconds`, `summary`, `error` |
| `AlteryxExecutionError` | raised internally after 3 failed engine attempts; callers fall back |
| `run_ingestion_workflow(csv_path, settings=None, contract=None)` | workflow 1 |
| `run_data_quality_workflow(csv_path, contract=None, settings=None)` | workflow 2 |

**Engine attempt**: when configured, `AlteryxEngineCmd.exe <workflow.yxmd>
<csv_path>` is run via `subprocess`, retried up to **3** times (fixed 1s
backoff) on a non-zero exit code or timeout. All 3 failing raises
`AlteryxExecutionError`, caught by the runner, which then executes the
Python fallback and records the failure in `WorkflowResult.error` - the
run still succeeds overall, it's just unverified.

**Python fallback contract**:
- `run_ingestion_workflow` -> `summary = {"columns_ok": bool, "row_count": int}`,
  checking the file's header against `src.validation.get_contract().columns`.
- `run_data_quality_workflow` -> calls `src.validation.validate_csv()`
  directly (the already-verified Phase 11 validator) and reshapes its
  `ValidationResult` into
  `summary = {"rows_checked", "rows_valid", "rows_rejected",
  "violations_by_category": {...}, "violations_by_dimension": {...}}`.
  Calling back into Phase 11's validator - rather than re-deriving separate
  DQ logic - is what guarantees the fallback's output is identical to what
  the rest of the pipeline already computed for the same file.

## 5. `alteryx/01_ingestion.yxmd` and `alteryx/02_data_quality.yxmd`

Both read the raw daily CSV (`Input Data`), matching the 22-column contract
in `config/data_contract.yaml`:

- **`01_ingestion`**: `Input Data` -> `Select` (enforces the exact column
  set/order, `allow_extra_columns: false`) -> `Formula` (tags lineage) ->
  `Output Data`.
- **`02_data_quality`**: `Input Data` -> `Formula` (per-field checks mirroring
  `src/validation.py`'s `missing_value` / `bad_pattern` / `out_of_range` /
  `not_in_allowed_values` categories) -> `Filter` (valid vs. violating) -> two
  `Output Data` tools. Hierarchy checks and the full type/length rule set stay
  with the Python fallback for exact parity with Phase 11; the Alteryx
  workflow demonstrates the same category of check in the ETL tool.

## 6. Orchestrator wiring (stage 4)

`src/orchestrator.py::run_file()`, after validation (stage 3):

```python
ing_wf = run_ingestion_workflow(result.raw_path, alteryx_settings)
dq_wf  = run_data_quality_workflow(result.raw_path, settings=alteryx_settings)
```

Both `WorkflowResult`s are merged into `stage_metrics` as `alteryx_ingestion`
/ `alteryx_dq`, and into the `data/processed/<name>.json` summary. The run
still closes **`PARTIAL`** (`_PARTIAL_NOTE` now reads "star-schema load not
implemented yet (Phase 13)"). An unexpected exception from either workflow
closes the run **`FAILED`** and returns **exit code 7** - new in this phase
(`0` ok, `2` no mode, `3` nothing to do, `4` DB unavailable, `5` ingestion
failure, `6` schema validation failure, `7` Alteryx/DQ workflow failure).

## 7. Configuration

`.env`:
```
ALTERYX_ENGINE_CMD=            # blank -> Python fallback (default in this environment)
ALTERYX_WORKFLOW_DIR=alteryx
```

## 8. Verify

```powershell
python -c "import xml.etree.ElementTree as ET; ET.parse('alteryx/01_ingestion.yxmd'); ET.parse('alteryx/02_data_quality.yxmd')"
pytest -q tests/test_phase12_alteryx_workflows.py
python run_pipeline.py --scan   # inspect data/processed/<name>.json for alteryx_ingestion / alteryx_dq
```

## 9. Related documents

- [`validation.md`](validation.md) - stage 3, the validator this phase's DQ fallback reuses
- [`ingestion.md`](ingestion.md) - stages 1-2 that feed this one
- [`data-flow.md`](data-flow.md) - full pipeline stage list
- [`system-components.md`](system-components.md) - Phase 12-13 component spec (03/04 workflows + star-schema load remain Phase 13)
