"""
api/miniapp.py — Telegram Mini App backend for the barista shift calendar.

A friendlier face on the scheduling the bot already runs: the same
published_shifts / barista_availability / swap_requests tables, the same 5-day
lock, surfaced as a small set of read-mostly HTTP endpoints that the
barista_calendar.html Mini App calls.

Routes (registered via register_miniapp_routes(app)):
  GET  /miniapp                    → serve the calendar page (identity comes from
                                     initData, so nothing secret is embedded)
  GET  /miniapp/schedule           → this barista's shifts + team roster + availability
  POST /miniapp/availability       → set/clear one date (honours the 5-day lock)
  POST /miniapp/swap               → create a swap request
  POST /miniapp/swap/{swap_id}     → accept / decline a swap request

AUTH MODEL — every data route depends on require_barista, which validates the
signed tg.initData with the bot token (api.helpers.verify_init_data) and maps it
to a barista row. This is what lets each barista see only their own data with no
login. initDataUnsafe is never trusted. These routes are intentionally OUTSIDE
the dashboard's Basic-auth prefixes in main.py — they carry their own auth.
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from migrate import get_connection, DEFAULT_DB
from api.helpers import verify_init_data
from agent import scheduler_bot

# The Mini App HTML. Defaults to api/barista_calendar.html (next to this module),
# overridable via MINIAPP_HTML_PATH for a file kept elsewhere. We never trust cwd.
_DEFAULT_HTML = Path(__file__).resolve().parent / "barista_calendar.html"

# How far ahead the calendar asks for by default when no explicit range is given.
_DEFAULT_RANGE_DAYS = 45


# ── Request bodies ────────────────────────────────────────────────────────────

class AvailabilityBody(BaseModel):
    date: str                      # YYYY-MM-DD
    available: bool | None = None  # True / False to set, null to clear


class SwapCreateBody(BaseModel):
    date: str                          # YYYY-MM-DD
    target: str | None = None          # @username or name (canonical field)
    to_barista: str | None = None      # the front end sends this; same meaning
    slot_name: str | None = None       # optional — derived from the caller's shift
                                       # that day when absent (the client knows the
                                       # day and the person; the server owns the slot)


class SwapResolveBody(BaseModel):
    action: str                    # "accept" | "decline"


# ── Per-request DB dependency (mirrors main.get_db; FastAPI caches it per request,
#    so require_barista and the endpoint share one connection) ──────────────────

def _get_db():
    conn = get_connection(DEFAULT_DB)
    try:
        yield conn
    finally:
        conn.close()


# Stable, namespaced Telegram id for the local dev bypass barista (used only when
# MINIAPP_DEV_USER is a name rather than an existing barista's numeric id).
_DEV_BYPASS_TG_ID = -9999


def _dev_bypass_barista(conn) -> dict | None:
    """
    Dev-only auth bypass for testing the live fetch path on localhost without
    Telegram. Active ONLY when MINIAPP_DEV_USER is set AND we are not in
    production (DRY_RUN != "false"). Returns None otherwise, so production —
    where the flag is unset and DRY_RUN=false — never touches this path.

    MINIAPP_DEV_USER may be:
      * a numeric Telegram id  → impersonate that existing barista (real data)
      * any other string       → get/create a throwaway barista of that name
    """
    dev_user = os.getenv("MINIAPP_DEV_USER", "").strip()
    if not dev_user:
        return None
    if os.getenv("DRY_RUN", "true").strip().lower() == "false":
        return None  # production: bypass is inert even if the flag was left set

    try:
        tg_id, name = int(dev_user), "Dev Tester"
    except ValueError:
        tg_id, name = _DEV_BYPASS_TG_ID, dev_user
    return scheduler_bot._get_or_create_barista(conn, tg_id, name, "devuser")


def require_barista(request: Request, conn=Depends(_get_db)) -> dict:
    """
    Resolve the calling barista from the signed Telegram initData header, or 401.

    The page sends initData as `X-Init-Data`. We validate its HMAC against the
    bot token before trusting any user id — this is the whole security boundary.
    """
    dev = _dev_bypass_barista(conn)
    if dev is not None:
        return dev

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        # Fail closed: without the token we cannot verify anyone.
        raise HTTPException(status_code=503, detail="Telegram bot token not configured")

    init_data = request.headers.get("X-Init-Data", "")
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing X-Init-Data header")

    user = verify_init_data(init_data, bot_token)
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Invalid or expired init data")

    return scheduler_bot._get_or_create_barista(
        conn,
        int(user["id"]),
        user.get("first_name") or "Barista",
        user.get("username"),
    )


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field} — use YYYY-MM-DD")


# ── Storage → Mini App contract adapter ───────────────────────────────────────
# The ONE translation point between how the schedule is stored (days map,
# slot_label as a joined "HH:MM–HH:MM" string, is_me, one-way swaps) and how the
# calendar front end reads it (shifts[] with start/end, me as a name, swaps_*).
# Keeping it here means the boundary lives at the boundary: any other consumer
# of schedule data reads the same clean shape instead of re-solving this puzzle.

_DASH = re.compile(r"\s*[–—-]\s*")     # en-dash, em-dash, or hyphen, padded or not
_HHMM = re.compile(r"\d{1,2}:\d{2}")


def _split_slot_label(label: str | None) -> tuple[str, str]:
    """'08:00–14:00' -> ('08:00', '14:00'). Defensive: if it is not a clean
    time–time pair, return (label, '') so the UI shows the label verbatim rather
    than a corrupted start time that would silently mis-colour the day."""
    if not label:
        return "", ""
    parts = _DASH.split(label.strip(), maxsplit=1)
    if len(parts) == 2 and _HHMM.fullmatch(parts[0]) and _HHMM.fullmatch(parts[1]):
        return parts[0], parts[1]
    return label, ""                   # fallback: never fabricate a time


def _swaps_for_barista(conn, barista_id: int) -> tuple[list, list]:
    """Read this barista's pending swaps from the real (one-way 'cover') model.

    swaps_in  = requests addressed to me — someone wants me to cover their shift.
    swaps_out = my own requests still pending a teammate's reply.

    The swapped shift is identified only by (shift_date, slot_name); we LEFT JOIN
    published_shifts on the requester's row to recover the time label, which the
    swap_requests table itself does not store.
    """
    rows_in = conn.execute(
        """SELECT sr.id, sr.shift_date, sr.slot_name, rb.name AS from_name,
                  ps.slot_label
           FROM swap_requests sr
           JOIN baristas rb ON rb.id = sr.requester_id
           LEFT JOIN published_shifts ps
                  ON ps.shift_date = sr.shift_date
                 AND lower(ps.slot_name) = lower(sr.slot_name)
                 AND ps.barista_id = sr.requester_id
           WHERE sr.target_id = ? AND sr.status = 'pending'
           ORDER BY sr.created_at DESC""",
        (barista_id,),
    ).fetchall()

    rows_out = conn.execute(
        """SELECT sr.id, sr.shift_date, sr.slot_name, sr.status, tb.name AS to_name,
                  ps.slot_label
           FROM swap_requests sr
           JOIN baristas tb ON tb.id = sr.target_id
           LEFT JOIN published_shifts ps
                  ON ps.shift_date = sr.shift_date
                 AND lower(ps.slot_name) = lower(sr.slot_name)
                 AND ps.barista_id = sr.requester_id
           WHERE sr.requester_id = ? AND sr.status = 'pending'
           ORDER BY sr.created_at DESC""",
        (barista_id,),
    ).fetchall()

    def one_way(r) -> dict:
        start, end = _split_slot_label(r["slot_label"])
        return {"id": str(r["id"]), "date": r["shift_date"],
                "label": r["slot_name"], "start": start, "end": end}

    swaps_in = [{**one_way(r), "from": r["from_name"]} for r in rows_in]
    swaps_out = [{**one_way(r), "to": r["to_name"], "status": r["status"]}
                 for r in rows_out]
    return swaps_in, swaps_out


def _adapt_schedule(payload: dict, conn, barista_id: int) -> dict:
    """Translate the internal schedule payload into the Mini App contract."""
    shifts = []
    for the_date, day in (payload.get("days") or {}).items():
        for slot in day.get("team", []):
            start, end = _split_slot_label(slot.get("slot_label"))
            shifts.append({
                "date":    the_date,
                "label":   slot.get("slot_name") or slot.get("slot_label", ""),
                "start":   start,
                "end":     end,
                "barista": slot.get("name", ""),
                "mine":    bool(slot.get("is_me")),     # exact, not name-matched
            })

    me = payload.get("me") or {}
    swaps_in, swaps_out = _swaps_for_barista(conn, barista_id)
    return {
        "me":                me.get("name", ""),        # contract wants a string
        "published_through": payload.get("published_through"),
        "lock_days":         payload.get("lock_days", 5),
        "availability":      payload.get("availability", {}),
        "shifts":            shifts,
        "swaps_in":          swaps_in,
        "swaps_out":         swaps_out,
    }


def _slot_name_for(conn, barista_id: int, the_date: date) -> str | None:
    """The caller's slot on a given day, so a swap request need not send it."""
    row = conn.execute(
        """SELECT slot_name FROM published_shifts
           WHERE barista_id = ? AND shift_date = ? ORDER BY rowid LIMIT 1""",
        (barista_id, the_date.isoformat()),
    ).fetchone()
    return row["slot_name"] if row else None


