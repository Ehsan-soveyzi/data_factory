-- Read the explicitly refreshed Q5 result cache, not current source tables.
SELECT * FROM olist.payment_analysis_snapshot
ORDER BY analysis_level, payment_method_kind, payment_type NULLS LAST,
         payment_installments NULLS LAST, installment_kind;
