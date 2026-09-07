"""IAM認証済みHTTP APIから、注文と送信待ちイベントを同時確定する。"""

import json
from domain import event_id, validate_order
import store as s


def response(status, value):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": s.encode(value)}


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "GET":
        ctl = s.control()
        actual, total = s.snapshot(ctl["generation"])
        return response(200, {"generation": ctl["generation"], "mode": ctl["mode"],
                              "order_count": int(total.get("order_count", 0)),
                              "total_amount": int(total.get("total_amount", 0)), "processed_ids": len(actual)})
    try:
        value = validate_order(json.loads(event.get("body") or "{}"))
    except (ValueError, TypeError):
        return response(400, {"error": "invalid synthetic order"})
    key = {"pk": "ORDER#" + value["order_id"], "sk": "ORDER"}
    old = s.get(s.ORDERS, key["pk"], key["sk"])
    if old:
        return response(200 if int(old["amount"]) == value["amount"] else 409,
                        {"order_id": value["order_id"], "duplicate": True})
    d = {**value, "event_id": event_id(value["order_id"]), "currency": "JPY", "schema_version": 1, "occurred_at": s.now()}
    try:
        # トランザクション内の条件は低水準APIの文字列表現を使用する。
        s.tx.transact_write_items(TransactItems=[
            {"Update": {"TableName": s.CONTROL, "Key": {"pk": "SYSTEM", "sk": "CONTROL"},
                        "UpdateExpression": "ADD accepted_count :one",
                        "ConditionExpression": "#mode = :open AND accepted_count < :limit", "ExpressionAttributeNames": {"#mode": "mode"},
                        "ExpressionAttributeValues": {":open": "OPEN", ":limit": 1000, ":one": 1}}},
            {"Put": {"TableName": s.ORDERS, "Item": {**key, **value}, "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": s.ORDERS, "Item": {"pk": "OUTBOX", "sk": d["event_id"], "detail": d, "delivery_status": "PENDING"},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ])
    except s.tx.exceptions.TransactionCanceledException:
        old = s.get(s.ORDERS, key["pk"], key["sk"])
        if old:
            return response(200 if int(old["amount"]) == value["amount"] else 409, {"order_id": value["order_id"], "duplicate": True})
        if s.control()["mode"] != "OPEN":
            return response(503, {"error": "order intake paused"})
        if int(s.control()["accepted_count"]) >= 1000:
            return response(429, {"error": "synthetic order limit reached"})
        raise
    return response(201, {"order_id": value["order_id"], "event_id": d["event_id"], "duplicate": False})
