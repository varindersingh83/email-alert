"""HTTP + PostgreSQL integration tests; Telegram itself is mocked."""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import psycopg
import pytest

import server


pytestmark = pytest.mark.integration
API_KEY = "integration-test-key"
KEY_PREFIX = f"integration-{uuid.uuid4()}-"


@pytest.fixture(scope="module")
def api(test_database_url, monkeypatch_module):
    monkeypatch_module.setattr(server, "DATABASE_URL", test_database_url)
    monkeypatch_module.setattr(server, "API_KEY", API_KEY)
    server.initialize_database()
    with psycopg.connect(test_database_url) as conn:
        conn.execute("TRUNCATE TABLE telegram_jobs RESTART IDENTITY")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()
    with psycopg.connect(test_database_url) as conn:
        conn.execute("TRUNCATE TABLE telegram_jobs RESTART IDENTITY")


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope="session")
def test_database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database to run integration tests")
    try:
        with psycopg.connect(url) as conn:
            db_name = conn.info.dbname.lower()
    except psycopg.Error as error:
        pytest.fail(f"Cannot connect to TEST_DATABASE_URL: {type(error).__name__}")
    if not db_name.endswith(("_test", "-test")):
        pytest.fail("Refusing to use a database whose name does not end in '_test' or '-test'")
    return url


def send(api, path, payload=None, key=None, auth=API_KEY):
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = key or KEY_PREFIX + str(uuid.uuid4())
    request = Request(
        api + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        response = urlopen(request)
    except Exception as error:
        if hasattr(error, "code"):
            return error.code, json.load(error)
        raise
    with response:
        return response.status, json.load(response)


def test_authenticated_queue_deduplicates_and_tracks_delivery(api, monkeypatch):
    key = KEY_PREFIX + "same-event"
    payload = {"text": "mock integration message"}

    status, first = send(api, "/send", payload, key)
    assert status == 202
    status, duplicate = send(api, "/send", payload, key)
    assert status == 202
    assert first["job_id"] == duplicate["job_id"]

    status, conflict = send(api, "/send", {"text": "different"}, key)
    assert status == 409
    assert "different text" in conflict["error"]

    monkeypatch.setattr(server, "telegram_send", lambda text: (True, 777, None, None))
    assert server.process_one() is True
    status, job = send(api, f"/jobs/{first['job_id']}")
    assert status == 200
    assert job["status"] == "sent"
    assert job["telegram_message_id"] == 777


def test_auth_validation_and_health(api):
    status, body = send(api, "/send", {"text": "not authorized"}, KEY_PREFIX + "unauth", auth="wrong")
    assert status == 401
    assert body["error"] == "unauthorized"

    status, body = send(api, "/send", {"text": "   "}, KEY_PREFIX + "empty")
    assert status == 400

    status, body = send(api, "/health")
    assert status == 200
    assert body == {"ok": True}
