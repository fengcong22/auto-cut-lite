"""Target-local Volcengine ASR setup for the portable Auto-Cut plugin."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from scripts.utils.volc_asr_onboarding import (  # noqa: E402
    build_volc_guide,
    configure_volc,
    inspect_volc_config,
)


def _config_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is required for portable ASR configuration")
    return Path(local_app_data).resolve() / "Auto-Cut" / "auto-cut-lite" / "config"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "config", "guide"):
        child = commands.add_parser(name)
        child.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    indent = None if as_json else 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=indent))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = inspect_volc_config(_config_root())
        elif args.command == "config":
            payload = configure_volc(_config_root())
        else:
            payload = build_volc_guide()
            payload["commands"] = [
                "python scripts/portable/volc_setup.py config",
                "python scripts/portable/volc_setup.py status --json",
            ]
    except (OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "failed", "error": str(exc)}, as_json=args.as_json)
        return 1
    _emit(payload, as_json=args.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
