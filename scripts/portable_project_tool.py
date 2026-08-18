from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from portable.build_importer import build_importer
from portable_project.errors import PortableProjectError
from portable_project.gui import importer_self_check, run_importer_gui
from portable_project.importer import import_project
from portable_project.manifest import read_manifest
from portable_project.packager import package_project
from portable_project.provenance import versions_equal
from portable_project.validator import validate_package
from portable_project.version_policy import SUPPORTED_APP_IDENTITIES
from utils.jianying_env import detect_jianying_environment, infer_app_name_from_path

EnvironmentDetector = Callable[..., Mapping[str, Any]]


class ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PortableProjectError("invalid_arguments", "Invalid command arguments")


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="as_json")


def build_parser() -> argparse.ArgumentParser:
    parser = ClosedArgumentParser(
        description="Package, validate, and import complete portable JianYing projects."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    package = commands.add_parser("package")
    package.add_argument("--draft", type=Path, required=True)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--importer-exe", type=Path, required=True)
    package.add_argument("--importer-receipt", type=Path)
    package.add_argument("--snapshot", type=Path)
    package.add_argument("--snapshot-job-digest")
    package.add_argument("--snapshot-receipt-sha256")
    package.add_argument("--source-app")
    package.add_argument("--source-version")
    package.add_argument(
        "--structure-policy",
        choices=("revision", "standard"),
        default="revision",
    )
    _add_json_argument(package)

    validate = commands.add_parser("validate")
    validate.add_argument("--package", type=Path, required=True)
    _add_json_argument(validate)

    import_command = commands.add_parser("import")
    import_command.add_argument("--package", type=Path, required=True)
    import_command.add_argument("--target-drafts-root", type=Path)
    import_command.add_argument("--project-name")
    import_command.add_argument("--diagnostic-override", action="store_true")
    _add_json_argument(import_command)

    gui = commands.add_parser("importer-gui")
    gui.add_argument("--package", type=Path)
    gui.add_argument("--self-check", action="store_true")
    gui.add_argument("--self-check-output", type=Path)
    _add_json_argument(gui)

    build = commands.add_parser("build-importer")
    build.add_argument("--output-dir", type=Path)
    build.add_argument("--python", type=Path)
    _add_json_argument(build)
    return parser


def _emit(payload: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(dict(payload), ensure_ascii=True, sort_keys=True))
    else:
        print(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _source_environment(args: argparse.Namespace, detector: EnvironmentDetector) -> dict[str, Any]:
    explicit_app = str(args.source_app or "").strip()
    if explicit_app and explicit_app.casefold() not in SUPPORTED_APP_IDENTITIES:
        raise PortableProjectError(
            "unknown_source_app",
            "Source project application must be JianyingPro or CapCut",
        )
    if explicit_app:
        explicit_app = "JianyingPro" if explicit_app.casefold() == "jianyingpro" else "CapCut"
    path_app = infer_app_name_from_path(args.draft)
    if explicit_app and path_app and explicit_app.casefold() != path_app.casefold():
        raise PortableProjectError(
            "unknown_source_app",
            "Explicit source application conflicts with the draft directory owner",
        )
    preferred_app = explicit_app or path_app
    detected = dict(
        detector(
            force_refresh=True,
            include_process=False,
            preferred_app=preferred_app or None,
        )
    )
    detected_app = str(detected.get("app_name") or "").strip()
    candidate_apps = {
        str(row.get("app_name") or "").strip()
        for row in detected.get("candidates") or []
        if isinstance(row, Mapping)
        and str(row.get("app_name") or "").strip().casefold() in SUPPORTED_APP_IDENTITIES
    }
    if not preferred_app and (
        bool(detected.get("app_identity_ambiguous")) or len(candidate_apps) > 1
    ):
        raise PortableProjectError(
            "unknown_source_app",
            "Source application is ambiguous because JianYing and CapCut are both installed",
        )
    if detected_app.casefold() not in SUPPORTED_APP_IDENTITIES:
        raise PortableProjectError(
            "unknown_source_app",
            "Source project application must be detected as JianyingPro or CapCut",
        )
    selected_app = preferred_app or detected_app
    if selected_app.casefold() != detected_app.casefold():
        raise PortableProjectError(
            "unknown_source_app",
            "Detected source application does not match the selected application",
        )
    selected_app = "JianyingPro" if selected_app.casefold() == "jianyingpro" else "CapCut"
    detected_version = str(detected.get("app_version") or "").strip()
    asserted_version = str(args.source_version or "").strip()
    if asserted_version and not versions_equal(asserted_version, detected_version):
        raise PortableProjectError(
            "source_version_mismatch",
            "Explicit source version does not match the detected application version",
        )
    detected_identities = {
        str(identity or "").strip()
        for identity in detected.get("detected_app_identities") or []
        if str(identity or "").strip().casefold() in SUPPORTED_APP_IDENTITIES
    }
    detected_identities.update(candidate_apps)
    if not detected_identities:
        detected_identities.add(detected_app)
    selection_basis = (
        "explicit_source_app"
        if explicit_app
        else "draft_path_owner" if path_app else "detector_single_app"
    )
    return {
        "app_name": selected_app,
        "app_version": detected_version,
        "detected_app_name": detected_app,
        "detected_app_version": detected_version,
        "selection_basis": selection_basis,
        "app_identity_ambiguous": bool(detected.get("app_identity_ambiguous"))
        or len(detected_identities) > 1,
        "detected_app_identities": sorted(detected_identities),
    }


def _dispatch(
    args: argparse.Namespace,
    *,
    detector: EnvironmentDetector,
) -> tuple[int, dict[str, Any] | None]:
    if args.command == "package":
        result = package_project(
            args.draft,
            args.output,
            args.importer_exe,
            snapshot_dir=args.snapshot,
            expected_snapshot_job_digest=args.snapshot_job_digest,
            expected_snapshot_receipt_sha256=args.snapshot_receipt_sha256,
            source_environment=_source_environment(args, detector),
            importer_receipt=args.importer_receipt,
            structure_policy=args.structure_policy,
        )
        return 0, result
    if args.command == "validate":
        return 0, validate_package(args.package)
    if args.command == "import":
        manifest = read_manifest(args.package)
        project = manifest.get("project")
        source_app = str(project.get("source_app") or "") if isinstance(project, dict) else ""
        target_root_app = (
            infer_app_name_from_path(args.target_drafts_root)
            if args.target_drafts_root is not None
            else ""
        )
        preferred_app = target_root_app or source_app
        environment = dict(
            detector(
                force_refresh=True,
                include_process=True,
                preferred_app=preferred_app or None,
            )
        )
        detected_app = str(environment.get("app_name") or "").strip()
        if target_root_app and detected_app.casefold() != target_root_app.casefold():
            raise PortableProjectError(
                "target_environment_mismatch",
                "The selected drafts directory does not belong to the detected application",
            )
        target_root = args.target_drafts_root or environment.get("drafts_root")
        if not target_root:
            raise PortableProjectError(
                "target_drafts_root_missing", "Could not detect the JianYing drafts directory"
            )
        result = import_project(
            args.package,
            Path(target_root),
            environment,
            project_name=args.project_name,
            diagnostic_override=args.diagnostic_override,
        )
        return 0, result
    if args.command == "importer-gui":
        if args.self_check:
            result = importer_self_check()
            if args.self_check_output is not None:
                target = args.self_check_output.resolve(strict=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(
                    f"{json.dumps(result, ensure_ascii=False, sort_keys=True)}\n",
                    encoding="utf-8",
                )
                os.replace(temporary, target)
            return 0, result
        return run_importer_gui(args.package, detector=detector), None
    if args.command == "build-importer":
        root = Path(__file__).resolve().parents[1]
        result = build_importer(
            root,
            output_dir=args.output_dir,
            python_executable=args.python,
        )
        return 0, result
    raise PortableProjectError("invalid_command", "Unknown portable project command")


def main(
    argv: Sequence[str] | None = None,
    *,
    detector: EnvironmentDetector = detect_jianying_environment,
) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False) and (not raw_arguments or raw_arguments[0].startswith("-")):
        raw_arguments.insert(0, "importer-gui")
    args: argparse.Namespace | None = None
    requested_json = "--json" in raw_arguments
    try:
        args = build_parser().parse_args(raw_arguments)
        exit_code, payload = _dispatch(args, detector=detector)
    except PortableProjectError as exc:
        payload = {"status": "error", **exc.as_dict()}
        _emit(payload, as_json=bool(getattr(args, "as_json", requested_json)))
        return 2
    except Exception:
        payload = {
            "status": "error",
            "code": "portable_project_operation_failed",
            "reason": "Portable project operation failed",
            "data": {},
        }
        _emit(payload, as_json=bool(getattr(args, "as_json", requested_json)))
        return 2
    if payload is not None:
        _emit(payload, as_json=bool(getattr(args, "as_json", requested_json)))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
