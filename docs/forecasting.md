# InsightForge AI - Forecasting & Forecast Evaluation

> Phase 25 deliverable (spec Phases 49-50, FR-18). `src/forecasting.py`
> produces exponential-smoothing forecasts for revenue, profit and orders
> at 7- and 30-day horizons, evaluates them with MAE/RMSE/MAPE, and
> persists to the pre-built `forecast_results` table.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, FR-18 (as captured in `docs/requirements.md`):

> Exponential-smoothing forecasts for revenue/profit/orders at 7 and 30
> days, stored with MAE/RMSE/MAPE.

`docs/business-questions.md` names the module and technique directly ("What
will happen next month?" -> `src/forecasting.py`, exponential smoothing 7 &
30 days -> `forecast_results`) and separately names forecast evaluation
("How accurate have our forecasts been?" -> MAE/RMSE/MAPE). **No specific
exponential-smoothing variant, smoothing parameters, or confidence-interval
method is specified anywhere** - this project's own choices follow, in the
same spirit as Phase 21's PSI thresholds or Phase 18's Z-score threshold.

## 2. Method: Holt's linear trend exponential smoothing

Revenue/profit/orders all trend over the dataset's ~8-month window, so
**simple** exponential smoothing (level only) would systematically lag a
trending series. **Holt's linear trend method** ("double" exponential
smoothing - level *and* trend) is the standard fix and is hand-rolled here
rather than pulled from `statsmodels` (not a project dependency): every
other statistical technique in this codebase - PSI, Z-score/IQR, rolling
baseline, RFM quintiles - is likewise implemented directly with
`numpy`/pure Python, reserving `scikit-learn` for genuinely complex ML
(Isolation Forest, Phase 19).

Recursion, one day at a time over the metric's full daily history:

```
level_t = alpha * y_t + (1 - alpha) * (level_{t-1} + trend_{t-1})
trend_t = beta  * (level_t - level_{t-1}) + (1 - beta) * trend_{t-1}
forecast(h) = level_T + h * trend_T
```

`FORECAST_ALPHA` (level smoothing, default **0.3**) and `FORECAST_BETA`
(trend smoothing, default **0.1**) are this project's own `.env`-configurable
defaults - same pattern as `ZSCORE_THRESHOLD` or the PSI bands: a documented
operational choice, not a spec requirement.

## 3. Confidence band and evaluation (this project's own choices)

- **Confidence band**: the standard deviation of the model's own in-sample
  one-step-ahead residuals (`actual - (level_{t-1} + trend_{t-1})`), widened
  by `sqrt(h)` as the horizon grows, and turned into a 95% z-interval
  (`± 1.95996 * std * sqrt(h)`). `lower_bound`/`upper_bound` are `NULL`-safe
  (`0.0` width) when there are fewer than 2 residuals to judge spread from.
- **Evaluation (MAE/RMSE/MAPE)**: a standard walk-forward holdout - fit on
  every day except the last `horizon` days, forecast `horizon` days ahead,
  and score against the actual held-out values. This is real backtested
  accuracy, not a fabricated number: a metric/horizon combo without enough
  history to both train (`FORECAST_MIN_TRAIN_DAYS`, default **14** days) and
  hold out `horizon` days is skipped entirely (`forecast_metric` returns
  `None`), the same "skip, don't fake" convention as `drift_detection`'s
  `MIN_BASELINE_ROWS`/`MIN_CURRENT_ROWS`. MAPE itself is `NULL` when every
  held-out actual was `0` (undefined percentage error).

## 4. Persistence: the pre-built `forecast_results` table

Unlike `drift_results` (a new table introduced by its own phase),
`forecast_results` already existed in `sql/schema.sql` since Phase 8:

```sql
CREATE TABLE forecast_results (
    forecast_id, run_id (FK -> pipeline_runs, NOT NULL), metric,
    horizon_days (CHECK IN (7, 30)), forecast_date, forecast_value,
    lower_bound, upper_bound, model DEFAULT 'exponential_smoothing',
    mae, rmse, mape, created_at,
    UNIQUE (run_id, metric, horizon_days, forecast_date)
);
```

