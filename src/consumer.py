"""通常集計・再構築・模擬通知を別の関数として実行する。"""

import json
import os
from domain import validate_event
import store as s


def aggregate(d, generation, rebuilding):
    pk = "GEN#" + generation
    existing = s.get(s.AGGREGATE, pk, "EVT#" + d["event_id"])
    if existing:
        if int(existing["amount"]) != d["amount"]:
            raise ValueError("conflicting event payload")
        return
    guards = [{"ConditionCheck": {"TableName": s.CONTROL, "Key": {"pk": pk, "sk": "META"},
        "ConditionExpression": "#status = :state", "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":state": "BUILDING" if rebuilding else "ACTIVE"}}}]
    if not rebuilding:
        guards.append({"ConditionCheck": {"TableName": s.CONTROL, "Key": {"pk": "SYSTEM", "sk": "CONTROL"},
            "ConditionExpression": "generation = :gen", "ExpressionAttributeValues": {":gen": generation}}})
    try:
        s.tx.transact_write_items(TransactItems=guards + [
            {"Put": {"TableName": s.AGGREGATE, "Item": {"pk": pk, "sk": "EVT#" + d["event_id"], "amount": d["amount"]},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Update": {"TableName": s.AGGREGATE, "Key": {"pk": pk, "sk": "TOTAL"},
                        "UpdateExpression": "ADD order_count :one, total_amount :amount",
                        "ExpressionAttributeValues": {":one": 1, ":amount": d["amount"]}}},
        ])
    except s.tx.exceptions.TransactionCanceledException:
        existing = s.get(s.AGGREGATE, pk, "EVT#" + d["event_id"])
        if existing and int(existing["amount"]) == d["amount"]:
            return
        raise


def notify(d):
    # 実際のメール送信は行わず、送信結果と処理済み記録を同じトランザクションにする。
    pk = "NOTIFICATIONS"
    try:
        s.tx.transact_write_items(TransactItems=[
            {"Put": {"TableName": s.NOTIFICATIONS, "Item": {"pk": pk, "sk": "SENT#" + d["event_id"], "order_id": d["order_id"], "amount": d["amount"]},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": s.NOTIFICATIONS, "Item": {"pk": pk, "sk": "DONE#" + d["event_id"]},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ])
    except s.tx.exceptions.TransactionCanceledException:
        existing = s.get(s.NOTIFICATIONS, pk, "SENT#" + d["event_id"])
        if existing and existing["order_id"] == d["order_id"] and int(existing["amount"]) == d["amount"]:
            return
        raise


def process(event, kind):
    d = validate_event(event)
    replay = event.get("replay-name")
    if kind == "notify":
        if replay:
            raise ValueError("replay rejected by notification consumer")
        notify(d)
    elif kind == "rebuild":
        if not replay:
            raise ValueError("replay name required")
        mapping = s.get(s.CONTROL, "REPLAY#" + replay, "MAP")
        if not mapping:
            raise ValueError("unknown replay")
        aggregate(d, mapping["generation"], True)
    else:
        if replay:
            raise ValueError("replay rejected by normal consumer")
        ctl = s.control()
        if ctl.get("aggregate_fault", False):
            raise RuntimeError("InjectedAggregateFailure")
        aggregate(d, ctl["generation"], False)


def handler(event, context):
    failed = []
    for record in event["Records"]:
        try:
            process(json.loads(record["body"]), os.environ["CONSUMER_KIND"])
        except Exception as exc:
            print(s.encode({"operation": os.environ["CONSUMER_KIND"], "error_type": type(exc).__name__}))
            failed.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failed}
