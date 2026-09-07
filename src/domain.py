"""配送回数とは独立した業務イベントと、再構築の照合条件。"""

import hashlib
import re


def validate_order(value):
    if not isinstance(value, dict) or set(value) != {"order_id", "amount"}:
        raise ValueError("order_id and amount are required")
    if not isinstance(value["order_id"], str) or not re.fullmatch(r"order-[0-9]{3,6}", value["order_id"]):
        raise ValueError("use synthetic order IDs")
    if type(value["amount"]) is not int or not 1 <= value["amount"] <= 1_000_000:
        raise ValueError("amount must be an integer between 1 and 1000000")
    return value


def event_id(order_id):
    return hashlib.sha256(("OrderCreated/v1/" + order_id).encode()).hexdigest()


def validate_event(event):
    if event.get("source") != "lab.orders" or event.get("detail-type") != "OrderCreated":
        raise ValueError("unsupported event type")
    d = event["detail"]
    if set(d) != {"event_id", "order_id", "amount", "currency", "schema_version", "occurred_at"}:
        raise ValueError("unsupported event schema")
    validate_order({"order_id": d["order_id"], "amount": d["amount"]})
    if d["event_id"] != event_id(d["order_id"]) or d["schema_version"] != 1 or d["currency"] != "JPY":
        raise ValueError("invalid domain event")
    return d


def matches(expected, actual, total):
    """件数と合計だけで相殺された欠落を見落とさない。"""
    return (
        expected == actual
        and int(total.get("order_count", 0)) == len(expected)
        and int(total.get("total_amount", 0)) == sum(expected.values())
    )

