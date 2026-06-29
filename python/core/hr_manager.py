"""
core/hr_manager.py — Barista HR Manager

Staff categorisation, wage compliance, and promotion tracking.

  ROOKIE  (0-60 days)    5.50 EUR/h default  Flag for Basic Skill Check every 2w
  SKILLED (61-270 days)  7.00 EUR/h           Monitor for leadership potential
  PRO     (270+ days)    9.00 EUR/h           Assign as Shift Lead in roster

Rules enforced:
  - Promotion Alert: ROOKIE hits 60 days -> notify owner, await GO to promote
  - Budget Guardrail: swap where PRO replaces ROOKIE -> alert cost diff to owner
  - Sodra Sync: on every rate change -> draft Sodra notification + log to DB

Usage:
    python core/hr_manager.py --check-promotions
    python core/hr_manager.py --list
    python core/hr_manager.py --roster-cost 2026-05-23
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "config" / "settings.env")

log = logging.getLogger("hr_manager")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Constants ──────────────────────────────────────────────────────────────────

ROOKIE_DAYS      = 60    # days before ROOKIE is eligible for SKILLED
SKILLED_DAYS     = 270   # days before SKILLED is eligible for PRO

LEVEL_RATES: dict[str, Decimal] = {
    "ROOKIE":  Decimal("5.50"),
    "SKILLED": Decimal("7.00"),
    "PRO":     Decimal("9.00"),
}

SHIFT_HOURS = Decimal(str(os.getenv("BARISTA_SHIFT_HOURS", "8")))

# ── DB migration SQL (imported by migrate.py as v8) ────────────────────────────

MIGRATION_V8_SQL: list[str] = [
    # Extend baristas with level, hourly_rate, hire_date
    "ALTER TABLE baristas ADD COLUMN level       TEXT    NOT NULL DEFAULT 'ROOKIE'",
    "ALTER TABLE baristas ADD COLUMN hourly_rate TEXT    NOT NULL DEFAULT '5.50'",
    "ALTER TABLE baristas ADD COLUMN hire_date   TEXT    DEFAULT NULL",

    # Full audit trail of every level and rate change
    """
    CREATE TABLE IF NOT EXISTS barista_level_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        barista_id   INTEGER NOT NULL REFERENCES baristas(id),
        changed_at   TEXT    NOT NULL,
        old_level    TEXT    NOT NULL,
        new_level    TEXT    NOT NULL,
        old_rate     TEXT    NOT NULL,
        new_rate     TEXT    NOT NULL,
        changed_by   TEXT    NOT NULL DEFAULT 'owner',
        notes        TEXT    DEFAULT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blh_barista ON barista_level_history (barista_id)",
    "CREATE INDEX IF NOT EXISTS idx_blh_changed ON barista_level_history (changed_at)",

    # Sodra draft notifications for rate/contract changes
    """
    CREATE TABLE IF NOT EXISTS sodra_rate_drafts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        barista_id   INTEGER NOT NULL REFERENCES baristas(id),
        created_at   TEXT    NOT NULL,
        old_rate     TEXT    NOT NULL,
        new_rate     TEXT    NOT NULL,
        effective_on TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'submitted', 'skipped')),
        submitted_at TEXT    DEFAULT NULL,
        notes        TEXT    DEFAULT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_srd_barista ON sodra_rate_drafts (barista_id)",
    "CREATE INDEX IF NOT EXISTS idx_srd_status  ON sodra_rate_drafts (status)",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tenure_days(hire_date: str) -> int:
    """Days since hire_date (ISO-8601 date or datetime string)."""
    try:
        d = date.fromisoformat(hire_date[:10])
    except ValueError:
        return 0
    return (date.today() - d).days


def _effective_hire(barista: object) -> str:
    """Use explicit hire_date if set, otherwise fall back to joined_at."""
    hd = barista["hire_date"] or barista["joined_at"]
    return hd or _now()


def _money(value: str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _notify(conn, event_type: str, event_key: str, msg: str) -> None:
    from migrate import notify_if_new
    notify_if_new(conn, event_type, event_key, msg)


# ── Core business logic ────────────────────────────────────────────────────────

def check_promotions(conn) -> list[dict]:
    """
    Scan all active ROOKIEs for 60-day tenure eligibility.
    Sends a Telegram alert for each eligible barista — owner confirms via GO.
    Returns list of eligible baristas.
    """
    rows = conn.execute(
        """SELECT id, name, level, hourly_rate, hire_date, joined_at
           FROM baristas
           WHERE is_active = 1 AND level = 'ROOKIE'"""
    ).fetchall()

    eligible = []
    for b in rows:
        days = _tenure_days(_effective_hire(b))
        if days >= ROOKIE_DAYS:
            eligible.append(dict(b) | {"tenure_days": days})
            event_key = f"BARISTA_PROMOTE:{b['id']}:SKILLED"
            msg = (
                f"HR Alert: Promotion eligible\n"
                f"Barista: {b['name']}\n"
                f"Current level: ROOKIE @ {b['hourly_rate']} EUR/h\n"
                f"Tenure: {days} days (threshold: {ROOKIE_DAYS})\n"
                f"Proposed level: SKILLED @ {LEVEL_RATES['SKILLED']} EUR/h\n\n"
                f"Reply GO {event_key} to promote and update wage.\n"
                f"Reply SKIP {event_key} to defer."
            )
            _notify(conn, "BARISTA_PROMOTE", event_key, msg)
            log.info("Promotion alert sent: %s (%d days)", b["name"], days)

    if not eligible:
        log.info("check_promotions: no ROOKIEs eligible yet")
    return eligible


def swap_cost_alert(conn, requester_id: int, target_id: int, shift_date: str) -> dict | None:
    """
    Budget guardrail for shift swaps.
    If the incoming barista (target) has a higher rate than the outgoing (requester),
    calculate the extra cost and return an alert dict.
    Returns None if no cost increase.
    """
    req = conn.execute(
        "SELECT name, level, hourly_rate FROM baristas WHERE id = ?", (requester_id,)
    ).fetchone()
    tgt = conn.execute(
        "SELECT name, level, hourly_rate FROM baristas WHERE id = ?", (target_id,)
    ).fetchone()
    if not req or not tgt:
        return None

    req_rate = _money(req["hourly_rate"])
    tgt_rate = _money(tgt["hourly_rate"])
    diff     = (tgt_rate - req_rate) * SHIFT_HOURS

    if diff <= Decimal("0"):
        return None

    return {
        "requester_name": req["name"],
        "requester_level": req["level"],
        "target_name": tgt["name"],
        "target_level": tgt["level"],
        "cost_increase_eur": str(diff),
        "message": (
            f"Budget alert: Shift swap cost increase\n"
            f"Outgoing: {req['name']} ({req['level']} @ {req_rate} EUR/h)\n"
            f"Incoming: {tgt['name']} ({tgt['level']} @ {tgt_rate} EUR/h)\n"
            f"Shift: {shift_date} ({SHIFT_HOURS}h)\n"
            f"Extra cost: +{diff} EUR\n\n"
            f"Reply GO to approve anyway, SKIP to decline the swap."
        ),
    }


def promote_barista(conn, barista_id: int, new_level: str, new_rate: Decimal | None = None) -> None:
    """
    Promote a barista to new_level, update hourly_rate, log history, and
    draft a Sodra rate-change notification.
    Called after owner confirms BARISTA_PROMOTE event.
    """
    if new_level not in LEVEL_RATES:
        raise ValueError(f"Unknown level: {new_level}")

    row = conn.execute(
        "SELECT id, name, level, hourly_rate, hire_date, joined_at FROM baristas WHERE id = ?",
        (barista_id,),
    ).fetchone()
    if not row:
        log.error("promote_barista: barista %d not found", barista_id)
        return

    old_level = row["level"]
    old_rate  = _money(row["hourly_rate"])
    rate      = new_rate if new_rate is not None else LEVEL_RATES[new_level]
    now       = _now()

    conn.execute(
        "UPDATE baristas SET level = ?, hourly_rate = ? WHERE id = ?",
        (new_level, str(rate), barista_id),
    )
    conn.execute(
        """INSERT INTO barista_level_history
           (barista_id, changed_at, old_level, new_level, old_rate, new_rate)
           VALUES (?,?,?,?,?,?)""",
        (barista_id, now, old_level, new_level, str(old_rate), str(rate)),
    )
    conn.commit()

    log.info(
        "Promoted %s: %s @ %s EUR/h -> %s @ %s EUR/h",
        row["name"], old_level, old_rate, new_level, rate,
    )

    # Sodra draft if rate actually changed
    if rate != old_rate:
        draft_sodra_rate_change(conn, barista_id, old_rate, rate)


def draft_sodra_rate_change(conn, barista_id: int, old_rate: Decimal, new_rate: Decimal) -> None:
    """
    Log a Sodra rate-change draft and notify the owner.
    Owner must review and submit manually — draft only, never auto-submit.
    """
    effective_on = date.today().isoformat()
    now          = _now()

    conn.execute(
        """INSERT INTO sodra_rate_drafts
           (barista_id, created_at, old_rate, new_rate, effective_on, status)
           VALUES (?,?,?,?,?,?)""",
        (barista_id, now, str(old_rate), str(new_rate), effective_on, "draft"),
    )
    conn.commit()

    row = conn.execute(
        "SELECT name FROM baristas WHERE id = ?", (barista_id,)
    ).fetchone()
    name = row["name"] if row else f"barista #{barista_id}"

    event_key = f"SODRA_RATE:{barista_id}:{effective_on}"
    msg = (
        f"Sodra rate-change draft ready\n"
        f"Employee: {name}\n"
        f"Old rate: {old_rate} EUR/h\n"
        f"New rate: {new_rate} EUR/h\n"
        f"Effective: {effective_on}\n\n"
        f"A draft has been logged. Please update the employment contract "
        f"and notify Sodra if required by the contract type.\n"
        f"Reply GO {event_key} to mark as submitted.\n"
        f"Reply SKIP {event_key} to dismiss."
    )
    _notify(conn, "SODRA_RATE_CHANGE", event_key, msg)
    log.info("Sodra rate draft created: %s %s -> %s", name, old_rate, new_rate)


# ── Read helpers ───────────────────────────────────────────────────────────────

def list_baristas(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, name, level, hourly_rate, hire_date, joined_at, is_active
           FROM baristas ORDER BY name"""
    ).fetchall()
    result = []
    for b in rows:
        days = _tenure_days(_effective_hire(b))
        result.append({
            **dict(b),
            "tenure_days": days,
            "next_milestone": _next_milestone(b["level"], days),
        })
    return result


