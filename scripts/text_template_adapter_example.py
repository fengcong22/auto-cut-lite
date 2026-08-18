import argparse
import csv
import json
import os
import sys
from typing import Dict


def _skill_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir() -> str:
    return os.path.join(_skill_root(), "data")


def _fixture_dir() -> str:
    return os.path.join(_skill_root(), "tests", "fixtures")


def _load_index() -> Dict[str, Dict[str, str]]:
    mapping: Dict[str, Dict[str, str]] = {}
    path = os.path.join(_data_dir(), "text_templates.csv")
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            resource_id = str(row.get("resource_id", "")).strip()
            if resource_id:
                mapping[resource_id] = row
    return mapping


def _fixture_path_for_resource(resource_id: str) -> str:
    fixture_map = {
        "7311954538750151999": "text_template_sample.json",
        "7311954538750152000": "text_template_multi_slot.json",
    }
    fixture_name = fixture_map.get(resource_id)
    if not fixture_name:
        raise FileNotFoundError(
            f"No example payload fixture mapped for template resource_id={resource_id}"
        )
    return os.path.join(_fixture_dir(), fixture_name)


def _load_payload_for_template(resource_id: str) -> Dict[str, object]:
    index = _load_index()
    if resource_id not in index:
        raise FileNotFoundError(f"Unknown template resource_id: {resource_id}")
    path = _fixture_path_for_resource(resource_id)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Example JY_TEXT_TEMPLATE_ADAPTER implementation")
    parser.add_argument(
        "-r", "--resource-id", required=True, help="Official text template resource id"
    )
    parser.add_argument(
        "-t",
        "--texts",
        required=False,
        default="[]",
        help="JSON array of replacement texts. Accepted for API compatibility; payload expansion is returned as-is.",
    )
    args = parser.parse_args(argv)

    try:
        json.loads(args.texts)
    except json.JSONDecodeError as exc:
        print(f"Invalid --texts payload: {exc}", file=sys.stderr)
        return 2

    try:
        payload = _load_payload_for_template(str(args.resource_id))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    json.dump(payload, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
