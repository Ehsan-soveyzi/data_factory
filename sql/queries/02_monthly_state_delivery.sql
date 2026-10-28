-- Q2: late-delivery percentage by customer state and purchase month.
-- Bind start_date (inclusive), end_date (exclusive), and order_status=delivered.
-- The estimate is a DATE: delivery at any time on that date is on time.
-- Retain delivered orders with unknown/invalid dates in coverage counts only.
WITH classified_orders AS (
    SELECT
        date_trunc('month', o.order_purchase_timestamp)::date AS purchase_month,
        c.customer_state,
        (
            o.delivery_delay_eligible
            AND o.order_delivered_customer_date IS NOT NULL
            AND o.order_estimated_delivery_date IS NOT NULL
            AND o.order_delivered_customer_date >= o.order_purchase_timestamp
        ) AS is_eligible,
        o.order_delivered_customer_date::date > o.order_estimated_delivery_date AS is_late
    FROM olist.orders AS o
    JOIN olist.customers AS c ON c.customer_id = o.customer_id
    WHERE o.order_status = :order_status
      AND o.order_purchase_timestamp >= :start_date
      AND o.order_purchase_timestamp < :end_date
)
SELECT
    purchase_month,
    customer_state,
    COUNT(*) AS delivered_order_count,
    COUNT(*) FILTER (WHERE is_eligible) AS eligible_order_count,
    COUNT(*) FILTER (WHERE NOT is_eligible) AS excluded_order_count,
    COUNT(*) FILTER (WHERE is_eligible AND is_late) AS late_order_count,
    COUNT(*) FILTER (WHERE is_eligible AND NOT is_late) AS on_time_order_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE is_eligible AND is_late)
        / NULLIF(COUNT(*) FILTER (WHERE is_eligible), 0),
        2
    ) AS late_delivery_pct
FROM classified_orders
GROUP BY purchase_month, customer_state
ORDER BY purchase_month, customer_state;
