"""Q3: latest recorded review score versus calendar-day delivery delay."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import text

from data_factory.database.query_benchmark import run_readonly_benchmark


QUERY_FILE = "03_delivery_review_satisfaction.sql"
BUCKET_LABELS = {1: "Early", 2: "On promised date", 3: "Late 1-3 days",
                 4: "Late 4-7 days", 5: "Late 8-14 days", 6: "Late 15+ days"}


def _rounded_ratio(numerator, denominator):
    return (Decimal(numerator) / denominator).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) if denominator else None


def verify_satisfaction_results(connection, params, rows):
    """Select reviews and classify delay again in Python from streamed source rows.

    Source rows include all delivered orders so date exclusions are accounted for.
    Reviews are not ranked in this control SQL; Python independently picks max.
    """
    source = text("""
        SELECT o.order_id, o.order_purchase_timestamp,
               o.order_delivered_customer_date, o.order_estimated_delivery_date,
               o.delivery_delay_eligible, r.review_id, r.review_score,
               r.review_creation_date, r.review_answer_timestamp
        FROM olist.orders AS o
        LEFT JOIN olist.order_reviews AS r ON r.order_id = o.order_id
        WHERE o.order_status = :order_status
          AND o.order_purchase_timestamp >= :start_date
          AND o.order_purchase_timestamp < :end_date
    """)
    orders = {}
    with connection.execution_options(stream_results=True).execute(source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                order = orders.get(row["order_id"])
                if order is None:
                    order = orders[row["order_id"]] = {
                        "purchase": row["order_purchase_timestamp"],
                        "actual": row["order_delivered_customer_date"],
                        "estimate": row["order_estimated_delivery_date"],
                        "flag": row["delivery_delay_eligible"],
                        "review_rows": 0, "selected_key": None, "score": None,
                    }
                if row["review_id"] is not None:
                    order["review_rows"] += 1
                    key = (row["review_answer_timestamp"], row["review_creation_date"], row["review_id"])
                    if order["selected_key"] is None or key > order["selected_key"]:
                        order["selected_key"] = key
                        order["score"] = row["review_score"]
    groups = defaultdict(Counter)
    coverage = Counter()
    for order in orders.values():
        actual, estimate = order["actual"], order["estimate"]
        if actual is None or estimate is None:
            coverage["excluded_missing_dates"] += 1
            continue
        if not order["flag"] or actual < order["purchase"]:
            coverage["excluded_other_ineligible"] += 1
            continue
        delay = (actual.date() - estimate).days
        if delay < 0:
            bucket = 1
        elif delay == 0:
            bucket = 2
        elif delay <= 3:
            bucket = 3
        elif delay <= 7:
            bucket = 4
        elif delay <= 14:
            bucket = 5
        else:
            bucket = 6
        counts = groups[bucket]
        counts["eligible_order_count"] += 1
        counts["delay_sum"] += delay
        coverage["raw_review_rows_for_eligible_orders"] += order["review_rows"]
        coverage["eligible_orders_with_multiple_reviews"] += order["review_rows"] > 1
        if order["score"] is None:
            counts["missing_review_count"] += 1
        else:
            score = order["score"]
            counts["reviewed_order_count"] += 1
            counts["score_sum"] += score
            counts["low_review_count"] += score <= 2
            counts["high_review_count"] += score >= 4
            counts["selected_review_before_delivery_count"] += order["selected_key"][0] < actual
    expected = []
    for bucket, counts in sorted(groups.items()):
        eligible, reviewed = counts["eligible_order_count"], counts["reviewed_order_count"]
        expected.append({
            "delay_bucket_order": bucket, "delay_bucket": BUCKET_LABELS[bucket],
            "eligible_order_count": eligible, "reviewed_order_count": reviewed,
            "missing_review_count": counts["missing_review_count"],
            "review_coverage_pct": _rounded_ratio(100 * reviewed, eligible),
            "avg_review_score": _rounded_ratio(counts["score_sum"], reviewed),
            "low_review_count": counts["low_review_count"],
            "low_review_pct": _rounded_ratio(100 * counts["low_review_count"], reviewed),
            "high_review_count": counts["high_review_count"],
            "high_review_pct": _rounded_ratio(100 * counts["high_review_count"], reviewed),
            "avg_delay_days": _rounded_ratio(counts["delay_sum"], eligible),
            "selected_review_before_delivery_count": counts["selected_review_before_delivery_count"],
        })
    if rows != expected:
        raise AssertionError("Q3 SQL differs from independent Python review selection/classification.")
    totals = {field: sum(row[field] for row in rows) for field in (
        "eligible_order_count", "reviewed_order_count", "missing_review_count",
        "low_review_count", "high_review_count", "selected_review_before_delivery_count")}
    totals["avg_review_score"] = _rounded_ratio(
        sum(counts["score_sum"] for counts in groups.values()), totals["reviewed_order_count"])
    return {
        "passed": True, "method": "Independent Python latest-review selection and delay classification",
        "all_delay_groups_verified": len(groups), "fetch_batch_size": 5000,
        "delivered_orders_in_scope": len(orders), "totals": totals,
        "exclusion_reasons": {key: coverage[key] for key in (
            "excluded_missing_dates", "excluded_other_ineligible")},
        "raw_review_rows_for_eligible_orders": coverage["raw_review_rows_for_eligible_orders"],
        "eligible_orders_with_multiple_reviews": coverage["eligible_orders_with_multiple_reviews"],
        "review_rows_not_selected": coverage["raw_review_rows_for_eligible_orders"] - totals["reviewed_order_count"],
    }


def run_satisfaction_baseline(engine, project_root, *, start_date=date(2016, 1, 1),
                              end_date=date(2019, 1, 1), warmups=2,
                              repetitions=7, budget_ms=200):
    if type(start_date) is not date or type(end_date) is not date or start_date >= end_date:
        raise ValueError("Use date objects with start_date < end_date.")
    params = {"start_date": start_date, "end_date": end_date, "order_status": "delivered"}
    rows, report, plan = run_readonly_benchmark(
        engine, Path(project_root) / "sql/queries" / QUERY_FILE, params,
        relations=("orders", "order_reviews"), verify=verify_satisfaction_results,
        warmups=warmups, repetitions=repetitions, budget_ms=budget_ms,
    )
    report.update({
        "question": "Q3 latest review satisfaction versus calendar-day delivery delay",
        "semantics": {
            "scope": "delivered orders with eligible flag, known dates and delivery at/after purchase",
            "delay_days": "actual delivery DATE minus estimated DATE; same date is zero",
            "review_selection": "latest answer timestamp, then creation timestamp, then review_id DESC",
            "weight": "one selected review per order; all other review rows retained in source",
            "missing_review": "order retained in coverage, score statistics exclude NULL",
            "low_score": "selected score 1-2; denominator reviewed orders",
            "high_score": "selected score 4-5; denominator reviewed orders",
            "review_timing": "pre-delivery responses retained and counted separately",
            "interpretation": "descriptive association; no causal or significance claim",
            "absent_buckets": "no invented zero rows; all-unreviewed buckets have NULL score statistics",
        },
        "scope": "Q3 initial measurement only; no new indexes, views or other questions",
    })
    return rows, report, plan
