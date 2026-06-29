"""
core/shift_suggester.py — converts a labor forecast into a concrete roster suggestion

Input:  forecast dict from labor_forecast.forecast_day()
Output: shift suggestion dict + Telegram message draft

Suggestion logic:
  1. Divide the day into 4 slots (opening, lunch, afternoon, evening).
  2. For each slot, compute revenue intensity vs daily peak.
  3. Map intensity to barista count (1–3) using configurable thresholds.
  4. Compute labor cost at BARISTA_HOURLY_RATE.
  5. Compare total cost to LABOR_TARGET_PCT of forecast revenue.
  6. If over budget: suggest removing a barista from the quietest slot.
  7. Draft Telegram message with APPROVE / EDIT / SKIP HITL actions.

All arithmetic uses Python float (not Decimal) — these are suggestions,
not financial filings. Revenue and cost figures are rounded to 2dp for display.

CLI:
    python core/shift_suggester.py --date 2026-05-17
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("core.shift_suggester")

BARISTA_HOURLY_RATE = float(os.getenv("BARISTA_HOURLY_RATE", "9.50"))   # EUR/hr
LABOR_TARGET_PCT    = float(os.getenv("LABOR_TARGET_PCT",    "0.32"))   # 32% default
SLOT_HOURS          = 3                                                   # hours per slot

SLOTS = [
    {"name": "Opening",     "label": "08:00–11:00", "hours": ["08", "09", "10"]},
    {"name": "Lunch peak",  "label": "11:00–14:00", "hours": ["11", "12", "13"]},
    {"name": "Afternoon",   "label": "14:00–17:00", "hours": ["14", "15", "16"]},
    {"name": "Evening",     "label": "17:00–21:00", "hours": ["17", "18", "19", "20"]},
]


def suggest_shifts(forecast: dict) -> dict:
    """
    Convert a forecast dict (from labor_forecast.forecast_day) into
    a shift suggestion with labor cost breakdown and Telegram draft.

    Returns:
        {
          "date":        str,
          "dow_name":    str,
          "slots":       [{name, label, baristas, cost_eur, intensity, tier}, ...],
          "total_cost":  float,
          "budget":      float,
          "saving":      float,   # positive = under budget
          "over_budget": bool,
          "confidence":  int,
          "telegram_msg": str,
          "event_key":   str,     # for notify_if_new() deduplication
        }
    """
    hours       = {h: float(v) for h, v in forecast["hours"].items()}
    total_rev   = float(forecast["total_forecast"])
    dow_name    = forecast["dow_name"]
    target_date = forecast["date"]
    confidence  = forecast["confidence"]
    weather     = forecast.get("weather_desc", "unknown")

    budget = round(total_rev * LABOR_TARGET_PCT, 2)

    # ── Build slot breakdown ──────────────────────────────────────────────────
    slot_revs = []
    for slot in SLOTS:
        rev = sum(hours.get(h, 0) for h in slot["hours"])
        slot_revs.append(rev)

    peak_rev = max(slot_revs) if slot_revs else 1.0

    slots_out = []
    for slot, rev in zip(SLOTS, slot_revs):
        intensity = rev / peak_rev if peak_rev > 0 else 0
        baristas  = _baristas_for_intensity(intensity)
        tier      = "peak" if intensity > 0.80 else "normal" if intensity > 0.55 else "quiet"
        cost      = round(baristas * len(slot["hours"]) * BARISTA_HOURLY_RATE, 2)
        slots_out.append({
            "name":      slot["name"],
            "label":     slot["label"],
            "baristas":  baristas,
            "cost_eur":  cost,
            "revenue":   round(rev, 2),
            "intensity": round(intensity, 3),
            "tier":      tier,
        })

    total_cost = round(sum(s["cost_eur"] for s in slots_out), 2)
    saving     = round(budget - total_cost, 2)
    over       = saving < 0

    # ── If over budget, trim quietest non-single slot ─────────────────────────
    warnings = []
    if over:
        trimmable = [s for s in slots_out if s["baristas"] > 1]
        if trimmable:
            quietest = min(trimmable, key=lambda s: s["intensity"])
            warnings.append(
                f"Over budget by \u20ac{abs(saving):.2f} \u2014 consider reducing "
                f"{quietest['name']} to {quietest['baristas'] - 1} barista."
            )
        else:
            warnings.append(f"Over budget by \u20ac{abs(saving):.2f} \u2014 no easy trim available.")

    # ── Telegram message ──────────────────────────────────────────────────────
    iso_week = date.fromisoformat(target_date).strftime("%Y-W%W")
    event_key = f"SHIFT_SUGGESTION:{target_date}"

    slot_lines = "\n".join(
        f"  {s['label']}  {s['baristas']} barista{'s' if s['baristas'] > 1 else ''}  "
        f"({s['name']})  \u20ac{s['cost_eur']:.2f}"
        for s in slots_out
    )
    warning_lines = "\n\u26a0\ufe0f " + "\n\u26a0\ufe0f ".join(warnings) if warnings else ""

    telegram_msg = (
        f"\U0001f4c5 *Shift suggestion \u2014 {dow_name} {target_date}*\n"
        f"Model confidence: `{confidence}%` \u00b7 Weather: _{weather}_\n"
        f"Forecast revenue: `\u20ac{total_rev:.2f}` \u00b7 Labor budget: `\u20ac{budget:.2f}`\n\n"
        f"{slot_lines}\n\n"
        f"*Total labor cost: \u20ac{total_cost:.2f}*"
        f"{warning_lines}\n\n"
        f"Reply *APPROVE {event_key}* to publish roster.\n"
        f"Reply *EDIT {event_key}* to adjust in dashboard.\n"
        f"Reply *SKIP {event_key}* to dismiss for this week."
    )

    log.info(
        "Shift suggestion: %s — total cost \u20ac%.2f vs budget \u20ac%.2f (%s)",
        target_date, total_cost, budget, "OVER" if over else "OK"
    )

    return {
        "date":         target_date,
        "dow_name":     dow_name,
        "slots":        slots_out,
        "total_cost":   total_cost,
        "budget":       budget,
        "saving":       saving,
        "over_budget":  over,
        "warnings":     warnings,
        "confidence":   confidence,
        "weather":      weather,
        "telegram_msg": telegram_msg,
        "event_key":    event_key,
    }


def _baristas_for_intensity(intensity: float) -> int:
    """
    Map revenue intensity (0–1, relative to daily peak) to barista count.
    Configurable via env vars for shops with different team sizes.
    """
    peak_thresh   = float(os.getenv("SCHEDULE_PEAK_THRESH",   "0.80"))
    normal_thresh = float(os.getenv("SCHEDULE_NORMAL_THRESH", "0.55"))
    peak_count    = int(os.getenv("SCHEDULE_PEAK_COUNT",      "3"))
    normal_count  = int(os.getenv("SCHEDULE_NORMAL_COUNT",    "2"))
    quiet_count   = int(os.getenv("SCHEDULE_QUIET_COUNT",     "1"))

    if intensity >= peak_thresh:   return peak_count
    if intensity >= normal_thresh: return normal_count
    return quiet_count


# ─── HITL: write approved suggestion to barista_shifts.csv ────────────────────

def apply_approved_suggestion(suggestion: dict, shifts_file: Path) -> None:
    """
    Called when owner replies APPROVE {event_key}.
    Appends suggested barista slots to barista_shifts.csv as placeholder rows.
    The owner or manager fills in actual barista names.
    """
    import csv
    target_date = suggestion["date"]
    rows_to_add = []

    for slot in suggestion["slots"]:
        for i in range(slot["baristas"]):
            rows_to_add.append({
                "name":       f"[TBD barista {i+1}]",
                "first_day":  target_date,
                "personal_code": "",
                "position":   "Barista",
                "shift":      slot["label"],
                "slot":       slot["name"],
                "notes":      f"Auto-suggested — {slot['tier']} slot",
            })

    file_exists = shifts_file.exists()
    with shifts_file.open("a", newline="", encoding="utf-8") as f:
        fieldnames = ["name", "first_day", "personal_code", "position", "shift", "slot", "notes"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows_to_add)

    log.info("Applied %d shift rows to %s for %s", len(rows_to_add), shifts_file, target_date)


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from migrate import apply_migrations, get_connection, DEFAULT_DB
    from core.labor_forecast import forecast_day

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today() + timedelta(days=1)))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    apply_migrations(DEFAULT_DB)
    conn = get_connection(DEFAULT_DB)

    forecast   = forecast_day(conn, date.fromisoformat(args.date))
    suggestion = suggest_shifts(forecast)
    conn.close()

    print(f"\nSuggested roster — {suggestion['dow_name']} {suggestion['date']}")
    print(f"Confidence: {suggestion['confidence']}%  Weather: {suggestion['weather']}")
    print(f"Budget: \u20ac{suggestion['budget']:.2f}\n")
    for slot in suggestion["slots"]:
        bar = "\u2588" * slot["baristas"]
        print(f"  {slot['label']}  {bar}  {slot['baristas']} barista(s)  "
              f"\u20ac{slot['cost_eur']:.2f}  [{slot['tier']}]")
    print(f"\nTotal: \u20ac{suggestion['total_cost']:.2f}  "
          f"({'OVER budget' if suggestion['over_budget'] else 'within budget'})")
    for w in suggestion["warnings"]:
        print(f"  \u26a0  {w}")
    print(f"\n--- Telegram preview ---\n{suggestion['telegram_msg']}")
