"""
Authentication and HITL utilities for api/main.py

Provides:
  - validate_telegram_secret_token: Check Telegram webhook secret
  - validate_bearer_token: Check API bearer token
  - validate_pos_secret: Check POS webhook secret
  - verify_init_data: Validate a Telegram Mini App initData string (HMAC)
  - atomic_acknowledge: Idempotent event acknowledgment
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from fastapi import HTTPException


def validate_telegram_secret_token(authorization_header: str) -> bool:
    """
    Validate Telegram Bot API secret token.
    Telegram sends: X-Telegram-Bot-Api-Secret-Token header with the configured secret.

    Args:
        authorization_header: Value of X-Telegram-Bot-Api-Secret-Token header

    Returns:
        True if token matches, False otherwise

    Raises:
        HTTPException(401) if validation fails
    """
    expected = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="Telegram secret token not configured"
        )

    # Constant-time compare to avoid leaking the secret via response timing.
    if not authorization_header or not secrets.compare_digest(authorization_header, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid Telegram secret token"
        )

    return True


def validate_bearer_token(authorization_header: str | None) -> bool:
    """
    Validate API bearer token for /confirm and /dismiss endpoints.

    Args:
        authorization_header: Value of Authorization header (e.g., "Bearer <token>")

    Returns:
        True if token is valid

    Raises:
        HTTPException(401) if validation fails or token missing
    """
    expected = os.getenv("API_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="API bearer token not configured"
        )

    if not authorization_header:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header"
        )

    # Parse "Bearer <token>"
    parts = authorization_header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format (use 'Bearer <token>')"
        )

    token = parts[1]
    # Constant-time compare to avoid leaking the token via response timing.
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token"
        )

    return True


def validate_pos_secret(authorization_header: str | None) -> bool:
    """
    Validate POS webhook shared secret.

    Args:
        authorization_header: Value of Authorization header (e.g., "Bearer <secret>")

    Returns:
        True if secret is valid

    Raises:
        HTTPException(401) if validation fails
    """
    expected = os.getenv("WEBHOOK_POS_SECRET", "")

    # If not configured, allow (backward compatible with internal-only POS)
    if not expected:
        return True

    if not authorization_header:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header"
        )

    # Parse "Bearer <secret>"
    parts = authorization_header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format (use 'Bearer <secret>')"
        )

    secret = parts[1]
    # Constant-time compare to avoid leaking the secret via response timing.
    if not secrets.compare_digest(secret, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid POS webhook secret"
        )

    return True


def verify_init_data(init_data: str, bot_token: str, max_age_s: int = 3600) -> dict | None:
    """
    Validate a Telegram Mini App initData string and return the user dict.

    This is the ONLY trustworthy way to learn who opened the Mini App: the page
    sends tg.initData (a signed query string); we recompute its HMAC with the bot
    token and compare. Never trust tg.initDataUnsafe server-side — without this
    check anyone could forge a request as any barista and read their schedule.

    The signature scheme (per Telegram's WebApp docs):
      secret   = HMAC-SHA256(key="WebAppData", msg=bot_token)
      expected = hex( HMAC-SHA256(key=secret, msg=data_check_string) )
    where data_check_string is the "key=value" pairs (all fields except `hash`),
    sorted by key and joined with newlines.

    Args:
        init_data:  Raw initData string (the value of tg.initData).
        bot_token:  TELEGRAM_BOT_TOKEN — the same token @BotFather issued.
        max_age_s:  Reject data older than this many seconds (replay guard).
                    Pass 0 to skip the auth_date freshness check.

    Returns:
        The validated user dict ({'id':..., 'first_name':..., ...}), or
        None if the data is missing, malformed, forged, or stale.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received = pairs.pop("hash", None)
    if not received:
        return None

    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

    # Constant-time compare so a forged hash can't be discovered byte-by-byte.
    if not hmac.compare_digest(expected, received):
        return None

    if max_age_s:
        try:
            auth_age = time.time() - int(pairs.get("auth_date", 0))
        except (TypeError, ValueError):
            return None
        if auth_age > max_age_s:
            return None  # stale — possible replay

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except (json.JSONDecodeError, TypeError):
        return None


def atomic_acknowledge(conn: sqlite3.Connection, event_key: str) -> bool:
    """
    Atomically acknowledge an event notification, preventing double-dispatch.

    Executes: UPDATE notifications_sent SET acknowledged_at=NOW
              WHERE event_key=? AND acknowledged_at IS NULL

    Only the first call succeeds (returns True). Subsequent calls return False.
    This is atomic at the SQLite row level due to ACID serialization.

    Args:
        conn: SQLite connection
        event_key: Unique event key (e.g., "RECEIPT_DRAFTED:invoice_2025_05_01.jpg")

    Returns:
        True if this was the first acknowledgment (dispatch should proceed)
        False if already acknowledged (dispatch should be skipped)
    """
    cursor = conn.execute(
        """UPDATE notifications_sent
           SET acknowledged_at = ?
           WHERE event_key = ? AND acknowledged_at IS NULL""",
        (datetime.now(timezone.utc).isoformat(), event_key),
    )
    conn.commit()

    # rowcount is 0 if no rows matched (already ack'd or doesn't exist)
    # rowcount is 1 if exactly one row was updated (first ack, success)
    return cursor.rowcount == 1
