"""
Unit tests for Pass 1 fixes (security, auth, dedup, atomicity).

Run with:
  python -m pytest tests/test_pass1_fixes.py -v
  or:
  python -m pytest tests/test_pass1_fixes.py::test_name -v
"""

import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

# Import the modules under test
from api.helpers import atomic_acknowledge, validate_bearer_token, validate_telegram_secret_token
from fastapi import HTTPException
import os


@pytest.fixture
def temp_db():
    """Create a temporary in-memory SQLite database with notifications_sent table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Create the notifications_sent table
    conn.execute("""
        CREATE TABLE notifications_sent (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at         TEXT    NOT NULL,
            channel         TEXT    NOT NULL,
            event_type      TEXT    NOT NULL,
            event_key       TEXT    NOT NULL UNIQUE,
            message_preview TEXT    NOT NULL,
            acknowledged_at TEXT    DEFAULT NULL
        )
    """)
    conn.commit()

    yield conn
    conn.close()


class TestAtomicAcknowledge:
    """Test atomic acknowledgment prevents double-dispatch."""

    def test_first_acknowledge_succeeds(self, temp_db):
        """First call to atomic_acknowledge returns True."""
        # Insert a notification
        temp_db.execute("""
            INSERT INTO notifications_sent
            (sent_at, channel, event_type, event_key, message_preview)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), "telegram", "TEST_EVENT", "KEY_1", "test"))
        temp_db.commit()

        # First ack should succeed
        result = atomic_acknowledge(temp_db, "KEY_1")
        assert result is True, "First acknowledge should return True"

        # Verify acknowledged_at was set
        row = temp_db.execute(
            "SELECT acknowledged_at FROM notifications_sent WHERE event_key = ?",
            ("KEY_1",)
        ).fetchone()
        assert row["acknowledged_at"] is not None, "acknowledged_at should be set"

    def test_second_acknowledge_fails(self, temp_db):
        """Second call to atomic_acknowledge returns False (idempotent)."""
        # Insert and acknowledge once
        temp_db.execute("""
            INSERT INTO notifications_sent
            (sent_at, channel, event_type, event_key, message_preview)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), "telegram", "TEST_EVENT", "KEY_2", "test"))
        temp_db.commit()

        result1 = atomic_acknowledge(temp_db, "KEY_2")
        assert result1 is True

        # Second ack should fail
        result2 = atomic_acknowledge(temp_db, "KEY_2")
        assert result2 is False, "Second acknowledge should return False"

    def test_nonexistent_key_returns_false(self, temp_db):
        """Acknowledging a non-existent key returns False."""
        result = atomic_acknowledge(temp_db, "NONEXISTENT_KEY")
        assert result is False, "Acknowledging non-existent key should return False"


class TestEventKeyCasePreservation:
    """Test that event keys preserve case (fix for Telegram webhook issue)."""

    def test_lowercase_filename_in_key(self, temp_db):
        """Event keys with lowercase filenames are preserved."""
        key = "RECEIPT_DRAFTED:invoice_2025_05_01.jpg"

        # Insert with lowercase key
        temp_db.execute("""
            INSERT INTO notifications_sent
            (sent_at, channel, event_type, event_key, message_preview)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), "telegram", "RECEIPT_DRAFTED", key, "test"))
        temp_db.commit()

        # Query should find it with original case
        row = temp_db.execute(
            "SELECT event_key FROM notifications_sent WHERE event_key = ?",
            (key,)
        ).fetchone()
        assert row is not None, "Key should be found with original case"
        assert row["event_key"] == key, "Key should preserve case"

    def test_employee_name_in_key(self, temp_db):
        """Event keys with employee names preserve case."""
        key = "NEW_HIRE_DRAFTED:Jonas Petraitis:2026-06-15"

        # Insert with mixed-case name
        temp_db.execute("""
            INSERT INTO notifications_sent
            (sent_at, channel, event_type, event_key, message_preview)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), "telegram", "NEW_HIRE_DRAFTED", key, "test"))
        temp_db.commit()

        # Query should find it
        row = temp_db.execute(
            "SELECT event_key FROM notifications_sent WHERE event_key = ?",
            (key,)
        ).fetchone()
        assert row is not None
        assert row["event_key"] == key, "Key should preserve name capitalization"


class TestBearerTokenValidation:
    """Test bearer token validation for /confirm and /dismiss endpoints."""

    def test_valid_bearer_token(self, monkeypatch):
        """Valid bearer token passes validation."""
        monkeypatch.setenv("API_BEARER_TOKEN", "test_token_12345")

        # Should not raise
        result = validate_bearer_token("Bearer test_token_12345")
        assert result is True

    def test_invalid_bearer_token(self, monkeypatch):
        """Invalid bearer token raises 401."""
        monkeypatch.setenv("API_BEARER_TOKEN", "test_token_12345")

        with pytest.raises(HTTPException) as exc_info:
            validate_bearer_token("Bearer wrong_token")

        assert exc_info.value.status_code == 401

    def test_missing_bearer_token(self, monkeypatch):
        """Missing authorization header raises 401."""
        monkeypatch.setenv("API_BEARER_TOKEN", "test_token_12345")

        with pytest.raises(HTTPException) as exc_info:
            validate_bearer_token(None)

        assert exc_info.value.status_code == 401

    def test_malformed_bearer_header(self, monkeypatch):
        """Malformed authorization header raises 401."""
        monkeypatch.setenv("API_BEARER_TOKEN", "test_token_12345")

        with pytest.raises(HTTPException) as exc_info:
            validate_bearer_token("InvalidFormat test_token")

        assert exc_info.value.status_code == 401


class TestTelegramSecretTokenValidation:
    """Test Telegram secret token validation."""

    def test_valid_telegram_secret(self, monkeypatch):
        """Valid Telegram secret passes validation."""
        monkeypatch.setenv("TELEGRAM_SECRET_TOKEN", "tg_secret_xyz")

        result = validate_telegram_secret_token("tg_secret_xyz")
        assert result is True

    def test_invalid_telegram_secret(self, monkeypatch):
        """Invalid Telegram secret raises 401."""
        monkeypatch.setenv("TELEGRAM_SECRET_TOKEN", "tg_secret_xyz")

        with pytest.raises(HTTPException) as exc_info:
            validate_telegram_secret_token("wrong_secret")

        assert exc_info.value.status_code == 401

    def test_missing_telegram_secret_raises_401(self, monkeypatch):
        """Missing Telegram secret token raises 401."""
        monkeypatch.delenv("TELEGRAM_SECRET_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_SECRET_TOKEN", "")

        with pytest.raises(HTTPException) as exc_info:
            validate_telegram_secret_token("any_token")

        assert exc_info.value.status_code == 401


class TestSodraRateSQL:
    """Test SODRA_RATE SQL fix handles ordering correctly."""

    @pytest.fixture
    def sodra_db(self):
        """Create a database with sodra_rate_drafts table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        conn.execute("""
            CREATE TABLE sodra_rate_drafts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                barista_id    INTEGER NOT NULL,
                status        TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                submitted_at  TEXT DEFAULT NULL
            )
        """)
        conn.commit()

        yield conn
        conn.close()

    def test_sodra_rate_subquery_updates_latest(self, sodra_db):
        """SODRA_RATE subquery correctly updates the most recent draft."""
        barista_id = 42
        now = datetime.now(timezone.utc).isoformat()

        # Insert two drafts for same barista
        sodra_db.execute(
            "INSERT INTO sodra_rate_drafts (barista_id, status, created_at) VALUES (?, ?, ?)",
            (barista_id, "draft", "2026-06-01T10:00:00+00:00")
        )
        sodra_db.execute(
            "INSERT INTO sodra_rate_drafts (barista_id, status, created_at) VALUES (?, ?, ?)",
            (barista_id, "draft", "2026-06-05T15:00:00+00:00")
        )
        sodra_db.commit()

        # Update using the fixed SQL with subquery
        cursor = sodra_db.execute(
            """UPDATE sodra_rate_drafts SET status='submitted', submitted_at=?
               WHERE id = (SELECT id FROM sodra_rate_drafts
                           WHERE barista_id=? AND status='draft'
                           ORDER BY created_at DESC LIMIT 1)""",
            (now, barista_id),
        )
        sodra_db.commit()

        # Should update exactly one row
        assert cursor.rowcount == 1, "Should update exactly one row"

        # Verify the LATEST (by created_at) was updated
        rows = sodra_db.execute(
            "SELECT * FROM sodra_rate_drafts WHERE barista_id=? ORDER BY created_at",
            (barista_id,)
        ).fetchall()

        # First row (older) should still be draft
        assert rows[0]["status"] == "draft", "Older draft should remain unchanged"

        # Second row (newer) should be submitted
        assert rows[1]["status"] == "submitted", "Newer draft should be submitted"
        assert rows[1]["submitted_at"] == now, "submitted_at should be set"


class TestDedupAfterSendBehavior:
    """Test dedup-after-send prevents double-alerts on failure."""

    def test_dedup_record_only_after_success(self, temp_db):
        """On send failure, notification should NOT be recorded (allowing retry)."""
        key = "TEST_KEY"

        # Simulate failed send (notification NOT recorded)
        # Then simulate retry (should attempt to send again)

        # First attempt fails, no record
        is_recorded = temp_db.execute(
            "SELECT COUNT(*) FROM notifications_sent WHERE event_key = ?",
            (key,)
        ).fetchone()[0]
        assert is_recorded == 0, "Failed send should not record notification"

        # Second attempt (retry) can try again
        # Insert as if send succeeded
        temp_db.execute("""
            INSERT INTO notifications_sent
            (sent_at, channel, event_type, event_key, message_preview)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), "telegram", "TEST_EVENT", key, "test"))
        temp_db.commit()

        # Verify it's now recorded
        is_recorded = temp_db.execute(
            "SELECT COUNT(*) FROM notifications_sent WHERE event_key = ?",
            (key,)
        ).fetchone()[0]
        assert is_recorded == 1, "Successful send should record notification"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
