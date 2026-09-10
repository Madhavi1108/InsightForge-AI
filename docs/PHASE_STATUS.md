# InsightForge AI - Phase Status

Single source of truth for build progress. Updated after every phase.

The 69 phases of the master specification (`INSIGHTFORGE AI.pdf`) are tracked here
as a **consolidated 36-phase** roadmap. No scope is dropped - see
[`PHASE_MAP.md`](PHASE_MAP.md) for the exact 69 -> 36 crosswalk.

Legend: `NOT STARTED` | `IN PROGRESS` | `PARTIAL` | `COMPLETE` | `BLOCKED`

| Phase | Spec | Title | Status | Notes |
|------:|:----:|-------|--------|-------|
| 0  | 0     | Project initialization | **COMPLETE** | Repo scaffold, venv (Python 3.12.10), Git, secrets excluded |
| 1  | 1-4   | Architecture, requirements, business questions & dataset design | **COMPLETE** | `docs/architecture.md`, `system-components.md`, `data-flow.md`, `security.md`, `requirements.md`, `business-questions.md`, `dataset-design.md` |
| 2  | 5-6   | Data generator & business data model | **COMPLETE** | `scripts/generate_dataset.py`; 150k clean rows, window 2026-01-01..2026-09-08, seed 20260909 -> `data/full_dataset.csv`; Revenue/Profit model asserted |
| 3  | 7-9   | Customer, product & geographical data | **COMPLETE** | `generate_dataset.py`: lognormal product `appeal` (top-decile revenue concentration), ~15% repeat customers via lognormal `purchase_weight` + boost, segment-driven basket size & discount, expanded Indian geography (23 states / 74 cities) with metro-weighted city demand + faster metro shipping; internal weight columns kept out of the 22-col CSV; determinism + all Phase 2 invariants preserved; `tests/test_phase03_customer_product_geo.py` |
| 4  | 10    | Seasonality engine | **COMPLETE** | `generate_dataset.py`: pure `day_seasonality_weights()` helper multiplies weekend uplift, last-3-day month-end uplift, curated `FESTIVE_PERIODS` sale windows (max, non-compounding) and a mid-Nov-peak annual cosine curve; mean-normalised, so only `Order_Date` distribution changes. `generate_orders` draws dates via `rng.choice(p=...)`; `summarise()` reports weekend/festive shares. Determinism + all Phase 2/3 invariants preserved; `tests/test_phase04_seasonality.py` |
| 5  | 11-12 | Data quality injection & business anomaly ground truth | **COMPLETE** | `generate_dataset.py`: `inject_data_quality_issues()` (9-category taxonomy -> `data/full_dataset_issues.csv` issue log, each defect mapped to a DQ dimension, ~1.2% of rows, `Revenue`/`Profit` left inconsistent) + `inject_business_anomaly()` (two-tier `West -> Electronics -> Laptop` anomaly over 2026-08-01..09-08: orders/revenue down, discount/returns/shipping up; rows re-derived so they stay valid). `build_delivery_dataset()` orchestrates clean -> anomaly -> issues; child RNGs `seed+1`/`seed+2` keep the clean stream + all Phase 2-4 tests untouched. `docs/anomaly-ground-truth.md` auto-generated with realised deltas. CLI `--clean` / `--no-anomaly` / `--no-issues`. Seed-reproducible (NFR-07). `tests/test_phase05_dataquality_anomaly.py` |
| 6  | 13    | Daily data generation & data/ lifecycle directories | NOT STARTED | |
| 7  | 14    | PostgreSQL installation & configuration | NOT STARTED | Docker container (decided) |
| 8  | 15-17 | Star schema, operational tables & indexing | NOT STARTED | |
| 9  | 18    | Database connection layer | NOT STARTED | `src/database.py` |
| 10 | 19-20 | File ingestion & SHA-256 fingerprinting | NOT STARTED | orchestrator lands here |
| 11 | 21-22 | Data contract & schema validation | NOT STARTED | |
| 12 | 23-24 | Alteryx ingestion & data-quality workflows | NOT STARTED | Real `.yxmd` + Python fallback (decided) |
| 13 | 25-26 | Alteryx sales / customer / product ETL | NOT STARTED | |
| 14 | 27-29 | Data quality engine, score & rejected-record management | NOT STARTED | |
| 15 | 30-31 | Core SQL KPI engine & advanced SQL analytics | NOT STARTED | |
| 16 | 32    | Analytical views | NOT STARTED | |
| 17 | 33-34 | Period comparison & business change detection | NOT STARTED | |
| 18 | 35-36 | Z-score & IQR anomaly detection | NOT STARTED | |
| 19 | 37-38 | Rolling baseline & Isolation Forest | NOT STARTED | |
| 20 | 39-40 | Anomaly fusion & severity engine | NOT STARTED | |
| 21 | 41-42 | Data drift detection & drift reporting | NOT STARTED | |
| 22 | 43-45 | Root-cause engine, contribution analysis & RCA confidence | NOT STARTED | |
| 23 | 46    | Business impact engine | NOT STARTED | |
| 24 | 47-48 | Customer RFM & product intelligence | NOT STARTED | |
| 25 | 49-50 | Forecasting & forecast evaluation | NOT STARTED | |
| 26 | 51-52 | Recommendation engine & priority engine | NOT STARTED | |
| 27 | 53    | AI evidence layer | NOT STARTED | Gemini (decided) |
| 28 | 54-55 | AI Analyst & AI explanation engine | NOT STARTED | |
| 29 | 56-57 | Natural-language-to-SQL & AI security | NOT STARTED | |
| 30 | 58-59 | Streamlit application & pipeline control | NOT STARTED | |
| 31 | 60    | Power BI data model | NOT STARTED | |
| 32 | 61-63 | Power BI executive, intelligence & forecast/pipeline pages | NOT STARTED | |
| 33 | 64-65 | Automated Excel & PDF reports | NOT STARTED | |
| 34 | 66    | Alert & email engine | NOT STARTED | |
| 35 | 67    | Observability & failure recovery | NOT STARTED | includes scheduler wiring |
| 36 | 68-69 | Security, testing, performance & final end-to-end delivery | NOT STARTED | |

## Environment notes

- **Python**: 3.12.10, project venv at `.venv/` (installed via winget).
- **PostgreSQL**: to be provisioned via Docker in Phase 7 (`config/docker-compose.postgres.yml`).
- **Alteryx**: not installed in this environment. Real `.yxmd` workflow files plus a
  Python ETL fallback (identical contract output) will be built; automated Alteryx
  execution will be documented as unverified rather than faked.
- **Power BI Desktop**: installed. Automated dataset refresh is a documented manual/gateway step.
- **LLM**: Google Gemini via `google-generativeai`; deterministic template fallback when no key.
- **Docker**: available (v29.7.2).
