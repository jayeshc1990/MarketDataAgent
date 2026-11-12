"""Send messages to Telegram through a bot (Bot API over HTTPS, no extra packages).

Setup:
  1. In Telegram, message @BotFather -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN.
  2. Send any message to your new bot (or add it to a group and post there).
  3. python -m market_agent.telegram --find-chat     # prints the chat id -> .env TELEGRAM_CHAT_ID
  4. python -m market_agent.telegram --test          # sends a test message
"""
import argparse
import os
import sys

import requests

from market_agent.config import PROJECT_ROOT, load_env

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 15


class TelegramError(RuntimeError):
    pass


def _settings():
    load_env(PROJECT_ROOT / ".env")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat


def configured() -> bool:
    return all(_settings())


def _call(token: str, method: str, **params):
    try:
        r = requests.post(API.format(token=token, method=method), json=params, timeout=TIMEOUT)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        # the URL contains the token, and requests puts it in its error messages
        raise TelegramError(f"{method}: {type(e).__name__}: {str(e).replace(token, '<token>')}") from None
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
    return data["result"]


def send(text: str) -> None:
    """Sends `text` (Telegram HTML: <b>, <i>, <code>) to TELEGRAM_CHAT_ID; raises TelegramError on failure."""
    token, chat = _settings()
    if not (token and chat):
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    _call(token, "sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)


def bot_username() -> str:
    """The bot's @username; raises TelegramError if the token is wrong."""
    token, _ = _settings()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing in .env")
    return "@" + _call(token, "getMe")["username"]


def find_chats() -> list:
    """(chat id, name) of chats that recently messaged the bot."""
    token, _ = _settings()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing in .env")
    seen = {}
    for u in _call(token, "getUpdates"):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    return list(seen.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find-chat", action="store_true", help="list chats that messaged the bot")
    ap.add_argument("--chat-ids", action="store_true", help="only the chat ids, one per line (for scripts)")
    ap.add_argument("--whoami", action="store_true", help="check the token: print the bot's @username")
    ap.add_argument("--test", action="store_true", help="send a test message")
    a = ap.parse_args()
    try:
        if a.whoami:
            print(bot_username())
        elif a.chat_ids:
            for cid, _ in find_chats():
                print(cid)
        elif a.find_chat:
            chats = find_chats()
            if not chats:
                print("No messages yet: send any message to the bot in Telegram, then run this again.")
            for cid, name in chats:
                print(f"TELEGRAM_CHAT_ID={cid}    ({name})")
        elif a.test:
            send("✅ MarketDataAgent: Telegram alerts are working.")
            print("Sent.")
        else:
            ap.print_help()
    except TelegramError as e:
        sys.exit(f"Telegram error: {e}")


if __name__ == "__main__":
    main()
