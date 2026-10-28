"""Q2 delivery timeliness definitions and independent streamed verification."""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import text

from data_factory.database.query_benchmark import run_readonly_benchmark


QUERY_FILE = "02_monthly_state_delivery.sql"


def verify_delivery_results(connection, params, rows):
    """Compute all month/state counts again in Python from streamed order rows."""
    groups = defaultdict(Counter)
    coverage = Counter()
    first_purchase = last_purchase = None
    source = text("""
        SELECT o.order_purchase_timestamp, c.customer_state,
               o.order_delivered_customer_date, o.order_estimated_delivery_date,
               o.delivery_delay_eligible
        FROM olist.orders AS o
        JOIN olist.customers AS c ON c.customer_id = o.customer_id
        WHERE o.order_status = :order_status
          AND o.order_purchase_timestamp >= :start_date
          AND o.order_purchase_timestamp < :end_date
    """)
    # The large row-level control is fetched in batches, outside query timing.
    with connection.execution_options(stream_results=True).execute(source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                purchased = row["order_purchase_timestamp"]
                actual = row["order_delivered_customer_date"]
                estimate = row["order_estimated_delivery_date"]
                group = groups[(purchased.date().replace(day=1), row["customer_state"].strip())]
                group["delivered_order_count"] += 1
                coverage["delivered_order_count"] += 1
                first_purchase = purchased if first_purchase is None else min(first_purchase, purchased)
                last_purchase = purchased if last_purchase is None else max(last_purchase, purchased)
                if actual is None or estimate is None:
                    coverage["missing_dates"] += 1
                    group["excluded_order_count"] += 1
                    continue
                if not row["delivery_delay_eligible"] or actual < purchased:
                    coverage["other_ineligible"] += 1
                    group["excluded_order_count"] += 1
                    continue
                group["eligible_order_count"] += 1
                coverage["eligible_order_count"] += 1
                label = "late_order_count" if actual.date() > estimate else "on_time_order_count"
                group[label] += 1
                coverage[label] += 1
                if actual.date() == estimate:
                    coverage["delivered_on_estimated_date"] += 1
    count_fields = ("delivered_order_count", "eligible_order_count", "excluded_order_count",
                    "late_order_count", "on_time_order_count")
    observed = {}
    for row in rows:
        key = (row["purchase_month"], row["customer_state"].strip())
        if key in observed:
            raise AssertionError("Duplicate purchase-month/state group.")
        observed[key] = row
    if set(observed) != set(groups):
        raise AssertionError("SQL month/state groups differ from the Python control.")
    for key, counts in groups.items():
        row = observed[key]
        if any(row[field] != counts[field] for field in count_fields):
            raise AssertionError("SQL counts differ from independent Python counts.")
        eligible = counts["eligible_order_count"]
        expected = ((Decimal(100) * counts["late_order_count"] / eligible)
                    .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if eligible else None
        if row["late_delivery_pct"] != expected:
            raise AssertionError("SQL percentage differs from the Python control.")
    totals = {field: sum(row[field] for row in rows) for field in count_fields}
    eligible = totals["eligible_order_count"]
    totals["late_delivery_pct"] = ((Decimal(100) * totals["late_order_count"] / eligible)
                                  .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if eligible else None
    return {"passed": True, "method": "Independent Python classification of streamed order rows",
            "all_month_state_groups_verified": len(groups), "fetch_batch_size": 5000,
            "totals": totals,
            "exclusion_reasons": {"missing_dates": coverage["missing_dates"],
                                  "other_ineligible": coverage["other_ineligible"]},
            "delivered_on_estimated_date": coverage["delivered_on_estimated_date"],
            "first_purchase": first_purchase, "last_purchase": last_purchase}


def run_delivery_baseline(engine, project_root, *, start_date=date(2016, 1, 1),
                          end_date=date(2019, 1, 1), warmups=2,
                          repetitions=7, budget_ms=200):
    if type(start_date) is not date or type(end_date) is not date or start_date >= end_date:
        raise ValueError("Use date objects with start_date < end_date.")
    params = {"start_date": start_date, "end_date": end_date, "order_status": "delivered"}
    rows, report, plan = run_readonly_benchmark(
        engine, Path(project_root) / "sql/queries" / QUERY_FILE, params,
        relations=("orders", "customers"), verify=verify_delivery_results,
        warmups=warmups, repetitions=repetitions, budget_ms=budget_ms,
    )
    report.update({
        "question": "Q2 late-delivery percentage by customer state and purchase month",
        "semantics": {
            "month": "purchase month, source timestamps without timezone conversion",
            "state": "customer_state via the order's customer_id",
            "late": "actual delivery calendar date strictly after estimated delivery DATE",
            "denominator": "delivered orders with eligible flag, both dates, actual timestamp >= purchase",
            "excluded": "retained in delivered count; excluded from the rate denominator",
            "zero_denominator": "NULL percentage, never zero",
            "overall_percentage": "100 * total late / total eligible; not average of group percentages",
            "absent_month_state": "no synthetic zero rows",
        },
        "scope": "Q2 initial measurement only; no new indexes, views or other questions",
    })
    return rows, report, plan
