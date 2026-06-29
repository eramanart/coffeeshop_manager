"""
agent/scheduler_bot.py — Telegram scheduling bot for the barista team

Three channel roles
  #announcements  read-only, bot posts published rosters here
  #scheduling     baristas submit AVAIL / OFF / MY SHIFTS / ROSTER / SWAP
  #team-chat      free chat; bot monitors for SWAP CONFIRM / DECLINE

Entry points
  handle_message(update, conn)      called from api/main.py Telegram webhook
  run_weekly_roster_job(conn, fn)   called by APScheduler every Sunday 18:10 UTC
  publish_approved_roster(conn, …)  called by _dispatch_action on APPROVE

Constants exported for manual setup
  PINNED_INFO      post once to #scheduling and pin it
  MIGRATION_V6_SQL list of SQL strings for migration v6 in migrate.py
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("agent.scheduler_bot")

AGENT_NAME        = os.getenv("AGENT_NAME", "CoffeeBot")
BOT_USERNAME      = os.getenv("TELEGRAM_BOT_USERNAME", "barista_agent_bot")
LOCK_DAYS         = 5
ROSTER_DRAFTS_DIR = Path("data/rosters")

# ── Pinned info card — post to #scheduling once and pin it ────────────────────
# First line names the bot's @username so a new hire knows WHERE to type, not just
# what. SWAP uses a real slot name (Opening / Lunch peak / Afternoon / Evening from
# core.shift_suggester.SLOTS) rather than the placeholder word "slot".
PINNED_INFO = f"""\
📌 @{BOT_USERNAME} — Scheduling Commands
Message me directly (@{BOT_USERNAME}) with any of these:

AVAIL Mon Tue Wed Fri      available next week
OFF Sat Sun                unavailable next week
MY SHIFTS                  your upcoming shifts
ROSTER                     published roster
SWAP @name YYYY-MM-DD <slot>   ask someone to cover
   slots: Opening · Lunch peak · Afternoon · Evening
SWAP CONFIRM <id>          confirm a cover request
SWAP DECLINE <id>          decline a cover request
HELP                       show this list

Availability closes Sunday 17:00 UTC.
Changes within {LOCK_DAYS} days of a shift are locked.\
"""

# ── Migration v6 SQL — append to MIGRATIONS in migrate.py ────────────────────
MIGRATION_V6_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS baristas (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        username    TEXT    DEFAULT NULL,
        joined_at   TEXT    NOT NULL,
        is_active   INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_b_telegram ON baristas (telegram_id)",

    """
    CREATE TABLE IF NOT EXISTS barista_availability (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        barista_id   INTEGER NOT NULL REFERENCES baristas(id),
        week_start   TEXT    NOT NULL,
        day_of_week  INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        available    INTEGER NOT NULL DEFAULT 1,
        note         TEXT    DEFAULT NULL,
        submitted_at TEXT    NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ba_unique "
    "ON barista_availability (barista_id, week_start, day_of_week)",
    "CREATE INDEX IF NOT EXISTS idx_ba_week "
    "ON barista_availability (week_start, day_of_week)",

    """
    CREATE TABLE IF NOT EXISTS published_shifts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        shift_date   TEXT    NOT NULL,
        barista_id   INTEGER NOT NULL REFERENCES baristas(id),
        slot_name    TEXT    NOT NULL,
        slot_label   TEXT    NOT NULL,
        roster_key   TEXT    NOT NULL,
        published_at TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ps_date    ON published_shifts (shift_date)",
    "CREATE INDEX IF NOT EXISTS idx_ps_barista ON published_shifts (barista_id)",
    "CREATE INDEX IF NOT EXISTS idx_ps_roster  ON published_shifts (roster_key)",

    """
    CREATE TABLE IF NOT EXISTS swap_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL REFERENCES baristas(id),
        target_id    INTEGER NOT NULL REFERENCES baristas(id),
        shift_date   TEXT    NOT NULL,
        slot_name    TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','confirmed','declined','cancelled')),
        created_at   TEXT    NOT NULL,
        resolved_at  TEXT    DEFAULT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sr_requester ON swap_requests (requester_id)",
    "CREATE INDEX IF NOT EXISTS idx_sr_target    ON swap_requests (target_id)",
    "CREATE INDEX IF NOT EXISTS idx_sr_status    ON swap_requests (status)",
]

