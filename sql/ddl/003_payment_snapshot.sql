-- Section 7 derived result cache, fixed to the scope in v_payment_analysis.
-- WITH NO DATA installs the schema without running the expensive source query.
-- No per-row index is needed for the small, fully read result set.
CREATE TABLE IF NOT EXISTS olist.payment_analysis_snapshot AS
    SELECT * FROM olist.v_payment_analysis WITH NO DATA;

CREATE TABLE IF NOT EXISTS olist.analysis_snapshot_metadata (
    snapshot_name TEXT PRIMARY KEY,
    source_snapshot_at TIMESTAMPTZ NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL,
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    view_definition_sha256 TEXT NOT NULL
);
