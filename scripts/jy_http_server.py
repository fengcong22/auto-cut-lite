import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from jy_adapter_core import invoke_tool, list_tools
from utils.cli_protocol import make_result


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class AdapterHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "CapCutMateHTTP/0.1"

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Tuple[Dict[str, Any], Dict[str, Any] | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}, make_result(False, "invalid_input", "Invalid Content-Length")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return {}, make_result(False, "invalid_input", f"Invalid JSON: {exc}")
        if not isinstance(data, dict):
            return {}, make_result(False, "invalid_input", "JSON body must be an object")
        return data, None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, make_result(True, "ok", "", {"status": "ok"}))
            return
        if parsed.path == "/tools":
            self._send_json(200, make_result(True, "ok", "", {"tools": list_tools()}))
            return
        self._send_json(404, make_result(False, "not_found", f"Unknown route: {parsed.path}"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload, error = self._read_json()
        if error is not None:
            self._send_json(400, error)
            return

        if parsed.path == "/invoke":
            tool_name = payload.get("tool")
            arguments = payload.get("arguments") or {}
            result = invoke_tool(tool_name, arguments)
            self._send_json(200 if result["ok"] else 400, result)
            return

        if parsed.path.startswith("/tools/"):
            tool_name = parsed.path.rsplit("/", 1)[-1]
            result = invoke_tool(tool_name, payload)
            self._send_json(200 if result["ok"] else 400, result)
            return

        self._send_json(404, make_result(False, "not_found", f"Unknown route: {parsed.path}"))

    def log_message(self, format: str, *args) -> None:
        return


def create_http_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), AdapterHTTPRequestHandler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Lightweight HTTP adapter for jy_wrapper tool surface."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    server = create_http_server(args.host, args.port)
    try:
        print(
            json.dumps({"ok": True, "code": "ok", "data": {"host": args.host, "port": args.port}})
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
