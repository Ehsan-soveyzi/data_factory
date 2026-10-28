"""Q4: seller revenue ranking and order-weighted preparation/review proxies."""

from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import text

from data_factory.database.query_benchmark import run_readonly_benchmark


QUERY_FILE = "04_top_seller_performance.sql"


def _rounded_ratio(numerator, denominator):
    return (Decimal(numerator) / denominator).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) if denominator else None


def verify_seller_results(connection, params, rows):
    """Rebuild seller/order shares, rankings and averages from streamed raw rows."""
    source = text("""
        SELECT oi.seller_id, oi.order_id, oi.price,
               o.order_approved_at, o.order_delivered_carrier_date,
               o.approval_to_carrier_duration_valid
        FROM olist.order_items AS oi
        JOIN olist.orders AS o ON o.order_id = oi.order_id
        WHERE o.order_status = :order_status
          AND o.order_purchase_timestamp >= :start_date
          AND o.order_purchase_timestamp < :end_date
    """)
    sellers = {}
    seller_orders = {}
    with connection.execution_options(stream_results=True).execute(source, params) as result:
        for batch in result.mappings().partitions(5000):
            for row in batch:
                seller = sellers.setdefault(row["seller_id"], {"sales": Decimal("0"), "items": 0, "orders": set()})
                seller["sales"] += row["price"]
                seller["items"] += 1
                seller["orders"].add(row["order_id"])
                key = (row["seller_id"], row["order_id"])
                if key not in seller_orders:
                    approved, carrier = row["order_approved_at"], row["order_delivered_carrier_date"]
                    seconds = None
                    if (row["approval_to_carrier_duration_valid"] and approved is not None
                            and carrier is not None and carrier >= approved):
                        duration = carrier - approved
                        microseconds = ((duration.days * 86400 + duration.seconds) * 1000000
                                        + duration.microseconds)
                        seconds = Decimal(microseconds) / 1000000
                    seller_orders[key] = seconds
    ranked = sorted(sellers, key=lambda key: (-sellers[key]["sales"], key))[:params["top_n"]]
    top_order_ids = {order for seller in ranked for order in sellers[seller]["orders"]}
    latest = {}
    raw_review_rows = 0
    if top_order_ids:
        review_source = text("""
            SELECT r.order_id, r.review_id, r.review_score,
                   r.review_answer_timestamp, r.review_creation_date
            FROM olist.order_reviews AS r
            JOIN olist.orders AS o ON o.order_id = r.order_id
            WHERE o.order_status = :order_status
              AND o.order_purchase_timestamp >= :start_date
              AND o.order_purchase_timestamp < :end_date
        """)
        with connection.execution_options(stream_results=True).execute(review_source, params) as result:
            for batch in result.mappings().partitions(5000):
                for row in batch:
                    if row["order_id"] not in top_order_ids:
                        continue
                    raw_review_rows += 1
                    key = (row["review_answer_timestamp"], row["review_creation_date"], row["review_id"])
                    previous = latest.get(row["order_id"])
                    if previous is None or key > previous[0]:
                        latest[row["order_id"]] = (key, row["review_score"])
        attributes = {row["seller_id"]: row for row in connection.execute(text("""
            SELECT seller_id, seller_city, seller_state
            FROM olist.sellers WHERE seller_id = ANY(:seller_ids)
        """), {"seller_ids": ranked}).mappings()}
    else:
        attributes = {}
    expected = []
    for rank, key in enumerate(ranked, 1):
        seller = sellers[key]
        prep = [seller_orders[(key, order)] for order in seller["orders"]
                if seller_orders[(key, order)] is not None]
        scores = [latest[order][1] for order in seller["orders"] if order in latest]
        expected.append({
            "revenue_rank": rank, "seller_id": key,
            "seller_city": attributes[key]["seller_city"],
            "seller_state": attributes[key]["seller_state"],
            "gross_product_sales_brl": seller["sales"],
            "sold_order_count": len(seller["orders"]), "sold_item_count": seller["items"],
            "preparation_eligible_count": len(prep),
            "preparation_excluded_count": len(seller["orders"]) - len(prep),
            "avg_preparation_days": _rounded_ratio(sum(prep, Decimal("0")), len(prep) * 86400),
            "reviewed_order_count": len(scores),
            "missing_review_count": len(seller["orders"]) - len(scores),
            "avg_review_score": _rounded_ratio(sum(scores), len(scores)),
        })
    if rows != expected:
        raise AssertionError("Q4 SQL differs from independent Python seller ranking/metrics.")
    order_seller_counts = Counter(order for seller, order in seller_orders)
    return {
        "passed": True, "method": "Independent Python item-price accumulation, seller/order sets and review selection",
        "fetch_batch_size": 5000, "all_ranked_rows_verified": len(expected),
        "all_seller_count": len(sellers),
        "all_product_sales_brl": sum((s["sales"] for s in sellers.values()), Decimal("0")),
        "all_item_count": sum(s["items"] for s in sellers.values()),
        "distinct_scoped_order_count": len(order_seller_counts),
        "seller_order_assignments": len(seller_orders),
        "multi_seller_order_count": sum(count > 1 for count in order_seller_counts.values()),
        "top_totals": {field: sum(row[field] for row in rows) for field in (
            "gross_product_sales_brl", "sold_order_count", "sold_item_count",
            "preparation_eligible_count", "preparation_excluded_count",
            "reviewed_order_count", "missing_review_count")},
        "top_distinct_order_count": len(top_order_ids),
        "top_distinct_reviewed_order_count": len(latest),
        "top_raw_review_rows": raw_review_rows,
        "top_review_rows_not_selected": raw_review_rows - len(latest),
    }


def run_seller_baseline(engine, project_root, *, start_date=date(2016, 1, 1),
                        end_date=date(2019, 1, 1), top_n=20,
                        warmups=2, repetitions=7, budget_ms=200):
    if type(start_date) is not date or type(end_date) is not date or start_date >= end_date:
        raise ValueError("Use date objects with start_date < end_date.")
    if type(top_n) is not int or top_n < 1:
        raise ValueError("top_n must be a positive integer.")
    params = {"start_date": start_date, "end_date": end_date,
              "order_status": "delivered", "top_n": top_n}
    rows, report, plan = run_readonly_benchmark(
        engine, Path(project_root) / "sql/queries" / QUERY_FILE, params,
        relations=("orders", "order_items", "sellers", "order_reviews"),
        verify=verify_seller_results, warmups=warmups,
        repetitions=repetitions, budget_ms=budget_ms,
    )
    report.update({
        "question": "Q4 top sellers by completed merchandise sales with preparation and review proxies",
        "semantics": {
            "scope": "delivered orders by purchase timestamp, start inclusive/end exclusive",
            "revenue": "seller's own SUM(item price), nominal BRL, excluding freight/payments",
            "ranking": "revenue descending, seller_id ascending for ties; exact top_n cap",
            "averaging_grain": "one seller/order pair, irrespective of item/review count",
            "preparation": "(carrier handoff timestamp - approval timestamp) in fractional days",
            "preparation_validity": "own validity flag, both dates and nonnegative interval; zero is valid",
            "missing_preparation": "seller revenue retained, duration excluded from average with count",
            "review_selection": "latest answer, then creation, then review_id descending, same as Q3",
            "missing_review": "seller revenue retained, score average excludes NULL with coverage count",
            "multi_seller_limit": "preparation timestamp and review score describe the whole order, not individual seller performance",
        },
        "scope": "Q4 initial measurement only; no new indexes, views or other questions",
    })
    return rows, report, plan