def _next_milestone(level: str, days: int) -> str:
    if level == "ROOKIE":
        remaining = max(0, ROOKIE_DAYS - days)
        return f"SKILLED eligible in {remaining} days" if remaining > 0 else "SKILLED eligible now"
    if level == "SKILLED":
        remaining = max(0, SKILLED_DAYS - days)
        return f"PRO eligible in {remaining} days" if remaining > 0 else "PRO eligible now"
    return "PRO — no further promotion"


def roster_cost(conn, shift_date: str) -> dict:
    """Calculate total wage cost for all published shifts on a given date."""
    rows = conn.execute(
        """SELECT b.name, b.level, b.hourly_rate, ps.slot_name
           FROM published_shifts ps
           JOIN baristas b ON b.id = ps.barista_id
           WHERE ps.shift_date = ?""",
        (shift_date,),
    ).fetchall()

    total = Decimal("0.00")
    lines = []
    for r in rows:
        rate = _money(r["hourly_rate"])
        cost = (rate * SHIFT_HOURS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += cost
        lines.append({
            "name":     r["name"],
            "level":    r["level"],
            "rate":     str(rate),
            "hours":    str(SHIFT_HOURS),
            "cost":     str(cost),
            "slot":     r["slot_name"],
        })

    return {
        "shift_date":  shift_date,
        "shifts":      lines,
        "total_eur":   str(total),
        "shift_count": len(lines),
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    from migrate import apply_migrations, get_connection, DEFAULT_DB

    DB_PATH = Path(os.getenv("DB_PATH", str(DEFAULT_DB)))
    apply_migrations(DB_PATH)

    parser = argparse.ArgumentParser(description="Barista HR Manager")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--check-promotions", action="store_true",
                     help="Scan ROOKIEs for 60-day promotion eligibility")
    grp.add_argument("--list", action="store_true",
                     help="List all baristas with level and tenure")
    grp.add_argument("--roster-cost", metavar="YYYY-MM-DD",
                     help="Calculate wage cost for a shift date")
    args = parser.parse_args()

    conn = get_connection(DB_PATH)
    try:
        if args.check_promotions:
            eligible = check_promotions(conn)
            print(json.dumps(eligible, indent=2, default=str))
        elif args.list:
            print(json.dumps(list_baristas(conn), indent=2, default=str))
        elif args.roster_cost:
            print(json.dumps(roster_cost(conn, args.roster_cost), indent=2, default=str))
    finally:
        conn.close()
