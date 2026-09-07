import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def app(monkeypatch):
    # Moto内だけで実行し、実アカウントの認証とエンドポイントを使用しない。
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from aws_resource_names import generate_name
    with mock_aws():
        db = boto3.resource("dynamodb")
        for key in ["ORDERS", "CONTROL", "AGGREGATE", "NOTIFICATIONS"]:
            name = generate_name(prefix="miruky", compact=False, random_length=16).name
            monkeypatch.setenv(key + "_TABLE", name)
            db.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ["pk", "sk"]], KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        import store, orders, consumer, controller
        importlib.reload(store)
        controller.initialize()
        yield store, orders, consumer, controller


def request(orders, number=1, amount=100):
    return orders.handler({"requestContext": {"http": {"method": "POST"}}, "body": json.dumps({"order_id": f"order-{number:03d}", "amount": amount})}, None)


def sample(store, orders):
    assert request(orders)["statusCode"] == 201
    d = store.partition(store.ORDERS, "OUTBOX")[0]["detail"]
    d["amount"] = int(d["amount"])
    d["schema_version"] = int(d["schema_version"])
    return {"source": "lab.orders", "detail-type": "OrderCreated", "detail": d}


def test_order_and_outbox_are_idempotent_together(app):
    s, orders, _, controller = app
    assert request(orders)["statusCode"] == 201
    assert request(orders)["statusCode"] == 200
    assert request(orders, amount=200)["statusCode"] == 409
    assert len(s.partition(s.ORDERS, "OUTBOX")) == 1
    assert int(s.control()["accepted_count"]) == 1


def test_pause_rejects_the_whole_order_transaction(app):
    s, orders, _, _ = app
    s.db.Table(s.CONTROL).update_item(Key={"pk": "SYSTEM", "sk": "CONTROL"}, UpdateExpression="SET #m = :m", ExpressionAttributeNames={"#m": "mode"}, ExpressionAttributeValues={":m": "PAUSED"})
    assert request(orders)["statusCode"] == 503
    assert not s.get(s.ORDERS, "ORDER#order-001", "ORDER")
    assert s.partition(s.ORDERS, "OUTBOX") == []


def test_delivery_id_changes_do_not_duplicate_business_effects(app):
    s, orders, consumer, controller = app
    event = sample(s, orders)
    for delivery_id in ["delivery-a", "delivery-b", "delivery-c"]:
        consumer.process({**event, "id": delivery_id}, "aggregate")
        consumer.process({**event, "id": delivery_id}, "notify")
    result = controller.status()
    assert (result["aggregate_count"], result["aggregate_amount"], result["notification_count"]) == (1, 100, 1)


def test_aggregate_failure_does_not_block_notification(app):
    s, orders, consumer, controller = app
    event = sample(s, orders)
    controller.handler({"action": "fault", "data": {"enabled": True}}, None)
    with pytest.raises(RuntimeError):
        consumer.process(event, "aggregate")
    consumer.process(event, "notify")
    result = controller.status()
    assert (result["aggregate_count"], result["notification_count"]) == (0, 1)


def test_same_event_is_counted_once_in_each_generation(app):
    s, orders, consumer, _ = app
    event = sample(s, orders)
    consumer.process(event, "aggregate")
    s.put(s.CONTROL, {"pk": "GEN#next", "sk": "META", "status": "BUILDING"})
    for _ in range(3):
        consumer.aggregate(event["detail"], "next", True)
    assert s.snapshot("initial")[1]["total_amount"] == 100
    assert s.snapshot("next")[1]["total_amount"] == 100


def test_freeze_blocks_new_events_but_allows_already_counted_duplicates(app):
    s, orders, consumer, _ = app
    first = sample(s, orders)
    s.put(s.CONTROL, {"pk": "GEN#next", "sk": "META", "status": "BUILDING"})
    consumer.aggregate(first["detail"], "next", True)
    s.db.Table(s.CONTROL).update_item(Key={"pk": "GEN#next", "sk": "META"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "VERIFIED"})
    consumer.aggregate(first["detail"], "next", True)
    second = dict(first["detail"], event_id="different", amount=200)
    with pytest.raises(s.tx.exceptions.TransactionCanceledException):
        consumer.aggregate(second, "next", True)
    assert s.snapshot("next")[1]["order_count"] == 1


