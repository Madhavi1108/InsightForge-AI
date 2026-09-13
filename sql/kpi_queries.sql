-- ==========================================================================
-- InsightForge AI - core SQL KPI engine & advanced SQL analytics
-- (Phase 15 / spec Phases 30-31)
-- --------------------------------------------------------------------------
-- Ten standalone, independently-runnable queries against the star schema
-- (sql/schema.sql). Each is preceded by a "-- @query: <name>" marker; every
-- block from one marker to the next (or EOF) is valid SQL on its own - run
-- it directly (psql, a DB client, or Database.fetch_all(sql_text)).
--
-- The master spec (INSIGHTFORGE AI.pdf, phases 30-31) names the 10 metrics
-- and the 11 SQL techniques to demonstrate but gives no formulas; the
-- formulas below are this project's own operational definition (documented
-- in docs/kpi-engine.md), consistent with fact_sales' grain of one row per
-- order line.
--
-- Grain covered per FR-08 (day/month/region/category/product/customer):
-- core_kpis_daily (day), core_kpis_monthly (month), regional_performance
-- (region), category_performance_ranked (category), top_products_by_revenue
-- (product), customer_performance (customer).
--
-- Phase 16 (spec 32, not this phase) wraps these same calculations into the
-- 6 named views (daily_kpis, monthly_kpis, regional_performance,
-- category_performance, product_performance, customer_performance) -
-- deliberately different names are used here to keep the two phases distinct.
-- ==========================================================================


-- @query: core_kpis_overall
-- Grain: whole dataset (one row). Techniques: CASE/FILTER, aggregates.
-- All 10 spec metrics in one place.
SELECT
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
FROM fact_sales;


-- @query: core_kpis_daily
-- Grain: day. Techniques: JOIN (dim_date), GROUP BY, CASE/FILTER.
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
GROUP BY f.order_date, d.day_name, d.is_weekend
ORDER BY f.order_date;


-- @query: core_kpis_monthly
-- Grain: month. Techniques: CTE, JOIN (dim_date), date_trunc.
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
GROUP BY month_start, year, month, month_name
ORDER BY month_start;


-- @query: regional_performance
-- Grain: region. Techniques: JOIN (dim_region), scalar subquery.
-- Also flags regions performing above the overall average revenue.
SELECT
    f.region,
    ROUND(SUM(f.revenue), 2)                                           AS revenue,
    ROUND(SUM(f.profit), 2)                                            AS profit,
    ROUND(100.0 * SUM(f.profit) / NULLIF(SUM(f.revenue), 0), 2)        AS margin_pct,
    COUNT(DISTINCT f.order_id)                                         AS orders,
    COUNT(DISTINCT f.customer_id)                                      AS customers,
    SUM(f.quantity)                                                    AS units,
    ROUND(100.0 * COUNT(*) FILTER (WHERE f.is_returned)
          / NULLIF(COUNT(*), 0), 2)                                    AS return_rate_pct,
    COUNT(DISTINCT r.state)                                            AS states_covered,
    SUM(f.revenue) > (
        SELECT AVG(region_revenue) FROM (
            SELECT SUM(revenue) AS region_revenue
            FROM fact_sales
            GROUP BY region
        ) per_region
    )                                                                   AS above_avg_region_revenue
FROM fact_sales f
JOIN dim_region r ON r.region_key = f.region_key
GROUP BY f.region
ORDER BY revenue DESC;


-- @query: category_performance_ranked
-- Grain: category. Techniques: window functions RANK(), DENSE_RANK().
SELECT
    category,
    ROUND(SUM(revenue), 2)                                             AS revenue,
    ROUND(SUM(profit), 2)                                              AS profit,
    SUM(quantity)                                                      AS units,
    RANK()       OVER (ORDER BY SUM(revenue) DESC)                     AS rank_by_revenue,
    DENSE_RANK() OVER (ORDER BY SUM(profit) DESC)                      AS dense_rank_by_profit
FROM fact_sales
GROUP BY category
ORDER BY revenue DESC;


-- @query: top_products_by_revenue
-- Grain: product. Techniques: JOIN (dim_product), subquery (above-average
-- product revenue), window function RANK().
WITH product_totals AS (
    SELECT
        f.product_id, p.product_name, p.category,
        SUM(f.revenue) AS revenue, SUM(f.profit) AS profit, SUM(f.quantity) AS units
    FROM fact_sales f
    JOIN dim_product p ON p.product_key = f.product_key
    GROUP BY f.product_id, p.product_name, p.category
)
SELECT
    product_id, product_name, category,
    ROUND(revenue, 2) AS revenue, ROUND(profit, 2) AS profit, units,
    RANK() OVER (ORDER BY revenue DESC) AS rank_by_revenue
FROM product_totals
WHERE revenue > (SELECT AVG(revenue) FROM product_totals)
ORDER BY revenue DESC;


-- @query: customer_performance
-- Grain: customer. Techniques: JOIN (dim_customer), window function RANK().
SELECT
    f.customer_id, c.customer_name, c.customer_segment,
    ROUND(SUM(f.revenue), 2)                                           AS revenue,
    ROUND(SUM(f.profit), 2)                                            AS profit,
    COUNT(DISTINCT f.order_id)                                         AS orders,
    SUM(f.quantity)                                                    AS units,
    ROUND(SUM(f.revenue) / NULLIF(COUNT(DISTINCT f.order_id), 0), 2)   AS aov,
    RANK() OVER (ORDER BY SUM(f.revenue) DESC)                         AS rank_by_revenue
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
GROUP BY f.customer_id, c.customer_name, c.customer_segment
ORDER BY revenue DESC;


-- @query: monthly_revenue_trend
-- Grain: month. Techniques: CTE, window functions LAG(), LEAD().
WITH monthly_revenue AS (
    SELECT
        date_trunc('month', order_date)::date AS month_start,
        SUM(revenue) AS revenue
    FROM fact_sales
    GROUP BY date_trunc('month', order_date)
)
SELECT
    month_start,
    ROUND(revenue, 2)                                                  AS revenue,
    ROUND(LAG(revenue) OVER (ORDER BY month_start), 2)                 AS prev_month_revenue,
    ROUND(LEAD(revenue) OVER (ORDER BY month_start), 2)                AS next_month_revenue,
    ROUND(100.0 * (revenue - LAG(revenue) OVER (ORDER BY month_start))
          / NULLIF(LAG(revenue) OVER (ORDER BY month_start), 0), 2)    AS mom_change_pct
FROM monthly_revenue
ORDER BY month_start;


-- @query: rolling_7day_avg_revenue
-- Grain: day. Techniques: CTE, window function rolling average.
WITH daily_revenue AS (
    SELECT order_date, SUM(revenue) AS revenue
    FROM fact_sales
    GROUP BY order_date
)
SELECT
    order_date,
    ROUND(revenue, 2)                                                  AS revenue,
    ROUND(AVG(revenue) OVER (
        ORDER BY order_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ), 2)                                                               AS rolling_7day_avg_revenue
FROM daily_revenue
ORDER BY order_date;


-- @query: running_total_revenue
-- Grain: day. Techniques: CTE, window function running total.
WITH daily_revenue AS (
    SELECT order_date, SUM(revenue) AS revenue
    FROM fact_sales
    GROUP BY order_date
)
SELECT
    order_date,
    ROUND(revenue, 2)                                                  AS revenue,
    ROUND(SUM(revenue) OVER (ORDER BY order_date), 2)                  AS running_total_revenue
FROM daily_revenue
ORDER BY order_date;
