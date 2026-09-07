"""受付停止、アーカイブ再生、内容照合、参照世代の切替を制御する。"""

import os
import re
from datetime import datetime, timedelta, timezone
from boto3.dynamodb.conditions import Attr
from domain import matches
import store as s


def job(job_id):
    value = s.get(s.CONTROL, "JOB#" + job_id, "JOB")
    if not value:
        raise ValueError("rebuild job not found")
    return value


def initialize():
    if s.get(s.CONTROL, "SYSTEM", "CONTROL"):
        return status()
    s.tx.transact_write_items(TransactItems=[
        {"Put": {"TableName": s.CONTROL, "Item": {"pk": "SYSTEM", "sk": "CONTROL", "generation": "initial", "mode": "OPEN", "aggregate_fault": False, "accepted_count": 0, "rebuild_count": 0}, "ConditionExpression": "attribute_not_exists(pk)"}},
        {"Put": {"TableName": s.CONTROL, "Item": {"pk": "GEN#initial", "sk": "META", "status": "ACTIVE"}, "ConditionExpression": "attribute_not_exists(pk)"}},
    ])
    return status()


def status():
    ctl = s.control()
    actual, total = s.snapshot(ctl["generation"])
    sent = [r for r in s.partition(s.NOTIFICATIONS, "NOTIFICATIONS") if r["sk"].startswith("SENT#")]
    outbox = s.partition(s.ORDERS, "OUTBOX")
    return {"generation": ctl["generation"], "mode": ctl["mode"], "aggregate_fault": ctl["aggregate_fault"],
            "accepted_orders": int(ctl["accepted_count"]), "outbox_sent": sum(r["delivery_status"] == "SENT" for r in outbox),
            "aggregate_count": int(total.get("order_count", 0)), "aggregate_amount": int(total.get("total_amount", 0)),
            "processed_event_ids": len(actual), "notification_count": len(sent)}


def prepare(data):
    jid, replay = data["job_id"], data["replay_name"]
    if not re.fullmatch(r"[a-f0-9]{32}", jid) or not re.fullmatch(r"miruky-[a-z]{16}", replay):
        raise ValueError("invalid generated identifiers")
    existing = s.get(s.CONTROL, "JOB#" + jid, "JOB")
    if not existing:
        ctl = s.control()
        s.tx.transact_write_items(TransactItems=[
            {"Update": {"TableName": s.CONTROL, "Key": {"pk": "SYSTEM", "sk": "CONTROL"},
                        "UpdateExpression": "SET #mode = :paused, job_id = :job ADD rebuild_count :one",
                        "ConditionExpression": "attribute_not_exists(job_id) AND generation = :old AND rebuild_count < :limit",
                        "ExpressionAttributeNames": {"#mode": "mode"},
                        "ExpressionAttributeValues": {":paused": "PAUSED", ":job": jid, ":old": ctl["generation"], ":one": 1, ":limit": 2}}},
            {"Put": {"TableName": s.CONTROL, "Item": {"pk": "JOB#" + jid, "sk": "JOB", "status": "PREPARING", "generation": jid, "old_generation": ctl["generation"], "replay_name": replay}, "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": s.CONTROL, "Item": {"pk": "GEN#" + jid, "sk": "META", "status": "BUILDING"}, "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": s.CONTROL, "Item": {"pk": "REPLAY#" + replay, "sk": "MAP", "generation": jid}, "ConditionExpression": "attribute_not_exists(pk)"}},
        ])
    current = job(jid)
    if current["replay_name"] != replay or s.control().get("job_id") != jid:
        raise ValueError("another rebuild owns the lock")
    if "expected" not in current:
        outbox = s.partition(s.ORDERS, "OUTBOX")
        if not outbox or any(x["delivery_status"] != "SENT" for x in outbox):
            raise ValueError("all outbox events must be delivered before rebuilding")
        expected = {x["detail"]["event_id"]: int(x["detail"]["amount"]) for x in outbox}
        times = [datetime.fromisoformat(x["detail"]["occurred_at"]) for x in outbox]
        # Waitの時刻はUTCのZ表記を要求する。秒の切り捨てで10分未満にならないよう切り上げる。
        ready_at = (max(datetime.fromisoformat(x["sent_at"]) for x in outbox) + timedelta(minutes=10, seconds=1)).replace(microsecond=0)
        # 停止中の受付はトランザクションの条件で拒否され、この対象集合は変わらない。
        s.db.Table(s.CONTROL).update_item(
            Key={"pk": "JOB#" + jid, "sk": "JOB"},
            UpdateExpression="SET expected = :e, event_start = :start, event_end = :end, archive_ready_at = :at, #status = :state",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":e": expected, ":start": (min(times)-timedelta(seconds=1)).isoformat(),
                ":end": (max(times)+timedelta(seconds=1)).isoformat(), ":at": ready_at.isoformat().replace("+00:00", "Z"), ":state": "PREPARED"},
            ConditionExpression=Attr("status").eq("PREPARING"),
        )
        current = job(jid)
    return {"job_id": jid, "replay_name": replay, "archive_ready_at": current["archive_ready_at"], "polls": 0}


