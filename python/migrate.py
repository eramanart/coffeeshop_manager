"""
migrate.py — CoffeeManager-OS memory.sqlite schema initialiser
Run once on first setup, or any time to apply new migrations safely.

Usage:
    python migrate.py
    python migrate.py --db /custom/path/memory.sqlite
"""

import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("migrate")

# Anchored to this file's location (python/), NOT the current working directory.
# A cwd-relative path silently created/used an EMPTY database when the app was
# launched from anywhere other than python/ (e.g. `uvicorn api.main:app` from the
# repo root). Anchoring guarantees one canonical DB regardless of launch dir.
DEFAULT_DB = Path(__file__).resolve().parent / "data" / "memory.sqlite"

# ---------------------------------------------------------------------------
# Schema: each migration is (version, description, list_of_sql_statements)
# Append new tuples to MIGRATIONS to evolve the schema — never edit old ones.
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Initial schema: audit_log, portal_actions, notifications_sent",
        [
            # ----------------------------------------------------------------
            # audit_log
            # Stores every daily Z-report vs i.EKA comparison run.
            # ----------------------------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at          TEXT    NOT NULL,           -- ISO-8601 UTC
                audit_date      TEXT    NOT NULL,           -- YYYY-MM-DD
                pos_total       TEXT    NOT NULL,           -- Decimal as string
                ieka_total      TEXT    NOT NULL,
                discrepancy     TEXT    NOT NULL,
                status          TEXT    NOT NULL            -- 'OK' | 'MISMATCH'
                    CHECK (status IN ('OK', 'MISMATCH')),
                notes           TEXT    DEFAULT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_audit_date ON audit_log (audit_date)",
            "CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_log (status)",

            # ----------------------------------------------------------------
            # portal_actions
            # Full audit trail of every OpenClaw browser interaction.
            # One row per atomic action (navigate, fill_field, click, etc.)
            # ----------------------------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS portal_actions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                acted_at        TEXT    NOT NULL,           -- ISO-8601 UTC
                portal          TEXT    NOT NULL,           -- 'vmi_imas' | 'sodra' | 'eds' | 'other'
                action_type     TEXT    NOT NULL,           -- 'navigate' | 'fill' | 'submit_draft' | 'screenshot' | 'error'
                url             TEXT    DEFAULT NULL,
                description     TEXT    NOT NULL,           -- human-readable summary
                payload_json    TEXT    DEFAULT NULL,       -- JSON blob of form data or API params
                outcome         TEXT    NOT NULL            -- 'success' | 'failure' | 'skipped'
                    CHECK (outcome IN ('success', 'failure', 'skipped')),
                error_detail    TEXT    DEFAULT NULL,       -- populated on failure
                session_id      TEXT    DEFAULT NULL        -- groups actions in one agent run
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pa_portal   ON portal_actions (portal)",
            "CREATE INDEX IF NOT EXISTS idx_pa_acted_at ON portal_actions (acted_at)",
            "CREATE INDEX IF NOT EXISTS idx_pa_outcome  ON portal_actions (outcome)",
            "CREATE INDEX IF NOT EXISTS idx_pa_session  ON portal_actions (session_id)",

            # ----------------------------------------------------------------
            # notifications_sent
            # Deduplication guard — prevents the same alert flooding Telegram
            # on repeated retries within the same day.
            # ----------------------------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS notifications_sent (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at         TEXT    NOT NULL,           -- ISO-8601 UTC
                channel         TEXT    NOT NULL            -- 'telegram' | 'email' | 'watch'
                    CHECK (channel IN ('telegram', 'email', 'watch')),
                event_type      TEXT    NOT NULL,           -- e.g. 'AUDIT_MISMATCH' | 'NEW_HIRE' | 'STOCK_LOW'
                event_key       TEXT    NOT NULL,           -- unique key for dedup, e.g. 'AUDIT_MISMATCH:2025-06-01'
                message_preview TEXT    NOT NULL,           -- first 200 chars of message sent
                acknowledged_at TEXT    DEFAULT NULL        -- set when owner confirms receipt
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_dedup ON notifications_sent (event_key)",
            "CREATE INDEX IF NOT EXISTS idx_notif_sent_at ON notifications_sent (sent_at)",
        ],
    ),
    (
        2,
        "Add receipt_processing table for OCR + i.SAF workflow tracking",
        [
            # ----------------------------------------------------------------
            # receipt_processing
            # Tracks every receipt image through OCR → XML → VMI draft.
            # ----------------------------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS receipt_processing (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,
                filename        TEXT    NOT NULL UNIQUE,    -- original file in workspace/
                file_hash       TEXT    DEFAULT NULL,       -- SHA-256 for dedup
                ocr_status      TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (ocr_status IN ('pending', 'processing', 'done', 'failed')),
                ocr_raw_json    TEXT    DEFAULT NULL,       -- raw docTR / EasyOCR output
                supplier_vat    TEXT    DEFAULT NULL,       -- extracted LT VAT code
                supplier_code   TEXT    DEFAULT NULL,       -- extracted company code
                doc_date        TEXT    DEFAULT NULL,
                net_amount      TEXT    DEFAULT NULL,
                pvm_amount      TEXT    DEFAULT NULL,
                pvm_code        TEXT    DEFAULT NULL        -- PVM1 | PVM2 | ...
                    CHECK (pvm_code IN ('PVM1', 'PVM2', 'PVM5', NULL)),
                isaf_xml_path   TEXT    DEFAULT NULL,       -- path to generated XML file
                vmi_draft_ref   TEXT    DEFAULT NULL,       -- VMI portal draft reference number
                vmi_status      TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (vmi_status IN ('pending', 'drafted', 'signed', 'rejected')),
                notes           TEXT    DEFAULT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rp_ocr_status ON receipt_processing (ocr_status)",
            "CREATE INDEX IF NOT EXISTS idx_rp_vmi_status ON receipt_processing (vmi_status)",
        ],
    ),
    (
        3,
        "Add hr_actions table for Sodra 1-SD new hire workflow",
        [
            # ----------------------------------------------------------------
            # hr_actions
            # One row per new hire detected; tracks Sodra 1-SD lifecycle.
            # ----------------------------------------------------------------
            """
            CREATE TABLE IF NOT EXISTS hr_actions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at         TEXT    NOT NULL,
                employee_name       TEXT    NOT NULL,
                first_working_day   TEXT    NOT NULL,       -- YYYY-MM-DD; must be >= detected_at + 24h
                sodra_status        TEXT    NOT NULL DEFAULT 'detected'
                    CHECK (sodra_status IN (
                        'detected', 'draft_started', 'draft_ready',
                        'owner_notified', 'signed', 'failed'
                    )),
                draft_url           TEXT    DEFAULT NULL,   -- Sodra portal URL of saved draft
                notification_key    TEXT    DEFAULT NULL,   -- FK ref to notifications_sent.event_key
                signed_at           TEXT    DEFAULT NULL,
                notes               TEXT    DEFAULT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hr_status ON hr_actions (sodra_status)",
            "CREATE INDEX IF NOT EXISTS idx_hr_name   ON hr_actions (employee_name)",
        ],
    ),
    (
        5,
        "Predictive labor scheduling: hourly_sales, weather_cache, shift_suggestions",
        [
            """
            CREATE TABLE IF NOT EXISTS hourly_sales (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_date     TEXT    NOT NULL,
                hour          INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
                day_of_week   INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
                revenue_eur   REAL    NOT NULL,
                recorded_at   TEXT    NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hs_date_hour ON hourly_sales (sale_date, hour)",
            "CREATE INDEX IF NOT EXISTS idx_hs_dow ON hourly_sales (day_of_week)",

            """
            CREATE TABLE IF NOT EXISTS weather_cache (
                forecast_date TEXT    PRIMARY KEY,
                weather_code  INTEGER NOT NULL,
                weather_mult  REAL    NOT NULL,
                weather_desc  TEXT    NOT NULL,
                fetched_at    TEXT    NOT NULL
            )
            """,

            """
            CREATE TABLE IF NOT EXISTS shift_suggestions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_date     TEXT    NOT NULL UNIQUE,
                dow                 INTEGER NOT NULL,
                forecast_json       TEXT    NOT NULL,
                total_forecast_eur  REAL    NOT NULL,
                weather_code        INTEGER,
                weather_mult        REAL,
                confidence          INTEGER NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','edited','skipped')),
                owner_response      TEXT,
                created_at          TEXT    NOT NULL,
                responded_at        TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ss_date ON shift_suggestions (suggestion_date)",
            "CREATE INDEX IF NOT EXISTS idx_ss_status ON shift_suggestions (status)",
        ],
    ),
    (
        4,
        "Add schema_migrations bookkeeping table",
        [
            # This migration creates the table that tracks migrations.
            # It is bootstrapped manually in apply_migrations() below
            # before any migration runs, so it is safe to include here
            # as a no-op idempotent statement.
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT    NOT NULL,
                applied_at  TEXT    NOT NULL
            )
            """,
        ],
    ),
    (
        6,
        "Barista scheduling: baristas, barista_availability, published_shifts, swap_requests",
        # SQL imported from agent/scheduler_bot.py so the source of truth lives
        # alongside the feature code, not split across two files.
        __import__("agent.scheduler_bot", fromlist=["MIGRATION_V6_SQL"]).MIGRATION_V6_SQL,
    ),
    (
        7,
        "Customer sentiment loop: google_reviews, winback_posts",
        __import__("core.sentiment_loop", fromlist=["MIGRATION_V7_SQL"]).MIGRATION_V7_SQL,
    ),
    (
        8,
        "Barista HR: level/rate columns on baristas, barista_level_history, sodra_rate_drafts",
        __import__("core.hr_manager", fromlist=["MIGRATION_V8_SQL"]).MIGRATION_V8_SQL,
    ),
    (
        9,
        "POS events: pos_events table for webhook-originated events",
        [
            """
            CREATE TABLE IF NOT EXISTS pos_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at     TEXT    NOT NULL,           -- ISO-8601 UTC
                pos_total       TEXT    NOT NULL,           -- decimal as string
                period_start    TEXT    NOT NULL,           -- ISO-8601 date
                period_end      TEXT    NOT NULL,           -- ISO-8601 date
                source          TEXT    NOT NULL            -- 'webhook' | 'manual'
                    CHECK (source IN ('webhook', 'manual')),
                notes           TEXT    DEFAULT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pos_events_received ON pos_events (received_at)",
        ],
    ),
    (
        10,
        "Structured alert fields: notifications_sent.fields_json for dashboard cards",
        [
            # Additive, nullable column. Existing rows stay NULL and the dashboard
            # falls back to message_preview for them; new notifications populate it.
            "ALTER TABLE notifications_sent ADD COLUMN fields_json TEXT DEFAULT NULL",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def _bootstrap_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            description TEXT    NOT NULL,
            applied_at  TEXT    NOT NULL
        )
    """)
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    _bootstrap_migrations_table(conn)
    applied = _applied_versions(conn)

    pending = [m for m in MIGRATIONS if m[0] not in applied]
    if not pending:
        log.info("Schema is up to date. No migrations to apply.")
        conn.close()
        return

    for version, description, statements in pending:
        log.info(f"Applying migration v{version}: {description}")
        try:
            with conn:                          # atomic per migration
                for sql in statements:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?,?,?)",
                    (version, description, datetime.now(timezone.utc).isoformat()),
                )
            log.info(f"  v{version} applied successfully.")
        except sqlite3.Error as exc:
            log.error(f"  v{version} FAILED: {exc}")
            conn.close()
            raise SystemExit(1)

    conn.close()
    log.info(f"All migrations applied. Database ready at: {db_path}")