def test_replay_is_rejected_by_notification_consumer(app):
    s, orders, consumer, controller = app
    event = sample(s, orders)
    with pytest.raises(ValueError, match="replay rejected"):
        consumer.process({**event, "replay-name": "a-replay"}, "notify")
    assert controller.status()["notification_count"] == 0


def test_partial_putevents_failure_does_not_mark_outbox_sent(app, monkeypatch):
    s, orders, _, _ = app
    sample(s, orders)
    item = s.partition(s.ORDERS, "OUTBOX")[0]
    monkeypatch.setattr(s.events, "put_events", lambda **kwargs: {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]})
    with pytest.raises(RuntimeError):
        s.publish_outbox(item)
    assert s.partition(s.ORDERS, "OUTBOX")[0]["delivery_status"] == "PENDING"


def test_publication_before_ack_can_repeat_without_double_counting(app, monkeypatch):
    s, orders, consumer, controller = app
    event = sample(s, orders)
    item = s.partition(s.ORDERS, "OUTBOX")[0]
    published = []
    def send(**kwargs):
        published.append(json.loads(kwargs["Entries"][0]["Detail"]))
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "delivery"}]}
    monkeypatch.setattr(s.events, "put_events", send)
    original = s.db.Table
    class CrashOnAck:
        def update_item(self, **kwargs):
            raise RuntimeError("crash after publication")
    monkeypatch.setattr(s.db, "Table", lambda name: CrashOnAck())
    with pytest.raises(RuntimeError, match="crash"):
        s.publish_outbox(item)
    monkeypatch.setattr(s.db, "Table", original)
    s.publish_outbox(item)
    for payload in published:
        consumer.process({**event, "detail": payload}, "aggregate")
    assert len(published) == 2
    assert controller.status()["aggregate_count"] == 1


def test_activation_requires_verified_generation_even_when_totals_match(app):
    s, orders, consumer, controller = app
    event = sample(s, orders)
    jid = "b" * 32
    s.put(s.CONTROL, {"pk": "GEN#"+jid, "sk": "META", "status": "BUILDING"})
    s.put(s.CONTROL, {"pk": "JOB#"+jid, "sk": "JOB", "generation": jid, "old_generation": "initial", "status": "PREPARED", "expected": {event["detail"]["event_id"]: 100}})
    s.db.Table(s.CONTROL).update_item(Key={"pk":"SYSTEM","sk":"CONTROL"},UpdateExpression="SET #m = :m, job_id = :j", ExpressionAttributeNames={"#m":"mode"},ExpressionAttributeValues={":m":"PAUSED",":j":jid})
    consumer.aggregate(event["detail"], jid, True)
    with pytest.raises(s.tx.exceptions.TransactionCanceledException):
        controller.activate({"job_id":jid})
    assert s.control()["generation"] == "initial"
    result = controller.verify({"job_id":jid})
    assert result["verified"]
    controller.activate(result)
    assert s.control()["generation"] == jid
    assert s.get(s.CONTROL,"GEN#initial","META")["status"] == "RETIRED"


def test_archive_wait_uses_z_and_never_rounds_below_ten_minutes(app):
    from datetime import datetime, timezone
    from aws_resource_names import generate_name
    s, orders, _, controller = app
    sample(s, orders)
    item = s.partition(s.ORDERS, "OUTBOX")[0]
    sent_at = datetime.now(timezone.utc).replace(microsecond=900000)
    s.db.Table(s.ORDERS).update_item(Key={"pk": "OUTBOX", "sk": item["sk"]},
        UpdateExpression="SET delivery_status = :s, sent_at = :t",
        ExpressionAttributeValues={":s": "SENT", ":t": sent_at.isoformat()})
    replay = generate_name(prefix="miruky", compact=False, random_length=16).name
    result = controller.prepare({"job_id": "c" * 32, "replay_name": replay})
    assert result["archive_ready_at"].endswith("Z")
    delay = datetime.fromisoformat(result["archive_ready_at"]) - sent_at
    assert 600 <= delay.total_seconds() < 601
