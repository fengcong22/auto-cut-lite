import argparse
import json
import sys
from typing import Any, Dict, Optional

from jy_adapter_core import invoke_tool, list_tools


class MCPAdapterServer:
    SERVER_INFO = {
        "name": "capcut-mate-mcp",
        "version": "0.1.0",
    }

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        try:
            if method == "initialize":
                return self._response(
                    request_id,
                    {
                        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                        "serverInfo": self.SERVER_INFO,
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "tools/list":
                return self._response(request_id, {"tools": list_tools()})
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                result = invoke_tool(tool_name, arguments)
                return self._response(
                    request_id,
                    {
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                        ],
                        "isError": not result.get("ok", False),
                        "structuredContent": result,
                    },
                )
            return self._error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))

    def _response(self, request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _read_message(stdin) -> Optional[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    while True:
        line = stdin.buffer.readline()
        if not line:
            return None
        decoded = line.decode("utf-8").strip()
        if not decoded:
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(stdout, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    stdout.buffer.write(body)
    stdout.buffer.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Lightweight MCP adapter for jy_wrapper tool surface."
    )
    parser.parse_args(argv)

    server = MCPAdapterServer()
    while True:
        request = _read_message(sys.stdin)
        if request is None:
            break
        response = server.handle_request(request)
        _write_message(sys.stdout, response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
