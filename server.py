#!/usr/bin/env python3
"""Authenticated, Postgres-backed Telegram message queue."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


WORKER_POLL_SECONDS = 1
MAX_BODY_BYTES = 65_536
JOB_LEASE_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 86_400
WORKER_THREAD: threading.Thread | None = None


def log_event(event: str, *, level: str = "info", **fields: object) -> None:
    """Emit searchable JSON without message text, credentials, keys, or chat IDs."""
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        **fields,
    }, separators=(",", ":")), flush=True)


@dataclass(frozen=True)
class TelegramResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None
    retry_after: int | None = None
    retryable: bool = False
    error_code: int | None = None


@dataclass(frozen=True)
class Destination:
    name: str
    label: str
    chat_id: str


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
DESTINATIONS_JSON = os.environ.get("TELEGRAM_DESTINATIONS_JSON", "").strip()
API_KEY = os.environ.get("LOCAL_API_KEY", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "8"))


def configured_destinations() -> dict[str, Destination]:
    """Return the server-side destination allowlist, with legacy single-chat support."""
    destinations: dict[str, Destination] = {}
    if DESTINATIONS_JSON:
        try:
            raw_destinations = json.loads(DESTINATIONS_JSON)
        except json.JSONDecodeError as error:
            raise ValueError("TELEGRAM_DESTINATIONS_JSON must be valid JSON") from error
        if not isinstance(raw_destinations, dict) or not raw_destinations:
            raise ValueError("TELEGRAM_DESTINATIONS_JSON must be a non-empty JSON object")
        if len(raw_destinations) > 50:
            raise ValueError("at most 50 Telegram destinations may be configured")

        for name, value in raw_destinations.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ValueError(
                    "destination names must start with a lowercase letter and use only "
                    "lowercase letters, numbers, '_' or '-'"
                )
            if not isinstance(value, dict):
                raise ValueError(f"destination '{name}' must have a label and chat_id")
            label = value.get("label")
            raw_chat_id = value.get("chat_id")
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > 100:
                raise ValueError(
                    f"destination '{name}' must have a non-empty label of at most 100 characters"
                )
            if isinstance(raw_chat_id, bool) or not isinstance(raw_chat_id, (str, int)):
                raise ValueError(f"destination '{name}' must have a numeric chat_id")
            chat_id = str(raw_chat_id)
            if not re.fullmatch(r"-?[1-9][0-9]*", chat_id):
                raise ValueError(f"destination '{name}' must have a numeric chat_id")
            destinations[name] = Destination(name, label.strip(), chat_id)

    # Keep TELEGRAM_CHAT_ID working for existing one-chat deployments and queued jobs.
    if CHAT_ID and not re.fullmatch(r"-?[1-9][0-9]*", CHAT_ID):
        raise ValueError("TELEGRAM_CHAT_ID must be a numeric Telegram chat ID")
    if CHAT_ID and "default" in destinations and destinations["default"].chat_id != CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID and the 'default' JSON destination must use the same chat ID")
    if CHAT_ID and "default" not in destinations:
        destinations["default"] = Destination("default", "Default chat", CHAT_ID)
    return destinations


def public_destinations(destinations: dict[str, Destination]) -> list[dict[str, str]]:
    """Expose choices to authenticated callers without exposing the underlying chat IDs."""
    return [
        {"name": item.name, "label": item.label}
        for item in sorted(destinations.values(), key=lambda item: item.name)
    ]


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
                destination TEXT NOT NULL DEFAULT 'default',
                chat_id TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'sending', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                telegram_message_id BIGINT,
                last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                claim_token UUID,
                lease_until TIMESTAMPTZ
            )
            """
        )
        # Additive migration for databases created by earlier releases.
        conn.execute(
            "ALTER TABLE telegram_jobs ADD COLUMN IF NOT EXISTS "
            "destination TEXT NOT NULL DEFAULT 'default'"
        )
        conn.execute(
            "ALTER TABLE telegram_jobs ADD COLUMN IF NOT EXISTS chat_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute("ALTER TABLE telegram_jobs ADD COLUMN IF NOT EXISTS claim_token UUID")
        conn.execute("ALTER TABLE telegram_jobs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ")
        legacy_chat_id = CHAT_ID
        if not legacy_chat_id:
            default_destination = configured_destinations().get("default")
            if default_destination:
                legacy_chat_id = default_destination.chat_id
        if legacy_chat_id:
            conn.execute(
                "UPDATE telegram_jobs SET chat_id=%s "
                "WHERE chat_id=''",
                (legacy_chat_id,),
            )
            # Old app versions insert without destination columns during an
            # overlapping deployment. Keep those inserts pinned to the legacy target.
            conn.execute(
                sql.SQL("ALTER TABLE telegram_jobs ALTER COLUMN chat_id SET DEFAULT {}")
                .format(sql.Literal(legacy_chat_id))
            )
        missing_target = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM telegram_jobs "
            "WHERE status IN ('queued', 'sending') AND chat_id='') AS missing"
        ).fetchone()["missing"]
        if missing_target:
            raise ValueError(
                "queued jobs from the single-chat configuration need TELEGRAM_CHAT_ID "
                "during migration; keep it configured until those jobs drain"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS telegram_jobs_ready_idx "
            "ON telegram_jobs (next_attempt_at, id) WHERE status = 'queued'"
        )
        # Give jobs claimed by an older release a grace period. Never reset an
        # active claim during startup; overlapping Railway deployments may both
        # be serving traffic during a rollout.
        conn.execute(
            "UPDATE telegram_jobs SET lease_until=now() + (%s * interval '1 second') "
            "WHERE status='sending' AND lease_until IS NULL",
            (JOB_LEASE_SECONDS,),
        )


