-- Q4: top sellers by completed merchandise sales (BRL, excluding freight).
-- Bind start_date inclusive, end_date exclusive, order_status=delivered, top_n.
-- Aggregate at seller/order grain before averaging order-level proxies.
WITH seller_order_sales AS (
    SELECT oi.seller_id, oi.order_id,
           SUM(oi.price) AS seller_order_sales_brl,
           COUNT(*) AS item_count
    FROM olist.order_items AS oi
    JOIN olist.orders AS o ON o.order_id = oi.order_id
    WHERE o.order_status = :order_status
      AND o.order_purchase_timestamp >= :start_date
      AND o.order_purchase_timestamp < :end_date
    GROUP BY oi.seller_id, oi.order_id
), seller_revenue AS (
    SELECT seller_id,
           SUM(seller_order_sales_brl) AS gross_product_sales_brl,
           COUNT(*) AS sold_order_count,
           SUM(item_count)::bigint AS sold_item_count
    FROM seller_order_sales
    GROUP BY seller_id
), top_sellers AS (
    SELECT ROW_NUMBER() OVER (
               ORDER BY gross_product_sales_brl DESC, seller_id
           ) AS revenue_rank,
           seller_id, gross_product_sales_brl, sold_order_count, sold_item_count
    FROM seller_revenue
    ORDER BY gross_product_sales_brl DESC, seller_id
    LIMIT :top_n
), top_seller_orders AS (
    SELECT so.seller_id, so.order_id,
           CASE WHEN o.approval_to_carrier_duration_valid
                     AND o.order_approved_at IS NOT NULL
                     AND o.order_delivered_carrier_date IS NOT NULL
                     AND o.order_delivered_carrier_date >= o.order_approved_at
                THEN EXTRACT(EPOCH FROM (o.order_delivered_carrier_date - o.order_approved_at))
           END AS preparation_seconds
    FROM seller_order_sales AS so
    JOIN top_sellers AS t ON t.seller_id = so.seller_id
    JOIN olist.orders AS o ON o.order_id = so.order_id
), top_order_ids AS (
    SELECT DISTINCT order_id FROM top_seller_orders
), latest_reviews AS (
    SELECT DISTINCT ON (r.order_id)
           r.order_id, r.review_score
    FROM olist.order_reviews AS r
    JOIN top_order_ids AS ids ON ids.order_id = r.order_id
    ORDER BY r.order_id, r.review_answer_timestamp DESC,
             r.review_creation_date DESC, r.review_id DESC
), seller_metrics AS (
    SELECT so.seller_id,
           COUNT(so.preparation_seconds) AS preparation_eligible_count,
           COUNT(*) - COUNT(so.preparation_seconds) AS preparation_excluded_count,
           ROUND(AVG(so.preparation_seconds) / 86400.0, 2) AS avg_preparation_days,
           COUNT(r.review_score) AS reviewed_order_count,
           COUNT(*) - COUNT(r.review_score) AS missing_review_count,
           ROUND(AVG(r.review_score), 2) AS avg_review_score
    FROM top_seller_orders AS so
    LEFT JOIN latest_reviews AS r ON r.order_id = so.order_id
    GROUP BY so.seller_id
)
SELECT t.revenue_rank, t.seller_id, s.seller_city, s.seller_state,
       t.gross_product_sales_brl, t.sold_order_count, t.sold_item_count,
       m.preparation_eligible_count, m.preparation_excluded_count,
       m.avg_preparation_days, m.reviewed_order_count,
       m.missing_review_count, m.avg_review_score
FROM top_sellers AS t
JOIN olist.sellers AS s ON s.seller_id = t.seller_id
JOIN seller_metrics AS m ON m.seller_id = t.seller_id
ORDER BY t.revenue_rank;