# ── Day name → weekday int (0 = Monday) ──────────────────────────────────────
_DAY_MAP: dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DOW_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_days(tokens: list[str]) -> list[int]:
    """Parse day-name tokens into sorted weekday ints. Supports Mon-Fri ranges."""
    days: set[int] = set()
    for tok in tokens:
        tok = tok.lower().strip(",.")
        if "-" in tok:
            a, _, b = tok.partition("-")
            s, e = _DAY_MAP.get(a), _DAY_MAP.get(b)
            if s is not None and e is not None:
                days.update(range(s, e + 1))
        elif tok in _DAY_MAP:
            days.add(_DAY_MAP[tok])
    return sorted(days)


def _next_week_start() -> date:
    today      = date.today()
    days_ahead = (7 - today.weekday()) % 7
    return today + timedelta(days=days_ahead if days_ahead else 7)


# ── Auto-registration ─────────────────────────────────────────────────────────

def _get_or_create_barista(conn, telegram_id: int, name: str, username: str | None) -> dict:
    row = conn.execute(
        "SELECT * FROM baristas WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row:
        return dict(row)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO baristas (telegram_id, name, username, joined_at) VALUES (?, ?, ?, ?)",
        (telegram_id, name, username, now),
    )
    conn.commit()
    log.info("Auto-registered barista: %s (tg=%s)", name, telegram_id)
    return dict(conn.execute(
        "SELECT * FROM baristas WHERE telegram_id = ?", (telegram_id,)
    ).fetchone())


# ── Webhook entry point ───────────────────────────────────────────────────────

