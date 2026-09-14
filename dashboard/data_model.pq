// ============================================================================
// InsightForge AI - Power BI data model source queries (Phase 31 / spec
// Phase 60, part of FR-23).
// ----------------------------------------------------------------------------
// Real, syntactically valid Power Query M - paste each "let ... in" block
// below into Power BI Desktop's Advanced Editor as its own query (Get Data ->
// PostgreSQL database -> Advanced Editor), or use "Get Data -> Blank Query"
// once per block. No password is embedded anywhere in this file - Power BI's
// own credential manager (prompted the first time PGServer/PGDatabase are
// used) holds that, exactly docs/security.md's "no secret ever hard-coded"
// rule applied to this new surface.
//
// Import mode is this project's own choice (the spec names no refresh
// strategy) - consistent with docs/data-flow.md stage 16 and
// docs/PHASE_STATUS.md's own environment notes: "Automated dataset refresh
// is a documented manual/gateway step." DirectQuery is a viable alternative
// but not the default here.
//
// Full design rationale: docs/powerbi-data-model.md
// ============================================================================

// ---------------------------------------------------------------------------
// Parameters (Power BI: Home -> Manage Parameters -> New).
// Defaults match .env.example's POSTGRES_HOST / POSTGRES_PORT / POSTGRES_DB.
// ---------------------------------------------------------------------------
// PGServer   (Text, current value: "localhost:5432")
// PGDatabase (Text, current value: "insightforge")

// ---------------------------------------------------------------------------
// Star schema (Phase 8) - imported raw, not pre-aggregated, so every Phase 32
// page can still slice/filter by any dimension.
// ---------------------------------------------------------------------------

// Query: dim_date
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_dim_date = Source{[Schema="public",Item="dim_date"]}[Data]
in
    public_dim_date

// Query: dim_customer
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_dim_customer = Source{[Schema="public",Item="dim_customer"]}[Data]
in
    public_dim_customer

// Query: dim_product
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_dim_product = Source{[Schema="public",Item="dim_product"]}[Data]
in
    public_dim_product

// Query: dim_region
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_dim_region = Source{[Schema="public",Item="dim_region"]}[Data]
in
    public_dim_region

// Query: fact_sales
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_fact_sales = Source{[Schema="public",Item="fact_sales"]}[Data]
in
    public_fact_sales

// ---------------------------------------------------------------------------
// Insight / operational tables (Phases 8, 14, 20, 21, 25, 26) - what Phase
// 32's Risk & Anomalies / Forecast / Pipeline Health pages will read.
// ---------------------------------------------------------------------------

// Query: pipeline_runs
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_pipeline_runs = Source{[Schema="public",Item="pipeline_runs"]}[Data]
in
    public_pipeline_runs

// Query: data_quality_results
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_data_quality_results = Source{[Schema="public",Item="data_quality_results"]}[Data]
in
    public_data_quality_results

// Query: anomalies
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_anomalies = Source{[Schema="public",Item="anomalies"]}[Data]
in
    public_anomalies

// Query: drift_results
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_drift_results = Source{[Schema="public",Item="drift_results"]}[Data]
in
    public_drift_results

// Query: forecast_results
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_forecast_results = Source{[Schema="public",Item="forecast_results"]}[Data]
in
    public_forecast_results

// Query: recommendations
let
    Source = PostgreSQL.Database(PGServer, PGDatabase),
    public_recommendations = Source{[Schema="public",Item="recommendations"]}[Data]
in
    public_recommendations
