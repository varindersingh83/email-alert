#!/usr/bin/env python3
"""List chat IDs visible in pending Telegram Bot API updates, without message text."""

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def load_local_env() -> None:
    """Load .env values without overriding values already exported in the shell."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print chat IDs from Telegram updates without printing message contents."
    )
    parser.add_argument(
        "--groups-only",
        action="store_true",
        help="show only group and supergroup chats (recommended when configuring a group)",
    )
    args = parser.parse_args()

    load_local_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing; add it to .env or export it first.")

    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise SystemExit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.") from None
        if error.code == 409:
            raise SystemExit(
                "Telegram cannot return getUpdates while a webhook or another poller is active. "
                "For a live bot, read message.chat.id from its existing update handler instead."
            ) from None
        raise SystemExit(f"Telegram returned HTTP {error.code}") from None
    except URLError as error:
        raise SystemExit(f"Could not reach Telegram: {error.reason}") from None

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        code = payload.get("error_code") if isinstance(payload, dict) else None
        if code == 401:
            raise SystemExit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.")
        if code == 409:
            raise SystemExit(
                "Telegram cannot return getUpdates while a webhook or another poller is active. "
                "For a live bot, read message.chat.id from its existing update handler instead."
            )
        raise SystemExit("Telegram did not return updates successfully.")

    chats = {}
    updates = payload.get("result", [])
    if not isinstance(updates, list):
        updates = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = next(
            (
                update[key]
                for key in ("message", "edited_message", "channel_post", "edited_channel_post")
                if isinstance(update.get(key), dict)
            ),
            {},
        )
        chat = message.get("chat", {})
        chat_type = chat.get("type")
        if chat_type not in ("private", "group", "supergroup"):
            continue
        if args.groups_only and chat_type not in ("group", "supergroup"):
            continue
        label = chat.get("title") or ("private chat" if chat_type == "private" else "untitled group")
        chats[chat["id"]] = (chat_type, label)

    if not chats:
        if args.groups_only:
            raise SystemExit(
                "No group updates are available to this bot. In the group, send a command "
                "addressed to this bot, such as /start@YourBotUsername (replace the placeholder "
                "with the actual bot username), then run this command again. If another service "
                "already consumes the bot's updates, get the ID from that service's update handler."
            )
        raise SystemExit(
            "No private or group updates are available. Message the bot privately or add it "
            "to a group and send /start@YourBotUsername there, then run this command again."
        )

    print("CHAT_ID\tTYPE\tTITLE")
    for chat_id, (chat_type, title) in sorted(
        chats.items(), key=lambda item: (item[1][0] == "private", item[1][1].casefold())
    ):
        print(f"{chat_id}\t{chat_type}\t{title}")


if __name__ == "__main__":
    main()
