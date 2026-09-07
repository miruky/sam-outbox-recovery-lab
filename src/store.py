"""共有する接続と、単一リージョン内の条件付きトランザクション。"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=5)
db = boto3.resource("dynamodb", config=CONFIG)
tx = db.meta.client
events = boto3.client("events", config=CONFIG)
ORDERS = os.environ.get("ORDERS_TABLE", "")
CONTROL = os.environ.get("CONTROL_TABLE", "")
AGGREGATE = os.environ.get("AGGREGATE_TABLE", "")
NOTIFICATIONS = os.environ.get("NOTIFICATIONS_TABLE", "")
BUS = os.environ.get("EVENT_BUS", "")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=lambda v: int(v) if isinstance(v, Decimal) else str(v))


def get(table, pk, sk):
    return db.Table(table).get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")


def partition(table, pk):
    # 再構築対象は合成注文1,000件以内に制限し、ページ末尾も取得する。
    rows = []
    for page in tx.get_paginator("query").paginate(
        TableName=table, KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": pk}, ConsistentRead=True
    ):
        rows.extend(page.get("Items", []))
        if len(rows) > 2002:
            raise ValueError("bounded demonstration limit exceeded")
    return rows


def control():
    item = get(CONTROL, "SYSTEM", "CONTROL")
    if not item:
        raise ValueError("initialize the application first")
    return item


def put(table, item):
    return db.Table(table).put_item(Item=item, ConditionExpression=Attr("pk").not_exists())


def publish_outbox(item):
    d = item["detail"]
    result = events.put_events(Entries=[{
        "EventBusName": BUS, "Source": "lab.orders", "DetailType": "OrderCreated",
        "Detail": encode(d), "Time": datetime.fromisoformat(d["occurred_at"]),
    }])
    result_item = result["Entries"][0]
    if result.get("FailedEntryCount", 0) or "EventId" not in result_item:
        raise RuntimeError("PutEvents item failed: " + result_item.get("ErrorCode", "unknown"))
    # この更新より前に停止すると再送される。受信側の業務IDで重複を除く。
    db.Table(ORDERS).update_item(
        Key={"pk": "OUTBOX", "sk": d["event_id"]},
        UpdateExpression="SET delivery_status = :sent, sent_at = :at",
        ConditionExpression=Attr("pk").exists(),
        ExpressionAttributeValues={":sent": "SENT", ":at": now()},
    )


def snapshot(generation):
    rows = partition(AGGREGATE, "GEN#" + generation)
    actual = {r["sk"][4:]: int(r["amount"]) for r in rows if r["sk"].startswith("EVT#")}
    total = next((r for r in rows if r["sk"] == "TOTAL"), {})
    return actual, total