def _telegram_error(payload: object, http_status: int | None = None) -> TelegramResult:
    if not isinstance(payload, dict):
        retryable = http_status == 429 or (http_status is not None and 500 <= http_status <= 599)
        label = f"Telegram HTTP {http_status}" if http_status else "Telegram returned an invalid response"
        return TelegramResult(False, error=label, retryable=retryable, error_code=http_status)

    parameters = payload.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    error_code = payload.get("error_code")
    effective_code = http_status if http_status is not None else error_code
    retryable = effective_code == 429 or (
        isinstance(effective_code, int) and not isinstance(effective_code, bool)
        and 500 <= effective_code <= 599
    )
    retry_after = parameters.get("retry_after")
    if not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 0:
        retry_after = None
    if not retryable:
        retry_after = None
    elif retry_after is not None:
        retry_after = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    description = payload.get("description")
    if not isinstance(description, str) or not description:
        description = f"Telegram HTTP {http_status}" if http_status else "Telegram rejected the message"
    return TelegramResult(
        False, error=description[:500], retry_after=retry_after,
        retryable=retryable,
        error_code=effective_code if isinstance(effective_code, int) and not isinstance(effective_code, bool) else None,
    )


def telegram_send(message: str, chat_id: str | None = None) -> TelegramResult:
    destination_chat_id = CHAT_ID if chat_id is None else chat_id
    request = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": destination_chat_id, "text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return TelegramResult(False, error="Telegram returned an invalid response", retryable=True)
    except HTTPError as error:
        try:
            result = json.load(error)
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            return _telegram_error(None, error.code)
        return _telegram_error(result, error.code)
    except (URLError, TimeoutError, OSError):
        return TelegramResult(False, error="Telegram network error", retryable=True)

    if not isinstance(result, dict):
        return TelegramResult(False, error="Telegram returned an invalid response", retryable=True)
    if result.get("ok") is True:
        sent = result.get("result")
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            return TelegramResult(False, error="Telegram returned an invalid success response", retryable=True)
        return TelegramResult(True, message_id=message_id)
    if result.get("ok") is not False:
        return TelegramResult(False, error="Telegram returned an invalid response", retryable=True)

    return _telegram_error(result)


def recover_expired_jobs(conn: psycopg.Connection) -> list[dict[str, object]]:
    """Return expired claims to the queue, or fail those out of attempts."""
    result = conn.execute(
        """
        UPDATE telegram_jobs
        SET status=CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END,
            last_error='Worker lease expired; Telegram outcome may be ambiguous',
            next_attempt_at=CASE WHEN attempts >= %s THEN next_attempt_at
                ELSE now() + (LEAST(power(2, attempts), 300)::int * interval '1 second') END,
            claim_token=NULL, lease_until=NULL, updated_at=now()
        WHERE status='sending' AND lease_until <= now()
        RETURNING id, destination, status, attempts
        """,
        (MAX_ATTEMPTS, MAX_ATTEMPTS),
    )
    return result.fetchall()


