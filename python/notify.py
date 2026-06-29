"""
notify.py — Telegram notification helper for CoffeeManager-OS.

All outbound Telegram messages go through send_telegram().
Deduplication is enforced via notifications_sent in SQLite,
so the same event_key will never fire twice.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("notify")


def send_telegram(
    conn,
    event_type: str,
    event_key: str,
    message: str,
) -> bool:
    """
    Send a Telegram message, deduplicated by event_key.

    Returns True if the message was sent (new event).
    Returns False if suppressed (already sent or not configured).
    """
    from migrate import notify_if_new

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat_id:
        log.warning(
            "Telegram not configured — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in settings.env"
        )
        return False

    if not notify_if_new(conn, event_type, event_key, message):
        log.debug("Notification suppressed (already sent): %s", event_key)
        return False

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        if result.get("ok"):
            log.info("Telegram sent: %s", event_key)
            return True

        log.error("Telegram API error for %s: %s", event_key, result)

    except urllib.error.HTTPError as exc:
        log.error("Telegram HTTP %s for %s: %s", exc.code, event_key, exc.read())
    except Exception as exc:
        log.error("Telegram send failed for %s: %s", event_key, exc)

    # Remove the dedup record so the next run can retry
    conn.execute(
        "DELETE FROM notifications_sent WHERE event_key = ?", (event_key,)
    )
    conn.commit()
    return False
