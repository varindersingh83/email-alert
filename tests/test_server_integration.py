"""HTTP and PostgreSQL tests; Telegram delivery is fail-closed and mocked."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg import sql
import pytest

import server


API_KEY = "integration-test-key"
KEY_PREFIX = f"integration-{uuid.uuid4()}-"
_AUTO_KEY = object()


def validate_test_database_url(url: str) -> dict[str, str]:
    """Fail closed before connecting unless this is the dedicated local test DB."""
    try:
        params = conninfo_to_dict(url)
    except (TypeError, ValueError) as error:
        raise ValueError("TEST_DATABASE_URL is not valid PostgreSQL connection info") from error
    if params.get("dbname") != "email_alert_test":
        raise ValueError("TEST_DATABASE_URL must name the dedicated email_alert_test database")
    if params.get("service"):
        raise ValueError("TEST_DATABASE_URL must not use a PostgreSQL service definition")

    host = params.get("host", "")
    if not host:
        raise ValueError("TEST_DATABASE_URL must name localhost or a local Unix socket explicitly")
    hosts = host.split(",")
    for item in hosts:
        item = item.strip().strip("[]")
        if item.startswith("/") or item.lower() == "localhost":
            continue
        try:
            if ipaddress.ip_address(item).is_loopback:
                continue
        except ValueError:
            pass
        raise ValueError("TEST_DATABASE_URL must connect only to localhost or a local Unix socket")

    hostaddr = params.get("hostaddr")
    if hostaddr:
        try:
            if not ipaddress.ip_address(hostaddr).is_loopback:
                raise ValueError("TEST_DATABASE_URL hostaddr must be a loopback IP")
        except ValueError as error:
            raise ValueError("TEST_DATABASE_URL hostaddr must be a loopback IP") from error
    return params


def test_test_database_guard_rejects_remote_and_ambiguous_targets():
    with pytest.raises(ValueError, match="email_alert_test"):
        validate_test_database_url("postgresql://localhost/app_test")
    with pytest.raises(ValueError, match="localhost"):
        validate_test_database_url("postgresql://db.example.com/email_alert_test")
    with pytest.raises(ValueError, match="hostaddr"):
        validate_test_database_url("host=localhost hostaddr=203.0.113.10 dbname=email_alert_test")


@pytest.fixture(scope="session")
def test_database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to the local email_alert_test PostgreSQL database")
    validate_test_database_url(url)
    try:
        with psycopg.connect(url, connect_timeout=3):
            pass
    except psycopg.Error as error:
        pytest.fail(f"Cannot connect to TEST_DATABASE_URL: {type(error).__name__}")
    return url


@pytest.fixture(autouse=True)
def forbid_unmocked_telegram(monkeypatch):
    def fail_if_called(_message, _chat_id):
        pytest.fail("Integration tests must replace telegram_send with a mock")

    monkeypatch.setattr(server, "telegram_send", fail_if_called)


@pytest.fixture
def api(test_database_url, monkeypatch):
    schema = "email_alert_" + uuid.uuid4().hex
    with psycopg.connect(test_database_url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(test_database_url, options=f"-c search_path={schema}")
    monkeypatch.setattr(server, "DATABASE_URL", scoped_url)
    monkeypatch.setattr(server, "API_KEY", API_KEY)
    monkeypatch.setattr(server, "CHAT_ID", "-100123456789")
    monkeypatch.setattr(server, "DESTINATIONS_JSON", "")
    try:
        server.initialize_database()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(server, "WORKER_THREAD", thread)
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        if "httpd" in locals():
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()
        with psycopg.connect(test_database_url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def send(api, path, payload=None, key=_AUTO_KEY, auth=API_KEY, raw_body=None):
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    is_post = payload is not None or raw_body is not None
    data = raw_body
    if payload is not None:
        data = json.dumps(payload).encode()
    if is_post:
        headers["Content-Type"] = "application/json"
        if key is _AUTO_KEY:
            key = KEY_PREFIX + str(uuid.uuid4())
        if key is not None:
            headers["Idempotency-Key"] = key
    request = Request(
        api + path,
        data=data,
        headers=headers,
        method="POST" if is_post else "GET",
    )
    try:
        response = urlopen(request, timeout=4)
    except Exception as error:
        if hasattr(error, "code"):
            return error.code, json.load(error)
        raise
    with response:
        return response.status, json.load(response)


def read_job(job_id):
    with server.database() as conn:
        return conn.execute("SELECT * FROM telegram_jobs WHERE id=%s", (job_id,)).fetchone()


@pytest.mark.integration
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

    monkeypatch.setattr(server, "telegram_send", lambda text, _chat_id: server.TelegramResult(True, message_id=777))
    assert server.process_one() is True
    status, job = send(api, f"/jobs/{first['job_id']}")
    assert status == 200
    assert job["status"] == "sent"
    assert job["telegram_message_id"] == 777
    assert job["destination"] == "default"

    status, replay = send(api, "/send", payload, key)
    assert status == 200
    assert replay["status"] == "sent"


@pytest.mark.integration
def test_named_destinations_are_listed_and_persisted_for_delivery(api, monkeypatch, capsys):
    destinations = {
        "founders": {"label": "Founders group", "chat_id": "-100555111"},
        "personal": {"label": "Personal chat", "chat_id": "123456789"},
    }
    monkeypatch.setattr(server, "CHAT_ID", "")
    monkeypatch.setattr(server, "DESTINATIONS_JSON", json.dumps(destinations))

    status, choices = send(api, "/destinations")
    assert status == 200
    assert choices == {
        "destinations": [
            {"name": "founders", "label": "Founders group"},
            {"name": "personal", "label": "Personal chat"},
        ]
    }
    assert "-100555111" not in json.dumps(choices)
    assert "123456789" not in json.dumps(choices)
    assert send(api, "/destinations", auth="wrong")[0] == 401

    status, missing_choice = send(api, "/send", {"text": "choose destination"}, KEY_PREFIX + "choice-required")
    assert status == 400
    assert [item["name"] for item in missing_choice["destinations"]] == ["founders", "personal"]

    status, unknown_choice = send(
        api,
        "/send",
        {"text": "choose destination", "destination": "not-allowed"},
        KEY_PREFIX + "unknown-destination",
    )
    assert status == 400
    assert "unknown destination" in unknown_choice["error"]

    status, rejected_chat_id = send(
        api,
        "/send",
        {"text": "do not accept caller-supplied IDs", "chat_id": "-100attacker"},
        KEY_PREFIX + "caller-chat-id",
    )
    assert status == 400
    assert "chat_id cannot be supplied" in rejected_chat_id["error"]

    status, accepted = send(
        api,
        "/send",
        {"text": "founders only", "destination": "founders"},
        KEY_PREFIX + "founders-target",
    )
    assert status == 202
    assert accepted["destination"] == "founders"
    stored_job = read_job(accepted["job_id"])
    assert stored_job["destination"] == "founders"
    assert stored_job["chat_id"] == "-100555111"

    destinations["founders"]["chat_id"] = "-100555999"
    monkeypatch.setattr(server, "DESTINATIONS_JSON", json.dumps(destinations))

    sent_to = []

    def record_send(message, chat_id):
        sent_to.append((message, chat_id))
        return server.TelegramResult(True, message_id=779)

    monkeypatch.setattr(server, "telegram_send", record_send)
    assert server.process_one() is True
    assert sent_to == [("founders only", "-100555111")]
    logs = capsys.readouterr().out
    events = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    assert any(event.get("event") == "job_result" and event.get("job_id") == accepted["job_id"]
               and event.get("status") == "sent" for event in events)
    assert all(value not in logs for value in ("founders only", "-100555111", "-100555999"))

    status, conflict = send(
        api,
        "/send",
        {"text": "founders only", "destination": "personal"},
        KEY_PREFIX + "founders-target",
    )
    assert status == 409
    assert "destination" in conflict["error"]


@pytest.mark.integration
def test_concurrent_same_key_requests_create_one_job(api):
    key = KEY_PREFIX + "concurrent-event"
    payload = {"text": "one logical event"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: send(api, "/send", payload, key), range(8)))

    assert all(status == 202 for status, _ in results)
    assert len({body["job_id"] for _, body in results}) == 1
    with server.database() as conn:
        count = conn.execute(
            "SELECT count(*) AS count FROM telegram_jobs WHERE idempotency_key=%s", (key,)
        ).fetchone()["count"]
    assert count == 1


@pytest.mark.integration
def test_http_validation_and_unicode_body_limit(api, monkeypatch):
    status, _ = send(api, "/send", {"text": "not authorized"}, KEY_PREFIX + "unauth", auth="wrong")
    assert status == 401
    status, _ = send(api, f"/jobs/{99999999}", auth="wrong")
    assert status == 401

    cases = [
        (None, b"{", KEY_PREFIX + "malformed"),
        ([], None, KEY_PREFIX + "array"),
        ({}, None, KEY_PREFIX + "missing-text"),
        (None, b'{"text":"\\ud800"}', KEY_PREFIX + "unpaired-surrogate"),
        ({"text": "   "}, None, KEY_PREFIX + "blank"),
        ({"text": "valid"}, None, None),
        ({"text": "valid"}, None, "k" * 201),
        ({"text": "x" * 4097}, None, KEY_PREFIX + "too-long"),
    ]
    for payload, raw_body, key in cases:
        status, _ = send(api, "/send", payload, key=key, raw_body=raw_body)
        assert status == 400

    status, accepted = send(api, "/send", {"text": "🙂" * 4096}, KEY_PREFIX + "max-unicode")
    assert status == 202
    assert accepted["status"] == "queued"
    status, accepted = send(api, "/send", {"text": "max key"}, "k" * 200)
    assert status == 202

    status, _ = send(api, "/send", raw_body=b" " * (server.MAX_BODY_BYTES + 1), key=KEY_PREFIX + "body-too-large")
    assert status == 400
    status, missing = send(api, "/jobs/99999999")
    assert status == 404
    assert missing["error"] == "job not found"
    status, body = send(api, "/health")
    assert status == 200
    assert body == {"ok": True}

    monkeypatch.setattr(server, "API_KEY", "")
    status, _ = send(api, "/send", {"text": "fail closed"}, KEY_PREFIX + "empty-api-key")
    assert status == 401


@pytest.mark.integration
def test_database_outage_returns_service_unavailable(api, monkeypatch):
    def unavailable():
        raise psycopg.OperationalError("simulated database outage")

    monkeypatch.setattr(server, "database", unavailable)
    status, body = send(api, "/send", {"text": "temporary database failure"})
    assert status == 503
    assert "queue unavailable" in body["error"]
    status, body = send(api, "/jobs/1")
    assert status == 503
    assert "could not read" in body["error"]
    status, body = send(api, "/health")
    assert status == 503
    assert body == {"ok": False}


@pytest.mark.integration
def test_health_detects_a_stopped_worker(api, monkeypatch):
    class StoppedWorker:
        def is_alive(self):
            return False

    monkeypatch.setattr(server, "WORKER_THREAD", StoppedWorker())
    assert send(api, "/health") == (503, {"ok": False})


@pytest.mark.integration
def test_missing_persisted_chat_id_fails_without_fallback(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "target must stay pinned"})
    with server.database() as conn:
        conn.execute("UPDATE telegram_jobs SET chat_id='' WHERE id=%s", (accepted["job_id"],))
    monkeypatch.setattr(server, "CHAT_ID", "-100999999")

    assert server.process_one()
    job = read_job(accepted["job_id"])
    assert job["status"] == "failed"
    assert job["last_error"] == "Queued job has no destination chat ID"


@pytest.mark.integration
def test_permanent_delivery_failure_is_terminal_and_replay_needs_new_key(api, monkeypatch):
    key = KEY_PREFIX + "permanent-error"
    _, accepted = send(api, "/send", {"text": "invalid target"}, key)
    monkeypatch.setattr(
        server,
        "telegram_send",
        lambda _text, _chat_id: server.TelegramResult(False, error="Bad Request: chat not found"),
    )

    assert server.process_one()
    status, job = send(api, f"/jobs/{accepted['job_id']}")
    assert status == 200
    assert job["status"] == "failed"
    status, replay = send(api, "/send", {"text": "invalid target"}, key)
    assert status == 200
    assert replay["status"] == "failed"
    # A deliberate resubmission after terminal failure uses a fresh key.
    status, replacement = send(api, "/send", {"text": "invalid target"}, KEY_PREFIX + "new-attempt")
    assert status == 202
    assert replacement["job_id"] != accepted["job_id"]


@pytest.mark.integration
def test_transient_retries_and_exhaustion(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "temporary failure"})
    monkeypatch.setattr(server, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        server,
        "telegram_send",
        lambda _text, _chat_id: server.TelegramResult(False, error="Telegram HTTP 503", retryable=True),
    )

    assert server.process_one()
    job = read_job(accepted["job_id"])
    assert job["status"] == "queued"
    assert job["attempts"] == 1
    with server.database() as conn:
        conn.execute("UPDATE telegram_jobs SET next_attempt_at=now() WHERE id=%s", (accepted["job_id"],))

    assert server.process_one()
    job = read_job(accepted["job_id"])
    assert job["status"] == "failed"
    assert job["attempts"] == 2


@pytest.mark.integration
def test_rate_limit_respects_retry_after_and_logs_job_context(api, monkeypatch, capsys):
    _, accepted = send(api, "/send", {"text": "rate limited"})
    monkeypatch.setattr(
        server, "telegram_send",
        lambda _text, _chat_id: server.TelegramResult(
            False, error="Too Many Requests", retry_after=7, retryable=True, error_code=429
        ),
    )

    assert server.process_one()
    with server.database() as conn:
        job = conn.execute(
            "SELECT status, attempts, EXTRACT(EPOCH FROM next_attempt_at - now()) AS wait_seconds "
            "FROM telegram_jobs WHERE id=%s", (accepted["job_id"],)
        ).fetchone()
    assert job["status"] == "queued"
    assert job["attempts"] == 1
    assert 5 <= job["wait_seconds"] <= 7
    assert not server.process_one()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert any(event.get("event") == "job_result" and event.get("job_id") == accepted["job_id"]
               and event.get("telegram_error_code") == 429
               and event.get("retry_delay_seconds") == 7 for event in events)


@pytest.mark.integration
def test_active_claim_survives_restart_and_expired_claim_is_recovered(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "recover me"})
    job_id = accepted["job_id"]
    with server.database() as conn:
        conn.execute(
            "UPDATE telegram_jobs SET status='sending', attempts=1, claim_token=%s, "
            "lease_until=now() + interval '5 minutes' WHERE id=%s",
            (uuid.uuid4(), job_id),
        )

    server.initialize_database()
    assert not server.process_one()
    assert read_job(job_id)["status"] == "sending"

    with server.database() as conn:
        conn.execute("UPDATE telegram_jobs SET lease_until=now() - interval '1 second' WHERE id=%s", (job_id,))
    monkeypatch.setattr(server, "telegram_send", lambda _text, _chat_id: server.TelegramResult(True, message_id=888))
    assert not server.process_one()  # Expiration is recovered with a backoff.
    assert read_job(job_id)["status"] == "queued"
    with server.database() as conn:
        conn.execute("UPDATE telegram_jobs SET next_attempt_at=now() WHERE id=%s", (job_id,))
    assert server.process_one()
    assert read_job(job_id)["status"] == "sent"


@pytest.mark.integration
def test_existing_database_migrates_without_requeueing_live_jobs(test_database_url, monkeypatch):
    schema = "email_alert_migration_" + uuid.uuid4().hex
    with psycopg.connect(test_database_url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(test_database_url, options=f"-c search_path={schema}")
    monkeypatch.setattr(server, "DATABASE_URL", scoped_url)
    monkeypatch.setattr(server, "CHAT_ID", "-100987654321")
    monkeypatch.setattr(server, "DESTINATIONS_JSON", "")
    monkeypatch.setattr(server, "API_KEY", API_KEY)
    try:
        with psycopg.connect(scoped_url) as conn:
            conn.execute(
                """
                CREATE TABLE telegram_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'sending', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id BIGINT,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                "INSERT INTO telegram_jobs (idempotency_key, request_hash, text, status) "
                "VALUES (%s, %s, %s, 'sending')",
                ("legacy-key", hashlib.sha256(b"in-flight").hexdigest(), "in-flight"),
            )

        server.initialize_database()
        with psycopg.connect(scoped_url) as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='telegram_jobs'",
                    (schema,),
                ).fetchall()
            }
            job = conn.execute("SELECT status, lease_until, destination, chat_id FROM telegram_jobs").fetchone()
            conn.execute(
                "INSERT INTO telegram_jobs (idempotency_key, request_hash, text) "
                "VALUES ('old-version-insert', 'old-hash', 'deployed during rollout')"
            )
            old_insert = conn.execute(
                "SELECT destination, chat_id FROM telegram_jobs WHERE idempotency_key='old-version-insert'"
            ).fetchone()
        assert {"claim_token", "lease_until", "destination", "chat_id"} <= columns
        assert job[0] == "sending"
        assert job[1] is not None
        assert job[2] == "default"
        assert job[3] == "-100987654321"
        assert old_insert == ("default", "-100987654321")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_port}"
            status, replay = send(base, "/send", {"text": "in-flight"}, "legacy-key")
            assert status == 202
            assert replay["status"] == "sending"
            assert send(base, "/send", {"text": "changed"}, "legacy-key")[0] == 409
        finally:
            httpd.shutdown()
            thread.join(timeout=3)
            httpd.server_close()
    finally:
        with psycopg.connect(test_database_url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.integration
def test_expired_claim_at_attempt_limit_becomes_failed(api):
    _, accepted = send(api, "/send", {"text": "attempt limit"})
    with server.database() as conn:
        conn.execute(
            "UPDATE telegram_jobs SET status='sending', attempts=%s, claim_token=%s, "
            "lease_until=now() - interval '1 second' WHERE id=%s",
            (server.MAX_ATTEMPTS, uuid.uuid4(), accepted["job_id"]),
        )
    assert not server.process_one()
    job = read_job(accepted["job_id"])
    assert job["status"] == "failed"
    assert "lease expired" in job["last_error"]


@pytest.mark.integration
def test_stale_worker_cannot_overwrite_a_newer_claim(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "claim ownership"})
    newer_token = uuid.uuid4()

    def simulate_reclaim(_text, _chat_id):
        with server.database() as conn:
            conn.execute(
                "UPDATE telegram_jobs SET claim_token=%s, lease_until=now() + interval '1 minute' "
                "WHERE id=%s",
                (newer_token, accepted["job_id"]),
            )
        return server.TelegramResult(True, message_id=889)

    monkeypatch.setattr(server, "telegram_send", simulate_reclaim)
    assert server.process_one()
    job = read_job(accepted["job_id"])
    assert job["status"] == "sending"
    assert job["claim_token"] == newer_token
    assert job["telegram_message_id"] is None