# ── Route registration ────────────────────────────────────────────────────────

def register_miniapp_routes(app: FastAPI) -> None:

    @app.get("/miniapp", response_class=HTMLResponse, tags=["MiniApp"])
    async def miniapp_page():
        """Serve the calendar page. Identity comes from initData at runtime, so
        nothing secret is embedded — safe to serve without Basic auth."""
        html_path = Path(os.getenv("MINIAPP_HTML_PATH", str(_DEFAULT_HTML)))
        if not html_path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"Mini App page not found at {html_path}. Save barista_calendar.html "
                       f"there, or set MINIAPP_HTML_PATH to its location.",
            )
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            media_type="text/html; charset=utf-8",
        )

    @app.get("/miniapp/schedule", tags=["MiniApp"])
    async def miniapp_schedule(
        barista: dict = Depends(require_barista),
        conn=Depends(_get_db),
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
    ) -> dict:
        """This barista's shifts + the team roster + their availability in range."""
        start = _parse_date(date_from, "from") if date_from else date.today()
        end = _parse_date(date_to, "to") if date_to else start + timedelta(days=_DEFAULT_RANGE_DAYS)
        if end < start:
            raise HTTPException(status_code=400, detail="'to' must not precede 'from'")

        payload = scheduler_bot.get_barista_schedule(conn, barista["id"], start, end)
        payload["me"] = {"id": barista["id"], "name": barista["name"]}
        out = _adapt_schedule(payload, conn, barista["id"])
        out["range"] = {"from": start.isoformat(), "to": end.isoformat()}
        return out

    @app.post("/miniapp/availability", tags=["MiniApp"])
    async def miniapp_availability(
        body: AvailabilityBody,
        barista: dict = Depends(require_barista),
        conn=Depends(_get_db),
    ) -> dict:
        """Set or clear this barista's availability for one date (5-day lock enforced)."""
        the_date = _parse_date(body.date, "date")
        result = scheduler_bot.set_availability_for_date(
            conn, barista["id"], the_date, body.available
        )
        if not result["ok"]:
            # 409: the request was well-formed but the lock window refuses it.
            raise HTTPException(status_code=409, detail=result.get("error", "Locked"))
        return result

    @app.post("/miniapp/swap", tags=["MiniApp"])
    async def miniapp_swap_create(
        body: SwapCreateBody,
        barista: dict = Depends(require_barista),
        conn=Depends(_get_db),
    ) -> dict:
        """Ask a teammate to cover one of your shifts.

        The client sends only what it knows — the day and the person ({date,
        to_barista}). The slot is the caller's shift that day, which the server
        already owns, so we derive it rather than make the client send it."""
        the_date = _parse_date(body.date, "date")
        target = body.target or body.to_barista
        if not target:
            raise HTTPException(status_code=400, detail="target (or to_barista) is required")
        slot_name = body.slot_name or _slot_name_for(conn, barista["id"], the_date)
        if not slot_name:
            raise HTTPException(status_code=409, detail="You have no shift on that date to swap")
        result = scheduler_bot.create_swap(
            conn, barista["id"], target, the_date, slot_name
        )
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["error"])
        return result

    @app.post("/miniapp/swap/{swap_id}", tags=["MiniApp"])
    async def miniapp_swap_resolve(
        swap_id: int,
        body: SwapResolveBody,
        barista: dict = Depends(require_barista),
        conn=Depends(_get_db),
    ) -> dict:
        """Accept or decline a swap request addressed to you."""
        action = body.action.lower().strip()
        if action not in ("accept", "decline"):
            raise HTTPException(status_code=400, detail="action must be 'accept' or 'decline'")
        status = "confirmed" if action == "accept" else "declined"
        result = scheduler_bot.resolve_swap(conn, barista["id"], swap_id, status)
        if not result["ok"]:
            # Not pending / not yours / unknown id — 404 fits better than 409 here.
            raise HTTPException(status_code=404, detail=result["error"])
        return result