# ---------------------------------------------------------------------------
# Convenience helpers — import these in your other modules
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Return a WAL-mode connection with row_factory set."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def log_audit(conn: sqlite3.Connection, result: dict) -> None:
    conn.execute(
        """INSERT INTO audit_log
           (run_at, audit_date, pos_total, ieka_total, discrepancy, status, notes)
           VALUES (:run_at, :audit_date, :pos_total, :ieka_total, :discrepancy, :status, :notes)""",
        {
            "run_at":       datetime.now(timezone.utc).isoformat(),
            "audit_date":   result["date"],
            "pos_total":    result["pos_total"],
            "ieka_total":   result["ieka_total"],
            "discrepancy":  result["discrepancy"],
            "status":       result["status"],
            "notes":        result.get("notes"),
        },
    )
    conn.commit()


def log_portal_action(
    conn: sqlite3.Connection,
    portal: str,
    action_type: str,
    description: str,
    outcome: str,
    url: str | None = None,
    payload: dict | None = None,
    error: str | None = None,
    session_id: str | None = None,
) -> None:
    import json as _json
    conn.execute(
        """INSERT INTO portal_actions
           (acted_at, portal, action_type, url, description,
            payload_json, outcome, error_detail, session_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            portal, action_type, url, description,
            _json.dumps(payload) if payload else None,
            outcome, error, session_id,
        ),
    )
    conn.commit()


def upsert_receipt(
    conn: sqlite3.Connection,
    filename: str,
    *,
    ocr_status: str | None = None,
    supplier_vat: str | None = None,
    supplier_code: str | None = None,
    doc_date: str | None = None,
    net_amount: str | None = None,
    pvm_amount: str | None = None,
    pvm_code: str | None = None,
    isaf_xml_path: str | None = None,
    vmi_draft_ref: str | None = None,
    vmi_status: str | None = None,
    notes: str | None = None,
) -> None:
    """
    Insert or update a receipt_processing row for the given filename.
    Only non-None keyword args are written; existing values are preserved.
    """
    now = datetime.now(timezone.utc).isoformat()
    # Ensure the row exists
    conn.execute(
        "INSERT OR IGNORE INTO receipt_processing (created_at, filename) VALUES (?,?)",
        (now, filename),
    )
    # Build SET clause from non-None kwargs
    updates: dict[str, object] = {}
    if ocr_status    is not None: updates["ocr_status"]    = ocr_status
    if supplier_vat  is not None: updates["supplier_vat"]  = supplier_vat
    if supplier_code is not None: updates["supplier_code"] = supplier_code
    if doc_date      is not None: updates["doc_date"]      = doc_date
    if net_amount    is not None: updates["net_amount"]    = net_amount
    if pvm_amount    is not None: updates["pvm_amount"]    = pvm_amount
    if pvm_code      is not None: updates["pvm_code"]      = pvm_code
    if isaf_xml_path is not None: updates["isaf_xml_path"] = isaf_xml_path
    if vmi_draft_ref is not None: updates["vmi_draft_ref"] = vmi_draft_ref
    if vmi_status    is not None: updates["vmi_status"]    = vmi_status
    if notes         is not None: updates["notes"]         = notes
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE receipt_processing SET {set_clause} WHERE filename=?",
            (*updates.values(), filename),
        )
    conn.commit()


def notify_if_new(
    conn: sqlite3.Connection,
    event_type: str,
    event_key: str,
    message: str,
    channel: str = "telegram",
    fields: dict | None = None,
) -> bool:
    """
    Insert a notification record only if event_key has not been sent before.
    Returns True if the notification is new (caller should actually send it).
    Returns False if it was already sent (caller should skip).

    `fields` is an optional dict of structured key→value pairs (e.g.
    {"Item": "Espresso beans", "Order": "6 kg"}) stored as JSON for the dashboard
    cards. When omitted, the card falls back to message_preview.
    """
    import json as _json
    try:
        conn.execute(
            """INSERT INTO notifications_sent
               (sent_at, channel, event_type, event_key, message_preview, fields_json)
               VALUES (?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                channel, event_type, event_key, message[:200],
                _json.dumps(fields) if fields else None,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False     # UNIQUE constraint hit — already sent


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply CoffeeManager-OS DB migrations")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"Path to SQLite database (default: {DEFAULT_DB})")
    args = parser.parse_args()
    apply_migrations(args.db)
