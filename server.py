#!/usr/bin/env python3
"""Small authenticated HTTP bridge for sending Telegram messages."""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_dotenv() -> None:
    """Load simple KEY=value entries from the project .env without dependencies."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY = os.environ.get("LOCAL_API_KEY", "")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    server_version = "EmailAlert/1.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/send":
            self.send_json(404, {"error": "not found"})
            return

        provided_key = self.headers.get("Authorization", "")
        if not API_KEY or not hmac.compare_digest(provided_key, f"Bearer {API_KEY}"):
            self.send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16_384:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            message = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(message, str) or not message.strip() or len(message) > 4096:
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "send JSON with a non-empty text field (max 4096 characters)"})
            return

        if not BOT_TOKEN or not CHAT_ID:
            self.send_json(503, {"error": "Telegram is not configured"})
            return

        request = Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": CHAT_ID, "text": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
                description = result.get("description", "Telegram rejected the request")
            except (json.JSONDecodeError, AttributeError):
                description = "Telegram rejected the request"
            self.send_json(502, {"error": description})
            return
        except (URLError, TimeoutError):
            self.send_json(502, {"error": "could not reach Telegram"})
            return

        if not result.get("ok"):
            self.send_json(502, {"error": result.get("description", "Telegram rejected the request")})
            return

        sent = result.get("result", {})
        self.send_json(200, {"ok": True, "message_id": sent.get("message_id")})

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging request bodies, credentials, or message text.
        print(f"{self.address_string()} - {format % args}")


if not API_KEY:
    raise SystemExit("Set LOCAL_API_KEY in .env before starting the server.")

print(f"Listening on http://{HOST}:{PORT}")
ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
