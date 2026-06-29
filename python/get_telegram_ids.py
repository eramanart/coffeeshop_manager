"""
get_telegram_ids.py — extract group ID and topic thread IDs from bot updates

Usage:
  1. Add the bot to your Telegram group and make it Admin
  2. Send one message in the main group and one in each topic
     (#announcements, #scheduling, #team-chat)
  3. Run:  python get_telegram_ids.py
  4. Copy the printed values into config/settings.env
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "config" / "settings.env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    print("TELEGRAM_BOT_TOKEN not set in config/settings.env")
    sys.exit(1)


def api_get(method: str, params: dict = {}) -> dict:
    qs  = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://api.telegram.org/bot{TOKEN}/{method}?{qs}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def main() -> None:
    result = api_get("getUpdates", {"limit": 100, "timeout": 0})
    if not result.get("ok"):
        print("getUpdates failed:", result)
        sys.exit(1)

    updates = result.get("result", [])
    if not updates:
        print("No updates yet — send a message in the group and each topic first.")
        sys.exit(0)

    groups: dict[int, set] = {}   # chat_id → set of thread_ids seen

    thread_messages: dict[tuple, list] = {}  # (cid, tid) → list of texts

    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        if chat.get("type") not in ("supergroup", "group"):
            continue
        cid = chat["id"]
        tid = msg.get("message_thread_id")
        groups.setdefault(cid, set())
        if tid:
            groups[cid].add(tid)
            key = (cid, tid)
            text = msg.get("text") or msg.get("caption") or "<no text>"
            thread_messages.setdefault(key, []).append(text)

    if not groups:
        print("No group messages found in recent updates.")
        print("Make sure the bot is in the group and you sent messages there.")
        sys.exit(0)

    print("=" * 55)
    print("Found the following — copy into config/settings.env:")
    print("=" * 55)
    for cid, threads in groups.items():
        print(f"\nTELEGRAM_GROUP_ID={cid}")
        if threads:
            for i, tid in enumerate(sorted(threads), 1):
                msgs = thread_messages.get((cid, tid), [])
                preview = " | ".join(msgs[:3])
                print(f"# Thread {i}: {tid}  ← messages: {preview}")
            print()
            print("# Paste the matching thread IDs below:")
            print("TELEGRAM_ANNOUNCE_THREAD=")
            print("TELEGRAM_SCHEDULE_THREAD=")
            print("TELEGRAM_TEAMCHAT_THREAD=")
        else:
            print("# No topic thread IDs seen yet.")
            print("# Send a message inside each topic, then re-run this script.")
    print("=" * 55)


if __name__ == "__main__":
    main()
