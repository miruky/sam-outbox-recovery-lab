"""SAM CLIの設定と、公開しないリソース名台帳を作る。"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".run"


def generate(count):
    generator = Path(os.environ.get("AWS_RESOURCE_NAME_GENERATOR", Path(__file__).with_name("aws_resource_names.py")))
    output = subprocess.check_output([sys.executable, str(generator), "generate", "--count", str(count), "--json"], text=True)
    return json.loads(output)["names"]


def main():
    if RUN.exists():
        raise SystemExit(".run already exists; reuse its ledger instead of replacing resource names")
    RUN.mkdir(mode=0o700)
    fields = json.loads(Path(__file__).with_name("name-fields.json").read_text())
    extra = [{"logical": key, "service": svc, "field": field, "parameter": key + "Name"} for key, svc, field in [
        ("BootstrapStack", "cloudformation", "StackName"), ("ApplicationStack", "cloudformation", "StackName"),
        ("BootstrapChangeSet", "cloudformation", "ChangeSetName"), ("ApplicationChangeSet", "cloudformation", "ChangeSetName"),
        ("ArtifactBucket", "s3", "BucketName"),
    ]]
    resources = []
    values = {}
    for field, generated in zip(fields + extra, generate(len(fields) + len(extra))):
        values[field["logical"]] = generated["name"]
        resources.append({"logical_label": field["logical"], "service": field["service"], "region": "ap-northeast-1",
                          "resource_name": generated["name"], "name_core": generated["name"], "generated_by": generated["generated_by"],
                          "name_field": field["field"], "create_target": "SAM CLI deployment", "cleanup_target": "exact name recorded here",
                          "absence_check": "pending", "final_state": "planned"})
    unnamed = [("QueuePolicy", "sqs", "queue attribute"), ("ArtifactBucketPolicy", "s3", "bucket policy"),
               ("EventSourceMappings", "lambda", "UUID"), ("HttpApiRoutesAndIntegrations", "apigateway", "route and integration IDs"),
               ("InvokePermissions", "lambda", "CloudFormation generated policy statement IDs"),
               ("ArchiveManagedRule", "events", "archive service-managed rule"), ("FunctionLogStreams", "logs", "Lambda service-generated log streams")]
    ledger = {"schema_version": 1, "inventory_complete": "PASS", "name_field_inventory_complete": "PASS", "resources": resources,
              "aws_generated_identifiers": [{"logical_label": label, "service": svc, "region": "ap-northeast-1", "identifier_kind": kind,
                  "exemption_reason": "no-supported-user-name-field"} for label, svc, kind in unnamed]}
    (RUN / "resource-ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    (RUN / "names.json").write_text(json.dumps(values, indent=2) + "\n")
    plan = {values[stem+"Stack"]: {"name": values[stem+"ChangeSet"], "generated_by": "aws-resource-name-v1"} for stem in ["Bootstrap", "Application"]}
    (RUN / "sam-changesets.json").write_text(json.dumps(plan, indent=2) + "\n")
    env = '# このシェルだけにSAMの命名補助を設定します。\nexport SAM_RANDOM_NAMES_FILE="$PWD/.run/sam-changesets.json"\nexport PYTHONPATH="$PWD/scripts/sam_hooks${PYTHONPATH:+:$PYTHONPATH}"\nexport SAM_CLI_TELEMETRY=0\n'
    (RUN / "deploy-env.sh").write_text(env)
    config = ['version = 0.1']
    for section, template, stack, params in [
        ("bootstrap", "bootstrap.yaml", values["BootstrapStack"], ["ArtifactBucketName="+values["ArtifactBucket"]]),
        ("application", ".aws-sam/build/template.yaml", values["ApplicationStack"], [f['parameter']+"="+values[f['logical']] for f in fields]),
    ]:
        config += [f"[{section}.deploy.parameters]", 'template_file = '+json.dumps(template), 'stack_name = '+json.dumps(stack),
                   'region = "ap-northeast-1"', 'confirm_changeset = false', 'capabilities = "CAPABILITY_NAMED_IAM"',
                   'parameter_overrides = '+json.dumps(" ".join(params))]
        if section == "application":
            config += ['s3_bucket = '+json.dumps(values["ArtifactBucket"]), 'resolve_s3 = false']
    (RUN / "samconfig.toml").write_text("\n".join(config) + "\n")
    print(json.dumps({"region": "ap-northeast-1", "named_objects": len(resources), "configuration": ".run/samconfig.toml"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
