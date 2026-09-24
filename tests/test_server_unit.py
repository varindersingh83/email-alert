"""Unit tests use mocked Telegram responses and never need credentials or PostgreSQL."""

from __future__ import annotations

import io
import json
import re
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

import server


def telegram_http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError("https://api.telegram.org/", status, "Telegram error", {}, io.BytesIO(body))


def test_telegram_send_success(monkeypatch):
    monkeypatch.setattr(server, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server, "CHAT_ID", "-100123")
    response = io.BytesIO(b'{"ok":true,"result":{"message_id":91}}')

    with patch.object(server, "urlopen", return_value=response) as urlopen:
        result = server.telegram_send("fixture message")

    assert result == server.TelegramResult(True, message_id=91)
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/sendMessage")
    assert json.loads(request.data) == {"chat_id": "-100123", "text": "fixture message"}


def test_telegram_send_uses_the_selected_destination(monkeypatch):
    monkeypatch.setattr(server, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server, "CHAT_ID", "-100111")
    response = io.BytesIO(b'{"ok":true,"result":{"message_id":92}}')

    with patch.object(server, "urlopen", return_value=response) as urlopen:
        result = server.telegram_send("fixture message", "-100222")

    assert result == server.TelegramResult(True, message_id=92)
    request = urlopen.call_args.args[0]
    assert json.loads(request.data) == {"chat_id": "-100222", "text": "fixture message"}


def test_configured_destinations_parse_a_named_allowlist(monkeypatch):
    monkeypatch.setattr(
        server,
        "DESTINATIONS_JSON",
        '{"founders":{"label":"Founders group","chat_id":"-100123"},'
        '"personal":{"label":"Personal chat","chat_id":"123456"}}',
    )
    monkeypatch.setattr(server, "CHAT_ID", "")

    destinations = server.configured_destinations()

    assert destinations == {
        "founders": server.Destination("founders", "Founders group", "-100123"),
        "personal": server.Destination("personal", "Personal chat", "123456"),
    }
    assert server.public_destinations(destinations) == [
        {"name": "founders", "label": "Founders group"},
        {"name": "personal", "label": "Personal chat"},
    ]


@pytest.mark.parametrize(
    "raw_config",
    [
        "not-json",
        "[]",
        "{}",
        '{"Founders":{"label":"Founders","chat_id":"-100123"}}',
        '{"founders":{"label":"Founders","chat_id":"not-a-number"}}',
        '{"founders":{"label":" ","chat_id":"-100123"}}',
    ],
)
def test_configured_destinations_reject_invalid_allowlists(monkeypatch, raw_config):
    monkeypatch.setattr(server, "DESTINATIONS_JSON", raw_config)
    monkeypatch.setattr(server, "CHAT_ID", "")

    with pytest.raises(ValueError):
        server.configured_destinations()


def test_legacy_single_chat_config_becomes_default_destination(monkeypatch):
    monkeypatch.setattr(server, "DESTINATIONS_JSON", "")
    monkeypatch.setattr(server, "CHAT_ID", "-100123")

    assert server.configured_destinations() == {
        "default": server.Destination("default", "Default chat", "-100123")
    }


def test_legacy_default_cannot_conflict_with_named_default(monkeypatch):
    monkeypatch.setattr(server, "DESTINATIONS_JSON", '{"default":{"label":"Other","chat_id":"-100456"}}')
    monkeypatch.setattr(server, "CHAT_ID", "-100123")

    with pytest.raises(ValueError, match="must use the same chat ID"):
        server.configured_destinations()


