# Email Alert Telegram Sender

An authenticated HTTP API that queues text messages in PostgreSQL and delivers them to one Telegram chat. Each client deploys a separate copy with their own Telegram bot, chat, API key, and Railway project.

## What delivery means

- `POST /send` stores the job in PostgreSQL and returns `202 Accepted` with a job ID.
- A background worker sends queued jobs and retries temporary network errors and Telegram rate limits with backoff.
- The `Idempotency-Key` header prevents the same caller retry from creating a second job. Reuse the exact same key and body when retrying a request.
- `GET /jobs/{job_id}` returns `queued`, `sending`, `sent`, or `failed`.
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
   | `TELEGRAM_CHAT_ID` | Private or group ID that should receive messages |
   | `LOCAL_API_KEY` | A new random API key for calling this service |
   | `DATABASE_URL` | Reference the PostgreSQL service's `DATABASE_URL` |

   In Railway's variable editor, reference the database variable with the service's name, for example `${{Postgres.DATABASE_URL}}` (use the exact PostgreSQL service name shown in the project). Keep all credentials in Railway Variables, never in GitHub.

5. Deploy. `railway.json` starts the app, configures `/health`, and restarts the process after failures. Railway supplies the `PORT`; the app listens on `0.0.0.0`.
6. Generate a unique caller key, for example `openssl rand -hex 32`, and save it in Railway as `LOCAL_API_KEY`. Share it only with the systems allowed to send to this client's Telegram chat.
7. Open the service's generated HTTPS domain and verify `/health` returns `{"ok": true}`. The health endpoint intentionally reports process health only; it does not expose configuration or database details.

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

1. For a private chat, open the bot and send it `/start` or another message. For a group, enable **Allow Groups** in BotFather if needed, add the bot, then send `/start@YourBotUsername` in the group.
2. On a local machine with Python 3, configure the token in a temporary environment and run `python3 get_chat_id.py` (see local setup below). The helper prints pending private and group IDs without message contents.
3. Put the intended ID in Railway's `TELEGRAM_CHAT_ID` variable.

Telegram's `getUpdates` cannot be used while a webhook is set, and another poller may consume the update first. If the existing bot serves a live integration, do not disable its webhook or stop its poller just for this helper; ask the bot owner to read `message.chat.id` from their existing update handler. See [Telegram getUpdates](https://core.telegram.org/bots/api#getupdates).

## Call the API

Send a unique idempotency key per logical message. If the caller times out or gets a network error, retry with the same key and exact same text.

```bash
curl -sS -X POST "https://YOUR-RAILWAY-DOMAIN/send" \
  -H "Authorization: Bearer YOUR_LOCAL_API_KEY" \
  -H "Idempotency-Key: email-event-12345" \
  -H "Content-Type: application/json" \
  -d '{"text":"You got mail from Vir"}'
```

Example accepted response:

```json
{"ok":true,"job_id":42,"status":"queued"}
```

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

## Operational notes

- Use a dedicated bot per client deployment unless the bot owner has explicitly approved sharing it.
- The caller API key is a bearer credential. Rotate it by changing `LOCAL_API_KEY` in Railway Variables and updating callers.
- Telegram bot tokens are credentials too. Rotate via BotFather and update Railway Variables if exposed.
- The queue retains messages and status rows. Define a retention/cleanup policy before high-volume use.
- This service is a single-process deployment. PostgreSQL job locking prevents duplicate workers from claiming the same queued job, but Telegram's send API still leaves the ambiguous-timeout duplicate case described above.
- This repo sends messages only. It does not read a mailbox or trigger on incoming email; connect an approved email automation to `POST /send` separately.
