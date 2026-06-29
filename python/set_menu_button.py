"""
set_menu_button.py — run ONCE after MINIAPP_URL is live.

Sets the persistent "My shifts" chat menu button for every private chat with the
bot (no chat_id = the default button for all users), so each barista opens their
own calendar straight from the Telegram compose bar. Telegram hands the page the
signed initData; the backend (require_barista) validates it and resolves the
barista — no login.

Run from the python/ working root with the venv active:
    python set_menu_button.py

Idempotent: re-running just re-sets the same button. To remove it later, call
setChatMenuButton with {"menu_button": {"type": "default"}}.
"""
import json
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / "settings.env")

import os

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
URL = os.getenv("MINIAPP_URL", "").strip()


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not TOKEN:
    _fail("TELEGRAM_BOT_TOKEN is not set in config/settings.env.")
if not URL:
    _fail("MINIAPP_URL is not set in config/settings.env - set it to the live "
          "https://.../miniapp URL first, then re-run.")
if not URL.startswith("https://"):
    # Telegram requires HTTPS for web_app URLs; an http:// URL would be rejected.
    _fail(f"MINIAPP_URL must be an https:// URL (got {URL!r}).")

payload = {
    "menu_button": {
        "type": "web_app",
        "text": "My shifts",
        "web_app": {"url": URL},
    }
}
req = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/setChatMenuButton",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
except Exception as exc:  # network / Telegram error
    _fail(f"setChatMenuButton call failed: {exc}")

if result.get("ok"):
    print(f"OK - menu button 'My shifts' set for all private chats -> {URL}")
else:
    _fail(f"Telegram rejected the request: {result}")
