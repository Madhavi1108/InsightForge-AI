# InsightForge AI - Phase Map (69 -> 36)

The master specification (`INSIGHTFORGE AI.pdf`) defines **69 implementation
phases**. This project tracks progress against a **consolidated 36-phase**
roadmap: related spec phases are merged into a single build phase. **No scope is
dropped** - every spec phase 1-69 appears exactly once below, and the spec's
acceptance criteria still apply.

Phase 0 (Project initialization) is unchanged and already complete.

| New phase | Spec phases | Title | Spec wave |
|----------:|-------------|-------|-----------|
| 0  | 0     | Project initialization | Day 1 |
| 1  | 1-4   | Architecture, requirements, business questions & dataset design | Day 1 |
| 2  | 5-6   | Data generator & business data model | Day 1 |
| 3  | 7-9   | Customer, product & geographical data | Day 1 |
| 4  | 10    | Seasonality engine | Day 1 |
| 5  | 11-12 | Data quality injection & business anomaly ground truth | Day 1 |
| 6  | 13    | Daily data generation & data/ lifecycle directories | Day 1 |
| 7  | 14    | PostgreSQL installation & configuration (Docker) | Day 2 |
| 8  | 15-17 | Star schema, operational tables & indexing | Day 2 |
| 9  | 18    | Database connection layer (`src/database.py`) | Day 2 |
| 10 | 19-20 | File ingestion & SHA-256 fingerprinting | Day 2 |
| 11 | 21-22 | Data contract & schema validation | Day 2 |
| 12 | 23-24 | Alteryx ingestion & data-quality workflows | Day 2 |
| 13 | 25-26 | Alteryx sales / customer / product ETL | Day 2 |
| 14 | 27-29 | Data quality engine, score & rejected-record management | Day 2 |
| 15 | 30-31 | Core SQL KPI engine & advanced SQL analytics | Day 3 |
| 16 | 32    | Analytical views | Day 3 |
| 17 | 33-34 | Period comparison & business change detection | Day 3 |
| 18 | 35-36 | Z-score & IQR anomaly detection | Day 4 |
| 19 | 37-38 | Rolling baseline & Isolation Forest | Day 4 |
| 20 | 39-40 | Anomaly fusion & severity engine | Day 4 |
| 21 | 41-42 | Data drift detection & drift reporting | Day 4 |
| 22 | 43-45 | Root-cause engine, contribution analysis & RCA confidence | Day 4 |
| 23 | 46    | Business impact engine | Day 4 |
| 24 | 47-48 | Customer RFM & product intelligence | Day 5 |
| 25 | 49-50 | Forecasting & forecast evaluation | Day 5 |
| 26 | 51-52 | Recommendation engine & priority engine | Day 5 |
| 27 | 53    | AI evidence layer | Day 6 |
| 28 | 54-55 | AI Analyst & AI explanation engine | Day 6 |
| 29 | 56-57 | Natural-language-to-SQL & AI security | Day 6 |
| 30 | 58-59 | Streamlit application & pipeline control | Day 6 |
| 31 | 60    | Power BI data model | Day 6 |
| 32 | 61-63 | Power BI executive, intelligence & forecast/pipeline pages | Day 6 |
| 33 | 64-65 | Automated Excel & PDF reports | Day 6 |
| 34 | 66    | Alert & email engine | Day 6 |
| 35 | 67    | Observability & failure recovery | Day 6 |
| 36 | 68-69 | Security, testing, performance & final end-to-end delivery | Day 7 |

## Unchanged from the specification

- **Development rules 1-12** (never fake functionality, PostgreSQL is the source
  of truth, the LLM is an interface layer, raw data is immutable, etc.).
- **Priority tiers**: P0 (ingestion, Alteryx ETL, data quality, PostgreSQL, SQL
  analytics, Power BI, automation, anomaly detection, RCA, business impact,
  recommendations, reports, email, logging, testing) must work before P1
  (forecasting, RFM, product intelligence, data drift, Streamlit) before P2
  (AI Analyst, NL-to-SQL, agentic orchestration, Docker, Airflow).
- **7-day execution waves** and the two-person team split.
- **Final killer demo**: drop `sales_2026_09_09.csv` into `data/incoming/` and let
  the pipeline run end to end, then ask the AI Analyst "Why did revenue decrease?".
