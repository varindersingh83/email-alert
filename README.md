# Email Alert Telegram Sender

An authenticated HTTP API that queues text messages in PostgreSQL and delivers them to an allowlisted Telegram chat selected by name. Each client deploys a separate copy with their own Telegram bot, destinations, API key, and Railway project.

## What delivery means

- `POST /send` stores the job in PostgreSQL and returns `202 Accepted` with a job ID.
- A background worker sends queued jobs and retries temporary network errors and Telegram rate limits with backoff.
- The `Idempotency-Key` header prevents the same caller retry from creating a second job. Reuse the exact same key and body when retrying a request.
- `GET /jobs/{job_id}` returns `queued`, `sending`, `sent`, or `failed`.
- Authenticated `GET /destinations` lists the configured destination names and labels without exposing chat IDs.
- Replaying the same key and body returns the existing job. Once it is `sent` or `failed`, the API returns `200` with that terminal status; a deliberate new send after failure needs a new key.
- Telegram does not support an idempotency key for `sendMessage`. If a connection drops after Telegram accepts the message but before this service records the result, a retry can produce a duplicate. This is durable at-least-once delivery, not a guarantee of exactly-once delivery.

Messages and idempotency keys are stored in PostgreSQL. Clients should send only the data they want Telegram to receive. The service does not log message bodies or credentials. Protect database access and set an appropriate data-retention policy for your client.

## Deploy a client copy to Railway

1. Fork this GitHub repository into the client's GitHub account (or use **Deploy from GitHub Repo** in Railway with access to the repository).
2. In Railway, create a project from the fork.
3. Add a Railway PostgreSQL service to the project. Railway provides its private `DATABASE_URL` variable.
4. In the app service's **Variables**, set:

   | Variable | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | Token for the client's bot from BotFather |
   | `TELEGRAM_DESTINATIONS_JSON` | Server-side JSON allowlist mapping destination names to labels and private/group chat IDs |
   | `TELEGRAM_CHAT_ID` | Optional legacy single-chat setting; exposed as the `default` destination |
   | `LOCAL_API_KEY` | A new random API key for calling this service |
   | `DATABASE_URL` | Reference the PostgreSQL service's `DATABASE_URL` |

   In Railway's variable editor, reference the database variable with the service's name, for example `${{Postgres.DATABASE_URL}}` (use the exact PostgreSQL service name shown in the project). Keep all credentials in Railway Variables, never in GitHub. For multiple named chats, set `TELEGRAM_DESTINATIONS_JSON` to a JSON object like `{"founders":{"label":"Founders group","chat_id":"-1001234567890"},"personal":{"label":"Personal chat","chat_id":"123456789"}}`; replace the sample IDs with the exact IDs from `get_chat_id.py`. For a single-chat deployment, `TELEGRAM_CHAT_ID` remains supported as `default`.

   If upgrading a live single-chat deployment, keep its old `TELEGRAM_CHAT_ID` set through the first rollout so already-queued jobs and any overlapping old app process stay pointed at the original chat. It will appear as the `default` option until you remove it. Drain the queue before removing it.

5. Deploy. `railway.json` starts the app, configures `/health`, and restarts the process after failures. Railway supplies the `PORT`; the app listens on `0.0.0.0`.
6. Generate a unique caller key, for example `openssl rand -hex 32`, and save it in Railway as `LOCAL_API_KEY`. Share it only with the systems allowed to send to this client's Telegram chat.
7. Generate an HTTPS domain for the app service if it does not already have one. Open `/health` and verify `{"ok": true}`. This check confirms the worker is running and PostgreSQL answers a query. It returns `503` with `{"ok": false}` if either is unavailable, without publishing internal details.

Railway health checks gate new deployments; they are not ongoing monitoring. Set up Railway alerts/log monitoring for service and database failures, and review database backups/retention for the client's needs. See [Railway healthchecks](https://docs.railway.com/deployments/healthchecks), [Railway PostgreSQL](https://docs.railway.com/databases/postgresql), and [Railway variables](https://docs.railway.com/variables).

## Create or reuse a Telegram bot

### Create a bot

1. Open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy its token directly into Railway's `TELEGRAM_BOT_TOKEN` variable.

### Reuse a bot you already manage

