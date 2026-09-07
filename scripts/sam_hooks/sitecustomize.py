"""SAM CLI 1.165.0 の変更セット名だけを事前生成値へ置き換える。

SAM CLIには変更セット名を指定する公開オプションがないための互換処理。
テンプレート変換、アップロード、変更セット作成・適用・待機はSAM CLIが行う。
有効化は、このリポジトリの専用デプロイ環境に限る。
"""

import os
import sys
import importlib.util

# SAM自身のPython環境だけに適用し、アプリ操作用のPython環境では読み飛ばす。
if os.environ.get("SAM_RANDOM_NAMES_FILE") and importlib.util.find_spec("samcli"):
    try:
        import inspect
        import json
        import re
        from importlib.metadata import version
        from pathlib import Path
        from samcli.lib.deploy.deployer import Deployer

        if version("aws-sam-cli") != "1.165.0":
            raise RuntimeError("review the naming adapter for this SAM CLI version")
        original = Deployer._create_change_set
        if list(inspect.signature(original).parameters) != ["self", "stack_name", "changeset_type", "kwargs"]:
            raise RuntimeError("SAM CLI method signature changed")
        plan = json.loads(Path(os.environ["SAM_RANDOM_NAMES_FILE"]).read_text())

        def with_generated_name(self, stack_name, changeset_type, **kwargs):
            entry = plan[stack_name]
            if entry["generated_by"] != "aws-resource-name-v1" or not re.fullmatch(r"miruky-[a-z]{16}", entry["name"]):
                raise RuntimeError("missing audited change-set name")
            # 名前以外のSAM CLI引数を変更しない。
            kwargs["ChangeSetName"] = entry["name"]
            return original(self, stack_name, changeset_type, **kwargs)

        Deployer._create_change_set = with_generated_name
    except Exception as exc:
        sys.stderr.write("SAM naming adapter stopped: " + str(exc) + "\n")
        # sitecustomizeの例外だけではPythonが続行するため、デプロイ前に終了する。
        os._exit(78)
