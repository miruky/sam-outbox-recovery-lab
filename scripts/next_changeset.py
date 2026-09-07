"""既存のリソース名を維持し、次のSAM変更セット名だけを生成する。"""

import json
import sys
from pathlib import Path
from prepare import generate


def main():
    run = Path(__file__).resolve().parents[1] / ".run"
    names = json.loads((run / "names.json").read_text())
    item = generate(1)[0]
    ledger_path = run / "resource-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["resources"].append({"logical_label": "ApplicationChangeSet-" + item["name"], "service": "cloudformation", "region": "ap-northeast-1",
        "resource_name": item["name"], "name_core": item["name"], "generated_by": item["generated_by"], "name_field": "ChangeSetName",
        "create_target": "sam deploy", "cleanup_target": "owning application stack", "absence_check": "pending", "final_state": "planned"})
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    plan_path = run / "sam-changesets.json"
    plan = json.loads(plan_path.read_text())
    plan[names["ApplicationStack"]] = {"name": item["name"], "generated_by": item["generated_by"]}
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    print("Prepared a generated change-set name; existing resource names are unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
