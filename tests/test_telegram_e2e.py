"""Opt-in test that sends one clearly labeled message through the deployed service."""

from __future__ import annotations

import ipaddress
import json
import os
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pytest


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_e2e_url(raw_url: str, expected_host: str = "") -> str:
    """Allow local HTTP or pinned HTTPS SIT hosts before sending the bearer key."""
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("E2E_BASE_URL must be an HTTP loopback URL or an approved HTTPS host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("E2E_BASE_URL must not contain credentials, query parameters, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("E2E_BASE_URL must point to the service root")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("E2E_BASE_URL has an invalid port") from error

    host = parsed.hostname
    if parsed.scheme == "http":
        if not _is_loopback(host):
            raise ValueError("Plain HTTP is allowed only for localhost or a loopback IP")
    elif not _is_loopback(host):
        if not expected_host or parsed.netloc.casefold() != expected_host.strip().casefold():
            raise ValueError("Remote HTTPS requires E2E_EXPECTED_HOST to match the exact URL host")
    elif expected_host and parsed.netloc.casefold() != expected_host.strip().casefold():
        raise ValueError("E2E_EXPECTED_HOST does not match E2E_BASE_URL")

    return f"{parsed.scheme}://{parsed.netloc}"


def request_json(url: str, api_key: str, payload: dict | None = None, key: str | None = None):
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = key or str(uuid.uuid4())
    request = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(RejectRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response), response.headers.get("X-Request-ID")
    except HTTPError as error:
        try:
            return error.code, json.load(error), error.headers.get("X-Request-ID")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return error.code, {"error": "service returned a non-JSON response"}, None


def test_e2e_url_validation_and_redirect_policy():
    assert validate_e2e_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert (
        validate_e2e_url("https://client-sit.up.railway.app", "client-sit.up.railway.app")
        == "https://client-sit.up.railway.app"
    )
    for url, expected in [
        ("http://example.com", ""),
        ("https://client-sit.up.railway.app", "other.up.railway.app"),
        ("https://user:secret@example.com", "example.com"),
        ("https://example.com/path", "example.com"),
        ("https://example.com/?token=secret", "example.com"),
        ("ftp://example.com", "example.com"),
    ]:
        with pytest.raises(ValueError):
            validate_e2e_url(url, expected)
    assert RejectRedirects().redirect_request(Request("https://example.com"), None, 302, "found", {}, "https://elsewhere.example") is None


@pytest.mark.telegram_e2e
def test_real_telegram_delivery():
    if os.environ.get("RUN_TELEGRAM_E2E") != "1":
        pytest.skip("Set RUN_TELEGRAM_E2E=1 to send a real test message")
    raw_base_url = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8080")
    try:
        base_url = validate_e2e_url(raw_base_url, os.environ.get("E2E_EXPECTED_HOST", ""))
    except ValueError as error:
        pytest.fail(str(error))
    api_key = os.environ.get("LOCAL_API_KEY", "")
    if not api_key:
        pytest.fail("Set LOCAL_API_KEY in the local environment before opting in")

    unique = uuid.uuid4().hex[:12]
    status, health, _ = request_json(base_url + "/health", api_key)
    assert (status, health) == (200, {"ok": True})
    assert request_json(base_url + "/destinations", "")[0] == 401
    status, choices, _ = request_json(base_url + "/destinations", api_key)
    assert status == 200, choices
    destinations = choices["destinations"]
    assert destinations and all(set(choice) == {"name", "label"} for choice in destinations)
    destination = os.environ.get("E2E_DESTINATION")
    if destination is None and len(destinations) == 1:
        destination = destinations[0]["name"]
    assert destination in {choice["name"] for choice in destinations}, (
        "Set E2E_DESTINATION to an allowlisted test chat before sending"
    )

    text = f"[Email Alert E2E TEST] delivery check {unique}"
    body = {"text": text, "destination": destination}
    assert request_json(base_url + "/send", api_key,
                        {**body, "destination": "unknown-sit-destination"},
                        key="telegram-e2e-invalid-" + unique)[0] == 400
    assert request_json(base_url + "/send", api_key,
                        {**body, "chat_id": "123"},
                        key="telegram-e2e-chat-id-" + unique)[0] == 400

    idempotency_key = "telegram-e2e-" + unique
    status, accepted, request_id = request_json(base_url + "/send", api_key, body, key=idempotency_key)
    assert status == 202, accepted
    assert accepted["destination"] == destination
    assert request_id and len(request_id) == 32
    replay_status, replay, _ = request_json(base_url + "/send", api_key, body, key=idempotency_key)
    assert replay_status in (200, 202), replay
    assert replay["job_id"] == accepted["job_id"]
    assert request_json(base_url + "/send", api_key,
                        {**body, "text": text + " changed"}, key=idempotency_key)[0] == 409
    assert request_json(f"{base_url}/jobs/{accepted['job_id']}", "")[0] == 401

    deadline = time.monotonic() + 45
    last_status = None
    while time.monotonic() < deadline:
        status, job, _ = request_json(f"{base_url}/jobs/{accepted['job_id']}", api_key)
        assert status == 200, job
        last_status = job["status"]
        if last_status == "sent":
            assert job["telegram_message_id"]
            assert job["destination"] == destination
            print(json.dumps({"sit": "passed", "job_id": accepted["job_id"],
                              "request_id": request_id, "destination": destination,
                              "telegram_message_id": job["telegram_message_id"]}))
            return
        if last_status == "failed":
            pytest.fail(f"Telegram delivery failed: {job.get('last_error')}")
        time.sleep(1)
    pytest.fail(f"Timed out waiting for Telegram delivery; final status was {last_status}")