@pytest.mark.integration
def test_finalization_database_error_recovers_after_lease_expiry(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "ambiguous send"})
    original_database = server.database
    calls = 0
    sends = 0

    def fail_finalization_connection():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise psycopg.OperationalError("simulated result-write outage")
        return original_database()

    def successful_send(_text, _chat_id):
        nonlocal sends
        sends += 1
        return server.TelegramResult(True, message_id=900 + sends)

    monkeypatch.setattr(server, "database", fail_finalization_connection)
    monkeypatch.setattr(server, "telegram_send", successful_send)
    with pytest.raises(psycopg.OperationalError):
        server.process_one()
    assert read_job(accepted["job_id"])["status"] == "sending"

    monkeypatch.setattr(server, "database", original_database)
    with server.database() as conn:
        conn.execute(
            "UPDATE telegram_jobs SET lease_until=now() - interval '1 second', next_attempt_at=now() "
            "WHERE id=%s",
            (accepted["job_id"],),
        )
    assert not server.process_one()  # Lease expiry schedules the safe retry delay.
    with server.database() as conn:
        conn.execute("UPDATE telegram_jobs SET next_attempt_at=now() WHERE id=%s", (accepted["job_id"],))
    assert server.process_one()
    assert read_job(accepted["job_id"])["status"] == "sent"
    assert sends == 2  # Telegram may have accepted the first send before the DB error.


