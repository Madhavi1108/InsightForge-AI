# InsightForge AI - Z-Score & IQR Anomaly Detection

> Phase 18 deliverable (spec Phases 35-36, part of FR-11). `src/anomaly_detection.py`
> implements two of the four detectors FR-11 names. Rolling baseline and
> Isolation Forest arrive in Phase 19 (same module); fusing all four into
> one result per metric/date and writing it to the `anomalies` table is
> Phase 20 - **this module never touches the database**.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 35-36 (verbatim):

> PHASE 35 - Z-SCORE ANOMALY DETECTION: Implement Z-score detection. Store:
> metric, date, score, threshold, anomaly status.
> PHASE 36 - IQR ANOMALY DETECTION: Implement IQR. Use: Q1, Q3, IQR, Lower
> Bound, Upper Bound. Detect outliers.

No numeric threshold or multiplier is given anywhere in the spec (no
`|z| > 3`, no `1.5 * IQR`) - same spec-silent-formula situation as Phases
14/15/17. Resolved here with the two most standard, textbook values for
each method, both `.env`-configurable:

- `ZSCORE_THRESHOLD` (default **3.0**)
- `IQR_MULTIPLIER` (default **1.5**)

## 2. Why this module doesn't write to the database

`docs/system-components.md`'s Anomaly detection entry attributes
"persist to `anomalies`" only to the **fusion** step (Phase 20), not to the
individual detectors. The `anomalies` table's columns (`detector_votes`,
`confidence`, `severity`, `persistence_days`) are all fusion/severity
concepts that don't make sense per individual detector, and there's no
unique constraint enabling a partial-row upsert pattern. So Z-score and IQR
results stay in-memory (`list[ZScoreResult]` / `list[IqrResult]`), the same
"pure logic, not persisted" shape `src/change_detection.py` (Phase 17)
already established - Phase 20 will call into this module and fuse its
output with rolling-baseline/Isolation-Forest results before the one INSERT
into `anomalies`.

## 3. Input: the daily KPI time series

Both detectors run against `src.change_detection.fetch_period_series(db,
"day")` - the exact same day-grain, 10-metric query already used by Phase
15's `sql/kpi_queries.sql`, Phase 16's `sql/views.sql`, and Phase 17's
`change_detection.py`. `daily_metric_series(db, metric)` extracts one
metric's non-`None` values, ordered ascending by date (a metric like
`avg_shipping_days` can be `None` on a day with no shipped orders - those
days are skipped for that metric, not treated as zero).

## 4. The two detectors

### Z-score (`detect_zscore`)

Population mean and standard deviation over the **whole** series (the point
being tested is included in its own baseline - the spec gives no
leave-one-out instruction, so this is the simplest defensible choice):

```
z = (value - mean) / std
is_anomaly = std != 0 and abs(z) > ZSCORE_THRESHOLD
```

A constant series (`std == 0`) has an undefined z-score - `z_score` is
`None` and the day is never flagged, rather than raising a
division-by-zero error.

### IQR (`detect_iqr`)

Q1/Q3 via `numpy.percentile` (linear interpolation, the standard default):

```
iqr = q3 - q1
lower_bound = q1 - IQR_MULTIPLIER * iqr
upper_bound = q3 + IQR_MULTIPLIER * iqr
is_outlier = value < lower_bound or value > upper_bound
```

### Minimum sample size

Both detectors return `[]` for a metric with fewer than
`ANOMALY_MIN_SAMPLE_SIZE` (**5**) daily points - not enough history to judge
yet, not an error.

## 5. API

| Symbol | Purpose |
|---|---|
| `ZScoreResult` / `IqrResult` | one per (metric, date) |
| `zscore_threshold()` / `iqr_multiplier()` | read the env vars, fresh each call |
| `detect_zscore(series, metric, threshold=None)` | pure - no DB |
| `detect_iqr(series, metric, multiplier=None)` | pure - no DB |
| `daily_metric_series(db, metric)` | one metric's `[(date, value), ...]` from `fact_sales` |
| `detect_all_zscore(db, metrics=METRICS, threshold=None)` | `{metric: [ZScoreResult, ...]}` |
| `detect_all_iqr(db, metrics=METRICS, multiplier=None)` | `{metric: [IqrResult, ...]}` |

## 6. Configuration

New env vars:
```
ZSCORE_THRESHOLD=3.0
IQR_MULTIPLIER=1.5
```

## 7. Verify

```powershell
pytest -q tests/test_phase18_anomaly_detection.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase18_anomaly_detection.py
```

```python
from src.anomaly_detection import detect_all_zscore, detect_all_iqr
from src.database import Database
db = Database()
print(detect_all_zscore(db, metrics=("revenue",)))
```

## 8. Related documents

- [`change-detection.md`](change-detection.md) - `fetch_period_series`, the shared input source
- [`kpi-engine.md`](kpi-engine.md) - the 10 metric formulas
- [`database-schema.md`](database-schema.md) - the `anomalies` table Phase 20 will write to