def process_one() -> bool:
    claim_token = uuid.uuid4()
    with database() as conn:
        recovered = recover_expired_jobs(conn)
        job = conn.execute(
            """
            WITH candidate AS (
                SELECT id FROM telegram_jobs
                WHERE status='queued' AND next_attempt_at <= now()
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE telegram_jobs AS j
            SET status='sending', attempts=attempts+1, claim_token=%s,
                lease_until=now() + (%s * interval '1 second'), updated_at=now()
            FROM candidate WHERE j.id=candidate.id
            RETURNING j.id, j.text, j.chat_id, j.destination, j.attempts, j.claim_token
            """,
            (claim_token, JOB_LEASE_SECONDS),
        ).fetchone()
    for recovered_job in recovered:
        log_event("job_recovered", level="warn", job_id=recovered_job["id"],
                  destination=recovered_job["destination"],
                  status=recovered_job["status"], attempt=recovered_job["attempts"])
    if not job:
        return False

    log_event("job_claimed", job_id=job["id"], destination=job["destination"], attempt=job["attempts"])
    destination_chat_id = job["chat_id"]
    if not destination_chat_id:
        result = TelegramResult(False, error="Queued job has no destination chat ID")
    else:
        try:
            result = telegram_send(job["text"], destination_chat_id)
        except Exception as error:  # Unexpected sender failures get the same lease/retry path.
            result = TelegramResult(False, error=f"Telegram sender error ({type(error).__name__})", retryable=True)
    status = "sent" if result.ok else "failed"
    delay = 0
    try:
        with database() as conn:
            if result.ok:
                updated = conn.execute(
                    "UPDATE telegram_jobs SET status='sent', telegram_message_id=%s, "
                    "last_error=NULL, claim_token=NULL, lease_until=NULL, updated_at=now() "
                    "WHERE id=%s AND claim_token=%s",
                    (result.message_id, job["id"], job["claim_token"]),
                ).rowcount
            elif result.retryable:
                if job["attempts"] >= MAX_ATTEMPTS:
                    status = "failed"
                else:
                    status = "queued"
                    delay = result.retry_after if result.retry_after is not None else min(2 ** job["attempts"], 300)
                updated = conn.execute(
                    "UPDATE telegram_jobs SET status=%s, last_error=%s, "
                    "next_attempt_at=now()+(%s * interval '1 second'), claim_token=NULL, "
                    "lease_until=NULL, updated_at=now() WHERE id=%s AND claim_token=%s",
                    (status, result.error, delay, job["id"], job["claim_token"]),
                ).rowcount
            else:
                updated = conn.execute(
                    "UPDATE telegram_jobs SET status='failed', last_error=%s, claim_token=NULL, "
                    "lease_until=NULL, updated_at=now() WHERE id=%s AND claim_token=%s",
                    (result.error, job["id"], job["claim_token"]),
                ).rowcount
    except psycopg.Error as error:
        log_event("job_finalize_error", level="error", job_id=job["id"],
                  destination=job["destination"], error_type=type(error).__name__)
        raise
    if updated:
        log_event("job_result", level="info" if status == "sent" else "warn",
                  job_id=job["id"], destination=job["destination"], attempt=job["attempts"],
                  status=status, telegram_message_id=result.message_id,
                  telegram_error_code=result.error_code, retry_delay_seconds=delay)
    else:
        log_event("job_result_ignored", level="warn", job_id=job["id"],
                  destination=job["destination"], attempt=job["attempts"])
    return True


