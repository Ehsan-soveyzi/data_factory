-- Q1: completed merchandise sales by purchase month and product category.
-- Bind start_date (inclusive), end_date (exclusive), and order_status.
-- Amounts are nominal BRL; freight and payment totals are excluded.
-- One order can count in several categories, but each item's price counts once.
WITH order_category_sales AS (
    SELECT
        date_trunc('month', o.order_purchase_timestamp)::date AS purchase_month,
        p.product_category_name,
        oi.order_id,
        SUM(oi.price) AS category_order_value_brl
    FROM olist.orders AS o
    JOIN olist.order_items AS oi ON oi.order_id = o.order_id
    JOIN olist.products AS p ON p.product_id = oi.product_id
    WHERE o.order_status = :order_status
      AND o.order_purchase_timestamp >= :start_date
      AND o.order_purchase_timestamp < :end_date
    GROUP BY 1, 2, oi.order_id
), monthly_sales AS (
    SELECT
        purchase_month,
        product_category_name,
        SUM(category_order_value_brl) AS gross_product_sales_brl,
        COUNT(*) AS unique_order_count,
        ROUND(AVG(category_order_value_brl), 2) AS avg_order_category_value_brl
    FROM order_category_sales
    GROUP BY purchase_month, product_category_name
)
SELECT
    s.purchase_month,
    s.product_category_name,
    COALESCE(c.product_category_name_english, s.product_category_name,
             'Uncategorized') AS category_label,
    s.gross_product_sales_brl,
    s.unique_order_count,
    s.avg_order_category_value_brl
FROM monthly_sales AS s
LEFT JOIN olist.product_categories AS c
    ON c.product_category_name = s.product_category_name
ORDER BY s.purchase_month, s.product_category_name NULLS LAST;
