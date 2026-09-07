"""StreamsのINSERTから未送信Outboxを配送する。"""

from boto3.dynamodb.types import TypeDeserializer
import store as s

decoder = TypeDeserializer()


def handler(event, context):
    failed = []
    for record in event["Records"]:
        if record["eventName"] != "INSERT":
            continue
        raw = record["dynamodb"].get("NewImage", {})
        if raw.get("pk", {}).get("S") != "OUTBOX":
            continue
        try:
            item = {k: decoder.deserialize(v) for k, v in raw.items()}
            s.publish_outbox(item)
        except Exception as exc:
            # メッセージ全体や識別情報をログへ出さず、失敗位置をLambdaへ返す。
            print(s.encode({"operation": "relay", "error_type": type(exc).__name__}))
            failed.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
    return {"batchItemFailures": failed}