def worker(stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            if not process_one():
                stop_event.wait(WORKER_POLL_SECONDS)
        except Exception as error:  # Keep the worker alive across transient DB faults.
            log_event("worker_error", level="error", error_type=type(error).__name__)
            stop_event.wait(2)


class Handler(BaseHTTPRequestHandler):
    server_version = "EmailAlert/2.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self.request_id)
        self.end_headers()
        self.wfile.write(body)
        route = self.path if self.path in ("/health", "/destinations", "/send") else (
            "/jobs/{id}" if re.fullmatch(r"/jobs/[1-9][0-9]*", self.path) else "other"
        )
        if route != "/health" or status != 200:
            log_event("http_request", level="warn" if status >= 400 else "info",
                      request_id=self.request_id, method=self.command, route=route, http_status=status)

    def do_GET(self) -> None:
        self.request_id = uuid.uuid4().hex
        if self.path == "/health":
            if WORKER_THREAD is None or not WORKER_THREAD.is_alive():
                log_event("health_unavailable", level="error", reason="worker")
                self.send_json(503, {"ok": False})
                return
            try:
                with database() as conn:
                    conn.execute("SELECT 1")
            except psycopg.Error:
                log_event("health_unavailable", level="error", reason="database")
                self.send_json(503, {"ok": False})
                return
            self.send_json(200, {"ok": True})
        elif self.path == "/destinations":
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                choices = public_destinations(configured_destinations())
            except ValueError:
                self.send_json(503, {"error": "destination configuration unavailable"})
                return
            if not choices:
                self.send_json(503, {"error": "no Telegram destinations configured"})
                return
            self.send_json(200, {"destinations": choices})
        elif re.fullmatch(r"/jobs/[1-9][0-9]*", self.path):
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                job_id = int(self.path.removeprefix("/jobs/"))
                with database() as conn:
                    job = conn.execute(
                        "SELECT id, destination, status, attempts, telegram_message_id, last_error, created_at "
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
        self.request_id = uuid.uuid4().hex
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
            if not isinstance(payload, dict):
                raise ValueError
            message = payload.get("text")
            destination_name = payload.get("destination")
            key = self.headers.get("Idempotency-Key", "").strip()
            if not isinstance(message, str) or not message.strip() or len(message) > 4096:
                raise ValueError
            if "chat_id" in payload:
                self.send_json(400, {"error": "choose a configured destination name; chat_id cannot be supplied"})
                return
            if destination_name is not None and not isinstance(destination_name, str):
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
        try:
            destinations = configured_destinations()
        except ValueError:
            self.send_json(503, {"error": "destination configuration unavailable"})
            return
        if not destinations:
            self.send_json(503, {"error": "no Telegram destinations configured"})
            return
        if destination_name is None and len(destinations) == 1:
            destination = next(iter(destinations.values()))
        elif destination_name in destinations:
            destination = destinations[destination_name]
        else:
            error = (
                "destination is required; choose one from GET /destinations"
                if destination_name is None
                else "unknown destination; choose one from GET /destinations"
            )
            self.send_json(400, {"error": error, "destinations": public_destinations(destinations)})
            return
        try:
            message_bytes = message.encode("utf-8")
        except UnicodeEncodeError:
            self.send_json(400, {"error": "text must contain valid Unicode"})
            return
        request_identity = json.dumps(
            {"destination": destination.name, "chat_id": destination.chat_id, "text": message},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(request_identity).hexdigest()
        try:
            with database() as conn:
                job = conn.execute(
                    "INSERT INTO telegram_jobs "
                    "(idempotency_key, request_hash, destination, chat_id, text) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING id, status, request_hash, destination",
                    (key, digest, destination.name, destination.chat_id, message),
                ).fetchone()
                created = job is not None
                if not job:
                    job = conn.execute(
                        "SELECT id, status, request_hash, destination, chat_id "
                        "FROM telegram_jobs WHERE idempotency_key=%s",
                        (key,),
                    ).fetchone()
                    legacy_replay = (
                        destination.name == "default" and job["destination"] == "default"
                        and job["chat_id"] == destination.chat_id
                        and job["request_hash"] == hashlib.sha256(message_bytes).hexdigest()
                    )
                    if job["request_hash"] != digest and not legacy_replay:
                        self.send_json(409, {"error": "Idempotency-Key was already used for different text or destination"})
                        return
            status = 202 if job["status"] in ("queued", "sending") else 200
            log_event("job_accepted" if created else "job_replayed", request_id=self.request_id,
                      job_id=job["id"], destination=job["destination"], status=job["status"])
            self.send_json(status, {
                "ok": True,
                "job_id": job["id"],
                "status": job["status"],
                "destination": job["destination"],
            })
        except psycopg.Error:
            self.send_json(503, {"error": "queue unavailable; retry with the same Idempotency-Key"})

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    global WORKER_THREAD
    try:
        destinations = configured_destinations()
    except ValueError as error:
        raise SystemExit(f"Invalid Telegram destination configuration: {error}") from error
    if not all((DATABASE_URL, BOT_TOKEN, API_KEY)) or not destinations:
        raise SystemExit(
            "Set DATABASE_URL, TELEGRAM_BOT_TOKEN, LOCAL_API_KEY, and either "
            "TELEGRAM_CHAT_ID or TELEGRAM_DESTINATIONS_JSON."
        )
    if not 1 <= MAX_ATTEMPTS <= 100:
        raise SystemExit("MAX_ATTEMPTS must be between 1 and 100")

    try:
        initialize_database()
    except ValueError as error:
        raise SystemExit(f"Database migration blocked: {error}") from error
    except psycopg.Error as error:
        raise SystemExit(f"Database initialization failed: {type(error).__name__}") from error

    WORKER_THREAD = threading.Thread(target=worker, name="telegram-queue-worker", daemon=True)
    WORKER_THREAD.start()
    log_event("service_started", host=HOST, port=PORT, destination_count=len(destinations))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