async def handle_message(update: dict, conn) -> str | None:
    """
    Single entry point from api/main.py webhook.
    Returns a reply string, or None if the message should be silently ignored.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None

    text = (msg.get("text") or "").strip()
    if not text:
        return None

    sender      = msg.get("from", {})
    telegram_id = sender.get("id")
    if not telegram_id:
        return None

    name     = sender.get("first_name") or "Barista"
    username = sender.get("username")
    barista  = _get_or_create_barista(conn, telegram_id, name, username)

    upper  = text.upper()
    tokens = text.split()

    try:
        if upper.startswith("AVAIL"):
            return _cmd_avail(conn, barista, tokens[1:], available=True)
        if upper.startswith("OFF"):
            return _cmd_avail(conn, barista, tokens[1:], available=False)
        if upper.startswith("MY SHIFT"):
            return _cmd_my_shifts(conn, barista)
        if upper.startswith("ROSTER"):
            return _cmd_roster(conn, tokens[1:])
        if upper.startswith("SWAP CONFIRM"):
            return _cmd_swap_resolve(conn, barista, tokens, "confirmed")
        if upper.startswith("SWAP DECLINE"):
            return _cmd_swap_resolve(conn, barista, tokens, "declined")
        if upper.startswith("SWAP"):
            return _cmd_swap_request(conn, barista, tokens[1:])
        if upper.strip() in ("HELP", "/HELP", "START", "/START"):
            return PINNED_INFO
    except Exception as exc:
        log.error("handle_message error (%r): %s", text, exc)
        return "Something went wrong — please try again or type HELP."

    return None


# ── AVAIL / OFF ───────────────────────────────────────────────────────────────

def _cmd_avail(conn, barista: dict, tokens: list[str], available: bool) -> str:
    days = _parse_days(tokens)
    if not days:
        eg = "AVAIL Mon Tue Wed" if available else "OFF Sat Sun"
        return f"Specify days. Example: {eg}"

    week_start = _next_week_start()
    now        = datetime.now(timezone.utc).isoformat()
    saved: list[str] = []
    locked: list[str] = []

    for dow in days:
        shift_date = week_start + timedelta(days=dow)
        if (shift_date - date.today()).days < LOCK_DAYS:
            locked.append(shift_date.strftime("%a %d"))
            continue
        conn.execute(
            """INSERT INTO barista_availability
               (barista_id, week_start, day_of_week, available, submitted_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(barista_id, week_start, day_of_week)
               DO UPDATE SET available = excluded.available,
                             submitted_at = excluded.submitted_at""",
            (barista["id"], week_start.isoformat(), dow, 1 if available else 0, now),
        )
        saved.append(shift_date.strftime("%a %d"))

    conn.commit()
    action = "available" if available else "unavailable"
    parts: list[str] = []
    if saved:
        parts.append(f"Marked {action}: {', '.join(saved)} (wk {week_start.strftime('%b %d')})")
    if locked:
        parts.append(f"Locked — within {LOCK_DAYS} days: {', '.join(locked)}")
    return "\n".join(parts) or "No changes made."


# ── MY SHIFTS ─────────────────────────────────────────────────────────────────

def _cmd_my_shifts(conn, barista: dict) -> str:
    rows = conn.execute(
        """SELECT shift_date, slot_label
           FROM published_shifts
           WHERE barista_id = ? AND shift_date >= date('now')
           ORDER BY shift_date, rowid
           LIMIT 14""",
        (barista["id"],),
    ).fetchall()
    if not rows:
        return f"No upcoming shifts published yet, {barista['name']}."
    lines = [f"Upcoming shifts — {barista['name']}:"]
    for r in rows:
        d = date.fromisoformat(r["shift_date"])
        lines.append(f"  {d.strftime('%a %d %b')}  {r['slot_label']}")
    return "\n".join(lines)


# ── ROSTER ────────────────────────────────────────────────────────────────────

def _cmd_roster(conn, tokens: list[str]) -> str:
    if tokens:
        try:
            anchor = date.fromisoformat(tokens[0])
        except ValueError:
            anchor = date.today()
    else:
        row = conn.execute(
            "SELECT MIN(shift_date) FROM published_shifts WHERE shift_date >= date('now')"
        ).fetchone()
        anchor = date.fromisoformat(row[0]) if (row and row[0]) else date.today()

    week_start = anchor - timedelta(days=anchor.weekday())
    week_end   = week_start + timedelta(days=6)

    rows = conn.execute(
        """SELECT ps.shift_date, ps.slot_label, b.name
           FROM published_shifts ps
           JOIN baristas b ON b.id = ps.barista_id
           WHERE ps.shift_date BETWEEN ? AND ?
           ORDER BY ps.shift_date, ps.rowid""",
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()

    if not rows:
        return "No roster published for that week."

    by_date: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        by_date.setdefault(r["shift_date"], {}).setdefault(r["slot_label"], []).append(r["name"])

    lines = [f"Roster — {week_start.strftime('%b %d')} to {week_end.strftime('%b %d')}:"]
    for d_str in sorted(by_date):
        d = date.fromisoformat(d_str)
        lines.append(f"\n{d.strftime('%A %d %b')}:")
        for label, names in by_date[d_str].items():
            lines.append(f"  {label}: {', '.join(names)}")
    return "\n".join(lines)


# ── SWAP request ──────────────────────────────────────────────────────────────

def _cmd_swap_request(conn, barista: dict, tokens: list[str]) -> str:
    if len(tokens) < 3:
        return "Usage: SWAP @name YYYY-MM-DD slot\nExample: SWAP @Ana 2026-05-23 Lunch peak"

    target_handle = tokens[0].lstrip("@")
    try:
        shift_date = date.fromisoformat(tokens[1])
    except ValueError:
        return "Invalid date — use YYYY-MM-DD."

    slot_name = " ".join(tokens[2:])
    res = create_swap(conn, barista["id"], target_handle, shift_date, slot_name)
    if not res["ok"]:
        return res["error"]
    return (
        f"Swap request #{res['swap_id']} sent to {res['target_name']}.\n"
        f"Ask them to reply: SWAP CONFIRM {res['swap_id']}"
    )


# ── SWAP CONFIRM / DECLINE ────────────────────────────────────────────────────

def _cmd_swap_resolve(conn, barista: dict, tokens: list[str], status: str) -> str:
    action = "CONFIRM" if status == "confirmed" else "DECLINE"
    if len(tokens) < 3 or not tokens[2].isdigit():
        return f"Usage: SWAP {action} <id>"

    res = resolve_swap(conn, barista["id"], int(tokens[2]), status)
    if not res["ok"]:
        return res["error"]
    word = "confirmed" if status == "confirmed" else "declined"
    return f"Swap #{res['swap_id']} with {res['requester_name']} has been {word}."


# ── Programmatic scheduling API ───────────────────────────────────────────────
# Structured (dict-returning) counterparts to the Telegram _cmd_* handlers, used
# by the Mini App endpoints in api/miniapp.py. The Telegram commands above
# delegate to create_swap / resolve_swap so the two surfaces share ONE
# implementation of the lock, the on-shift check, the cost-alert guardrail and
# the published_shifts transfer — there is no second copy to drift.

def create_swap(conn, requester_id: int, target_handle: str,
                shift_date: date, slot_name: str) -> dict:
    """
    Create a pending swap request. Enforces the 5-day lock, that the requester
    actually holds that shift, and that the target barista exists.

    Returns {'ok': True, 'swap_id', 'target_id', 'target_name'} on success, or
            {'ok': False, 'error': <human message>} otherwise.
    """
    if (shift_date - date.today()).days < LOCK_DAYS:
        return {"ok": False, "error": f"That shift is within {LOCK_DAYS} days and is locked."}

    on_shift = conn.execute(
        """SELECT 1 FROM published_shifts
           WHERE barista_id = ? AND shift_date = ? AND lower(slot_name) = ?""",
        (requester_id, shift_date.isoformat(), slot_name.lower()),
    ).fetchone()
    if not on_shift:
        return {"ok": False, "error": f"You are not on the '{slot_name}' shift on {shift_date}."}

    handle = target_handle.lstrip("@").lower()
    target = conn.execute(
        "SELECT * FROM baristas WHERE lower(name) = ? OR lower(username) = ?",
        (handle, handle),
    ).fetchone()
    if not target:
        return {"ok": False,
                "error": f"Barista '{handle}' not found — they need to message {AGENT_NAME} first."}

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO swap_requests
           (requester_id, target_id, shift_date, slot_name, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (requester_id, target["id"], shift_date.isoformat(), slot_name, now),
    )
    conn.commit()
    swap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"ok": True, "swap_id": swap_id,
            "target_id": target["id"], "target_name": target["name"]}


def resolve_swap(conn, target_id: int, swap_id: int, status: str) -> dict:
    """
    Confirm or decline a pending swap that is addressed to `target_id`.
    On confirm, runs the budget guardrail and transfers the published shift.

    Returns {'ok': True, 'swap_id', 'status', 'requester_id', 'requester_name'}
            on success, or {'ok': False, 'error': <human message>} otherwise.
    """
    row = conn.execute(
        "SELECT * FROM swap_requests WHERE id = ? AND target_id = ? AND status = 'pending'",
        (swap_id, target_id),
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"Swap request #{swap_id} not found or not pending for you."}

    row = dict(row)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE swap_requests SET status = ?, resolved_at = ? WHERE id = ?",
        (status, now, swap_id),
    )
    if status == "confirmed":
        # Budget guardrail: alert owner if higher-rate barista is taking the shift
        try:
            from core.hr_manager import swap_cost_alert
            alert = swap_cost_alert(conn, row["requester_id"], target_id, row["shift_date"])
            if alert:
                from migrate import notify_if_new
                event_key = f"SWAP_COST:{swap_id}"
                notify_if_new(conn, "SWAP_COST_ALERT", event_key, alert["message"])
        except Exception as _exc:
            import logging as _log
            _log.getLogger("scheduler_bot").warning("swap_cost_alert failed: %s", _exc)

        # Transfer requester's shift assignment to the target
        conn.execute(
            """UPDATE published_shifts SET barista_id = ?
               WHERE shift_date = ? AND lower(slot_name) = ? AND barista_id = ?""",
            (target_id, row["shift_date"], row["slot_name"].lower(), row["requester_id"]),
        )
    conn.commit()

    requester = conn.execute(
        "SELECT name FROM baristas WHERE id = ?", (row["requester_id"],)
    ).fetchone()
    return {"ok": True, "swap_id": swap_id, "status": status,
            "requester_id": row["requester_id"],
            "requester_name": requester["name"] if requester else "that barista"}


def set_availability_for_date(conn, barista_id: int, the_date: date,
                              available: bool | None) -> dict:
    """
    Set (or clear, when available is None) a barista's availability for ONE date,
    enforcing the same 5-day lock as the AVAIL/OFF commands.

    Returns {'ok', 'locked', 'available', 'date'} — ok=False with locked=True
    when the date falls inside the lock window.
    """
    if (the_date - date.today()).days < LOCK_DAYS:
        return {"ok": False, "locked": True, "available": None,
                "date": the_date.isoformat(),
                "error": f"That day is within {LOCK_DAYS} days and is locked."}

    week_start = the_date - timedelta(days=the_date.weekday())
    dow = the_date.weekday()

    if available is None:
        conn.execute(
            """DELETE FROM barista_availability
               WHERE barista_id = ? AND week_start = ? AND day_of_week = ?""",
            (barista_id, week_start.isoformat(), dow),
        )
        conn.commit()
        return {"ok": True, "locked": False, "available": None, "date": the_date.isoformat()}

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO barista_availability
           (barista_id, week_start, day_of_week, available, submitted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(barista_id, week_start, day_of_week)
           DO UPDATE SET available = excluded.available,
                         submitted_at = excluded.submitted_at""",
        (barista_id, week_start.isoformat(), dow, 1 if available else 0, now),
    )
    conn.commit()
    return {"ok": True, "locked": False, "available": available, "date": the_date.isoformat()}


