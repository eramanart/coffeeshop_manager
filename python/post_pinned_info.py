"""
post_pinned_info.py — send and pin the CoffeeBot info card in Telegram

Run once after filling in TELEGRAM_GROUP_ID and thread IDs in settings.env:
    python post_pinned_info.py

Posts PINNED_INFO to both #scheduling and #announcements, then pins each message.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "config" / "settings.env")

from agent.scheduler_bot import PINNED_INFO

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "")
THREADS  = {
    "scheduling":    os.getenv("TELEGRAM_SCHEDULE_THREAD", ""),
    "announcements": os.getenv("TELEGRAM_ANNOUNCE_THREAD", ""),
}


def _api(method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def send_and_pin(thread_name: str, thread_id: str) -> None:
    if not thread_id.lstrip("-").isdigit():
        print(f"  SKIP #{thread_name} — TELEGRAM_{thread_name.upper()}_THREAD not set")
        return

    print(f"  Posting to #{thread_name} (thread {thread_id})…")
    result = _api("sendMessage", {
        "chat_id":           GROUP_ID,
        "message_thread_id": int(thread_id),
        "text":              PINNED_INFO,
        "parse_mode":        "Markdown",
    })
    if not result.get("ok"):
        print(f"  ERROR sending: {result}")
        return

    msg_id = result["result"]["message_id"]
    print(f"  Pinning message {msg_id}…")
    pin = _api("pinChatMessage", {
        "chat_id":              GROUP_ID,
        "message_id":          msg_id,
        "disable_notification": True,
    })
    if pin.get("ok"):
        print(f"  Done — pinned in #{thread_name}.")
    else:
        print(f"  Sent but pin failed (bot may need Admin + Pin Messages): {pin}")


def main() -> None:
    missing = []
    if not TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GROUP_ID or not GROUP_ID.lstrip("-").isdigit():
        missing.append("TELEGRAM_GROUP_ID")
    if missing:
        print("Missing required settings.env values:", ", ".join(missing))
        print("Fill them in, then re-run: python post_pinned_info.py")
        sys.exit(1)

    print(f"Bot token: …{TOKEN[-6:]}")
    print(f"Group ID:  {GROUP_ID}\n")

    for name, tid in THREADS.items():
        send_and_pin(name, tid)

    print("\nAll done. The info card is live.")
    print("Any barista added to the group will see it pinned immediately.")


if __name__ == "__main__":
    main()
