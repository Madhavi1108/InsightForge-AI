-- ==========================================================================
-- InsightForge AI - database schema (Phase 8 / spec Phases 15-17)
-- --------------------------------------------------------------------------
--   Phase 15  star schema      : dim_date, dim_customer, dim_product,
--                                dim_region, fact_sales
--   Phase 16  operational tbls : pipeline_runs, file_registry,
--                                data_quality_results, rejected_records,
--                                anomalies, recommendations, forecast_results
--   Phase 17  indexing         : the six spec-named indexes + supporting
--                                FK / analytical indexes
--
-- Idempotent: every object uses IF NOT EXISTS, so this file can be re-applied.
-- Apply with:  python scripts/apply_schema.py
-- Relationships are documented in docs/database-schema.md.
-- The connection layer (src/database.py) and the ETL that populates these
-- tables arrive in Phases 9 and 12-13.
-- ==========================================================================

-- ========================================================================
-- Phase 16 - operational / audit tables
-- (created first: fact_sales and every insight table reference pipeline_runs)
-- ========================================================================

-- One row per pipeline execution. Referenced by every run-scoped row so that
-- any number can be traced back to the file and run that produced it.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    file_name      TEXT        NOT NULL,
    file_hash      TEXT,
    status         TEXT        NOT NULL DEFAULT 'RUNNING'
                   CHECK (status IN ('RUNNING','SUCCESS','PARTIAL','FAILED','SKIPPED_DUPLICATE')),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    duration_s     NUMERIC(10,3),
    rows_received  INTEGER,
    rows_valid     INTEGER,
    rows_rejected  INTEGER,
    dq_score       NUMERIC(5,2),
    stage_metrics  JSONB,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- SHA-256 fingerprint of every file ever seen. A repeat hash -> SKIPPED_DUPLICATE.
CREATE TABLE IF NOT EXISTS file_registry (
    file_id            BIGSERIAL PRIMARY KEY,
    file_name          TEXT        NOT NULL,
    file_hash          TEXT        NOT NULL UNIQUE,
    file_size_bytes    BIGINT,
    row_count          INTEGER,
    first_seen_run_id  BIGINT      REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-dimension data-quality scores for a run (7 dimensions + an 'Overall' row).
CREATE TABLE IF NOT EXISTS data_quality_results (
    dq_id            BIGSERIAL PRIMARY KEY,
    run_id           BIGINT      NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    dimension        TEXT        NOT NULL,
    score            NUMERIC(5,2) NOT NULL,
    passed           BOOLEAN,
    records_checked  INTEGER,
    records_failed   INTEGER,
    detail           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, dimension)
);

