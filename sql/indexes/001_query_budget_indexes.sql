-- Retained part of the one fixed index-only trial. Original SQL/settings stay intact.
-- Three item/payment candidates were removed after measured regressions.
-- Their definitions and before/after results remain in index_structure_trial.json
-- and evidence/benchmarks/indexing/. No further tuning trial is performed.
-- Q3/Q4: same latest-review order as DISTINCT ON in the original SQL.
CREATE INDEX idx_qbudget_reviews_latest ON olist.order_reviews
    (order_id, review_answer_timestamp DESC, review_creation_date DESC, review_id DESC)
    INCLUDE (review_score);
