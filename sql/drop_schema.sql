-- ==========================================================================
-- InsightForge AI - drop the Phase 8 schema (development / test reset only)
-- --------------------------------------------------------------------------
-- Used by  python scripts/apply_schema.py --drop  before a clean re-apply.
-- CASCADE clears dependent foreign keys. Order is not important with CASCADE
-- but is listed fact -> dims -> operational for readability.
-- ==========================================================================

DROP TABLE IF EXISTS fact_sales           CASCADE;

DROP TABLE IF EXISTS dim_date             CASCADE;
DROP TABLE IF EXISTS dim_customer         CASCADE;
DROP TABLE IF EXISTS dim_product          CASCADE;
DROP TABLE IF EXISTS dim_region           CASCADE;

DROP TABLE IF EXISTS forecast_results     CASCADE;
DROP TABLE IF EXISTS recommendations      CASCADE;
DROP TABLE IF EXISTS anomalies            CASCADE;
DROP TABLE IF EXISTS rejected_records     CASCADE;
DROP TABLE IF EXISTS data_quality_results CASCADE;
DROP TABLE IF EXISTS file_registry        CASCADE;
DROP TABLE IF EXISTS pipeline_runs        CASCADE;