1. Open [@BotFather](https://t.me/BotFather), send `/mybots`, and select your bot.
2. Open **API Token** and copy the token into Railway's `TELEGRAM_BOT_TOKEN` variable.
3. If the token was lost or exposed, use BotFather's `/token` flow to issue a replacement, then update every application that uses the old token.

You cannot retrieve another person's bot token from its username or Telegram profile. Ask its owner to configure the integration or create a bot of your own. A bot token lets its holder control the bot; keep it private and never commit it.

### Get a private or group chat ID

Use the same bot whose token you will configure in Railway. A bot must be in the destination group to send there.

1. For a private destination, open the bot and send `/start` or another message.
2. For a group destination, add the bot to the group and send `/start@YourBotUsername` in that group. Replace `YourBotUsername` with the bot's actual username. Addressing the command to the bot works with Telegram's default privacy mode; you do not need to disable privacy or make the bot an admin just to find the ID. If Telegram will not let you add the bot, use BotFather's `/setjoingroups` setting. See [Telegram privacy mode](https://core.telegram.org/bots/features#privacy-mode).
3. On a local machine with Python 3, put the bot token in this repository's ignored `.env` file as `TELEGRAM_BOT_TOKEN=...`, then run:

   ```bash
   python3 get_chat_id.py --groups-only
   ```

   Use `python3 get_chat_id.py` to list both private and group chats. The helper reads `.env` (or an already-exported `TELEGRAM_BOT_TOKEN`) and prints only chat IDs, type, and title; it never prints message contents or the token. Match the **group title** and confirm the type is `group` or `supergroup`. A `private` row is your direct bot chat, not the group. Do not substitute your private chat ID or try to derive the group ID from it.
4. Copy the matching chat ID into the destination configuration. For a single chat, set `TELEGRAM_CHAT_ID`. For named choices, add an entry to `TELEGRAM_DESTINATIONS_JSON`, using a lowercase name, a display label, and the exact ID. Chat IDs are not credentials, but keep the bot token private.

If the helper reports no group updates, send the addressed `/start@YourBotUsername` command in the group and run it again. It can only show chats present in updates Telegram still makes available to the bot.

`getUpdates` is incompatible with a configured webhook, and it can conflict with an existing poller. If this bot already powers a live integration, do not change its webhook or stop its poller to use this helper. Ask that integration's owner to read `message.chat.id` and `message.chat.title` from an update it already receives. See [Telegram getUpdates](https://core.telegram.org/bots/api#getupdates) and [getWebhookInfo](https://core.telegram.org/bots/api#getwebhookinfo).

## Call the API

First, let an authenticated caller retrieve the choices it may select:

```bash
curl -sS "https://YOUR-RAILWAY-DOMAIN/destinations" \
  -H "Authorization: Bearer YOUR_LOCAL_API_KEY"
```

The response contains destination names and labels, not chat IDs. Send the chosen name with the message. When exactly one destination is configured, `destination` may be omitted for compatibility; with multiple destinations, it is required. The server rejects caller-supplied `chat_id` values and resolves the name against its allowlist.

Send a unique idempotency key per logical message and destination. If the caller times out or gets a network error, retry with the same key and exact same body, including the destination name.

```bash
curl -sS -X POST "https://YOUR-RAILWAY-DOMAIN/send" \
  -H "Authorization: Bearer YOUR_LOCAL_API_KEY" \
  -H "Idempotency-Key: email-event-12345" \
  -H "Content-Type: application/json" \
  -d '{"text":"You got mail from Vir","destination":"founders"}'
```

Example accepted response:

```json
{"ok":true,"job_id":42,"status":"queued","destination":"founders"}
```

Every API response includes an `X-Request-ID` header. Keep this ID and the `job_id` when reporting a delivery problem; the Railway logs use both for correlation. Logs include destination **names**, statuses, attempts, retry delays, Telegram error codes, and Telegram message IDs. They omit message text, bot tokens, caller keys, idempotency keys, and raw chat IDs.

Check delivery status:

```bash
curl -sS "https://YOUR-RAILWAY-DOMAIN/jobs/42" \
  -H "Authorization: Bearer YOUR_LOCAL_API_KEY"
```

## Run locally

Requires Python 3 and the `psycopg` dependency. Create a local PostgreSQL database and set the values from `.env.example` in `.env`; keep `.env` untracked. Then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

To call it locally, use `http://127.0.0.1:8080` and a new idempotency key. For a local chat ID, ensure Telegram token is in your environment and run `python3 get_chat_id.py`.

## Local test layers

Install the development dependencies in the virtual environment above:

```bash
pip install -r requirements-dev.txt
```

### Unit tests (mock data only)

```bash
pytest -m "not integration and not telegram_e2e"
```

These cover Telegram request construction and success, rate-limit, and network-error responses. They do not need credentials, PostgreSQL, or network access.

### Integration tests (PostgreSQL plus mocked Telegram)

Create a **dedicated local database named exactly `email_alert_test`**. The integration suite connects only to localhost or a local Unix socket, creates a unique schema for each test, and drops only that schema. It does not truncate shared tables. Then set `TEST_DATABASE_URL` and run:

```bash
createdb email_alert_test
export TEST_DATABASE_URL="postgresql://localhost/email_alert_test"
pytest -m integration
```

These tests run the actual HTTP handler and queue against PostgreSQL while replacing the Telegram API with a fail-closed mock. They never send a Telegram message. The fixture refuses remote hosts and any database name other than `email_alert_test` before connecting.

### Local end-to-end test (real Telegram message)

Start the local app with the intended local `.env` values, then, in another shell, explicitly opt in:

```bash
set -a; source .env; set +a
RUN_TELEGRAM_E2E=1 E2E_BASE_URL=http://127.0.0.1:8080 pytest -m telegram_e2e
```

This sends a clearly labeled `[Email Alert E2E TEST]` message to the configured destination, waits for the job to become `sent`, and verifies Telegram returned a message ID. Set `E2E_DESTINATION` when multiple destinations are configured. It is skipped by default. Check the target chat before opting in. Do not run it against a production chat unless you intend to post a test message there.

The same opt-in test serves as the Railway SIT smoke test. It checks health, authentication, available destinations, rejected chat IDs and unknown destinations, idempotency, and one real Telegram delivery. For a remote HTTPS service, also set `E2E_EXPECTED_HOST` to the exact host portion of `E2E_BASE_URL`; this pins where the caller bearer key is sent. For example:

```bash
RUN_TELEGRAM_E2E=1 \
E2E_BASE_URL=https://your-service.up.railway.app \
E2E_EXPECTED_HOST=your-service.up.railway.app \
E2E_DESTINATION=default \
pytest -m telegram_e2e -s
```

Set `LOCAL_API_KEY` in the local shell to the key configured on that Railway app before running. Set `E2E_DESTINATION` to the name of the intended test chat. The test prints a run ID, job ID, request ID, destination name, and Telegram message ID for log lookup and visual comparison. Redirects are rejected. The test sends one real Telegram message, so verify the configured test chat before opting in.

GitHub Actions runs the unit and local PostgreSQL integration suites on every push and pull request. The Telegram test requires the explicit opt-in above and is excluded from CI.

## Railway logs and troubleshooting

In a terminal linked to the correct Railway project and SIT environment, read recent app logs with:

```bash
railway logs --service email-alert --environment SIT --since 30m --lines 200 --json
```

Find the `job_id` printed by SIT in `job_accepted`, `job_claimed`, and `job_result` events. `status=sent` plus `telegram_message_id` means Telegram accepted the send. A `queued` result with `retry_delay_seconds` is waiting for another attempt. A `failed` result is terminal; check authenticated `GET /jobs/{job_id}` for `last_error`, fix the cause, then use a fresh idempotency key for a deliberate retry.

For a `503` on `/health`, look for `health_unavailable` with `reason=worker` or `reason=database` and check the app and PostgreSQL services in Railway. For a `503` on `/send` or `/jobs/{id}`, check `DATABASE_URL` and database availability. `job_finalize_error` means Telegram may have accepted a message before the database write failed; a later `job_recovered` event shows the lease being retried or failed. Inspect the destination chat before resending manually because this path can produce a duplicate. `worker_error` records the exception type without exposing connection strings.

The configured destination is stored with each queued job. Editing the allowlist later does not reroute an already-queued message. A blank stored chat ID fails the job instead of sending it to another chat.

## Operational notes

- Use a dedicated bot per client deployment unless the bot owner has explicitly approved sharing it.
- The caller API key is a bearer credential. Rotate it by changing `LOCAL_API_KEY` in Railway Variables and updating callers.
- Telegram bot tokens are credentials too. Rotate via BotFather and update Railway Variables if exposed.
- The queue retains messages and status rows. Define a retention/cleanup policy before high-volume use.
- This service is a single-process deployment. PostgreSQL job locking prevents duplicate workers from claiming the same queued job, but Telegram's send API still leaves the ambiguous-timeout duplicate case described above.
- This repo sends messages only. It does not read a mailbox or trigger on incoming email; connect an approved email automation to `POST /send` separately.

## Manual SIT checklist

Use a test bot and chat that you control. Record the Railway deployment ID, destination name, job ID, request ID, Telegram message ID, and the visible result in Telegram.

1. Open `/health` on the SIT domain. Confirm HTTP `200` and `{"ok":true}`.
2. Call `/destinations` without a bearer key and then with a wrong key. Confirm both return `401`; the correct key lists only names and labels.
3. Confirm the destination name points to the intended private chat or group. For a group, verify the bot is a member and can post there.
4. Run the Railway SIT command above. Confirm exactly one labeled test message appears in the selected Telegram chat and its text matches the unique run ID.
5. Reuse the same `Idempotency-Key` and exact body. Confirm the same job ID returns and no second Telegram message appears. Change the body while keeping the key and confirm `409`.
6. Try an unknown destination and a caller-supplied `chat_id`. Confirm `400` and no message in any chat.
7. Find the job ID in Railway logs. Confirm `job_accepted`, `job_claimed`, and `job_result` connect to the same ID, and confirm no token, API key, raw chat ID, or message text appears in the app logs.
8. In an isolated SIT chat, remove the bot's permission to post and send a new test job. Confirm it becomes `failed` with a useful `last_error`; restore permission before repeating with a new key.
9. In an isolated SIT project, stop PostgreSQL and confirm `/health` returns `503`. Restore it, confirm `/health` returns `200`, and confirm queued jobs resume. Do not run this outage check in a client production project.
