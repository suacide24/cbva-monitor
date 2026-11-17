#!/usr/bin/env python3
"""
Telegram setup helper for the CBVA Monitor.

  1. In Telegram, message @BotFather → /newbot → copy the bot token.
  2. Open your new bot and press Start (or send it any message).
  3. Run:  TELEGRAM_BOT_TOKEN='...' python scripts/telegram_setup.py
     It lists everyone who has messaged the bot, with their chat id.
  4. Optional test message:
           TELEGRAM_BOT_TOKEN='...' python scripts/telegram_setup.py --test CHAT_ID

The chat id goes in users.json as "telegram_chat_id"; the token goes in the
repo's Actions secrets as TELEGRAM_BOT_TOKEN. The token is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(token: str, method: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        sys.exit(f"Telegram {method} failed: HTTP {e.code} {desc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="CHAT_ID", help="send a test message to this chat id")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in the environment first.")

    me = call(token, "getMe").get("result", {})
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")

    if args.test:
        call(token, "sendMessage", {
            "chat_id": args.test, "parse_mode": "HTML",
            "text": "🏐 <b>CBVA Monitor</b> is connected. Notifications will arrive here.",
        })
        print(f"Test message sent to {args.test}.")
        return

    chats: dict[int, str] = {}
    for upd in call(token, "getUpdates").get("result", []):
        chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat") or {}
        if chat.get("id") is not None:
            name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                   or chat.get("title") or chat.get("username") or "?"
            chats[chat["id"]] = f"{name} ({chat.get('type')})"
    if not chats:
        print("No chats yet — open the bot in Telegram, press Start, then run this again.")
        return
    print("Chats that have messaged the bot:")
    for cid, label in chats.items():
        print(f"  chat id {cid}  —  {label}")


if __name__ == "__main__":
    main()
