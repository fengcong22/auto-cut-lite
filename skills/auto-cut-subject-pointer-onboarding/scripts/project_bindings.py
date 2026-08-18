from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = 1
BINDINGS_FILENAME = "project-bindings.json"
PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
REQUIRED_BINDING_FIELDS = (
    "project_key",
    "project_path",
    "profile_key",
    "stage_id",
    "subject_id",
    "stage_name",
    "subject_name",
    "bound_by",
    "bound_at",
)
REQUIRED_RECEIPT_TEXT_FIELDS = (
    "registry_root",
    "status",
    "project_key",
    "project_path",
    "profile_key",
    "profile_path",
    "stage_id",
    "subject_id",
    "asset_path",
    "asset_sha256",
    "asset_role",
    "scale_reference_path",
    "scale_reference_sha256",
    "scale_reference_layout",
    "scale_reference_id",
    "approved_preview_path",
    "approved_preview_sha256",
)


def _load_profile_registry() -> ModuleType:
    path = Path(__file__).resolve().with_name("profile_registry.py")
    spec = importlib.util.spec_from_file_location("subject_pointer_profile_registry", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load profile registry: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile_registry = _load_profile_registry()


def _validate_project_key(project_key: str) -> str:
    value = str(project_key or "").strip()
    if PROJECT_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid project_key")
    return value


def _empty_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "bindings": {}}


