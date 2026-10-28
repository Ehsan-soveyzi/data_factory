-- Section 8: one result row per purchase month/state, for delivered orders.
CREATE TABLE IF NOT EXISTS olist.monthly_delivery_snapshot (
    purchase_month DATE NOT NULL CHECK (EXTRACT(DAY FROM purchase_month) = 1),
    customer_state CHAR(2) NOT NULL,
    delivered_order_count BIGINT NOT NULL CHECK (delivered_order_count >= 0),
    eligible_order_count BIGINT NOT NULL CHECK (eligible_order_count >= 0),
    excluded_order_count BIGINT NOT NULL CHECK (excluded_order_count >= 0),
    late_order_count BIGINT NOT NULL CHECK (late_order_count >= 0),
    on_time_order_count BIGINT NOT NULL CHECK (on_time_order_count >= 0),
    late_delivery_pct NUMERIC CHECK (late_delivery_pct BETWEEN 0 AND 100),
    PRIMARY KEY (purchase_month, customer_state),
    CHECK (delivered_order_count = eligible_order_count + excluded_order_count),
    CHECK (eligible_order_count = late_order_count + on_time_order_count)
);

CREATE TABLE IF NOT EXISTS olist.monthly_delivery_snapshot_runs (
    purchase_month DATE PRIMARY KEY CHECK (EXTRACT(DAY FROM purchase_month) = 1),
    source_snapshot_at TIMESTAMPTZ NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL,
    state_row_count INTEGER NOT NULL CHECK (state_row_count >= 0),
    delivered_order_count BIGINT NOT NULL CHECK (delivered_order_count >= 0)
);