-- Every row that failed structural or data-quality validation - never dropped.
CREATE TABLE IF NOT EXISTS rejected_records (
    rejected_id         BIGSERIAL PRIMARY KEY,
    run_id              BIGINT      NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    source_row_number   INTEGER,
    order_id            TEXT,
    rejection_category  TEXT        NOT NULL,
    rejection_reason    TEXT,
    dq_dimension        TEXT,
    raw_record          JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fused anomaly detector output: one row per metric / date / grain.
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id       BIGSERIAL PRIMARY KEY,
    run_id           BIGINT      NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    metric           TEXT        NOT NULL,
    anomaly_date     DATE        NOT NULL,
    grain            TEXT        NOT NULL DEFAULT 'business',
    observed_value   NUMERIC(18,4),
    expected_value   NUMERIC(18,4),
    deviation_pct    NUMERIC(12,4),
    direction        TEXT        CHECK (direction IN ('up','down')),
    zscore_flag      BOOLEAN     NOT NULL DEFAULT FALSE,
    iqr_flag         BOOLEAN     NOT NULL DEFAULT FALSE,
    rolling_flag     BOOLEAN     NOT NULL DEFAULT FALSE,
    iforest_flag     BOOLEAN     NOT NULL DEFAULT FALSE,
    detector_votes   SMALLINT    NOT NULL DEFAULT 0,
    confidence       NUMERIC(5,4),
    severity         TEXT        CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    persistence_days INTEGER,
    detail           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rule-based recommendations with a 0-100 priority (severity x impact x confidence).
CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id  BIGSERIAL PRIMARY KEY,
    run_id             BIGINT      NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    title              TEXT        NOT NULL,
    rationale          TEXT,
    rule_id            TEXT,
    linked_anomaly_id  BIGINT      REFERENCES anomalies(anomaly_id) ON DELETE SET NULL,
    severity           TEXT        CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    impact_value       NUMERIC(18,2),
    confidence         NUMERIC(5,4),
    priority_score     NUMERIC(5,2) CHECK (priority_score BETWEEN 0 AND 100),
    priority_band      TEXT        CHECK (priority_band IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status             TEXT        NOT NULL DEFAULT 'open',
    detail             JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exponential-smoothing forecasts (7 & 30 day) with evaluation metrics.
CREATE TABLE IF NOT EXISTS forecast_results (
    forecast_id     BIGSERIAL PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    metric          TEXT        NOT NULL,
    horizon_days    INTEGER     NOT NULL CHECK (horizon_days IN (7,30)),
    forecast_date   DATE        NOT NULL,
    forecast_value  NUMERIC(18,4) NOT NULL,
    lower_bound     NUMERIC(18,4),
    upper_bound     NUMERIC(18,4),
    model           TEXT        NOT NULL DEFAULT 'exponential_smoothing',
    mae             NUMERIC(18,4),
    rmse            NUMERIC(18,4),
    mape            NUMERIC(10,4),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, metric, horizon_days, forecast_date)
);

-- ========================================================================
-- Phase 15 - star schema dimensions
-- ========================================================================

-- Calendar dimension. Natural key = the date itself.
CREATE TABLE IF NOT EXISTS dim_date (
    date_key      DATE     PRIMARY KEY,
    year          SMALLINT NOT NULL,
    quarter       SMALLINT NOT NULL,
    month         SMALLINT NOT NULL,
    month_name    TEXT     NOT NULL,
    day           SMALLINT NOT NULL,
    day_of_week   SMALLINT NOT NULL,          -- 1 = Monday .. 7 = Sunday (ISO)
    day_name      TEXT     NOT NULL,
    week_of_year  SMALLINT NOT NULL,
    is_weekend    BOOLEAN  NOT NULL,
    is_month_end  BOOLEAN  NOT NULL
);

-- Customer dimension. Surrogate key + business key (CUST-######).
CREATE TABLE IF NOT EXISTS dim_customer (
    customer_key      BIGSERIAL PRIMARY KEY,
    customer_id       TEXT NOT NULL UNIQUE,
    customer_name     TEXT NOT NULL,
    customer_segment  TEXT NOT NULL
                      CHECK (customer_segment IN ('Consumer','Corporate','Home Office')),
    first_seen_run_id BIGINT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Product dimension. Surrogate key + business key (PROD-#####).
CREATE TABLE IF NOT EXISTS dim_product (
    product_key       BIGSERIAL PRIMARY KEY,
    product_id        TEXT NOT NULL UNIQUE,
    product_name      TEXT NOT NULL,
    category          TEXT NOT NULL
                      CHECK (category IN ('Electronics','Furniture','Office Supplies','Clothing','Home & Kitchen')),
    sub_category      TEXT NOT NULL,
    first_seen_run_id BIGINT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Geography dimension. Grain = one valid Region -> State -> City combination.
CREATE TABLE IF NOT EXISTS dim_region (
    region_key        BIGSERIAL PRIMARY KEY,
    region            TEXT NOT NULL
                      CHECK (region IN ('North','South','East','West','Central')),
    state             TEXT NOT NULL,
    city              TEXT NOT NULL,
    first_seen_run_id BIGINT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (region, state, city)
);

-- ========================================================================
-- Phase 15 - fact table
-- Grain: one order line (one CSV row). Surrogate PK; order_id is a degenerate
-- dimension (repeats across the lines of a multi-item order). A few high-use
-- business attributes (order_date, customer_id, product_id, region, category,
-- sub_category, customer_segment) are denormalised onto the fact so the KPI /
-- RCA queries and the Phase 17 indexes can hit it without a join.
-- ========================================================================
CREATE TABLE IF NOT EXISTS fact_sales (
    sales_key         BIGSERIAL PRIMARY KEY,

    -- lineage
    run_id            BIGINT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- dimension foreign keys
    date_key          DATE   NOT NULL REFERENCES dim_date(date_key),
    customer_key      BIGINT NOT NULL REFERENCES dim_customer(customer_key),
    product_key       BIGINT NOT NULL REFERENCES dim_product(product_key),
    region_key        BIGINT NOT NULL REFERENCES dim_region(region_key),

    -- degenerate / denormalised business attributes
    order_id          TEXT NOT NULL,
    order_date        DATE NOT NULL,
    customer_id       TEXT NOT NULL,
    product_id        TEXT NOT NULL,
    region            TEXT NOT NULL,
    category          TEXT NOT NULL,
    sub_category      TEXT NOT NULL,
    customer_segment  TEXT NOT NULL,

    -- measures
    quantity          INTEGER       NOT NULL CHECK (quantity >= 1),
    unit_price        NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    discount          NUMERIC(4,3)  NOT NULL CHECK (discount >= 0 AND discount < 1),
    revenue           NUMERIC(14,2) NOT NULL CHECK (revenue >= 0),
    cost              NUMERIC(14,2) NOT NULL CHECK (cost >= 0),
    profit            NUMERIC(14,2) NOT NULL,

    -- order attributes
    payment_method    TEXT,
    shipping_days     INTEGER CHECK (shipping_days IS NULL OR shipping_days >= 0),
    order_status      TEXT NOT NULL
                      CHECK (order_status IN ('Completed','Pending','Cancelled','Returned')),
    return_status     TEXT NOT NULL
                      CHECK (return_status IN ('Not Returned','Returned')),
    is_returned       BOOLEAN GENERATED ALWAYS AS (return_status = 'Returned') STORED
);

-- ========================================================================
-- Phase 17 - indexing
-- ========================================================================

-- The six indexes the specification names, on the fact table.
CREATE INDEX IF NOT EXISTS idx_fact_sales_order_id     ON fact_sales (order_id);
CREATE INDEX IF NOT EXISTS idx_fact_sales_order_date   ON fact_sales (order_date);
CREATE INDEX IF NOT EXISTS idx_fact_sales_customer_id  ON fact_sales (customer_id);
CREATE INDEX IF NOT EXISTS idx_fact_sales_product_id   ON fact_sales (product_id);
CREATE INDEX IF NOT EXISTS idx_fact_sales_region       ON fact_sales (region);
CREATE INDEX IF NOT EXISTS idx_fact_sales_category     ON fact_sales (category);

-- Foreign-key indexes (join performance to the dimensions).
CREATE INDEX IF NOT EXISTS idx_fact_sales_date_key     ON fact_sales (date_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_customer_key ON fact_sales (customer_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_product_key  ON fact_sales (product_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_region_key   ON fact_sales (region_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_run_id       ON fact_sales (run_id);

-- Composite index for the common region -> category -> time roll-up (KPIs, RCA).
CREATE INDEX IF NOT EXISTS idx_fact_sales_region_category_date
    ON fact_sales (region, category, order_date);

-- Operational-table access paths.
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status      ON pipeline_runs (status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at  ON pipeline_runs (started_at);
CREATE INDEX IF NOT EXISTS idx_dq_results_run_id         ON data_quality_results (run_id);
CREATE INDEX IF NOT EXISTS idx_rejected_records_run_id   ON rejected_records (run_id);
CREATE INDEX IF NOT EXISTS idx_rejected_records_category ON rejected_records (rejection_category);
CREATE INDEX IF NOT EXISTS idx_anomalies_run_id          ON anomalies (run_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_metric_date     ON anomalies (metric, anomaly_date);
CREATE INDEX IF NOT EXISTS idx_recommendations_run_id    ON recommendations (run_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_priority  ON recommendations (priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_forecast_results_run_id   ON forecast_results (run_id);
CREATE INDEX IF NOT EXISTS idx_forecast_results_metric   ON forecast_results (metric, forecast_date);
