#!/usr/bin/env python3
import json
import os
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOG_FILE_PATH = Path(os.environ.get("LOG_FILE_PATH", "/logs/app.log"))
HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
PORT = int(os.environ.get("HTTP_PORT", "8080"))
DEFAULT_LEVEL = os.environ.get("DEFAULT_LOG_LEVEL", "INFO")
LOG_FORMAT = os.environ.get(
    "LOG_FORMAT",
    "{timestamp} level={level} message={message}",
)

WRITE_LOCK = threading.Lock()


def ensure_log_file() -> None:
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE_PATH.touch(exist_ok=True)


def append_log(level: str, message: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = LOG_FORMAT.format(timestamp=timestamp, level=level, message=message)
    with WRITE_LOCK:
        with LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    return line


class LogRequestHandler(BaseHTTPRequestHandler):
    server_version = "oci-log-generator/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "log_file_path": str(LOG_FILE_PATH),
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/log":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
            return

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be valid JSON"})
            return

        level = str(payload.get("level", DEFAULT_LEVEL)).strip() or DEFAULT_LEVEL
        message = str(payload.get("message", "")).strip()

        if not message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
            return

        line = append_log(level=level.upper(), message=message)
        self._send_json(
            HTTPStatus.CREATED,
            {
                "status": "written",
                "line": line,
                "log_file_path": str(LOG_FILE_PATH),
            },
        )


def main() -> None:
    ensure_log_file()
    print(f"[generator] listening on {HOST}:{PORT}", flush=True)
    print(f"[generator] writing to {LOG_FILE_PATH}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), LogRequestHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
