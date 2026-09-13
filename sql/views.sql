-- ==========================================================================
-- InsightForge AI - analytical views (Phase 16 / spec Phase 32)
-- --------------------------------------------------------------------------
-- Six persistent, queryable wrappers around the star schema, reusing the
-- formulas and techniques sql/kpi_queries.sql (Phase 15) already established
-- and documented (docs/kpi-engine.md). Views don't support "IF NOT EXISTS"
-- portably, so CREATE OR REPLACE VIEW is this file's idempotent form - a
-- plain re-apply of this file is always a safe no-op / refresh.
--
-- Unlike a couple of Phase 15's demonstration queries (e.g.
-- top_products_by_revenue, which filtered to above-average products to show
-- off a subquery), every view here returns ALL rows at its grain - a
-- downstream consumer (Streamlit, Power BI, reporting) needs every product/
-- region/customer, not a filtered sample. Where Phase 15 filtered, this file
-- turns the check into a boolean column instead (see product_performance's
-- above_avg_revenue).
--
-- Apply with:  python scripts/apply_schema.py  (runs this file right after
-- sql/schema.sql). Dropping via sql/drop_schema.sql CASCADEs and removes
-- these views automatically.
-- ==========================================================================


-- ==========================================================================
-- daily_kpis - grain: day
-- ==========================================================================
CREATE OR REPLACE VIEW daily_kpis AS
SELECT
    f.order_date,
    d.day_name,
    d.is_weekend,
    ROUND(SUM(f.revenue), 2)                                           AS revenue,
    ROUND(SUM(f.profit), 2)                                            AS profit,
    ROUND(100.0 * SUM(f.profit) / NULLIF(SUM(f.revenue), 0), 2)        AS margin_pct,
    COUNT(DISTINCT f.order_id)                                         AS orders,
    COUNT(DISTINCT f.customer_id)                                      AS customers,
    SUM(f.quantity)                                                    AS units,
    ROUND(SUM(f.revenue) / NULLIF(COUNT(DISTINCT f.order_id), 0), 2)   AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(f.discount), 2)                                  AS avg_discount_pct,
    ROUND(AVG(f.shipping_days), 2)                                     AS avg_shipping_days
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY f.order_date, d.day_name, d.is_weekend;


-- ==========================================================================
-- monthly_kpis - grain: month
-- ==========================================================================
CREATE OR REPLACE VIEW monthly_kpis AS
WITH monthly AS (
    SELECT
        date_trunc('month', f.order_date)::date AS month_start,
        d.year, d.month, d.month_name,
        f.revenue, f.profit, f.quantity, f.discount, f.shipping_days,
        f.is_returned, f.order_id, f.customer_id
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
)
SELECT
    month_start, year, month, month_name,
    ROUND(SUM(revenue), 2)                                             AS revenue,
    ROUND(SUM(profit), 2)                                              AS profit,
    ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 2)            AS margin_pct,
    COUNT(DISTINCT order_id)                                           AS orders,
    COUNT(DISTINCT customer_id)                                        AS customers,
    SUM(quantity)                                                      AS units,
    ROUND(SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0), 2)       AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(discount), 2)                                    AS avg_discount_pct,
    ROUND(AVG(shipping_days), 2)                                       AS avg_shipping_days
FROM monthly
GROUP BY month_start, year, month, month_name;


-- ==========================================================================
-- regional_performance - grain: region
-- ==========================================================================
CREATE OR REPLACE VIEW regional_performance AS
SELECT
    f.region,
    COUNT(DISTINCT r.state)                                            AS states_covered,
    ROUND(SUM(f.revenue), 2)                                           AS revenue,
    ROUND(SUM(f.profit), 2)                                            AS profit,
    ROUND(100.0 * SUM(f.profit) / NULLIF(SUM(f.revenue), 0), 2)        AS margin_pct,
    COUNT(DISTINCT f.order_id)                                         AS orders,
    COUNT(DISTINCT f.customer_id)                                      AS customers,
    SUM(f.quantity)                                                    AS units,
    ROUND(SUM(f.revenue) / NULLIF(COUNT(DISTINCT f.order_id), 0), 2)   AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(f.discount), 2)                                  AS avg_discount_pct,
    ROUND(AVG(f.shipping_days), 2)                                     AS avg_shipping_days,
    SUM(f.revenue) > (
        SELECT AVG(region_revenue) FROM (
            SELECT SUM(revenue) AS region_revenue
            FROM fact_sales
            GROUP BY region
        ) per_region
    )                                                                   AS above_avg_region_revenue
