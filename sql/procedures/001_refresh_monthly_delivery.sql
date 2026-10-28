-- CALL owns no transaction boundary: the caller commits/rolls back the whole unit.
-- Accept any finite date in a month; normalize to its first day.
-- Only the requested month is replaced; source tables are read, never modified.
CREATE OR REPLACE PROCEDURE olist.refresh_monthly_delivery_snapshot(p_month DATE)
LANGUAGE plpgsql
SET search_path = pg_catalog, olist
AS $procedure$
DECLARE
    v_month DATE;
    v_end DATE;
    v_source_at TIMESTAMPTZ;
    v_source_snapshot TEXT;
    v_rows INTEGER;
BEGIN
    IF p_month IS NULL OR NOT isfinite(p_month) THEN
        RAISE EXCEPTION 'p_month must be a finite, non-NULL date'
            USING ERRCODE = '22023';
    END IF;
    v_month := date_trunc('month', p_month::timestamp)::date;
    v_end := (v_month + INTERVAL '1 month')::date;

    -- Transaction-scoped lock namespace + month key. Same-month calls serialize.
    -- Under REPEATABLE READ a competing writer may require a transaction retry.
    PERFORM pg_advisory_xact_lock(814008, v_month - DATE '2000-01-01');
    v_source_at := clock_timestamp();
    v_source_snapshot := pg_current_snapshot()::text;

    DELETE FROM olist.monthly_delivery_snapshot WHERE purchase_month = v_month;

    INSERT INTO olist.monthly_delivery_snapshot (
        purchase_month, customer_state, delivered_order_count, eligible_order_count,
        excluded_order_count, late_order_count, on_time_order_count, late_delivery_pct
    )
    WITH classified_orders AS (
        SELECT c.customer_state,
               (o.delivery_delay_eligible
                AND o.order_delivered_customer_date IS NOT NULL
                AND o.order_estimated_delivery_date IS NOT NULL
                AND o.order_delivered_customer_date >= o.order_purchase_timestamp) AS is_eligible,
               o.order_delivered_customer_date::date > o.order_estimated_delivery_date AS is_late
        FROM olist.orders AS o
        JOIN olist.customers AS c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'delivered'
          AND o.order_purchase_timestamp >= v_month
          AND o.order_purchase_timestamp < v_end
    )
    SELECT v_month, customer_state,
           COUNT(*),
           COUNT(*) FILTER (WHERE is_eligible),
           COUNT(*) FILTER (WHERE NOT is_eligible),
           COUNT(*) FILTER (WHERE is_eligible AND is_late),
           COUNT(*) FILTER (WHERE is_eligible AND NOT is_late),
           olist.safe_percentage(
               COUNT(*) FILTER (WHERE is_eligible AND is_late),
               COUNT(*) FILTER (WHERE is_eligible))
    FROM classified_orders
    GROUP BY customer_state;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    INSERT INTO olist.monthly_delivery_snapshot_runs (
        purchase_month, source_snapshot_at, source_snapshot_id, refreshed_at,
        state_row_count, delivered_order_count
    )
    SELECT v_month, v_source_at, v_source_snapshot, clock_timestamp(), v_rows,
           COALESCE(SUM(delivered_order_count), 0)
    FROM olist.monthly_delivery_snapshot
    WHERE purchase_month = v_month
    ON CONFLICT (purchase_month) DO UPDATE SET
        source_snapshot_at = EXCLUDED.source_snapshot_at,
        source_snapshot_id = EXCLUDED.source_snapshot_id,
        refreshed_at = EXCLUDED.refreshed_at,
        state_row_count = EXCLUDED.state_row_count,
        delivered_order_count = EXCLUDED.delivered_order_count;
    -- No COMMIT, exception swallowing or partial-success path.
END;
$procedure$;
