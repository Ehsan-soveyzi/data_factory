-- Q3: order-weighted satisfaction by calendar-day delivery delay.
-- Bind start_date (inclusive), end_date (exclusive), order_status=delivered.
-- Pick one latest answered review per order; never multiply orders by reviews.
WITH eligible_orders AS (
    SELECT
        o.order_id,
        o.order_delivered_customer_date,
        o.order_delivered_customer_date::date - o.order_estimated_delivery_date AS delay_days
    FROM olist.orders AS o
    WHERE o.order_status = :order_status
      AND o.order_purchase_timestamp >= :start_date
      AND o.order_purchase_timestamp < :end_date
      AND o.delivery_delay_eligible
      AND o.order_delivered_customer_date IS NOT NULL
      AND o.order_estimated_delivery_date IS NOT NULL
      AND o.order_delivered_customer_date >= o.order_purchase_timestamp
), latest_reviews AS (
    SELECT DISTINCT ON (r.order_id)
        r.order_id, r.review_score, r.review_answer_timestamp
    FROM olist.order_reviews AS r
    ORDER BY r.order_id, r.review_answer_timestamp DESC,
             r.review_creation_date DESC, r.review_id DESC
), classified_orders AS (
    SELECT
        CASE
            WHEN o.delay_days < 0 THEN 1
            WHEN o.delay_days = 0 THEN 2
            WHEN o.delay_days <= 3 THEN 3
            WHEN o.delay_days <= 7 THEN 4
            WHEN o.delay_days <= 14 THEN 5
            ELSE 6
        END AS delay_bucket_order,
        o.delay_days,
        r.review_score,
        r.review_answer_timestamp < o.order_delivered_customer_date AS review_before_delivery
    FROM eligible_orders AS o
    LEFT JOIN latest_reviews AS r ON r.order_id = o.order_id
)
SELECT
    delay_bucket_order,
    CASE delay_bucket_order
        WHEN 1 THEN 'Early'
        WHEN 2 THEN 'On promised date'
        WHEN 3 THEN 'Late 1-3 days'
        WHEN 4 THEN 'Late 4-7 days'
        WHEN 5 THEN 'Late 8-14 days'
        WHEN 6 THEN 'Late 15+ days'
    END AS delay_bucket,
    COUNT(*) AS eligible_order_count,
    COUNT(review_score) AS reviewed_order_count,
    COUNT(*) - COUNT(review_score) AS missing_review_count,
    ROUND(100.0 * COUNT(review_score) / COUNT(*), 2) AS review_coverage_pct,
    ROUND(AVG(review_score), 2) AS avg_review_score,
    COUNT(*) FILTER (WHERE review_score <= 2) AS low_review_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE review_score <= 2)
          / NULLIF(COUNT(review_score), 0), 2) AS low_review_pct,
    COUNT(*) FILTER (WHERE review_score >= 4) AS high_review_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE review_score >= 4)
          / NULLIF(COUNT(review_score), 0), 2) AS high_review_pct,
    ROUND(AVG(delay_days), 2) AS avg_delay_days,
    COUNT(*) FILTER (WHERE review_before_delivery) AS selected_review_before_delivery_count
FROM classified_orders
GROUP BY delay_bucket_order
ORDER BY delay_bucket_order;