def get_barista_schedule(conn, barista_id: int, date_from: date, date_to: date) -> dict:
    """
    Read-mostly payload for the Mini App calendar: this barista's published
    shifts and the full team roster (names + slots only) across [date_from,
    date_to], plus this barista's own availability marks in range.

    Shape:
      {
        "days": { "YYYY-MM-DD": {
            "mine": {"slot_name","slot_label"} | None,
            "team": [{"slot_name","slot_label","name","is_me"}, ...] } },
        "availability": { "YYYY-MM-DD": true|false },
        "published_through": "YYYY-MM-DD" | None,
        "lock_days": 5
      }
    """
    rows = conn.execute(
        """SELECT ps.shift_date, ps.slot_name, ps.slot_label, ps.barista_id, b.name
           FROM published_shifts ps
           JOIN baristas b ON b.id = ps.barista_id
           WHERE ps.shift_date BETWEEN ? AND ?
           ORDER BY ps.shift_date, ps.rowid""",
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchall()

    days: dict[str, dict] = {}
    for r in rows:
        day = days.setdefault(r["shift_date"], {"mine": None, "team": []})
        is_me = r["barista_id"] == barista_id
        day["team"].append({
            "slot_name": r["slot_name"], "slot_label": r["slot_label"],
            "name": r["name"], "is_me": is_me,
        })
        if is_me:
            day["mine"] = {"slot_name": r["slot_name"], "slot_label": r["slot_label"]}

    availability: dict[str, bool] = {}
    for r in conn.execute(
        "SELECT week_start, day_of_week, available FROM barista_availability WHERE barista_id = ?",
        (barista_id,),
    ).fetchall():
        d = date.fromisoformat(r["week_start"]) + timedelta(days=r["day_of_week"])
        if date_from <= d <= date_to:
            availability[d.isoformat()] = bool(r["available"])

    pub = conn.execute("SELECT MAX(shift_date) FROM published_shifts").fetchone()
    published_through = pub[0] if pub and pub[0] else None

    return {
        "days": days,
        "availability": availability,
        "published_through": published_through,
        "lock_days": LOCK_DAYS,
    }


# ── Roster builder ────────────────────────────────────────────────────────────

def build_weekly_roster(conn, week_start: date) -> dict:
    """
    Build the 7-day roster using forecast + fairness-scored availability.

    Returns:
        {
            "week_start":     "YYYY-MM-DD",
            "days":           [{date, dow_name, forecast_total, confidence, slots}, ...],
            "warnings":       [str, ...],
            "total_assigned": int,
        }
    Each slot has: {name, label, needed, assigned: [{id, name}], uncovered}.
    """
    from core.labor_forecast import forecast_day
    from core.shift_suggester import suggest_shifts, SLOTS

    fairness = _fairness_scores(conn)
    warnings: list[str] = []
    days_out: list[dict] = []

    for i in range(7):
        target = week_start + timedelta(days=i)
        try:
            forecast   = forecast_day(conn, target)
            suggestion = suggest_shifts(forecast)
        except Exception as exc:
            log.warning("Forecast failed for %s: %s", target, exc)
            continue

        available = _available_baristas(conn, target)
        available.sort(key=lambda b: (fairness.get(b["id"], 0), b["name"]))

        assigned_today: set[int] = set()
        slots_out: list[dict]    = []

        for slot_def, slot_sug in zip(SLOTS, suggestion["slots"]):
            needed  = slot_sug["baristas"]
            pool    = [b for b in available if b["id"] not in assigned_today]
            picked  = pool[:needed]
            for b in picked:
                assigned_today.add(b["id"])

            uncovered = needed - len(picked)
            if uncovered:
                warnings.append(
                    f"{target.strftime('%a %d')}: {slot_def['name']} needs "
                    f"{needed} — only {len(picked)} available."
                )

            slots_out.append({
                "name":      slot_def["name"],
                "label":     slot_def["label"],
                "needed":    needed,
                "assigned":  [{"id": b["id"], "name": b["name"]} for b in picked],
                "uncovered": uncovered,
            })

        days_out.append({
            "date":           target.isoformat(),
            "dow_name":       forecast["dow_name"],
            "forecast_total": forecast["total_forecast"],
            "confidence":     forecast["confidence"],
            "slots":          slots_out,
        })

    total = sum(len(s["assigned"]) for d in days_out for s in d["slots"])
    return {
        "week_start":     week_start.isoformat(),
        "days":           days_out,
        "warnings":       warnings,
        "total_assigned": total,
    }


def _fairness_scores(conn) -> dict[int, int]:
    """Return {barista_id: shifts_last_30_days}. Fewer shifts = higher priority."""
    rows = conn.execute(
        """SELECT barista_id, COUNT(*) AS cnt
           FROM published_shifts
           WHERE shift_date >= date('now', '-30 days')
           GROUP BY barista_id"""
    ).fetchall()
    return {r["barista_id"]: r["cnt"] for r in rows}


def _available_baristas(conn, target: date) -> list[dict]:
    """
    Baristas available on target date.
    Falls back to all active baristas when no availability is submitted for the week.
    """
    week_start = target - timedelta(days=target.weekday())
    dow        = target.weekday()

    has_week = conn.execute(
        "SELECT 1 FROM barista_availability WHERE week_start = ? LIMIT 1",
        (week_start.isoformat(),),
    ).fetchone()

    if not has_week:
        rows = conn.execute(
            "SELECT id, name FROM baristas WHERE is_active = 1"
        ).fetchall()
        return [dict(r) for r in rows]

    rows = conn.execute(
        """SELECT b.id, b.name
           FROM baristas b
           WHERE b.is_active = 1
             AND (
               EXISTS (
                 SELECT 1 FROM barista_availability ba
                 WHERE ba.barista_id = b.id
                   AND ba.week_start = ?
                   AND ba.day_of_week = ?
                   AND ba.available = 1
               )
               OR NOT EXISTS (
                 SELECT 1 FROM barista_availability ba
                 WHERE ba.barista_id = b.id AND ba.week_start = ?
               )
             )""",
        (week_start.isoformat(), dow, week_start.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


# ── APScheduler job ───────────────────────────────────────────────────────────

async def run_weekly_roster_job(conn, send_telegram_fn: Callable) -> None:
    """Called by APScheduler every Sunday 18:10 UTC."""
    next_monday = _next_week_start()
    iso_week    = next_monday.strftime("%Y-W%W")
    roster_key  = f"ROSTER_DRAFT:{iso_week}"

    try:
        roster = build_weekly_roster(conn, next_monday)
        _save_draft(roster_key, roster)

        warn_line = f"\n\n⚠️ {len(roster['warnings'])} coverage gap(s)." if roster["warnings"] else ""
        msg = (
            f"{_format_roster(roster)}{warn_line}\n\n"
            f"Reply *APPROVE {roster_key}* to publish to #announcements.\n"
            f"Reply *SKIP {roster_key}* to discard."
        )
        send_telegram_fn(conn, "ROSTER_DRAFT", roster_key, msg)
        log.info("Roster draft sent: %s (%d shifts)", iso_week, roster["total_assigned"])
    except Exception as exc:
        log.error("run_weekly_roster_job failed: %s", exc)


# ── Publish on APPROVE ────────────────────────────────────────────────────────

def publish_approved_roster(conn, iso_week: str, send_telegram_fn: Callable) -> None:
    """
    Write published_shifts rows and post the roster to #announcements.
    Called from _dispatch_action in api/main.py.
    """
    roster_key = f"ROSTER_DRAFT:{iso_week}"
    roster     = _load_draft(roster_key)
    if not roster:
        log.error("No saved roster draft for %s", roster_key)
        return

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for day in roster["days"]:
        for slot in day["slots"]:
            for barista in slot["assigned"]:
                conn.execute(
                    """INSERT OR IGNORE INTO published_shifts
                       (shift_date, barista_id, slot_name, slot_label, roster_key, published_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (day["date"], barista["id"], slot["name"], slot["label"], roster_key, now),
                )
                count += 1
    conn.commit()
    log.info("Published roster %s: %d shifts written", iso_week, count)

    announce_msg = (
        f"📅 *Roster — week of {roster['week_start']}*\n\n"
        f"{_format_roster(roster)}"
    )
    _telegram_post_group(
        announce_msg,
        thread_id=_int_env("TELEGRAM_ANNOUNCE_THREAD"),
    )
    send_telegram_fn(
        conn, "ROSTER_PUBLISHED", f"ROSTER_PUBLISHED:{iso_week}",
        f"✅ Roster published for {iso_week} — {count} shifts written.",
    )


# ── Draft persistence ─────────────────────────────────────────────────────────

def _save_draft(roster_key: str, roster: dict) -> None:
    ROSTER_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = roster_key.replace(":", "_") + ".json"
    (ROSTER_DRAFTS_DIR / fname).write_text(
        json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_draft(roster_key: str) -> dict | None:
    fname = roster_key.replace(":", "_") + ".json"
    path  = ROSTER_DRAFTS_DIR / fname
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ── Message formatter ─────────────────────────────────────────────────────────

def _format_roster(roster: dict) -> str:
    week_start = date.fromisoformat(roster["week_start"])
    week_end   = week_start + timedelta(days=6)
    lines = [f"*{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}*"]
    for day in roster["days"]:
        d = date.fromisoformat(day["date"])
        lines.append(f"\n*{d.strftime('%A %d %b')}* (confidence {day['confidence']}%)")
        for slot in day["slots"]:
            names   = ", ".join(b["name"] for b in slot["assigned"]) or "—"
            gap_str = f"  ⚠️ +{slot['uncovered']}" if slot["uncovered"] else ""
            lines.append(f"  {slot['label']}: {names}{gap_str}")
    return "\n".join(lines)


# ── Telegram group helper (for #announcements) ────────────────────────────────

def _telegram_post_group(text: str, thread_id: int | None) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_GROUP_ID", "")
    if not token or not chat_id:
        log.warning("TELEGRAM_GROUP_ID not set — skipping group post")
        return
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.error("Telegram group post failed: %s", result)
    except Exception as exc:
        log.error("Telegram group post error: %s", exc)


def _int_env(key: str) -> int | None:
    val = os.getenv(key, "")
    return int(val) if val.lstrip("-").isdigit() else None
