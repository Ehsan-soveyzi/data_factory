"""Q5: exclusive order-payment profiles and actual payment-plan distributions."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import text

from data_factory.database.query_benchmark import run_readonly_benchmark


QUERY_FILE = "05_payment_methods_installments.sql"
KEY_FIELDS = ("analysis_level", "payment_method_kind", "payment_type",
              "installment_kind", "payment_installments")


def _rounded_ratio(numerator, denominator):
    return (Decimal(numerator) / denominator).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) if denominator else None


def _empty_stats():
    return {"order_count": 0, "valued_order_count": 0, "payment_row_count": 0,
            "paid_sum": Decimal("0"), "value_sum": Decimal("0")}


def _add_order(stats, value, payment_count, paid):
    stats["order_count"] += 1
    if value is not None:
        stats["valued_order_count"] += 1
        stats["value_sum"] += value
    stats["payment_row_count"] += payment_count
    if payment_count:
        stats["paid_sum"] += paid


def verify_payment_results(connection, params, rows):
    """Rebuild every distribution from separately streamed orders/items/payments.

    No control query joins payment rows to item rows or uses SQL aggregation.
    Python collapses repeated same-plan payments within each order before taking
    the plan group's mean full merchandise order value.
    """
    orders = {}
    source = text("""
        SELECT order_id FROM olist.orders
        WHERE order_status = :order_status
          AND order_purchase_timestamp >= :start_date
          AND order_purchase_timestamp < :end_date
    """)
    with connection.execution_options(stream_results=True).execute(source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                orders[row["order_id"]] = {"value": None, "payments": []}
    item_source = text("""
        SELECT oi.order_id, oi.price
        FROM olist.order_items AS oi JOIN olist.orders AS o ON o.order_id = oi.order_id
        WHERE o.order_status = :order_status
          AND o.order_purchase_timestamp >= :start_date
          AND o.order_purchase_timestamp < :end_date
    """)
    with connection.execution_options(stream_results=True).execute(item_source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                order = orders[row["order_id"]]
                order["value"] = (order["value"] if order["value"] is not None else Decimal("0")) + row["price"]
    payment_source = text("""
        SELECT p.order_id, p.payment_type, p.payment_installments, p.payment_value
        FROM olist.order_payments AS p JOIN olist.orders AS o ON o.order_id = p.order_id
        WHERE o.order_status = :order_status
          AND o.order_purchase_timestamp >= :start_date
          AND o.order_purchase_timestamp < :end_date
    """)
    with connection.execution_options(stream_results=True).execute(payment_source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                orders[row["order_id"]]["payments"].append(
                    (row["payment_type"], row["payment_installments"], row["payment_value"]))
    total_rows = sum(len(order["payments"]) for order in orders.values())
    total_paid = sum((p[2] for order in orders.values() for p in order["payments"]), Decimal("0"))
    groups = defaultdict(_empty_stats)
    coverage = {key: 0 for key in (
        "orders_without_payment", "orders_without_items", "orders_with_multiple_payment_rows",
        "orders_with_mixed_methods", "orders_with_unknown_installments", "unknown_installment_rows")}
    for order in orders.values():
        payments, value = order["payments"], order["value"]
        coverage["orders_without_items"] += value is None
        coverage["orders_with_multiple_payment_rows"] += len(payments) > 1
        if not payments:
            coverage["orders_without_payment"] += 1
            key = ("order_profile", "no_payment", None, "no_payment", None)
            _add_order(groups[key], value, 0, None)
            continue
        methods = {p[0] for p in payments}
        known_plans = {p[1] for p in payments if p[1] is not None}
        unknown_rows = sum(p[1] is None for p in payments)
        method_kind = "single_method" if len(methods) == 1 else "mixed_methods"
        method = next(iter(methods)) if len(methods) == 1 else None
        coverage["orders_with_mixed_methods"] += len(methods) > 1
        coverage["orders_with_unknown_installments"] += unknown_rows > 0
        coverage["unknown_installment_rows"] += unknown_rows
        if unknown_rows == len(payments):
            installment_kind = "unknown"
        elif unknown_rows:
            installment_kind = "partly_unknown"
        elif len(known_plans) > 1:
            installment_kind = "mixed_plans"
        else:
            installment_kind = "single_plan"
        installments = next(iter(known_plans)) if len(known_plans) == 1 and not unknown_rows else None
        key = ("order_profile", method_kind, method, installment_kind, installments)
        _add_order(groups[key], value, len(payments), sum((p[2] for p in payments), Decimal("0")))
        plans = {}
        for method, installments, amount in payments:
            stats = plans.setdefault((method, installments), {"rows": 0, "paid": Decimal("0")})
            stats["rows"] += 1
            stats["paid"] += amount
        for (method, installments), stats in plans.items():
            key = ("payment_plan", "recorded_method", method,
                   "unknown" if installments is None else "single_plan", installments)
            _add_order(groups[key], value, stats["rows"], stats["paid"])
    expected = {}
    for key, stats in groups.items():
        expected[key] = {
            **dict(zip(KEY_FIELDS, key)), "order_count": stats["order_count"],
            "order_share_pct": _rounded_ratio(100 * stats["order_count"], len(orders)),
            "valued_order_count": stats["valued_order_count"],
            "payment_row_count": stats["payment_row_count"],
            "payment_row_share_pct": _rounded_ratio(100 * stats["payment_row_count"], total_rows),
            "recorded_payment_value_brl": stats["paid_sum"] if stats["payment_row_count"] else None,
            "avg_payment_row_value_brl": _rounded_ratio(stats["paid_sum"], stats["payment_row_count"]),
            "merchandise_value_brl": stats["value_sum"] if stats["valued_order_count"] else None,
            "avg_order_value_brl": _rounded_ratio(stats["value_sum"], stats["valued_order_count"]),
        }
    observed = {tuple(row[key] for key in KEY_FIELDS): row for row in rows}
    if len(observed) != len(rows) or observed != expected:
        raise AssertionError("Q5 SQL differs from independent Python payment profiles/plans.")
    profile_rows = [row for row in rows if row["analysis_level"] == "order_profile"]
    plan_rows = [row for row in rows if row["analysis_level"] == "payment_plan"]
    known_values = [order["value"] for order in orders.values() if order["value"] is not None]
    return {
        "passed": True, "method": "Independent Python classification of separate streamed orders/items/payments",
        "fetch_batch_size": 5000, "all_groups_verified": len(rows),
        "profile_group_count": len(profile_rows), "plan_group_count": len(plan_rows),
        "scoped_order_count": len(orders), "valued_order_count": len(known_values),
        "merchandise_value_brl": sum(known_values, Decimal("0")),
        "avg_order_value_brl": _rounded_ratio(sum(known_values, Decimal("0")), len(known_values)),
        "payment_row_count": total_rows, "recorded_payment_value_brl": total_paid,
        "coverage": coverage,
        "profile_order_count": sum(row["order_count"] for row in profile_rows),
        "plan_order_memberships": sum(row["order_count"] for row in plan_rows),
        "levels_must_not_be_summed": True,
    }


def run_payment_baseline(engine, project_root, *, start_date=date(2016, 1, 1),
                         end_date=date(2019, 1, 1), warmups=2,
                         repetitions=7, budget_ms=200):
    if type(start_date) is not date or type(end_date) is not date or start_date >= end_date:
        raise ValueError("Use date objects with start_date < end_date.")
    params = {"start_date": start_date, "end_date": end_date, "order_status": "delivered"}
    rows, report, plan = run_readonly_benchmark(
        engine, Path(project_root) / "sql/queries" / QUERY_FILE, params,
        relations=("orders", "order_items", "order_payments"), verify=verify_payment_results,
        warmups=warmups, repetitions=repetitions, budget_ms=budget_ms,
    )
    report.update({
        "question": "Q5 payment methods/installments and merchandise order values",
        "semantics": {
            "scope": "delivered orders by purchase date, start inclusive/end exclusive",
            "order_value": "SUM(item price) in nominal BRL, excluding freight; missing items remain NULL",
            "payment_value": "recorded payment row amount, summed once, never multiplied by installments",
            "order_profile": "exclusive method/plan classification; one membership per scoped order",
            "payment_plan": "recorded method/instalment groups; repeated same-plan rows collapsed per order for order averages",
            "overlap": "plan order counts and merchandise totals are not additive across plans or result levels",
            "mixed_methods": "separate exclusive order category; no arbitrary primary payment selection",
            "unknown_installments": "NULL preserved; unknown and partly unknown profiles kept separate",
            "missing_payment": "order retained; no-payment amount and payment average NULL",
            "interpretation": "descriptive group association, not causal or significance evidence",
        },
        "scope": "Q5 initial measurement only; no new indexes, views or other questions",
    })
    return rows, report, plan