def start_replay(data):
    current = job(data["job_id"])
    try:
        replay = s.events.describe_replay(ReplayName=current["replay_name"])
    except s.events.exceptions.ResourceNotFoundException:
        replay = s.events.start_replay(
            ReplayName=current["replay_name"], EventSourceArn=os.environ["ARCHIVE_ARN"],
            EventStartTime=datetime.fromisoformat(current["event_start"]), EventEndTime=datetime.fromisoformat(current["event_end"]),
            Destination={"Arn": os.environ["EVENT_BUS_ARN"], "FilterArns": [os.environ["REBUILD_RULE_ARN"]]},
        )
    return {**data, "replay_state": replay["State"], "polls": 0}


def poll_replay(data):
    result = s.events.describe_replay(ReplayName=job(data["job_id"])["replay_name"])
    if result["State"] in {"FAILED", "CANCELLED", "CANCELLING"}:
        raise RuntimeError("replay did not complete")
    count = data.get("polls", 0) + 1
    if count > 120:
        raise TimeoutError("replay wait limit exceeded")
    return {**data, "replay_state": result["State"], "polls": count}


def verify(data):
    jid = data["job_id"]
    current = job(jid)
    expected = {k: int(v) for k, v in current["expected"].items()}
    meta = s.get(s.CONTROL, "GEN#" + jid, "META")
    actual, total = s.snapshot(jid)
    count = data.get("verify_polls", 0) + 1
    if meta["status"] == "BUILDING":
        if not matches(expected, actual, total):
            if count > 60:
                raise TimeoutError("event IDs, count or amount did not match")
            return {**data, "verified": False, "verify_polls": count}
        # 書き込みを止めてから再照合する。照合直後の追加書き込みも許容しない。
        s.db.Table(s.CONTROL).update_item(
            Key={"pk": "GEN#" + jid, "sk": "META"}, UpdateExpression="SET #status = :frozen",
            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":frozen": "VERIFYING"},
            ConditionExpression=Attr("status").eq("BUILDING"),
        )
    elif meta["status"] not in {"VERIFYING", "VERIFIED"}:
        raise ValueError("generation cannot be verified")
    actual, total = s.snapshot(jid)
    if not matches(expected, actual, total):
        raise ValueError("frozen generation failed verification")
    s.db.Table(s.CONTROL).update_item(
        Key={"pk": "GEN#" + jid, "sk": "META"}, UpdateExpression="SET #status = :verified",
        ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":verified": "VERIFIED"},
        ConditionExpression=Attr("status").is_in(["VERIFYING", "VERIFIED"]),
    )
    return {**data, "verified": True, "verified_count": len(expected), "verified_amount": sum(expected.values())}


