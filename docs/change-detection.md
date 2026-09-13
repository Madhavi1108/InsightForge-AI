# InsightForge AI - Period Comparison & Business Change Detection

> Phase 17 deliverable (spec Phases 33-34, FR-09/FR-10). `src/change_detection.py`
> is the first Python analytics module - it compares the two most recent
> complete periods across the 10 core KPIs and flags significant movements.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 33-34 (verbatim):

> PHASE 33 - PERIOD COMPARISON ENGINE: Compare day vs previous day, week vs
> previous week, month vs previous month. Generate percentage changes.
> PHASE 34 - BUSINESS CHANGE DETECTION: Create `src/change_detection.py`.
> Detect significant movements. Example: Revenue 18.4%, Profit 23.7%,
> Orders 11.2%, Returns 8.3%.

Three grains, one file name, an illustrative output example - **no
significance threshold, no output schema, no persistence table**. Resolved
here (same pattern as Phase 14/15's spec-silent formulas):

- **Threshold**: `CHANGE_DETECTION_THRESHOLD_PCT` (default **10.0**),
  read fresh from the environment each call - this project's own
  operational definition, not a spec number.
- **Persistence**: none. `grep -n "change" sql/schema.sql` is empty, and
  `docs/data-flow.md` stage 8 says "Writes: change records" - not a table.
  This module returns a `list[ChangeRecord]`; it is **not** wired into
  `src/orchestrator.py`'s per-file pipeline. It's a downstream/batch
  analytics module, grouped with Phase 15/16 under "Analytics", meant to be
  called on demand (later: by anomaly detection, recommendations, the
  Streamlit/Power BI/reporting layers).
- **Output shape**: `(metric, current, previous, % change, direction)`
  (`docs/system-components.md`), extended with `magnitude` and `significant`
  per FR-10 ("movements beyond threshold flagged with direction and
  magnitude").

## 2. Why it queries `fact_sales` directly, not the Phase 16 views

`daily_kpis`/`monthly_kpis` (Phase 16) can't be safely re-aggregated in
Python for the week grain: summing daily `customers` (a distinct count)
across days would double-count a customer who ordered on two different days
in the same week. So `src/change_detection.py` recomputes the same 10 KPI
formulas fresh, grouped by `order_date` / `date_trunc('week', order_date)` /
`date_trunc('month', order_date)` - the week grain uses the same
`date_trunc` pattern `monthly_kpis` already established for month, avoiding
any ISO-week year-boundary arithmetic.

## 3. The comparison rule

For each grain, take the **two most recent periods present** in the data -
the latest complete period vs. the one immediately before it (not an
arbitrary historical range, and not necessarily "yesterday" if the pipeline
hasn't ingested every calendar day). Fewer than two periods -> `[]` (a young
dataset, not an error).

## 4. `ChangeRecord` fields

| Field | Meaning |
|---|---|
| `grain` | `"day"` \| `"week"` \| `"month"` |
| `metric` | one of the 10 keys in `docs/kpi-engine.md` (`revenue`, `profit`, `margin_pct`, `orders`, `customers`, `units`, `aov`, `return_rate_pct`, `avg_discount_pct`, `avg_shipping_days`) |
| `period_key` / `previous_period_key` | ISO date strings - the day, the week's Monday, or the month's first day |
| `current` / `previous` | the metric's value in each period |
| `pct_change` | `100 * (current - previous) / abs(previous)`, rounded to 2dp; `None` when `previous == 0` (undefined) or either side is missing |
| `direction` | `"up"` / `"down"` / `"flat"` (`"flat"` whenever either side is `None`) |
| `magnitude` | `abs(pct_change)`, or `None` when `pct_change` is `None` |
| `significant` | `magnitude >= threshold`, **or** a `0 -> non-zero` move (undefined `%`, but still a genuine event - never silently dropped just because the ratio can't be computed) |

## 5. `src/change_detection.py` API

| Symbol | Purpose |
|---|---|
| `GRAINS` | `("day", "week", "month")` |
| `METRICS` | the 10 metric keys, in order |
| `significant_change_threshold_pct()` | reads `CHANGE_DETECTION_THRESHOLD_PCT`, default 10.0 |
| `build_change_records(current_row, previous_row, grain, period_key, previous_period_key, threshold_pct=None)` | pure - no DB; one `ChangeRecord` per metric present in both rows |
| `compare_period(db, grain, threshold_pct=None)` | queries `fact_sales` for `grain`, compares the last two periods |
| `compare_all_periods(db, threshold_pct=None)` | `compare_period` for all 3 grains |

## 6. Configuration

New env var, not previously declared:
```
CHANGE_DETECTION_THRESHOLD_PCT=10.0
```

## 7. Verify

```powershell
pytest -q tests/test_phase17_change_detection.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase17_change_detection.py
```

```python
from src.change_detection import compare_all_periods
from src.database import Database
for r in compare_all_periods(Database()):
    print(r)
```

## 8. Related documents

- [`kpi-engine.md`](kpi-engine.md) - the 10 formulas this module recomputes per grain
- [`analytical-views.md`](analytical-views.md) - the views this module deliberately does *not* reuse, and why
- [`data-flow.md`](data-flow.md) - stage 8, "change records" (not persisted)