def _bindings_path(registry_root: str | os.PathLike[str] | None) -> Path:
    root = profile_registry._registry_root(registry_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / BINDINGS_FILENAME
    profile_registry._assert_no_reparse_below(root, path)
    return path


def _validate_binding(project_key: str, value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"binding must be an object: {project_key}")
    binding = dict(value)
    for field in REQUIRED_BINDING_FIELDS:
        if not isinstance(binding.get(field), str) or not binding[field].strip():
            raise ValueError(f"binding field is required: {project_key}.{field}")
    if binding["project_key"] != project_key:
        raise ValueError(f"binding key mismatch: {project_key}")
    _validate_project_key(project_key)
    expected_profile_key = profile_registry.profile_key(binding["stage_id"], binding["subject_id"])
    if binding["profile_key"] != expected_profile_key:
        raise ValueError(f"binding profile mismatch: {project_key}")
    if binding["bound_by"] != "explicit_user_command":
        raise ValueError(f"binding source is not explicit: {project_key}")
    return {field: binding[field] for field in REQUIRED_BINDING_FIELDS}


def _load_store(registry_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    path = _bindings_path(registry_root)
    if not path.exists():
        return _empty_store()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("project binding store must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported project binding schema_version")
    raw_bindings = raw.get("bindings")
    if not isinstance(raw_bindings, Mapping):
        raise ValueError("project binding store bindings must be an object")
    bindings = {
        str(project_key): _validate_binding(str(project_key), binding)
        for project_key, binding in raw_bindings.items()
    }
    return {"schema_version": SCHEMA_VERSION, "bindings": bindings}


def _atomic_write_store(
    registry_root: str | os.PathLike[str] | None, store: Mapping[str, Any]
) -> None:
    path = _bindings_path(registry_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".project-bindings-", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(store, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        profile_registry._assert_no_reparse_below(path.parent, path)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _resolve_project_path(project_path: str | Path, project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve(strict=True)
    project = Path(project_path).expanduser().resolve(strict=True)
    if not root.is_dir() or not project.is_dir():
        raise ValueError("project root and project path must be directories")
    if os.path.commonpath((str(root), str(project))) != str(root):
        raise ValueError("project path must stay inside project root")
    return project


def bind_project(
    *,
    project_key: str,
    project_path: str | Path,
    project_root: str | Path,
    stage_id: str,
    subject_id: str,
    registry_root: str | os.PathLike[str] | None = None,
    allow_rebind: bool = False,
    bound_at: str | None = None,
) -> dict[str, str]:
    """Bind one project family to one exact ready profile."""

    clean_project_key = _validate_project_key(project_key)
    resolved_project = _resolve_project_path(project_path, project_root)
    checked = profile_registry.check_profile(stage_id, subject_id, registry_root)
    if checked["status"] != "ready":
        raise ValueError(f"profile is not ready: {checked['status']}")
    profile = checked.get("profile")
    if not isinstance(profile, Mapping):
        raise ValueError("ready profile payload is missing")

    store = _load_store(registry_root)
    existing = store["bindings"].get(clean_project_key)
    if existing is not None and existing["profile_key"] != checked["key"] and not allow_rebind:
        raise ValueError("explicit rebind required")

    binding = {
        "project_key": clean_project_key,
        "project_path": str(resolved_project),
        "profile_key": checked["key"],
        "stage_id": stage_id,
        "subject_id": subject_id,
        "stage_name": str(profile["stage_name"]),
        "subject_name": str(profile["subject_name"]),
        "bound_by": "explicit_user_command",
        "bound_at": bound_at or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    store["bindings"][clean_project_key] = binding
    _atomic_write_store(registry_root, store)
    return dict(binding)


def get_project_binding(
    project_key: str, registry_root: str | os.PathLike[str] | None = None
) -> dict[str, str] | None:
    clean_project_key = _validate_project_key(project_key)
    binding = _load_store(registry_root)["bindings"].get(clean_project_key)
    return dict(binding) if binding is not None else None


def list_project_bindings(
    registry_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    bindings = _load_store(registry_root)["bindings"]
    return [dict(bindings[key]) for key in sorted(bindings, key=str.casefold)]


def _resolved_path_candidates(
    raw_path: Any, *, registry_root: Path, profile_directory: Path
) -> set[str]:
    value = str(raw_path or "").strip()
    if not value:
        return set()
    path = Path(value).expanduser()
    candidates = [path] if path.is_absolute() else [registry_root / path, profile_directory / path]
    return {os.path.normcase(str(candidate.resolve())) for candidate in candidates}


def _paths_match(
    raw_path: Any,
    expected_path: Path,
    *,
    registry_root: Path,
    profile_directory: Path,
) -> bool:
    expected = os.path.normcase(str(expected_path.resolve()))
    return expected in _resolved_path_candidates(
        raw_path,
        registry_root=registry_root,
        profile_directory=profile_directory,
    )


def _profile_evidence_entry(
    profile: Mapping[str, Any],
    field: str,
    receipt_path: Any,
    *,
    registry_root: Path,
    profile_directory: Path,
) -> Mapping[str, Any] | None:
    entries = profile.get(field)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        stored_path = str(entry.get("path") or "").strip()
        if not stored_path:
            continue
        expected_path = profile_directory / stored_path
        if _paths_match(
            receipt_path,
            expected_path,
            registry_root=registry_root,
            profile_directory=profile_directory,
        ):
            return entry
    return None


def _number_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        pair = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return pair if all(math.isfinite(item) for item in pair) else None


def _numbers_match(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(left_number)
        and math.isfinite(right_number)
        and abs(left_number - right_number) <= tolerance
    )


def _selected_placement_policies(
    profile: Mapping[str, Any], *, asset_role: Any, layout: Any
) -> list[dict[str, Any]]:
    policies = profile.get("placement_policies")
    if not isinstance(policies, list):
        return []
    return [
        dict(policy)
        for policy in policies
        if isinstance(policy, Mapping)
        and policy.get("asset_role") == asset_role
        and policy.get("layout") == layout
    ]


def _placement_policy_target_kinds(profile: Mapping[str, Any], *, asset_role: Any) -> list[str]:
    policies = profile.get("placement_policies")
    if not isinstance(policies, list):
        return []
    return sorted(
        {
            str(policy["target_kind"])
            for policy in policies
            if isinstance(policy, Mapping)
            and policy.get("asset_role") == asset_role
            and isinstance(policy.get("target_kind"), str)
            and bool(str(policy["target_kind"]).strip())
        }
    )


def validate_pointer_receipt(
    receipt: Mapping[str, Any],
    registry_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Fresh-check one binding-backed pointer receipt against current files."""

    if not isinstance(receipt, Mapping):
        return {"ok": False, "status": "invalid", "problems": ["receipt_not_object"]}
    payload = dict(receipt)
    problems = [
        f"missing_receipt_field:{field}"
        for field in REQUIRED_RECEIPT_TEXT_FIELDS
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip()
    ]
    if payload.get("status") != "ready":
        problems.append("receipt_status_not_ready")
    if _number_pair(payload.get("anchor")) is None:
        problems.append("missing_or_invalid_receipt_anchor")
    for field in ("visible_width_ratio", "visible_height_ratio"):
        if not _numbers_match(payload.get(field), payload.get(field)):
            problems.append(f"missing_or_invalid_receipt_field:{field}")
    if payload.get("approved_preview_approved") is not True:
        problems.append("receipt_preview_not_approved")
    root_value = registry_root if registry_root is not None else payload.get("registry_root")
    if not isinstance(root_value, (str, os.PathLike)):
        problems.append("missing_or_invalid_receipt_field:registry_root")
    if problems:
        return {"ok": False, "status": "invalid", "problems": sorted(set(problems))}

    root = profile_registry._registry_root(root_value)
    receipt_root = profile_registry._registry_root(payload["registry_root"])
    if os.path.normcase(str(receipt_root)) != os.path.normcase(str(root)):
        return {
            "ok": False,
            "status": "invalid",
            "problems": ["receipt_registry_root_mismatch"],
        }

    project_key = _validate_project_key(str(payload["project_key"]))
    binding = get_project_binding(project_key, root)
    if binding is None:
        return {
            "ok": False,
            "status": "unbound",
            "project_key": project_key,
            "problems": ["project_binding_missing"],
        }

    binding_fields = ("project_key", "profile_key", "stage_id", "subject_id")
    for field in binding_fields:
        if payload.get(field) != binding.get(field):
            problems.append(f"receipt_{field}_mismatch")
    if os.path.normcase(os.path.abspath(str(payload["project_path"]))) != os.path.normcase(
        os.path.abspath(binding["project_path"])
    ):
        problems.append("receipt_project_path_mismatch")

    checked = profile_registry.check_profile(binding["stage_id"], binding["subject_id"], root)
    if checked.get("status") != "ready":
        return {
            "ok": False,
            "status": str(checked.get("status") or "invalid"),
            "project_key": project_key,
            "binding": binding,
            "profile_key": checked.get("key"),
            "profile_path": checked.get("profile_path"),
            "problems": sorted(
                set(
                    [*problems, f"profile_status:{checked.get('status')}"]
                    + [str(item) for item in checked.get("problems") or []]
                )
            ),
        }

    profile = checked.get("profile")
    if not isinstance(profile, Mapping):
        problems.append("ready_profile_payload_missing")
        return {
            "ok": False,
            "status": "invalid",
            "project_key": project_key,
            "binding": binding,
            "problems": sorted(set(problems)),
        }

    profile_path = Path(str(checked["profile_path"]))
    profile_directory = profile_path.parent
    if payload.get("profile_key") != checked.get("key"):
        problems.append("receipt_profile_key_mismatch")
    if not _paths_match(
        payload.get("profile_path"),
        profile_path,
        registry_root=root,
        profile_directory=profile_directory,
    ):
        problems.append("receipt_profile_path_mismatch")

    asset = _profile_evidence_entry(
        profile,
        "assets",
        payload.get("asset_path"),
        registry_root=root,
        profile_directory=profile_directory,
    )
    if asset is None:
        problems.append("receipt_asset_path_mismatch")
    else:
        if payload.get("asset_sha256") != asset.get("sha256"):
            problems.append("receipt_asset_sha256_mismatch")
        if payload.get("asset_role") != asset.get("role"):
            problems.append("receipt_asset_role_mismatch")
        if _number_pair(payload.get("anchor")) != _number_pair(asset.get("anchor")):
            problems.append("receipt_anchor_mismatch")
        media_contract = asset.get("media_contract")
        if media_contract is not None and payload.get("media_contract") != media_contract:
            problems.append("receipt_media_contract_mismatch")

    scale_reference = _profile_evidence_entry(
        profile,
        "scale_references",
        payload.get("scale_reference_path"),
        registry_root=root,
        profile_directory=profile_directory,
    )
    if scale_reference is None:
        problems.append("receipt_scale_reference_path_mismatch")
    else:
        scale_fields = {
            "scale_reference_sha256": "sha256",
            "scale_reference_layout": "layout",
            "scale_reference_id": "reference_id",
        }
        for receipt_field, profile_field in scale_fields.items():
            if payload.get(receipt_field) != scale_reference.get(profile_field):
                problems.append(f"receipt_{receipt_field}_mismatch")
        for field in ("visible_width_ratio", "visible_height_ratio"):
            if not _numbers_match(payload.get(field), scale_reference.get(field)):
                problems.append(f"receipt_{field}_mismatch")

    selected_policies = _selected_placement_policies(
        profile,
        asset_role=payload.get("asset_role"),
        layout=payload.get("scale_reference_layout"),
    )
    if payload.get("placement_policies") != selected_policies:
        problems.append("receipt_placement_policies_mismatch")
    placement_policy_target_kinds = _placement_policy_target_kinds(
        profile, asset_role=payload.get("asset_role")
    )
    if payload.get("placement_policy_target_kinds") != placement_policy_target_kinds:
        problems.append("receipt_placement_policy_target_kinds_mismatch")
    approved_preview = _profile_evidence_entry(
        profile,
        "approved_previews",
        payload.get("approved_preview_path"),
        registry_root=root,
        profile_directory=profile_directory,
    )
    if approved_preview is None:
        problems.append("receipt_approved_preview_path_mismatch")
    else:
        if payload.get("approved_preview_sha256") != approved_preview.get("sha256"):
            problems.append("receipt_approved_preview_sha256_mismatch")
        if approved_preview.get("approved") is not True:
            problems.append("profile_preview_not_approved")

    return {
        "ok": not problems,
        "status": "ready" if not problems else "invalid",
        "project_key": project_key,
        "binding": binding,
        "profile_key": checked.get("key"),
        "profile_path": checked.get("profile_path"),
        "media_contract": asset.get("media_contract") if asset is not None else None,
        "placement_policies": selected_policies,
        "placement_policy_target_kinds": placement_policy_target_kinds,
        "problems": sorted(set(problems)),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage manual subject-profile bindings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bind_parser = subparsers.add_parser("bind", help="Bind one project family.")
    bind_parser.add_argument("--project-key", required=True)
    bind_parser.add_argument("--project-path", required=True)
    bind_parser.add_argument("--project-root", required=True)
    bind_parser.add_argument("--stage-id", required=True)
    bind_parser.add_argument("--subject-id", required=True)
    bind_parser.add_argument("--rebind", action="store_true")

    get_parser = subparsers.add_parser("get", help="Get one project-family binding.")
    get_parser.add_argument("--project-key", required=True)
    list_parser = subparsers.add_parser("list", help="List project-family bindings.")
    for command_parser in (bind_parser, get_parser, list_parser):
        command_parser.add_argument("--root", help="Subject profile registry root.")
        command_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, list):
        for binding in payload:
            print(f"{binding['project_key']}: {binding['profile_key']}")
        return
    if isinstance(payload, Mapping):
        if payload.get("status") == "unbound":
            print(f"{payload['project_key']}: unbound")
        elif "project_key" in payload and "profile_key" in payload:
            print(f"{payload['project_key']}: {payload['profile_key']}")
        else:
            print(payload)
        return
    print(payload)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "bind":
            payload = bind_project(
                project_key=args.project_key,
                project_path=args.project_path,
                project_root=args.project_root,
                stage_id=args.stage_id,
                subject_id=args.subject_id,
                registry_root=args.root,
                allow_rebind=args.rebind,
            )
            exit_code = 0
        elif args.command == "get":
            payload = get_project_binding(args.project_key, args.root)
            if payload is None:
                payload = {"status": "unbound", "project_key": args.project_key}
                exit_code = 1
            else:
                exit_code = 0
        else:
            payload = list_project_bindings(args.root)
            exit_code = 0
        _emit(payload, as_json=args.as_json)
        return exit_code
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        _emit({"status": "error", "error": str(error)}, as_json=args.as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
