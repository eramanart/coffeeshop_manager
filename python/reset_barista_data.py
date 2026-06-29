"""
reset_barista_data.py — wipe ALL synthetic barista/roster scaffolding from the
compliance DB so the first real row is a real one.

Pre-launch (POS=stub) the barista/shift tables hold only test data: TestBarista,
the Dev Tester smoke-test artifact, a test roster, a seeded RUNG1_SEED roster,
test availability, a seeded swap, and forecast drafts. A phantom barista skews the
fairness rotation the moment real people arrive, and this is the same DB whose
off-machine backup is the #1 infra gap — it should not carry fakes.

Scope (barista/roster domain only). Does NOT touch audit_log, hourly_sales,
weather_cache, pos_events, receipt_processing, hr_actions, notifications_sent.

Usage (run from python/, after the visual Rung 1 test):
    python reset_barista_data.py            # dry run — shows what WOULD be deleted
    python reset_barista_data.py --apply    # actually delete
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import migrate

# Child rows first (FK-safe regardless of PRAGMA foreign_keys state).
TABLES = [
    "swap_requests",
    "barista_availability",
    "shift_suggestions",
    "published_shifts",
    "baristas",
]
UNTOUCHED = [
    "audit_log", "hourly_sales", "weather_cache", "pos_events",
    "receipt_processing", "hr_actions", "notifications_sent",
]

apply = "--apply" in sys.argv
conn = migrate.get_connection(migrate.DEFAULT_DB)

print(f"DB: {migrate.DEFAULT_DB}")
print(f"Mode: {'APPLY (deleting)' if apply else 'DRY RUN (no changes)'}\n")

print("Will clear:")
for t in TABLES:
    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<22} {n} row(s)")

print("\nLeaving untouched:")
for t in UNTOUCHED:
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<22} {n} row(s)")
    except Exception:
        print(f"  {t:<22} (absent)")

if not apply:
    print("\nDry run only. Re-run with --apply to delete the rows above.")
    conn.close()
    sys.exit(0)

# ── Mandatory backup before any delete ────────────────────────────────────────
# This file is the compliance DB (2,400+ audit rows, 1,000+ sales rows) and there
# is no off-machine copy. We will NOT delete without first writing a verified,
# transactionally-consistent snapshot (sqlite backup API — safe even if the app is
# live). If the backup fails for any reason, we abort untouched.
src = Path(str(migrate.DEFAULT_DB))
bak = src.with_name(src.name + f".{datetime.now():%Y%m%d-%H%M%S}.bak")
try:
    bak_conn = sqlite3.connect(str(bak))
    with bak_conn:
        conn.backup(bak_conn)
    bak_conn.close()
except Exception as exc:
    print(f"\nERROR: backup failed ({exc}) — aborting, nothing deleted.", file=sys.stderr)
    conn.close()
    sys.exit(1)
if not bak.is_file() or bak.stat().st_size == 0:
    print("\nERROR: backup is missing or empty — aborting, nothing deleted.", file=sys.stderr)
    conn.close()
    sys.exit(1)
print(f"\nBackup written: {bak} ({bak.stat().st_size:,} bytes)")
print("  (still copy this off the machine — local .bak survives a typo, not a dead disk)\n")

for t in TABLES:
    conn.execute(f"DELETE FROM {t}")
conn.commit()

print("\nDeleted. Post-state (all should be 0):")
for t in TABLES:
    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<22} {n} row(s)")
conn.close()
print("\nBarista/roster tables are now empty. The first published_shifts row will be a real one.")
