# InsightForge AI - Business Questions

> Phase 1 deliverable (spec Phase 3). The questions the platform must answer, each
> mapped to the **source data**, the **analytics module** that answers it, and the
> **output surface** where the answer appears.

## Core questions (from the specification)

| # | Question | Source data | Analytics module | Output surface |
|--:|----------|-------------|------------------|----------------|
| 1 | **What happened?** | `fact_sales`, `daily_kpis`, `monthly_kpis` | KPI engine + period comparison (`src/change_detection.py`) | Streamlit Overview; Power BI Executive Overview; PDF executive summary |
| 2 | **Why did revenue fall?** | `fact_sales` + dims, KPI history | Change detection -> root-cause engine (`src/root_cause.py`) -> AI Analyst | Streamlit Root Cause & AI Analyst; PDF "root causes"; email (HIGH/CRITICAL) |
| 3 | **Which region caused the decline?** | `fact_sales`, `dim_region`, `regional_performance` | Contribution analysis (region tier of `src/root_cause.py`) | Streamlit Root Cause; Power BI Business Drivers |
| 4 | **Which products are underperforming?** | `fact_sales`, `dim_product`, `product_performance` | Product intelligence (`src/product_intelligence.py`) - Declining / High Return / Low Margin / Slow Moving | Streamlit Products; Power BI Product Intelligence; Excel "Products" |
| 5 | **Which customers are at risk?** | `fact_sales`, `dim_customer`, `customer_performance` | RFM analysis (`src/rfm.py`) - At Risk / Lost segments | Streamlit Customers; Power BI Customer Intelligence; Excel "Customers" |
| 6 | **What is the estimated financial impact?** | KPI expected vs actual, RCA output | Business impact engine (`src/impact_analysis.py`) - revenue/profit gap, at-risk, customers/orders affected | Streamlit Business Impact; PDF "impact"; email body |
| 7 | **What will happen next month?** | KPI time series (`daily_kpis`) | Forecasting (`src/forecasting.py`) - exponential smoothing 7 & 30 days | Streamlit Forecast; Power BI Forecast; `forecast_results` |
| 8 | **What should management investigate?** | change + anomaly + RCA + impact + forecast | Recommendation engine + priority engine (`src/recommendations.py`) | Streamlit Overview/AI Analyst; PDF "recommendations"; email (HIGH/CRITICAL) |

## Supporting questions

| # | Question | Source data | Analytics module | Output surface |
|--:|----------|-------------|------------------|----------------|
| 9 | Which categories drove the change? | `category_performance`, `fact_sales` | Contribution analysis (category tier) | Power BI Business Drivers; Streamlit Root Cause |
| 10 | Is today's data statistically abnormal? | KPI time series | Anomaly fusion (Z-score + IQR + rolling baseline + Isolation Forest) | Streamlit Anomalies; `anomalies`; Power BI Risk & Anomalies |
| 11 | Has the shape of the data changed (drift)? | current vs baseline distributions | `src/drift_detection.py` | Streamlit Data Quality; Power BI Pipeline Health |
| 12 | How healthy was today's data? | `data_quality_results` | Data quality engine + score | Streamlit Data Quality; Excel "Data Quality"; Power BI Pipeline Health |
| 13 | How severe is this issue? | anomaly deviation, impact, confidence, persistence | Severity engine | Streamlit Anomalies; email routing |
| 14 | How confident are we in this root cause? | contribution shares, evidence strength | RCA confidence scoring | Streamlit Root Cause; AI Analyst "Confidence" |
| 15 | How accurate have our forecasts been? | forecast vs actual history | Forecast evaluation (MAE / RMSE / MAPE) | Streamlit Forecast; PDF forecast limitations |
| 16 | Is the pipeline healthy? | `pipeline_runs` | Observability | Streamlit Pipeline & Logs; Power BI Pipeline Health |
| 17 | Can I ask an ad-hoc question in plain English? | analytical tables/views (whitelisted) | NL-to-SQL (`src/nl_to_sql.py`) | Streamlit AI Analyst |

## Mapping rule

Every question resolves to: **verified analytical result first**, then optional
**AI explanation** of that result. The AI Analyst never answers from the LLM's own
knowledge - it explains numbers already computed by the deterministic modules
above (see [`security.md`](security.md) section 3).