@pytest.mark.integration
def test_two_workers_do_not_claim_the_same_active_job(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "one worker only"})
    started = threading.Event()
    release = threading.Event()
    sends = 0

    def slow_send(_text, _chat_id):
        nonlocal sends
        sends += 1
        started.set()
        assert release.wait(3)
        return server.TelegramResult(True, message_id=901)

    monkeypatch.setattr(server, "telegram_send", slow_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(server.process_one)
        assert started.wait(3)
        assert server.process_one() is False
        release.set()
        assert first.result(timeout=3) is True
    assert sends == 1
    assert read_job(accepted["job_id"])["status"] == "sent"


@pytest.mark.integration
def test_worker_loop_delivers_and_stops_cleanly(api, monkeypatch):
    _, accepted = send(api, "/send", {"text": "worker loop"})
    delivered = threading.Event()
    stop = threading.Event()

    def mock_send(_text, _chat_id):
        delivered.set()
        return server.TelegramResult(True, message_id=902)

    monkeypatch.setattr(server, "telegram_send", mock_send)
    thread = threading.Thread(target=server.worker, args=(stop,), daemon=True)
    thread.start()
    assert delivered.wait(3)
    deadline = time.monotonic() + 3
    while read_job(accepted["job_id"])["status"] != "sent" and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert read_job(accepted["job_id"])["status"] == "sent"
