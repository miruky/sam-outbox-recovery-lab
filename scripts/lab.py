"""AWS CLIを呼び出す操作補助。実環境の値は.run配下だけに保存する。"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".run"


def aws(*args):
    command = ["aws", *args, "--region", "ap-northeast-1", "--no-cli-pager", "--output", "json"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        message = re.sub(r"arn:[^\s\"']+|\b[0-9]{12}\b", "[AWS identifier]", result.stderr)
        raise RuntimeError(message.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def outputs():
    path = RUN / "outputs.json"
    if not path.exists():
        names = json.loads((RUN / "names.json").read_text())
        result = aws("cloudformation", "describe-stacks", "--stack-name", names["ApplicationStack"])
        path.write_text(json.dumps({x["OutputKey"]: x["OutputValue"] for x in result["Stacks"][0]["Outputs"]}))
    return json.loads(path.read_text())


def invoke(action, data=None):
    request = RUN / "invocation.json"
    response = RUN / "response.json"
    request.write_text(json.dumps({"action": action, "data": data or {}}))
    metadata = aws("lambda", "invoke", "--function-name", outputs()["ControllerFunction"],
                   "--payload", "fileb://"+str(request), str(response))
    value = json.loads(response.read_text())
    if metadata.get("FunctionError"):
        raise RuntimeError(value.get("errorMessage", "controller invocation failed"))
    return value


def api(path, method="GET", payload=None):
    # 認証値をファイルや出力へ保存せず、署名を付けたAPI要求だけを送信する。
    value = aws("configure", "export-credentials", "--format", "process")
    credentials = Credentials(value["AccessKeyId"], value["SecretAccessKey"], value.get("SessionToken"))
    body = json.dumps(payload).encode() if payload is not None else None
    url = outputs()["ApiEndpoint"] + path
    request = AWSRequest(method=method, url=url, data=body, headers={"content-type": "application/json"})
    SigV4Auth(credentials, "execute-api", "ap-northeast-1").add_auth(request)
    try:
        with urlopen(Request(url, data=body, headers=dict(request.headers), method=method), timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def generated_record(label, service):
    from prepare import generate
    record = generate(1)[0]
    generator = os.environ.get("AWS_RESOURCE_NAME_GENERATOR", str(Path(__file__).with_name("aws_resource_names.py")))
    subprocess.run([sys.executable, generator, "audit", record["name"]], check=True, capture_output=True)
    ledger_path = RUN / "resource-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["resources"].append({"logical_label": label, "service": service, "region": "ap-northeast-1", "resource_name": record["name"], "name_core": record["name"], "generated_by": record["generated_by"], "name_field": "ReplayName" if service == "events" else "name", "create_target": "AWS CLI / workflow", "cleanup_target": "stop activity and remove owning stack", "absence_check": "pending", "final_state": "planned"})
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2)+"\n")
    return record["name"]


def start_rebuild():
    # 再生と実行の名前を別々に生成してから、AWSへ要求を送る。
    jid = uuid.uuid4().hex
    replay = generated_record("Replay-"+jid, "events")
    execution = generated_record("Execution-"+jid, "states")
    payload = {"job_id": jid, "replay_name": replay}
    result = aws("stepfunctions", "start-execution", "--state-machine-arn", outputs()["RebuildStateMachine"], "--name", execution, "--input", json.dumps(payload))
    (RUN / "rebuild.json").write_text(json.dumps({**payload, "execution_name": execution, "execution_arn": result["executionArn"]}))
    return {"job_id": jid, "state": "STARTED"}


def execution_status():
    current = json.loads((RUN / "rebuild.json").read_text())
    result = aws("stepfunctions", "describe-execution", "--execution-arn", current["execution_arn"])
    output = json.loads(result["output"]) if result.get("output") else None
    return {"job_id": current["job_id"], "state": result["status"], "output": output}


def duplicate(number, times):
    from hashlib import sha256
    eid = sha256(("OrderCreated/v1/"+f"order-{number:03d}").encode()).hexdigest()
    item = aws("dynamodb", "get-item", "--table-name", outputs()["OrdersTable"], "--consistent-read", "--key", json.dumps({"pk":{"S":"OUTBOX"},"sk":{"S":eid}}))
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer().deserialize(item["Item"]["detail"])
    d["amount"], d["schema_version"] = int(d["amount"]), int(d["schema_version"])
    request = [{"EventBusName":outputs()["OrderBus"],"Source":"lab.orders","DetailType":"OrderCreated","Detail":json.dumps(d),"Time":d["occurred_at"]}]
    for _ in range(times):
        result = aws("events", "put-events", "--entries", json.dumps(request))
        if result.get("FailedEntryCount"):
            raise RuntimeError("PutEvents reported an item failure")
    return {"order_id":d["order_id"],"extra_deliveries":times}


def queues():
    result = {}
    for name in ["AggregateQueue","NotificationQueue","RebuildQueue","AggregateDLQ","NotificationDLQ","RebuildDLQ","DeliveryDLQ","RelayDLQ"]:
        attrs = aws("sqs","get-queue-attributes","--queue-url",outputs()[name],"--attribute-names","ApproximateNumberOfMessages","ApproximateNumberOfMessagesNotVisible")["Attributes"]
        result[name] = {"visible":int(attrs["ApproximateNumberOfMessages"]), "in_flight":int(attrs["ApproximateNumberOfMessagesNotVisible"])}
    return result


def console_links():
    names = json.loads((RUN / "names.json").read_text())
    region = "https://ap-northeast-1.console.aws.amazon.com/"
    links = [
        ("本体スタック", region + "cloudformation/home?region=ap-northeast-1#/stacks?filteringText=" + names["ApplicationStack"]),
        ("注文テーブルのストリーム", region + "dynamodbv2/home?region=ap-northeast-1#table?name=" + names["OrdersTable"] + "&tab=streams"),
        ("通知用ルール", region + "events/home?region=ap-northeast-1#/eventbus/" + names["OrderBus"] + "/rules/" + names["NotificationRule"]),
        ("キュー一覧", region + "sqs/v3/home?region=ap-northeast-1#/queues"),
    ]
    current = RUN / "rebuild.json"
    if current.exists():
        execution = json.loads(current.read_text())["execution_arn"]
        links.append(("再構築の実行", region + "states/home?region=ap-northeast-1#/v2/executions/details/" + quote(execution, safe=":")))
    target = RUN / "console-links.md"
    target.write_text("# この環境の確認先\n\n" + "\n".join(f"- [{label}]({url})" for label, url in links) +
        "\n\n集計用DLQの名前: `" + names["AggregateDLQ"] + "`\n")
    return {"saved": ".run/console-links.md"}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["initialize","status","resume","rebuild","execution","read-api","console-links"]:
        sub.add_parser(command)
    sub.add_parser("queues").add_argument("--summary", action="store_true")
    seed = sub.add_parser("orders");seed.add_argument("--start",type=int,default=1);seed.add_argument("--count",type=int,default=12)
    fault = sub.add_parser("fault");fault.add_argument("state",choices=["on","off"])
    dup = sub.add_parser("duplicate");dup.add_argument("--order",type=int,default=1);dup.add_argument("--times",type=int,default=3)
    recover = sub.add_parser("recover-outbox");recover.add_argument("--limit",type=int,default=100)
    args = parser.parse_args()
    if args.command in {"initialize","status","resume"}:
        value = invoke(args.command)
    elif args.command == "fault":value = invoke("fault",{"enabled":args.state=="on"})
    elif args.command == "recover-outbox":value = invoke("recover-outbox",{"limit":args.limit})
    elif args.command == "orders":
        if not 1 <= args.count <= 1000 or not 1 <= args.start <= 1000 or args.start+args.count > 1001:
            raise ValueError("orders must be between 1 and 1000")
        results = []
        for number in range(args.start,args.start+args.count):
            code, body = api("/orders","POST",{"order_id":f"order-{number:03d}","amount":number*100})
            if code not in {200,201}:
                raise RuntimeError(json.dumps({"http_status":code,**body}))
            results.append(code)
            time.sleep(0.4)
        value = {"submitted":len(results),"created":results.count(201),"already_present":results.count(200)}
    elif args.command == "duplicate":
        if not 1 <= args.times <= 10:raise ValueError("times must be 1 to 10")
        value = duplicate(args.order,args.times)
    elif args.command == "queues":
        value = queues()
        if args.summary:
            for name, counts in value.items():
                print(f"{name:<18} visible={counts['visible']} in_flight={counts['in_flight']}")
            return 0
    elif args.command == "rebuild":value = start_rebuild()
    elif args.command == "execution":value = execution_status()
    elif args.command == "console-links":value = console_links()
    else:
        code, value = api("/aggregate")
        if code != 200:raise RuntimeError("aggregate API failed")
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def entrypoint():
    try:
        return main()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
