# InsightForge AI - Data Drift Detection & Drift Reporting

> Phase 21 deliverable (spec Phases 41-42, FR-13). `src/drift_detection.py`
> monitors 6 distributions via the Population Stability Index (PSI),
> classifies each Normal/Warning/Drift Detected, and persists to the new
> `drift_results` table.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 41-42 (verbatim):

> PHASE 41 - DATA DRIFT DETECTION: Create `src/drift_detection.py`. Monitor:
> price distribution, quantity distribution, discount distribution,
> shipping distribution, category mix, region mix.
> PHASE 42 - DRIFT REPORTING: Generate Normal/Warning/Drift Detected. Store
> historical drift results.

Only one file is named (`src/drift_detection.py`); Phase 42 ("drift
reporting") has no separate file - its classification and storage are
folded into the same module, the same way Phase 20's severity engine folded
into `anomaly_fusion.py` rather than getting its own file. **No statistical
method, threshold, or persistence table is specified anywhere** - confirmed
this session: `grep -i "drift" sql/schema.sql` was empty before this phase,
and `docs/data-flow.md`'s "drift history" was plain text, not a
backtick-quoted real table name like every other stage's output.

## 2. Method: Population Stability Index (PSI)

Unlike most spec-silent formulas in this project, **PSI's own conventional
thresholds are an external, industry-standard convention** (used widely in
credit risk and ML monitoring for exactly this "has this distribution
shifted" question), not invented here - and they map directly onto the
spec's three-tier output:

```
PSI = sum over bins/categories of: (current_pct - baseline_pct) * ln(current_pct / baseline_pct)
```

| PSI | Status |
|---|---|
| `< 0.1` | Normal |
| `0.1 - 0.25` | Warning |
| `>= 0.25` | Drift Detected |

For the 4 **continuous** features (price, quantity, discount, shipping),
bin edges are the baseline's own decile boundaries (`PSI_BINS = 10`) - the
baseline distribution is therefore uniform across bins by construction, and
all drift signal comes from how the *current* window's values redistribute
across those same bins. For the 2 **categorical** features (category mix,
region mix), the natural category values are the bins directly - no
binning needed.

## 3. Baseline vs. current windows (this project's own choice)

The spec names no window sizes. `DRIFT_BASELINE_DAYS` (default **30**) and
`DRIFT_CURRENT_DAYS` (default **7**) are `.env`-configurable: the "current"
window is the trailing `DRIFT_CURRENT_DAYS` days ending at the date passed
in (the orchestrator passes the just-ingested file's own latest
`Order_Date`); the "baseline" window is the `DRIFT_BASELINE_DAYS` days
immediately before that. Both windows need a minimum row count
(`MIN_BASELINE_ROWS = 30`, `MIN_CURRENT_ROWS = 5`) or the feature is skipped
entirely for that run - not enough history to judge yet, not an error.

## 4. Persistence: the new `drift_results` table

`sql/schema.sql` gains a new table (Phase 21):

```sql
CREATE TABLE drift_results (
    drift_id, run_id (FK -> pipeline_runs, NOT NULL), feature, psi_score,
    status (CHECK IN ('Normal','Warning','Drift Detected')),
    baseline_start, baseline_end, current_start, current_end,
    baseline_count, current_count, detail JSONB, created_at
);
```

`business-questions.md` names both a Streamlit page and a Power BI page as
consumers of drift results - a real table is the consistent choice with
every other phase's pattern of feeding both surfaces from PostgreSQL,
rather than a pure in-memory/file artifact.

## 5. Why drift detection is wired into the orchestrator

Same reasoning as Phase 20's anomaly fusion: `drift_results.run_id` is
`NOT NULL REFERENCES pipeline_runs(run_id)`, so a row only makes sense tied
to the run that checked it. `src/orchestrator.py::run_file()` calls
`detect_and_persist_drift` as **stage 8**, right after anomaly fusion (stage
7). **It never affects the run's terminal status** - advisory analytics,
not a correctness gate (the Phase 14 DQ gate already owns
`SUCCESS`/`WARNING`/`FAILED`). A failure here is caught and recorded as
`stage_metrics.drift.error`; the run still closes exactly as the DQ gate
determined.

## 6. API

| Symbol | Purpose |
|---|---|
| `FEATURES` | the 6 FR-13 distributions -> `(fact_sales column, is_categorical)` |
| `DriftResult` | mirrors the `drift_results` table |
| `classify_psi(psi)` | the 3-tier PSI classification |
| `compute_psi_continuous(baseline_values, current_values, bins=10)` | pure - no DB |
| `compute_psi_categorical(baseline_values, current_values)` | pure - no DB |
| `detect_drift_for_feature(...)` | pure - one feature, `None` below the minimum row counts |
| `baseline_days()` / `current_days()` | read the env vars, fresh each call |
| `detect_all_drift(db, current_end_date, ...)` | queries `fact_sales`, all 6 features |
| `persist_drift_results(db, run_id, results)` | bulk-insert into `drift_results` |
| `detect_and_persist_drift(db, run_id, current_end_date, ...)` | the orchestrator's entry point |

## 7. Configuration

New env vars:
```
DRIFT_BASELINE_DAYS=30
DRIFT_CURRENT_DAYS=7
```

## 8. Verify

```powershell
python scripts/apply_schema.py   # creates drift_results
pytest -q tests/test_phase21_drift_detection.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase21_drift_detection.py
```

```sql
SELECT feature, psi_score, status, current_start, current_end
FROM drift_results ORDER BY created_at DESC;
```

## 9. Related documents

- [`database-schema.md`](database-schema.md) - the `drift_results` table
- [`anomaly-fusion.md`](anomaly-fusion.md) - the sibling advisory-analytics stage this one follows
- [`data-flow.md`](data-flow.md) - stage 10
