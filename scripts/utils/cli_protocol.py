import json
from typing import Any, Dict, Optional

from utils.console import configure_utf8_stdio


def make_result(
    ok: bool, code: str, reason: str = "", data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "ok": bool(ok),
        "code": code,
        "reason": reason,
        "data": data or {},
    }


def emit_result(result: Dict[str, Any], as_json: bool) -> None:
    configure_utf8_stdio()
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
