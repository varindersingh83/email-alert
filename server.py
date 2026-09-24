#!/usr/bin/env python3
"""Authenticated, Postgres-backed Telegram message queue."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row


WORKER_POLL_SECONDS = 1
MAX_BODY_BYTES = 16_384


def load_dotenv() -> None:
    """Load local KEY=value settings without overriding the host environment."""
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY = os.environ.get("LOCAL_API_KEY", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "8"))


def database() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=dict_row)


def initialize_database() -> None:
    with database() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_jobs (
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
            "CREATE INDEX IF NOT EXISTS telegram_jobs_ready_idx "
            "ON telegram_jobs (next_attempt_at, id) WHERE status = 'queued'"
        )
        # A crash during delivery is ambiguous: Telegram may have accepted a send
        # before the process died. Requeue for at-least-once delivery and expose
        # this limitation in the API/docs.
        conn.execute(
            "UPDATE telegram_jobs SET status='queued', updated_at=now() WHERE status='sending'"
        )


def telegram_send(message: str) -> tuple[bool, int | None, str | None, int | None]:
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
        except (json.JSONDecodeError, AttributeError):
            return False, None, "Telegram HTTP error", error.code
    except (URLError, TimeoutError, OSError):
        return False, None, "Telegram network error", None

    if result.get("ok"):
        sent = result.get("result", {})
        return True, sent.get("message_id"), None, None

    parameters = result.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    description = str(result.get("description", "Telegram rejected the message"))[:500]
    return False, None, description, int(retry_after) if isinstance(retry_after, int) else None


def process_one() -> bool:
    with database() as conn:
        job = conn.execute(
            """
            WITH candidate AS (
                SELECT id FROM telegram_jobs
                WHERE status='queued' AND next_attempt_at <= now()
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE telegram_jobs AS j
            SET status='sending', attempts=attempts+1, updated_at=now()
            FROM candidate WHERE j.id=candidate.id
            RETURNING j.id, j.text, j.attempts
            """
        ).fetchone()
    if not job:
        return False

    ok, message_id, error, retry_after = telegram_send(job["text"])
    with database() as conn:
        if ok:
            conn.execute(
                "UPDATE telegram_jobs SET status='sent', telegram_message_id=%s, "
                "last_error=NULL, updated_at=now() WHERE id=%s",
                (message_id, job["id"]),
            )
        elif retry_after is not None or error == "Telegram network error" or error == "Telegram HTTP error":
            if job["attempts"] >= MAX_ATTEMPTS:
                status, delay = "failed", 0
            else:
                status = "queued"
                delay = retry_after if retry_after is not None else min(2 ** job["attempts"], 300)
            conn.execute(
                "UPDATE telegram_jobs SET status=%s, last_error=%s, "
                "next_attempt_at=now()+(%s * interval '1 second'), updated_at=now() WHERE id=%s",
                (status, error, delay, job["id"]),
            )
        else:
            conn.execute(
                "UPDATE telegram_jobs SET status='failed', last_error=%s, updated_at=now() WHERE id=%s",
                (error, job["id"]),
            )
    return True


def worker() -> None:
    while True:
        try:
            if not process_one():
                time.sleep(WORKER_POLL_SECONDS)
        except Exception as error:  # Keep the worker alive across transient DB faults.
            print(f"Queue worker error: {type(error).__name__}", flush=True)
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    server_version = "EmailAlert/2.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True})
        elif re.fullmatch(r"/jobs/[1-9][0-9]*", self.path):
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                job_id = int(self.path.removeprefix("/jobs/"))
                with database() as conn:
                    job = conn.execute(
                        "SELECT id, status, attempts, telegram_message_id, last_error, created_at "
                        "FROM telegram_jobs WHERE id=%s",
                        (job_id,),
                    ).fetchone()
                if job:
                    job["created_at"] = job["created_at"].isoformat()
                    self.send_json(200, job)
                else:
                    self.send_json(404, {"error": "job not found"})
            except psycopg.Error:
                self.send_json(503, {"error": "could not read job status"})
        else:
            self.send_json(404, {"error": "not found"})

    def authorized(self) -> bool:
        provided = self.headers.get("Authorization", "")
        return bool(API_KEY) and hmac.compare_digest(provided, f"Bearer {API_KEY}")

    def do_POST(self) -> None:
        if self.path != "/send":
            self.send_json(404, {"error": "not found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BODY_BYTES:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            message = payload.get("text") if isinstance(payload, dict) else None
            key = self.headers.get("Idempotency-Key", "").strip()
            if not isinstance(message, str) or not message.strip() or len(message) > 4096:
                raise ValueError
            if not 1 <= len(key) <= 200:
                raise ValueError
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "send JSON with non-empty text (max 4096 chars) and an Idempotency-Key header"})
            return

        message = message.strip()
        if not message:
            self.send_json(400, {"error": "text must contain a non-whitespace character"})
            return
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        try:
            with database() as conn:
                job = conn.execute(
                    "INSERT INTO telegram_jobs (idempotency_key, request_hash, text) "
                    "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING id, status, request_hash",
                    (key, digest, message),
                ).fetchone()
                if not job:
                    job = conn.execute(
                        "SELECT id, status, request_hash FROM telegram_jobs WHERE idempotency_key=%s",
                        (key,),
                    ).fetchone()
                    if job["request_hash"] != digest:
                        self.send_json(409, {"error": "Idempotency-Key was already used for different text"})
                        return
            self.send_json(202, {"ok": True, "job_id": job["id"], "status": job["status"]})
        except psycopg.Error:
            self.send_json(503, {"error": "queue unavailable; retry with the same Idempotency-Key"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


if not all((DATABASE_URL, BOT_TOKEN, CHAT_ID, API_KEY)):
    raise SystemExit("Set DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and LOCAL_API_KEY.")

try:
    initialize_database()
except psycopg.Error as error:
    raise SystemExit(f"Database initialization failed: {type(error).__name__}") from error

threading.Thread(target=worker, name="telegram-queue-worker", daemon=True).start()
print(f"Listening on {HOST}:{PORT}", flush=True)
ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
