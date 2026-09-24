# Email Alert Telegram Sender

A small Python HTTP service that sends a text message to one configured Telegram chat.
It uses only Python's standard library. Each person configures their own bot token,
chat ID, and sender API key in a local `.env` file.

## Get a Telegram bot token

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token BotFather returns into `TELEGRAM_BOT_TOKEN` in `.env`.
4. Keep the token private. Never commit `.env` or paste the token into a public issue.

## Get a group chat ID

1. In BotFather, open the bot settings and enable **Allow Groups** if it is disabled.
2. Add the bot to the Telegram group.
3. In the group, send `/start@YourBotUsername` (replace it with the bot's actual username).
4. Run `python3 get_chat_id.py`. It prints the group ID and title without printing message contents.
5. Put the numeric ID into `TELEGRAM_CHAT_ID` in `.env`.

The bot must be in the group and receive a group update before the ID appears. This helper
only reads pending Telegram updates; if another integration consumes them first, send a new
command in the group and retry.

## Configure

Clone the repository, copy `.env.example` to `.env`, and set these values. Keep `.env` local;
it is ignored by Git.

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
LOCAL_API_KEY=
HOST=127.0.0.1
PORT=8080
```

Generate a separate sender API key with `openssl rand -hex 32` and put the output in
`LOCAL_API_KEY`. This is not the Telegram token.

## Run locally

```bash
cd "/Users/varindernagra/Documents/GitHub/email alert"
python3 server.py
```

In another Terminal window, send a message through the local server:

```bash
curl -sS -X POST "http://127.0.0.1:8080/send" \
  -H "Authorization: Bearer ${LOCAL_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"text":"hello world"}'
```

Before running the request in that second window, load the local settings with
`set -a; source .env; set +a`.

The service binds to localhost by default and accepts only authenticated send requests.
For a hosted deployment, set `HOST=0.0.0.0`, configure the same secrets in the host's
secret manager, and place the public endpoint behind HTTPS. Do not commit `.env` or put
either key in source control.
