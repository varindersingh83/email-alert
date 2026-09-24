"""Opt-in live test that sends a clearly labeled message to the configured chat."""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


pytestmark = pytest.mark.telegram_e2e


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
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_real_telegram_delivery():
    if os.environ.get("RUN_TELEGRAM_E2E") != "1":
        pytest.skip("Set RUN_TELEGRAM_E2E=1 to send a real test message")
    base_url = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    api_key = os.environ.get("LOCAL_API_KEY", "")
    if not api_key:
        pytest.fail("Set LOCAL_API_KEY in the local environment before opting in")

    unique = uuid.uuid4().hex[:12]
    body = {"text": f"[Email Alert E2E TEST] delivery check {unique}"}
    status, accepted = request_json(
        base_url + "/send", api_key, body, key="telegram-e2e-" + unique
    )
    assert status == 202, accepted

    deadline = time.monotonic() + 45
    last_status = None
    while time.monotonic() < deadline:
        status, job = request_json(f"{base_url}/jobs/{accepted['job_id']}", api_key)
        assert status == 200, job
        last_status = job["status"]
        if last_status == "sent":
            assert job["telegram_message_id"]
            return
        if last_status == "failed":
            pytest.fail(f"Telegram delivery failed: {job.get('last_error')}")
        time.sleep(1)
    pytest.fail(f"Timed out waiting for Telegram delivery; final status was {last_status}")
