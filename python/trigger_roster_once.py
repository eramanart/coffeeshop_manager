"""One-shot: send next week's roster draft to the owner NOW.

Replicates exactly what the Sunday 18:10 UTC cron does in main.py:

    from notify import send_telegram
    await run_weekly_roster_job(conn, send_telegram)

but runnable on demand. Clears a stale dedup record for the target week first,
because notify.send_telegram records the event_key BEFORE sending and suppresses
any key already present (a 06-19 smoke test left ROSTER_DRAFT:2026-W25 behind).

Run from python/ with the project venv (.venv):
    ../.venv/Scripts/python.exe trigger_roster_once.py
"""

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / "settings.env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from migrate import get_connection                       # noqa: E402
from notify import send_telegram                          # noqa: E402
from agent.scheduler_bot import run_weekly_roster_job, _next_week_start  # noqa: E402


def main() -> None:
    conn = get_connection()
    next_monday = _next_week_start()
    roster_key = f"ROSTER_DRAFT:{next_monday.strftime('%Y-W%W')}"
    print(f"Target roster_key : {roster_key}  (week of {next_monday})")

    # Clear stale dedup record so the record-then-send dedup does not suppress us.
    cur = conn.execute(
        "DELETE FROM notifications_sent WHERE event_key = ?", (roster_key,)
    )
    conn.commit()
    print(f"Stale dedup rows cleared: {cur.rowcount}")

    captured: dict = {}

    def send_capture(c, event_type, event_key, message):
        ok = send_telegram(c, event_type, event_key, message)
        captured.update(ok=ok, event_key=event_key, chars=len(message))
        return ok

    asyncio.run(run_weekly_roster_job(conn, send_capture))
    conn.close()

    if captured:
        print("SEND RESULT:", captured)
    else:
        print("SEND RESULT: send function never called — the roster job errored "
              "before sending (see ERROR log above).")


if __name__ == "__main__":
    main()
