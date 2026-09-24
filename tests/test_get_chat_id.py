"""The chat-ID helper parses mocked Telegram updates without credentials or network access."""

from __future__ import annotations

import io
import json
import sys
from urllib.error import HTTPError
from unittest.mock import patch

import pytest

import get_chat_id


def telegram_response(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def run_helper(monkeypatch, capsys, payload, args=()):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(get_chat_id, "load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["get_chat_id.py", *args])

    with patch.object(get_chat_id, "urlopen", return_value=telegram_response(payload)) as urlopen:
        get_chat_id.main()

    return capsys.readouterr(), urlopen


def test_groups_only_prints_group_ids_and_titles_without_message_text(monkeypatch, capsys):
    payload = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 123, "type": "private"}, "text": "private secret"}},
            {
                "message": {
                    "chat": {"id": -55, "type": "group", "title": "Build Team"},
                    "text": "group secret",
                }
            },
            {
                "message": {
                    "chat": {"id": -10077, "type": "supergroup", "title": "Alerts"},
                    "text": "supergroup secret",
                }
            },
            {"channel_post": {"chat": {"id": -88, "type": "channel", "title": "News"}}},
        ],
    }

    output, urlopen = run_helper(monkeypatch, capsys, payload, ("--groups-only",))

    assert output.out.splitlines() == [
        "CHAT_ID\tTYPE\tTITLE",
        "-10077\tsupergroup\tAlerts",
        "-55\tgroup\tBuild Team",
    ]
    assert "secret" not in output.out
    assert "test-token" not in output.out
    assert urlopen.call_args.args[0].endswith("/bottest-token/getUpdates")
    assert urlopen.call_args.kwargs["timeout"] == 15


def test_default_mode_also_lists_private_chat(monkeypatch, capsys):
    payload = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 123, "type": "private"}}},
            {"message": {"chat": {"id": -55, "type": "group", "title": "Build Team"}}},
        ],
    }

    output, _ = run_helper(monkeypatch, capsys, payload)

    assert output.out.splitlines() == [
        "CHAT_ID\tTYPE\tTITLE",
        "-55\tgroup\tBuild Team",
        "123\tprivate\tprivate chat",
    ]


def test_groups_only_explains_how_to_generate_a_group_update(monkeypatch, capsys):
    payload = {
        "ok": True,
        "result": [{"message": {"chat": {"id": 123, "type": "private"}}}],
    }
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(get_chat_id, "load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["get_chat_id.py", "--groups-only"])
    monkeypatch.setattr(get_chat_id, "urlopen", lambda *_args, **_kwargs: telegram_response(payload))

    with pytest.raises(SystemExit, match="No group updates") as raised:
        get_chat_id.main()

    error = str(raised.value)
    assert "send" in error
    assert "replace" in error
    assert "actual bot username" in error


def test_http_409_points_to_existing_update_handler(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(get_chat_id, "load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["get_chat_id.py", "--groups-only"])
    error = HTTPError("https://api.telegram.org", 409, "Conflict", {}, io.BytesIO())
    monkeypatch.setattr(get_chat_id, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(SystemExit, match="another poller"):
        get_chat_id.main()


def test_missing_token_stops_before_request(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(get_chat_id, "load_local_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["get_chat_id.py", "--groups-only"])
    urlopen = patch.object(get_chat_id, "urlopen")

    with urlopen as request, pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN is missing"):
        get_chat_id.main()

    request.assert_not_called()