def test_destination_choice_handler_smoke(monkeypatch, capsys):
    monkeypatch.setattr(server, "API_KEY", "smoke-test-key")
    monkeypatch.setattr(server, "CHAT_ID", "")
    monkeypatch.setattr(
        server,
        "DESTINATIONS_JSON",
        '{"founders":{"label":"Founders group","chat_id":"-100123"},'
        '"personal":{"label":"Personal chat","chat_id":"123456"}}',
    )
    inserted = []

    class Result:
        def fetchone(self):
            _key, request_hash, destination, _chat_id, _message = inserted[-1]
            return {"id": 42, "status": "queued", "request_hash": request_hash, "destination": destination}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, parameters):
            assert "INSERT INTO telegram_jobs" in statement
            inserted.append(parameters)
            return Result()

    monkeypatch.setattr(server, "database", Connection)

    class InMemoryHandler(server.Handler):
        def __init__(self, method, path, body=b"", headers=None):
            self.command = method
            self.path = path
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.headers = {"Authorization": "Bearer smoke-test-key", **(headers or {})}
            self.status = None
            self.response_headers = {}

        def send_response(self, status, *_args):
            self.status = status

        def send_header(self, name, value):
            self.response_headers[name] = value

        def end_headers(self):
            pass

        def dispatch(self):
            if self.command == "GET":
                self.do_GET()
            else:
                self.do_POST()
            return self.status, json.loads(self.wfile.getvalue())

    status, choices = InMemoryHandler("GET", "/destinations").dispatch()
    assert status == 200
    assert choices == {
        "destinations": [
            {"name": "founders", "label": "Founders group"},
            {"name": "personal", "label": "Personal chat"},
        ]
    }

    body = json.dumps({"text": "smoke test", "destination": "founders"}).encode()
    handler = InMemoryHandler(
        "POST",
        "/send",
        body,
        {"Content-Length": str(len(body)), "Idempotency-Key": "smoke-key"},
    )
    status, accepted = handler.dispatch()

    assert status == 202
    assert accepted == {"ok": True, "job_id": 42, "status": "queued", "destination": "founders"}
    assert inserted[0][2:] == ("founders", "-100123", "smoke test")
    assert re.fullmatch(r"[0-9a-f]{32}", handler.response_headers["X-Request-ID"])
    logs = capsys.readouterr().out
    assert '"event":"job_accepted"' in logs
    assert '"job_id":42' in logs
    assert all(secret not in logs for secret in ("smoke-test-key", "smoke-key", "smoke test", "-100123"))


@pytest.mark.parametrize(
    ("status", "body", "retryable", "retry_after"),
    [
        (429, b'{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":7}}', True, 7),
        (429, b'{"ok":false,"description":"Too Many Requests"}', True, None),
        (503, b'{"ok":false,"description":"Telegram is unavailable"}', True, None),
        (400, b'{"ok":false,"description":"Bad Request"}', False, None),
        (503, b"not-json", True, None),
        (400, b"not-json", False, None),
    ],
)
def test_telegram_http_error_classification(status, body, retryable, retry_after):
    with patch.object(server, "urlopen", side_effect=telegram_http_error(status, body)):
        result = server.telegram_send("fixture message")

    assert not result.ok
    assert result.retryable is retryable
    assert result.retry_after == retry_after
    assert result.error_code == status


def test_telegram_retry_after_is_bounded():
    body = b'{"ok":false,"parameters":{"retry_after":999999999999}}'
    with patch.object(server, "urlopen", side_effect=telegram_http_error(429, body)):
        result = server.telegram_send("fixture message")

    assert result.retryable
    assert result.retry_after == server.MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b'{"ok":true,"result":{}}', b'{"ok":"yes"}'],
)
def test_telegram_malformed_success_responses_are_retryable(body):
    with patch.object(server, "urlopen", return_value=io.BytesIO(body)):
        result = server.telegram_send("fixture message")

    assert not result.ok
    assert result.retryable
    assert "invalid" in result.error


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timed out")])
def test_telegram_network_errors_are_retryable(error):
    with patch.object(server, "urlopen", side_effect=error):
        result = server.telegram_send("fixture message")

    assert result == server.TelegramResult(False, error="Telegram network error", retryable=True)
