-- Same Q5 output and fixed [2016-01-01, 2019-01-01) delivered-order scope.
SELECT * FROM olist.v_payment_analysis
ORDER BY analysis_level, payment_method_kind, payment_type NULLS LAST,
         payment_installments NULLS LAST, installment_kind;
