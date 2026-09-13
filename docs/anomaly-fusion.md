# InsightForge AI - Anomaly Fusion & Severity Engine

> Phase 20 deliverable (spec Phases 39-40, FR-11/FR-12). `src/anomaly_fusion.py`
> combines the four detectors in `src/anomaly_detection.py` (Phases 18-19)
> into one unified result per metric/date, classifies its severity, and is
> the **only** anomaly-related module that writes to the database.

## 1. What the spec says and what it leaves open

`INSIGHTFORGE AI.pdf`, phases 39-40 (verbatim):

> PHASE 39 - ANOMALY FUSION: Combine Z-score, IQR, Rolling baseline,
> Isolation Forest. Generate a unified anomaly result.
> PHASE 40 - SEVERITY ENGINE: Classify LOW/MEDIUM/HIGH/CRITICAL. Use:
> percentage deviation, business impact, confidence, persistence.

No vote-agreement rule, expected-value formula, confidence formula, or
severity weights/thresholds are given anywhere - same spec-silent-formula
situation as every prior phase. All of the below is this project's own
documented operational definition.

## 2. Why fusion is wired into the orchestrator (unlike Phases 17-19)

`anomalies.run_id` is `BIGINT NOT NULL REFERENCES pipeline_runs(run_id)` -
every row **must** belong to a specific run. The raw detectors
(`src/anomaly_detection.py`) have no output table and no such constraint, so
they stayed pure, unwired, on-demand analytics. Fusion is different: an
`anomalies` row only makes sense as "what fusion found for the date(s) this
run just ingested," so `src/orchestrator.py::run_file()` calls
`detect_and_persist_anomalies` as **stage 7**, right after the Phase 14 DQ
gate.

**Fusion never affects the run's terminal status.** It's advisory analytics,
not a correctness gate - the DQ gate already owns `SUCCESS`/`WARNING`/
`FAILED`. A failure in fusion (e.g. a query error) is caught and recorded as
`stage_metrics.anomalies.error`; the run still closes exactly as the DQ gate
determined.

The orchestrator passes the just-ingested file's own distinct `Order_Date`
values as `dates=...`, so re-ingesting a later file never re-persists
anomalies for dates an earlier run already recorded.

## 3. The fusion algorithm (`fuse_metric_series`)

For one metric, across its full daily history:

1. Run all four detectors once each (`detect_zscore`, `detect_iqr`,
   `detect_rolling_baseline`, `detect_isolation_forest`), indexed by date.
2. For every day, count how many of the four flag it -> `detector_votes`
   (0-4). Rolling baseline has no verdict for the first
   `ROLLING_WINDOW_DAYS` days of any series - absent, not counted as a "no".
3. **Vote gate**: only days with `detector_votes >= ANOMALY_FUSION_MIN_VOTES`
   (default **2**) become a `FusedAnomaly` - persisting every day would
   flood the table with mostly-noise rows.
4. `expected_value`: the rolling baseline's `rolling_mean` for that date
   when available (the closest thing to a forecast for "what today should
   look like"); otherwise the Z-score detector's whole-series mean.
5. `deviation_pct` / `direction`: same shape as `src.change_detection`'s
   `_pct_change`/`_direction` - `None`/`None` when `expected` is `0` or
   unknown, `None` direction when `observed == expected` (never an invented
   `up`/`down` for a zero-magnitude move).
6. `confidence = detector_votes / 4.0` - the simplest defensible signal:
   how many independent methods agree.
7. `persistence_days`: a running streak count - how many consecutive days
   (ending at this one) have cleared the vote gate for this metric. Resets
   to 0 the moment a day doesn't clear the gate.

## 4. Severity engine

FR-12 names four inputs; the score literally uses all four:

```
score = 0.35 * min(|deviation_pct|, 100)               # percentage deviation
      + 0.25 * (BUSINESS_IMPACT_WEIGHT[metric] * 100)   # business impact
      + 0.20 * (confidence * 100)                       # confidence
      + 0.20 * (min(persistence_days, 7) / 7 * 100)     # persistence
```

Classified `>= 70` **CRITICAL**, `>= 50` **HIGH**, `>= 30` **MEDIUM**, else
**LOW**. `BUSINESS_IMPACT_WEIGHT` is a small per-metric table (the spec
names "business impact" as a factor but never defines it) - Revenue/Profit
weighted highest (1.0), `avg_shipping_days` lowest (0.3):

| Metric | Weight |
|---|---|
| revenue, profit | 1.0 |
| margin_pct | 0.8 |
| orders, return_rate_pct | 0.7 |
| customers, aov | 0.6 |
| units | 0.5 |
| avg_discount_pct | 0.4 |
| avg_shipping_days | 0.3 |

## 5. API

| Symbol | Purpose |
|---|---|
| `FusedAnomaly` | mirrors the `anomalies` table exactly |
| `min_votes()` | reads `ANOMALY_FUSION_MIN_VOTES`, fresh each call |
| `fuse_metric_series(db, metric, votes_needed=None)` | fuse one metric's full history, gated by votes |
| `fuse_all_metrics(db, metrics=METRICS, votes_needed=None)` | fuse every metric |
| `persist_anomalies(db, run_id, anomalies)` | bulk-insert into `anomalies` |
| `detect_and_persist_anomalies(db, run_id, dates=None, metrics=METRICS, votes_needed=None)` | fuse, optionally filter to `dates`, persist - the orchestrator's entry point |

## 6. Configuration

New env var:
```
ANOMALY_FUSION_MIN_VOTES=2
```

## 7. Verify

```powershell
pytest -q tests/test_phase20_anomaly_fusion.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase20_anomaly_fusion.py
```

```sql
SELECT metric, anomaly_date, detector_votes, severity, confidence, persistence_days
FROM anomalies ORDER BY anomaly_date;
```

## 8. Related documents

- [`anomaly-detection.md`](anomaly-detection.md) - the four detectors fusion combines
- [`database-schema.md`](database-schema.md) - the `anomalies` table
- [`data-flow.md`](data-flow.md) - stage 9 ("Writes: anomalies", never changes `pipeline_runs.status`)
