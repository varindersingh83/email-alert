# Email Alert Telegram Sender

A small Python HTTP service that sends a text message to one configured Telegram chat.
It uses only Python's standard library. Each person configures their own bot token,
chat ID, and sender API key in a local `.env` file.

## Get a Telegram bot token

### Create a new bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token BotFather returns into `TELEGRAM_BOT_TOKEN` in `.env`.

### Reuse a bot you already manage

1. Open [@BotFather](https://t.me/BotFather), send `/mybots`, and select the bot you own.
2. Open its **API Token** option and copy the token into `TELEGRAM_BOT_TOKEN` in `.env`.
3. If you lost the token or it was exposed, use BotFather's `/token` flow to issue a replacement,
   then update every app that uses the old token.

You cannot retrieve another person's bot token from its username or Telegram profile. Ask its
owner to configure the integration or create a bot of your own. A bot token lets its holder
control that bot; keep it private, never commit `.env`, and never paste the token into a public issue.

## Get a private or group chat ID

1. For a private chat, open the bot and send it `/start` or another message. For a group, enable
   **Allow Groups** in BotFather if needed, add the bot, then send `/start@YourBotUsername` there.
2. Run `python3 get_chat_id.py`. It prints pending private and group chat IDs without message contents.
3. Copy the ID for the intended private chat or group into `TELEGRAM_CHAT_ID` in `.env`.

The bot must receive a message in the intended chat before its ID appears. The helper reads
pending updates through Telegram's `getUpdates` API. Telegram does not allow `getUpdates` while a
webhook is set, and another polling integration may consume updates first. If this bot already
serves a live app, do not disable its webhook or stop its poller just to run this helper; ask the
bot's owner to read `message.chat.id` from the existing webhook/polling handler instead. See the
[Telegram Bot API update docs](https://core.telegram.org/bots/api#getupdates).

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
`LOCAL_API_KEY`. This key is for this local HTTP service; it is not issued by Telegram and is
different from `TELEGRAM_BOT_TOKEN`.

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
