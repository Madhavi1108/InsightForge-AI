# InsightForge AI - Requirements

> Phase 1 deliverable (spec Phase 2). Functional requirements (FR) and
> non-functional requirements (NFR), each with an acceptance criterion. The phase
> that delivers each requirement is from [`PHASE_MAP.md`](PHASE_MAP.md).

## Functional requirements

| ID | Requirement | Acceptance criterion | Phase |
|----|-------------|----------------------|------:|
| FR-01 | Detect a new file dropped in `data/incoming/` | A file appearing in `data/incoming/` starts a pipeline run within one watcher poll interval; `run_pipeline.py --scan` processes all pending files | 10 |
| FR-02 | Fingerprint files and reject duplicates | SHA-256 stored in `file_registry`; re-submitting an identical file yields `SKIPPED_DUPLICATE` and no warehouse change | 10 |
| FR-03 | Validate schema against a data contract | Missing/extra columns, wrong types, or missing mandatory fields reject the file with a recorded reason | 11 |
| FR-04 | Clean and standardise records | Trimmed/normalised text, parsed dates, corrected types in `fact_sales`; raw file unchanged | 12-13 |
| FR-05 | Transform and load into PostgreSQL | Star schema (`fact_sales` + 4 dims) populated; `Revenue` and `Profit` derived per the business model | 8, 13 |
| FR-06 | Score data quality | 7 dimensions computed; overall score in `data_quality_results`; `>=95` PASS, `90-94.99` WARNING, `<90` REJECT | 14 |
| FR-07 | Retain rejected records | Every invalid row stored in `rejected_records`, classified and linked to `run_id`; nothing silently dropped | 14 |
| FR-08 | Calculate core KPIs | Revenue, Profit, Margin, Orders, Customers, Units, AOV, Return Rate, Avg Discount, Shipping Time available per day/month/region/category/product/customer | 15-16 |
| FR-09 | Compare periods | Day/week/month vs previous with % change | 17 |
| FR-10 | Detect significant business change | Movements beyond threshold flagged with direction and magnitude | 17 |
| FR-11 | Detect anomalies | Z-score, IQR, rolling baseline, Isolation Forest each run; fused into one result per metric/date in `anomalies` | 18-20 |
| FR-12 | Classify severity | Each anomaly labelled LOW/MEDIUM/HIGH/CRITICAL from deviation, impact, confidence, persistence | 20 |
| FR-13 | Detect data drift | Distribution shift in price/quantity/discount/shipping/category mix/region mix classified Normal/Warning/Drift Detected | 21 |
| FR-14 | Identify root cause | Hierarchical drill-down with primary driver, evidence, contribution, confidence | 22 |
| FR-15 | Quantify business impact | Expected vs actual revenue/profit, gaps, revenue/profit at risk, customers & orders affected | 23 |
| FR-16 | Segment customers (RFM) | Every active customer assigned one of Champions/Loyal/Potential Loyalists/New/At Risk/Lost | 24 |
| FR-17 | Score product health | Products classified (Star, High Profit, Declining, High Return, ...) with a health score | 24 |
| FR-18 | Forecast performance | Exponential-smoothing forecasts for revenue/profit/orders at 7 and 30 days, stored with MAE/RMSE/MAPE | 25 |
| FR-19 | Generate recommendations | Transparent rule-based recommendations with a 0-100 priority (severity x impact x confidence) | 26 |
| FR-20 | Explain results with AI | AI Analyst answers the standard business questions in a Summary/Evidence/Root Cause/Impact/Recommendation/Confidence structure, using only verified evidence | 27-28 |
| FR-21 | Natural-language to SQL | Safe read-only queries generated, validated, executed, and explained; unsafe queries blocked | 29 |
| FR-22 | Operational app | Streamlit app with all 11 pages and controlled pipeline actions | 30 |
| FR-23 | Executive BI | Power BI model + executive/intelligence/forecast/pipeline-health pages | 31-32 |
| FR-24 | Automated reports | Dated Excel (12 sheets) and management PDF generated per run into `reports/` | 33 |
| FR-25 | Severity-based alerts | LOW->dashboard, MEDIUM->Streamlit, HIGH->email, CRITICAL->immediate email; emails carry issue/evidence/impact/recommendation | 34 |
| FR-26 | Observability & recovery | Structured logs, run IDs, durations, retries (<=3), failure & recovery states | 35 |
| FR-27 | Audit trail | Any reported number is traceable to source rows, run ID, and archived file | 35-36 |
| FR-28 | End-to-end automation | Dropping `sales_2026_09_09.csv` runs the entire lifecycle with no manual stage execution | 36 |

## Non-functional requirements

| ID | Requirement | Acceptance criterion |
|----|-------------|----------------------|
| NFR-01 Reliability | Pipeline recovers from transient failures | DB / stage failures retry up to 3x; a `FAILED`/`PARTIAL` run leaves enough state to re-run; raw file always retained |
| NFR-02 Scalability | Handles the full dataset | Processes a 150k+ row dataset and daily files of ~10k rows within the performance budget below |
| NFR-03 Security | No credential exposure; safe queries | `.env`-only secrets, no keys in Git, parameterised SQL, read-only NL-to-SQL, prompt-injection protection, sanitized logs (see [`security.md`](security.md)) |
| NFR-04 Auditability | Full lineage | insight -> query -> rows -> run ID -> file hash -> archived file, for every metric |
| NFR-05 Performance | Bounded stage times | Per-run measurement of ingestion, ETL, DB load, SQL, analytics, report generation, and total pipeline time; recorded in `pipeline_runs.stage_metrics` |
| NFR-06 Maintainability | Understandable & tested | Architecture understandable without reading source; every important feature has a test; no complexity added purely for appearance |
| NFR-07 Reproducibility | Deterministic dataset | Fixed `RANDOM_SEED` regenerates an identical dataset, bad data, and anomaly |
| NFR-08 Portability | Optional components never block core | Alteryx absent -> Python ETL fallback; Docker/Airflow optional; Power BI refresh a documented manual step |

## Performance budget (measured, refined in Phase 36)

| Stage | Target (daily ~10k-row file) |
|-------|------------------------------|
| Ingestion + fingerprint | < 2 s |
| ETL (Python fallback) | < 20 s |
| Database load | < 10 s |
| SQL KPIs + views | < 5 s |
| Analytics + intelligence | < 30 s |
| Report generation (Excel + PDF) | < 15 s |
| **Total pipeline** | **< 90 s** |
