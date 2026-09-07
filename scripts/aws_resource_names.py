#!/usr/bin/env python3
"""Generate and audit clock-independent AWS resource-name cores."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import string
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_RANDOM_LENGTH = 16
MIN_RANDOM_LENGTH = 12
MAX_RANDOM_LENGTH = 56
GENERATOR_ID = "aws-resource-name-v1"
FORBIDDEN_SUBSTRINGS = ("qiita", "zenn")
DATE_OR_TIME_PATTERNS = (
    re.compile(r"(?:19|20)\d{2}[-_.]?(?:0[1-9]|1[0-2])[-_.]?(?:0[1-9]|[12]\d|3[01])"),
    re.compile(r"(?<!\d)\d{8}(?!\d)"),
    re.compile(r"(?<!\d)\d{10,13}(?!\d)"),
    re.compile(r"(?:timestamp|datetime|epoch)", re.IGNORECASE),
)


@dataclass(frozen=True)
class NameRecord:
    name: str
    prefix: str
    separator: str
    random_length: int
    alphabet: str = "lowercase-ascii"
    generated_by: str = GENERATOR_ID


def _random_letters(length: int) -> str:
    while True:
        value = "".join(secrets.choice(string.ascii_lowercase) for _ in range(length))
        if not any(term in value for term in FORBIDDEN_SUBSTRINGS):
            return value


def generate_name(*, prefix: str, compact: bool, random_length: int) -> NameRecord:
    if not MIN_RANDOM_LENGTH <= random_length <= MAX_RANDOM_LENGTH:
        raise ValueError(
            f"random length must be between {MIN_RANDOM_LENGTH} and {MAX_RANDOM_LENGTH}"
        )
    if prefix not in {"miruky", "none"}:
        raise ValueError("prefix must be 'miruky' or 'none'")

    random_part = _random_letters(random_length)
    if prefix == "none":
        return NameRecord(
            name=random_part,
            prefix="",
            separator="",
            random_length=random_length,
        )

    separator = "" if compact else "-"
    return NameRecord(
        name=f"miruky{separator}{random_part}",
        prefix="miruky",
        separator=separator,
        random_length=random_length,
    )


def validate_name(name: str) -> list[str]:
    errors: list[str] = []
    lowered = name.lower()

    for term in FORBIDDEN_SUBSTRINGS:
        if term in lowered:
            errors.append(f"contains prohibited platform term: {term}")

    if any(pattern.search(name) for pattern in DATE_OR_TIME_PATTERNS):
        errors.append("contains a date-, time-, epoch-, or timestamp-shaped fragment")

    random_part = ""
    if re.fullmatch(r"miruky-[a-z]+", name):
        random_part = name.removeprefix("miruky-")
    elif re.fullmatch(r"miruky[a-z]+", name):
        random_part = name.removeprefix("miruky")
    elif re.fullmatch(r"[a-z]+", name):
        random_part = name
    else:
        errors.append(
            "must be lowercase random letters with only the optional prefix 'miruky'"
        )

    if random_part and not MIN_RANDOM_LENGTH <= len(random_part) <= MAX_RANDOM_LENGTH:
        errors.append(
            f"random part must contain {MIN_RANDOM_LENGTH}-{MAX_RANDOM_LENGTH} lowercase letters"
        )

    if random_part and any(term in random_part for term in FORBIDDEN_SUBSTRINGS):
        errors.append("random part contains a prohibited platform term")

    return list(dict.fromkeys(errors))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or audit random AWS resource-name cores without dates or platform names."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="generate compliant names")
    generate_parser.add_argument("--count", type=int, default=1)
    generate_parser.add_argument(
        "--prefix", choices=("miruky", "none"), default="miruky"
    )
    generate_parser.add_argument("--compact", action="store_true")
    generate_parser.add_argument(
        "--random-length", type=int, default=DEFAULT_RANDOM_LENGTH
    )
    generate_parser.add_argument("--json", action="store_true")

    audit_parser = subparsers.add_parser("audit", help="audit one or more name cores")
    audit_parser.add_argument("names", nargs="+")

    ledger_parser = subparsers.add_parser(
        "audit-ledger", help="audit every resource-name record in a JSON ledger"
    )
    ledger_parser.add_argument("ledger", type=Path)
    ledger_parser.add_argument(
        "--final",
        action="store_true",
        help="also require a verified absent final state for every attempted name",
    )

    return parser


def _run_generate(args: argparse.Namespace) -> int:
    if not 1 <= args.count <= 1000:
        print("ERROR: count must be between 1 and 1000", file=sys.stderr)
        return 2

    try:
        records = [
            generate_name(
                prefix=args.prefix,
                compact=args.compact,
                random_length=args.random_length,
            )
            for _ in range(args.count)
        ]
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    names = [record.name for record in records]
    if len(names) != len(set(names)):
        print("ERROR: generator produced a duplicate name", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "naming_policy": GENERATOR_ID,
                    "names": [asdict(record) for record in records],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("\n".join(names))
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    failed = False
    for name in args.names:
        errors = validate_name(name)
        if errors:
            failed = True
            print(f"FAIL {name}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"PASS {name}")
    return 1 if failed else 0


def _run_audit_ledger(args: argparse.Namespace) -> int:
    findings: list[str] = []
    try:
        payload = json.loads(args.ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL {args.ledger}")
        print(f"  - cannot read a valid JSON ledger: {exc}")
        return 1

    if not isinstance(payload, dict):
        findings.append("ledger root must be a JSON object")
        resources: list[object] = []
        aws_generated_identifiers: list[object] = []
    else:
        if payload.get("schema_version") != 1:
            findings.append("schema_version must be 1")
        if payload.get("inventory_complete") != "PASS":
            findings.append("inventory_complete must be PASS")
        if payload.get("name_field_inventory_complete") != "PASS":
            findings.append("name_field_inventory_complete must be PASS")
        resources_value = payload.get("resources")
        if not isinstance(resources_value, list):
            findings.append("resources must be an array")
            resources = []
        else:
            resources = resources_value
        generated_value = payload.get("aws_generated_identifiers")
        if not isinstance(generated_value, list):
            findings.append("aws_generated_identifiers must be an array")
            aws_generated_identifiers = []
        else:
            aws_generated_identifiers = generated_value
        if not resources and not aws_generated_identifiers:
            findings.append(
                "resources and aws_generated_identifiers cannot both be empty"
            )

    seen_names: set[str] = set()
    seen_cores: set[str] = set()
    for index, resource in enumerate(resources, start=1):
        location = f"resources[{index}]"
        if not isinstance(resource, dict):
            findings.append(f"{location} must be an object")
            continue

        required_strings = (
            "logical_label",
            "service",
            "region",
            "resource_name",
            "name_core",
            "generated_by",
            "create_target",
            "cleanup_target",
            "absence_check",
        )
        for key in required_strings:
            if not isinstance(resource.get(key), str) or not resource[key].strip():
                findings.append(f"{location}.{key} must be a nonempty string")

        core = resource.get("name_core")
        full_name = resource.get("resource_name")
        if isinstance(core, str) and core:
            for error in validate_name(core):
                findings.append(f"{location}.name_core {error}")
            if core in seen_cores:
                findings.append(f"{location}.name_core duplicates another ledger entry")
            seen_cores.add(core)
        if isinstance(full_name, str) and isinstance(core, str) and full_name and core:
            lowered_full_name = full_name.lower()
            for term in FORBIDDEN_SUBSTRINGS:
                if term in lowered_full_name:
                    findings.append(
                        f"{location}.resource_name contains prohibited platform term: {term}"
                    )
            if any(pattern.search(full_name) for pattern in DATE_OR_TIME_PATTERNS):
                findings.append(
                    f"{location}.resource_name contains a date-, time-, epoch-, or timestamp-shaped fragment"
                )
            required_prefix = resource.get("required_prefix", "")
            required_suffix = resource.get("required_suffix", "")
            wrapper_justification = resource.get("wrapper_justification", "")
            wrapper_fields_valid = True
            for key, value in (
                ("required_prefix", required_prefix),
                ("required_suffix", required_suffix),
                ("wrapper_justification", wrapper_justification),
            ):
                if not isinstance(value, str):
                    findings.append(f"{location}.{key} must be a string when present")
                    wrapper_fields_valid = False

            if wrapper_fields_valid:
                has_wrapper = bool(required_prefix or required_suffix)
                if full_name == core:
                    if has_wrapper or wrapper_justification:
                        findings.append(
                            f"{location} must leave wrapper fields empty when resource_name equals name_core"
                        )
                else:
                    if not has_wrapper:
                        findings.append(
                            f"{location} must record every service-required prefix or suffix"
                        )
                    if not wrapper_justification.strip():
                        findings.append(
                            f"{location}.wrapper_justification must cite the service requirement"
                        )
                    if full_name != f"{required_prefix}{core}{required_suffix}":
                        findings.append(
                            f"{location}.resource_name must equal required_prefix + name_core + required_suffix"
                        )
            if full_name in seen_names:
                findings.append(f"{location}.resource_name duplicates another ledger entry")
            seen_names.add(full_name)

        if resource.get("generated_by") != GENERATOR_ID:
            findings.append(f"{location}.generated_by must be {GENERATOR_ID}")

        if args.final and resource.get("final_state") not in {
            "deleted-and-absent",
            "not-created-and-absent",
        }:
            findings.append(
                f"{location}.final_state must prove deleted-and-absent or not-created-and-absent"
            )

    for index, item in enumerate(aws_generated_identifiers, start=1):
        location = f"aws_generated_identifiers[{index}]"
        if not isinstance(item, dict):
            findings.append(f"{location} must be an object")
            continue
        for key in ("logical_label", "service", "region", "identifier_kind"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                findings.append(f"{location}.{key} must be a nonempty string")
        if item.get("exemption_reason") != "no-supported-user-name-field":
            findings.append(
                f"{location}.exemption_reason must be no-supported-user-name-field"
            )

    if findings:
        print(f"FAIL {args.ledger}")
        for finding in findings:
            print(f"  - {finding}")
        return 1

    print(
        f"PASS {args.ledger} ({len(resources)} names, "
        f"{len(aws_generated_identifiers)} AWS-generated identifiers)"
    )
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "generate":
        return _run_generate(args)
    if args.command == "audit":
        return _run_audit(args)
    return _run_audit_ledger(args)


if __name__ == "__main__":
    raise SystemExit(main())
