"""Unit tests use mocked Telegram responses and never need credentials or PostgreSQL."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError
from unittest.mock import patch

import server


def test_telegram_send_success(monkeypatch):
    monkeypatch.setattr(server, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server, "CHAT_ID", "-100123")
    response = io.BytesIO(b'{"ok":true,"result":{"message_id":91}}')

    with patch.object(server, "urlopen", return_value=response) as urlopen:
        result = server.telegram_send("fixture message")

    assert result == (True, 91, None, None)
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/sendMessage")
    assert json.loads(request.data) == {"chat_id": "-100123", "text": "fixture message"}


def test_telegram_send_rate_limit_exposes_retry_after(monkeypatch):
    monkeypatch.setattr(server, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server, "CHAT_ID", "-100123")
    body = io.BytesIO(b'{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":7}}')
    error = HTTPError("https://api.telegram.org/", 429, "rate limited", {}, body)

    with patch.object(server, "urlopen", side_effect=error):
        result = server.telegram_send("fixture message")

    assert result == (False, None, "Too Many Requests", 7)


def test_telegram_send_network_error_is_retryable():
    from urllib.error import URLError

    with patch.object(server, "urlopen", side_effect=URLError("offline")):
        result = server.telegram_send("fixture message")

    assert result == (False, None, "Telegram network error", None)