def activate(data):
    jid = data["job_id"]
    current = job(jid)
    if current["status"] == "ACTIVE" and s.control()["generation"] == jid:
        return data
    expected = {k: int(v) for k, v in current["expected"].items()}
    actual, total = s.snapshot(jid)
    if not matches(expected, actual, total):
        raise ValueError("activation verification failed")
    s.tx.transact_write_items(TransactItems=[
        {"Update": {"TableName": s.CONTROL, "Key": {"pk": "SYSTEM", "sk": "CONTROL"},
                    "UpdateExpression": "SET generation = :new REMOVE job_id",
                    "ConditionExpression": "job_id = :job AND generation = :old AND #mode = :paused",
                    "ExpressionAttributeNames": {"#mode": "mode"},
                    "ExpressionAttributeValues": {":new": jid, ":job": jid, ":old": current["old_generation"], ":paused": "PAUSED"}}},
        {"Update": {"TableName": s.CONTROL, "Key": {"pk": "GEN#" + jid, "sk": "META"},
                    "UpdateExpression": "SET #status = :active", "ConditionExpression": "#status = :verified",
                    "ExpressionAttributeNames": {"#status": "status"}, "ExpressionAttributeValues": {":active": "ACTIVE", ":verified": "VERIFIED"}}},
        {"Update": {"TableName": s.CONTROL, "Key": {"pk": "GEN#" + current["old_generation"], "sk": "META"},
                    "UpdateExpression": "SET #status = :retired", "ConditionExpression": "#status = :active",
                    "ExpressionAttributeNames": {"#status": "status"}, "ExpressionAttributeValues": {":retired": "RETIRED", ":active": "ACTIVE"}}},
        {"Update": {"TableName": s.CONTROL, "Key": {"pk": "JOB#" + jid, "sk": "JOB"},
                    "UpdateExpression": "SET #status = :active", "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":active": "ACTIVE"}}},
    ])
    return {"job_id": jid, "generation": jid, "verified_count": len(expected), "verified_amount": sum(expected.values()), "result": "ACTIVATED"}


def fail(data):
    jid = data.get("job_id", "")
    current = s.get(s.CONTROL, "JOB#" + jid, "JOB")
    if current and current["status"] != "ACTIVE":
        try:
            replay = s.events.describe_replay(ReplayName=current["replay_name"])
            if replay["State"] in {"STARTING", "RUNNING"}:
                s.events.cancel_replay(ReplayName=current["replay_name"])
        except s.events.exceptions.ResourceNotFoundException:
            pass
        s.db.Table(s.CONTROL).update_item(
            Key={"pk": "JOB#" + jid, "sk": "JOB"}, UpdateExpression="SET #status = :failed",
            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":failed": "FAILED"},
            ConditionExpression=Attr("status").ne("ACTIVE"),
        )
        s.db.Table(s.CONTROL).update_item(
            Key={"pk": "GEN#" + jid, "sk": "META"}, UpdateExpression="SET #status = :failed",
            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":failed": "FAILED"},
            ConditionExpression=Attr("status").ne("ACTIVE"),
        )
        try:
            s.db.Table(s.CONTROL).update_item(Key={"pk": "SYSTEM", "sk": "CONTROL"}, UpdateExpression="REMOVE job_id", ConditionExpression=Attr("job_id").eq(jid))
        except s.tx.exceptions.ConditionalCheckFailedException:
            pass
    return {"job_id": jid, "result": "OLD_GENERATION_RETAINED"}


def handler(event, context):
    action, data = event["action"], event.get("data", {})
    if action == "initialize":
        return initialize()
    if action == "status":
        return status()
    if action == "fault":
        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be boolean")
        s.db.Table(s.CONTROL).update_item(Key={"pk": "SYSTEM", "sk": "CONTROL"}, UpdateExpression="SET aggregate_fault = :fault", ExpressionAttributeValues={":fault": data["enabled"]}, ConditionExpression=Attr("pk").exists())
        return status()
    if action == "resume":
        s.db.Table(s.CONTROL).update_item(Key={"pk": "SYSTEM", "sk": "CONTROL"}, UpdateExpression="SET #mode = :open", ExpressionAttributeNames={"#mode": "mode"}, ExpressionAttributeValues={":open": "OPEN"}, ConditionExpression=Attr("job_id").not_exists() & Attr("pk").exists())
        return status()
    if action == "recover-outbox":
        pending = [r for r in s.partition(s.ORDERS, "OUTBOX") if r["delivery_status"] != "SENT"]
        limit = data.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1 to 100")
        for item in pending[:limit]:
            s.publish_outbox(item)
        return {"recovered": min(len(pending), limit), "remaining": max(0, len(pending)-limit)}
    routes = {"prepare": prepare, "start-replay": start_replay, "poll-replay": poll_replay, "verify": verify, "activate": activate, "fail": fail}
    return routes[action](data)
