-- Section 7: an ordinary one-table projection and a multi-table Q5 analysis.
-- Scope is fixed, NOT a parameterized view: delivered purchases in [2016, 2019).
-- The Q5 body below is reproduced from 05_payment_methods_installments.sql.
-- Only its fixed binds and its order source (v_orders) are substituted.
CREATE OR REPLACE VIEW olist.v_orders AS
SELECT order_id, order_status, order_purchase_timestamp FROM olist.orders;

CREATE OR REPLACE VIEW olist.v_payment_analysis AS
WITH scoped_orders AS (
    SELECT order_id
    FROM olist.v_orders
    WHERE order_status = 'delivered'
      AND order_purchase_timestamp >= DATE '2016-01-01'
      AND order_purchase_timestamp < DATE '2019-01-01'
), order_values AS (
    SELECT oi.order_id, SUM(oi.price) AS merchandise_value_brl
    FROM olist.order_items AS oi
    JOIN scoped_orders AS o ON o.order_id = oi.order_id
    GROUP BY oi.order_id
), order_payments AS (
    SELECT p.order_id, COUNT(*) AS payment_row_count,
           COUNT(DISTINCT p.payment_type) AS method_count,
           MIN(p.payment_type) AS single_method,
           COUNT(DISTINCT p.payment_installments) AS known_plan_count,
           COUNT(*) FILTER (WHERE p.payment_installments IS NULL) AS unknown_plan_rows,
           MAX(p.payment_installments) AS single_installments,
           SUM(p.payment_value) AS recorded_payment_value_brl
    FROM olist.order_payments AS p
    JOIN scoped_orders AS o ON o.order_id = p.order_id
    GROUP BY p.order_id
), order_facts AS (
    SELECT o.order_id, v.merchandise_value_brl,
           COALESCE(p.payment_row_count, 0) AS payment_row_count,
           p.recorded_payment_value_brl,
           CASE WHEN p.order_id IS NULL THEN 'no_payment'
                WHEN p.method_count = 1 THEN 'single_method'
                ELSE 'mixed_methods' END AS payment_method_kind,
           CASE WHEN p.method_count = 1 THEN p.single_method END AS payment_type,
           CASE WHEN p.order_id IS NULL THEN 'no_payment'
                WHEN p.unknown_plan_rows = p.payment_row_count THEN 'unknown'
                WHEN p.unknown_plan_rows > 0 THEN 'partly_unknown'
                WHEN p.known_plan_count > 1 THEN 'mixed_plans'
                ELSE 'single_plan' END AS installment_kind,
           CASE WHEN p.known_plan_count = 1 AND p.unknown_plan_rows = 0
                THEN p.single_installments END AS payment_installments
    FROM scoped_orders AS o
    LEFT JOIN order_values AS v ON v.order_id = o.order_id
    LEFT JOIN order_payments AS p ON p.order_id = o.order_id
), scope_totals AS (
    SELECT COUNT(*) AS order_count, SUM(payment_row_count) AS payment_row_count
    FROM order_facts
), profile_stats AS (
    SELECT 'order_profile'::text AS analysis_level,
           payment_method_kind, payment_type, installment_kind, payment_installments,
           COUNT(*) AS order_count,
           COUNT(merchandise_value_brl) AS valued_order_count,
           SUM(payment_row_count)::bigint AS payment_row_count,
           SUM(recorded_payment_value_brl) AS recorded_payment_value_brl,
           SUM(merchandise_value_brl) AS merchandise_value_brl,
           ROUND(AVG(merchandise_value_brl), 2) AS avg_order_value_brl
    FROM order_facts
    GROUP BY payment_method_kind, payment_type, installment_kind, payment_installments
), order_plan_payments AS (
    SELECT p.order_id, p.payment_type, p.payment_installments,
           COUNT(*) AS payment_row_count,
           SUM(p.payment_value) AS recorded_payment_value_brl
    FROM olist.order_payments AS p
    JOIN scoped_orders AS o ON o.order_id = p.order_id
    GROUP BY p.order_id, p.payment_type, p.payment_installments
), plan_stats AS (
    SELECT 'payment_plan'::text AS analysis_level,
           'recorded_method'::text AS payment_method_kind, p.payment_type,
           CASE WHEN p.payment_installments IS NULL THEN 'unknown'
                ELSE 'single_plan' END AS installment_kind,
           p.payment_installments, COUNT(*) AS order_count,
           COUNT(v.merchandise_value_brl) AS valued_order_count,
           SUM(p.payment_row_count)::bigint AS payment_row_count,
           SUM(p.recorded_payment_value_brl) AS recorded_payment_value_brl,
           SUM(v.merchandise_value_brl) AS merchandise_value_brl,
           ROUND(AVG(v.merchandise_value_brl), 2) AS avg_order_value_brl
    FROM order_plan_payments AS p
    LEFT JOIN order_values AS v ON v.order_id = p.order_id
    GROUP BY p.payment_type, p.payment_installments
), combined AS (
    SELECT * FROM profile_stats
    UNION ALL
    SELECT * FROM plan_stats
)
SELECT c.analysis_level, c.payment_method_kind, c.payment_type,
       c.installment_kind, c.payment_installments, c.order_count,
       ROUND(100.0 * c.order_count / NULLIF(t.order_count, 0), 2) AS order_share_pct,
       c.valued_order_count, c.payment_row_count,
       ROUND(100.0 * c.payment_row_count / NULLIF(t.payment_row_count, 0), 2) AS payment_row_share_pct,
       c.recorded_payment_value_brl,
       ROUND(c.recorded_payment_value_brl / NULLIF(c.payment_row_count, 0), 2) AS avg_payment_row_value_brl,
       c.merchandise_value_brl, c.avg_order_value_brl
FROM combined AS c
CROSS JOIN scope_totals AS t
ORDER BY c.analysis_level, c.payment_method_kind, c.payment_type NULLS LAST,
         c.payment_installments NULLS LAST, c.installment_kind;
