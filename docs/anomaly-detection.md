# InsightForge AI - Anomaly Detection: Z-Score, IQR, Rolling Baseline & Isolation Forest

> Phases 18-19 deliverable (spec Phases 35-38, part of FR-11). `src/anomaly_detection.py`
> implements all four detectors FR-11 names. Fusing them into one result per
> metric/date and writing it to the `anomalies` table is Phase 20 - **this
> module never touches the database**.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 35-38 (verbatim):

> PHASE 35 - Z-SCORE ANOMALY DETECTION: Implement Z-score detection. Store:
> metric, date, score, threshold, anomaly status.
> PHASE 36 - IQR ANOMALY DETECTION: Implement IQR. Use: Q1, Q3, IQR, Lower
> Bound, Upper Bound. Detect outliers.
> PHASE 37 - ROLLING BASELINE: Create rolling mean, median, standard
> deviation. Compare current values against expected range.
> PHASE 38 - ISOLATION FOREST: Implement sklearn IsolationForest. Use
> appropriate features. Return anomaly score, prediction, confidence.

None of the four give a numeric threshold, multiplier, window size, or
contamination rate - same spec-silent-formula situation as Phases
14/15/17. Resolved here with standard, documented defaults, all
`.env`-configurable:

| Setting | Default |
|---|---|
| `ZSCORE_THRESHOLD` | 3.0 |
| `IQR_MULTIPLIER` | 1.5 |
| `ROLLING_WINDOW_DAYS` | 7 |
| `ROLLING_BASELINE_MULTIPLIER` | 2.0 |
| `IFOREST_CONTAMINATION` | `"auto"` (sklearn's own data-driven default) |
| `IFOREST_RANDOM_STATE` | 42 (fixed - Isolation Forest is randomised; a project that never fakes results also never lets them be non-reproducible) |

## 2. Why this module doesn't write to the database

`docs/system-components.md`'s Anomaly detection entry attributes
"persist to `anomalies`" only to the **fusion** step (Phase 20), not to the
individual detectors. The `anomalies` table's columns (`detector_votes`,
`confidence`, `severity`, `persistence_days`) are all fusion/severity
concepts that don't make sense per individual detector, and there's no
unique constraint enabling a partial-row upsert pattern. So every
detector's results stay in-memory, the same "pure logic, not persisted"
shape `src/change_detection.py` (Phase 17) already established - Phase 20
will call into this module and fuse all four detectors' output before the
one INSERT into `anomalies`.

## 3. Input: the daily KPI time series

All four detectors run against `src.change_detection.fetch_period_series(db,
"day")` - the exact same day-grain, 10-metric query already used by Phase
15's `sql/kpi_queries.sql`, Phase 16's `sql/views.sql`, and Phase 17's
`change_detection.py`. `daily_metric_series(db, metric)` extracts one
metric's non-`None` values, ordered ascending by date (a metric like
`avg_shipping_days` can be `None` on a day with no shipped orders - those
days are skipped for that metric, not treated as zero).

## 4. The four detectors

### Z-score (`detect_zscore`) - whole-series baseline

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

### IQR (`detect_iqr`) - whole-series baseline

Q1/Q3 via `numpy.percentile` (linear interpolation, the standard default):

```
iqr = q3 - q1
lower_bound = q1 - IQR_MULTIPLIER * iqr
upper_bound = q3 + IQR_MULTIPLIER * iqr
is_outlier = value < lower_bound or value > upper_bound
```

### Rolling baseline (`detect_rolling_baseline`) - trailing window

Deliberately different from Z-score/IQR's whole-series baseline: for each
day, mean/median/std come from the **preceding** `ROLLING_WINDOW_DAYS` days
only (never including the day itself), then that day's actual value is
tested against the range the trailing window predicts:

