# ruff: noqa: E402,I001
"""Configure and inspect optional Auto-Cut user-action notifications."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = Path(__file__).resolve().parent
for import_root in (REPOSITORY_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from scripts.utils.cli_protocol import make_result
from scripts.utils import user_action_notifications as notifications


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "invalid notification command arguments\n")


class _OneDestinationAction(argparse.Action):
    def __init__(self, *args, destination_type: str, **kwargs) -> None:
        self.destination_type = destination_type
        super().__init__(*args, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error("exactly one notification destination must be provided")
        setattr(namespace, self.dest, values)
        setattr(namespace, "destination_type", self.destination_type)


class _OneIdentityAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error("exactly one notification identity must be provided")
        setattr(namespace, self.dest, values)


def _nonempty(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("value must be nonempty")
    return value


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")


def _add_destination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--as",
        dest="identity",
        required=True,
        choices=("user", "bot"),
        action=_OneIdentityAction,
    )
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--chat-id",
        dest="destination_id",
        action=_OneDestinationAction,
        destination_type="chat_id",
        type=_nonempty,
    )
    destination.add_argument(
        "--user-id",
        dest="destination_id",
        action=_OneDestinationAction,
        destination_type="open_id",
        type=_nonempty,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Configure optional Feishu alerts for Auto-Cut user-action waits."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup_preview = commands.add_parser(
        "setup-preview", help="Dry-run and review one notification destination"
    )
    _add_destination_arguments(setup_preview)
    _add_json_argument(setup_preview)

    setup_enable = commands.add_parser(
        "setup-enable", help="Enable the exact destination covered by a preview"
    )
    _add_destination_arguments(setup_enable)
    setup_enable.add_argument("--preview-digest", required=True)
    setup_enable.add_argument("--confirm-template", required=True)
    setup_enable.add_argument("--confirm-privacy", required=True)
    _add_json_argument(setup_enable)

    status = commands.add_parser("status", help="Inspect sanitized local notification status")
    _add_json_argument(status)

    disable = commands.add_parser("disable", help="Disable notifications on this computer")
    _add_json_argument(disable)

    notify = commands.add_parser("notify", help="Emit one closed user-action alert")
    notify.add_argument("--input-digest", required=True)
    notify.add_argument("--project-key", required=True, type=_nonempty)
    notify.add_argument(
        "--action-code",
        required=True,
        choices=tuple(sorted(notifications.ACTION_LABELS)),
    )
    notify.add_argument(
        "--item-id",
        dest="item_ids",
        action="append",
        required=True,
        type=_nonempty,
    )
    notify.add_argument("--prompt-revision", required=True, type=_positive_int)
    _add_json_argument(notify)
    return parser


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    reason = result.get("reason")
    print(reason if reason else result["code"])


def _run_command(
    args: argparse.Namespace,
    *,
    config_path: Path,
    receipts_root: Path,
    command_resolver: Callable[[], Sequence[str]] | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> tuple[int, dict[str, Any]]:
    if args.command == "setup-preview":
        data = notifications.preview_notification_setup(
            identity=args.identity,
            destination_type=args.destination_type,
            destination_id=args.destination_id,
            command_resolver=command_resolver,
            runner=runner,
        )
        return 0, make_result(True, "setup_preview_ready", "", data)
    if args.command == "setup-enable":
        data = notifications.enable_notification_setup(
            identity=args.identity,
            destination_type=args.destination_type,
            destination_id=args.destination_id,
            preview_digest=args.preview_digest,
            confirm_template=args.confirm_template,
            confirm_privacy=args.confirm_privacy,
            config_path=config_path,
        )
        return 0, make_result(True, "notifications_enabled", "", data)
    if args.command == "status":
        data = notifications.get_notification_status(
            config_path=config_path,
            command_resolver=command_resolver,
        )
        return 0, make_result(True, "ok", "", data)
    if args.command == "disable":
        data = notifications.disable_notification_config(config_path)
        return 0, make_result(True, "notifications_disabled", "", data)

    event = notifications.build_user_action_event(
        input_digest=args.input_digest,
        project_key=args.project_key,
        action_code=args.action_code,
        item_ids=args.item_ids,
        prompt_revision=args.prompt_revision,
    )
    receipt = notifications.deliver_user_action_required(
        event,
        config_path=config_path,
        receipts_root=receipts_root,
        command_resolver=command_resolver,
        runner=runner,
    )
    try:
        data = notifications.notification_receipt_summary(receipt, event)
    except (TypeError, ValueError) as error:
        raise RuntimeError("notification receipt validation failed") from error
    state = data["delivery_state"]
    return 0, make_result(
        state != "failed",
        f"notification_{state}",
        "" if state != "failed" else "notification delivery failed; the local wait remains valid",
        data,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    config_path: Path | None = None,
    receipts_root: Path | None = None,
    command_resolver: Callable[[], Sequence[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    effective_config_path = (
        notifications.DEFAULT_CONFIG_PATH if config_path is None else Path(config_path)
    )
    effective_receipts_root = (
        notifications.DEFAULT_RECEIPTS_ROOT if receipts_root is None else Path(receipts_root)
    )
    try:
        exit_code, result = _run_command(
            args,
            config_path=effective_config_path,
            receipts_root=effective_receipts_root,
            command_resolver=command_resolver,
            runner=runner,
        )
    except notifications.NotificationSetupError as error:
        exit_code = 1
        result = make_result(
            False,
            "setup_dry_run_failed",
            f"notification setup dry-run failed: {error.error_code}",
        )
    except (TypeError, ValueError, UnicodeError):
        exit_code = 2
        result = make_result(
            False,
            "invalid_input",
            "notification input or configuration is invalid",
        )
    except (notifications.NotificationPersistenceError, OSError):
        exit_code = 1
        result = make_result(False, "runtime_error", "notification operation failed")
    except Exception:
        exit_code = 1
        result = make_result(False, "runtime_error", "notification operation failed")
    _emit(result, args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
