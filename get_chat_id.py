#!/usr/bin/env python3
"""Print Telegram group chat IDs seen by the configured bot, without message text."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


for raw_line in Path(__file__).with_name(".env").read_text().splitlines():
    line = raw_line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not token:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")

try:
    with urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as response:
        payload = json.load(response)
except HTTPError as error:
    if error.code == 401:
        raise SystemExit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN in .env") from None
    raise SystemExit(f"Telegram returned HTTP {error.code}") from None
except URLError as error:
    raise SystemExit(f"Could not reach Telegram: {error.reason}") from None

chats = {}
for update in payload.get("result", []):
    message = next(
        (update[key] for key in ("message", "edited_message", "channel_post", "edited_channel_post") if key in update),
        {},
    )
    chat = message.get("chat", {})
    if chat.get("type") in ("private", "group", "supergroup"):
        label = chat.get("title", "private chat" if chat["type"] == "private" else "untitled group")
        chats[chat["id"]] = (chat["type"], label)

if not chats:
    raise SystemExit(
        "No private or group updates found. Message the bot privately or add it to a group "
        "and send /start@YourBotUsername there, then run this command again."
    )

for chat_id, (chat_type, title) in chats.items():
    print(f"{chat_id}\t{chat_type}\t{title}")