```
rolling_mean, rolling_median, rolling_std  <- the ROLLING_WINDOW_DAYS days before today
lower_bound = rolling_mean - ROLLING_BASELINE_MULTIPLIER * rolling_std
upper_bound = rolling_mean + ROLLING_BASELINE_MULTIPLIER * rolling_std
is_anomaly = value < lower_bound or value > upper_bound
```

This is the standard, causally-correct meaning of "rolling baseline"
(predict the expected range from history, then test the new point) - it's
why this is a genuinely different detector, not a re-run of Z-score. The
first `ROLLING_WINDOW_DAYS` days of any series have no trailing window yet
and are **omitted from the result entirely** (not flagged `False` -
genuinely not judged).

### Isolation Forest (`detect_isolation_forest`)

`sklearn.ensemble.IsolationForest` fit on the metric's own daily values,
reshaped `(n, 1)` - the simplest defensible feature set for a univariate KPI
series, since the spec's "use appropriate features" names nothing concrete
to build multivariate features from. `decision_function` returns *lower =
more anomalous*; this module negates it (`anomaly_score = -decision_function`,
so higher always means "more anomalous", matching Z-score/IQR's direction)
and derives `confidence` via a logistic squash of the raw decision value
into `(0, 1)` - **this project's own mapping, not an sklearn built-in**.
`prediction` is `"anomaly"`/`"normal"` (sklearn's `-1`/`1` translated to
readable strings).

### Minimum sample size

Every detector returns `[]` for a metric with fewer than
`ANOMALY_MIN_SAMPLE_SIZE` (**5**) daily points - not enough history to judge
yet, not an error. Rolling baseline additionally needs
`> ROLLING_WINDOW_DAYS` points before it can produce even one result.

## 5. API

| Symbol | Purpose |
|---|---|
| `ZScoreResult` / `IqrResult` / `RollingBaselineResult` / `IsolationForestResult` | one per (metric, date) |
| `zscore_threshold()` / `iqr_multiplier()` / `rolling_window_days()` / `rolling_baseline_multiplier()` / `iforest_contamination()` / `iforest_random_state()` | read the env vars, fresh each call |
| `detect_zscore(series, metric, threshold=None)` | pure - no DB |
| `detect_iqr(series, metric, multiplier=None)` | pure - no DB |
| `detect_rolling_baseline(series, metric, window=None, multiplier=None)` | pure - no DB |
| `detect_isolation_forest(series, metric, contamination=None, random_state=None)` | pure - no DB |
| `daily_metric_series(db, metric)` | one metric's `[(date, value), ...]` from `fact_sales` |
| `detect_all_zscore` / `detect_all_iqr` / `detect_all_rolling_baseline` / `detect_all_isolation_forest` `(db, metrics=METRICS, ...)` | `{metric: [Result, ...]}` for every metric |

## 6. Configuration

New env vars (Phase 18 + 19 combined):
```
ZSCORE_THRESHOLD=3.0
IQR_MULTIPLIER=1.5
ROLLING_WINDOW_DAYS=7
ROLLING_BASELINE_MULTIPLIER=2.0
IFOREST_CONTAMINATION=auto
IFOREST_RANDOM_STATE=42
```

## 7. Verify

```powershell
pytest -q tests/test_phase18_anomaly_detection.py tests/test_phase19_rolling_isolation_forest.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase18_anomaly_detection.py tests/test_phase19_rolling_isolation_forest.py
```

```python
from src.anomaly_detection import detect_all_zscore, detect_all_iqr, detect_all_rolling_baseline, detect_all_isolation_forest
from src.database import Database
db = Database()
print(detect_all_rolling_baseline(db, metrics=("revenue",)))
print(detect_all_isolation_forest(db, metrics=("revenue",)))
```

## 8. Related documents

- [`change-detection.md`](change-detection.md) - `fetch_period_series`, the shared input source
- [`kpi-engine.md`](kpi-engine.md) - the 10 metric formulas
- [`database-schema.md`](database-schema.md) - the `anomalies` table Phase 20 will write to
