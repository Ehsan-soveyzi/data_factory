-- Pure numeric calculation usable in SELECT, including aggregate expressions.
-- A zero denominator or a NULL input means an undefined percentage, not zero.
CREATE OR REPLACE FUNCTION olist.safe_percentage(
    p_numerator NUMERIC, p_denominator NUMERIC
)
RETURNS NUMERIC
LANGUAGE SQL
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
    SELECT pg_catalog.round(100.0 * p_numerator / NULLIF(p_denominator, 0), 2)
$function$;