FROM fact_sales f
JOIN dim_region r ON r.region_key = f.region_key
GROUP BY f.region;


-- ==========================================================================
-- category_performance - grain: category
-- ==========================================================================
CREATE OR REPLACE VIEW category_performance AS
SELECT
    category,
    ROUND(SUM(revenue), 2)                                             AS revenue,
    ROUND(SUM(profit), 2)                                              AS profit,
    ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 2)            AS margin_pct,
    COUNT(DISTINCT order_id)                                           AS orders,
    COUNT(DISTINCT customer_id)                                        AS customers,
    SUM(quantity)                                                      AS units,
    ROUND(SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0), 2)       AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(discount), 2)                                    AS avg_discount_pct,
    ROUND(AVG(shipping_days), 2)                                       AS avg_shipping_days,
    RANK()       OVER (ORDER BY SUM(revenue) DESC)                     AS rank_by_revenue,
    DENSE_RANK() OVER (ORDER BY SUM(profit) DESC)                      AS dense_rank_by_profit
FROM fact_sales
GROUP BY category;


-- ==========================================================================
-- product_performance - grain: product
-- Complete (all products) - Phase 15's demonstration query filtered to
-- above-average products; here that check is a boolean column instead.
-- ==========================================================================
CREATE OR REPLACE VIEW product_performance AS
WITH product_totals AS (
    SELECT
        f.product_id, p.product_name, p.category,
        SUM(f.revenue) AS revenue, SUM(f.profit) AS profit, SUM(f.quantity) AS units,
        COUNT(DISTINCT f.order_id) AS orders, COUNT(DISTINCT f.customer_id) AS customers,
        COUNT(*) FILTER (WHERE f.is_returned) AS returned_lines, COUNT(*) AS total_lines,
        AVG(f.discount) AS avg_discount, AVG(f.shipping_days) AS avg_shipping_days
    FROM fact_sales f
    JOIN dim_product p ON p.product_key = f.product_key
    GROUP BY f.product_id, p.product_name, p.category
)
SELECT
    product_id, product_name, category,
    ROUND(revenue, 2)                                                  AS revenue,
    ROUND(profit, 2)                                                   AS profit,
    ROUND(100.0 * profit / NULLIF(revenue, 0), 2)                      AS margin_pct,
    orders, customers, units,
    ROUND(revenue / NULLIF(orders, 0), 2)                              AS aov,
    ROUND(100.0 * returned_lines / NULLIF(total_lines, 0), 2)          AS return_rate_pct,
    ROUND(100.0 * avg_discount, 2)                                     AS avg_discount_pct,
    ROUND(avg_shipping_days, 2)                                        AS avg_shipping_days,
    RANK() OVER (ORDER BY revenue DESC)                                AS rank_by_revenue,
    revenue > (SELECT AVG(revenue) FROM product_totals)                AS above_avg_revenue
FROM product_totals;


-- ==========================================================================
-- customer_performance - grain: customer
-- Complete (all customers). No "customers" column - the grain is already
-- one row per customer.
-- ==========================================================================
CREATE OR REPLACE VIEW customer_performance AS
SELECT
    f.customer_id, c.customer_name, c.customer_segment,
    ROUND(SUM(f.revenue), 2)                                           AS revenue,
    ROUND(SUM(f.profit), 2)                                            AS profit,
    ROUND(100.0 * SUM(f.profit) / NULLIF(SUM(f.revenue), 0), 2)        AS margin_pct,
    COUNT(DISTINCT f.order_id)                                         AS orders,
    SUM(f.quantity)                                                    AS units,
    ROUND(SUM(f.revenue) / NULLIF(COUNT(DISTINCT f.order_id), 0), 2)   AS aov,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    ROUND(100.0 * AVG(f.discount), 2)                                  AS avg_discount_pct,
    ROUND(AVG(f.shipping_days), 2)                                     AS avg_shipping_days,
    RANK() OVER (ORDER BY SUM(f.revenue) DESC)                         AS rank_by_revenue
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
GROUP BY f.customer_id, c.customer_name, c.customer_segment;