One row per `(metric, horizon, forecast_date)` - so a `horizon_days=7`
forecast writes 7 rows (the next 7 days) and a `horizon_days=30` forecast
writes 30 rows; `mae`/`rmse`/`mape` are the same backtested figures repeated
across every row of that metric/horizon combo (they describe the model that
produced the whole trajectory, not a single day).

## 5. Why forecasting is on-demand but still persists (not orchestrator-wired)

This is a new combination relative to earlier phases:

- Phases 20/21 (anomaly fusion, drift) **persist and auto-wire** into
  `src/orchestrator.py`'s per-file pipeline, because they only ever need the
  just-ingested file's own dates.
- Phases 17/22/23/24 (change detection, RCA, business impact, RFM/product
  intelligence) are **on-demand and don't persist at all** - no table
  stores their output.
- Forecasting needs the metric's **whole** daily history
  (`src.anomaly_detection.daily_metric_series`, the same helper Phases
  18-19 use to fetch `fact_sales`-derived daily series) to fit a trend, not
  just one file's dates - the same reason RCA/impact/RFM stayed on-demand.
  `docs/data-flow.md`'s stage table places "Forecast" (13) in the same
  batch-analytics block as RCA (11) and Business impact (12), after the two
  stages the orchestrator actually implements (7 = anomalies, 8 = drift).
  But `forecast_results.run_id` **is** `NOT NULL` (unlike RCA/impact/RFM,
  which have no table), so `persist_forecast_results(db, run_id, points)`
  still takes a `run_id` - it is simply called on demand (a script, a test,
  or later the recommendation engine / Streamlit Forecast page) rather than
  from `src/orchestrator.py`.

A failure or insufficient-history skip never affects `pipeline_runs.status`
- forecasting isn't invoked from the per-file pipeline at all.

## 6. API

| Symbol | Purpose |
|---|---|
| `FORECAST_METRICS` | `("revenue", "profit", "orders")` - the 3 FR-18 metrics |
| `HORIZONS` | `(7, 30)` |
| `ForecastPoint` | mirrors one `forecast_results` row |
| `default_alpha()` / `default_beta()` / `min_train_days()` | read the env vars, fresh each call |
| `holt_fit(series, alpha, beta)` | pure - final `(level, trend)` + in-sample residuals |
| `holt_forecast(series, horizon, alpha, beta)` | pure - `horizon` x `(value, lower, upper)` |
| `evaluate_forecast_accuracy(actual, predicted)` | pure - `(mae, rmse, mape)` |
| `backtest_forecast(series, horizon, alpha, beta)` | pure - walk-forward holdout, `None` below the minimum history |
| `forecast_metric(db, metric, horizon, alpha=None, beta=None)` | queries `fact_sales` via `daily_metric_series`, `None` below the minimum history |
| `generate_all_forecasts(db, metrics=..., horizons=...)` | every metric x horizon combo |
| `persist_forecast_results(db, run_id, points)` | bulk-insert into `forecast_results` |
| `generate_and_persist_forecast(db, run_id, metrics=..., horizons=...)` | the on-demand entry point |

## 7. Configuration

New env vars:
```
FORECAST_ALPHA=0.3
FORECAST_BETA=0.1
FORECAST_MIN_TRAIN_DAYS=14
```

## 8. Verify

```powershell
python scripts/apply_schema.py   # forecast_results already exists (Phase 8)
pytest -q tests/test_phase25_forecasting.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase25_forecasting.py
```

```python
from src.database import Database
from src.forecasting import generate_and_persist_forecast

db = Database()
points = generate_and_persist_forecast(db, run_id=1)
```

```sql
SELECT metric, horizon_days, forecast_date, forecast_value, mae, rmse, mape
FROM forecast_results ORDER BY metric, horizon_days, forecast_date;
```

## 9. Related documents

- [`database-schema.md`](database-schema.md) - the `forecast_results` table (Phase 8)
- [`anomaly-detection.md`](anomaly-detection.md) - `daily_metric_series`, reused here
- [`data-flow.md`](data-flow.md) - stage 13
