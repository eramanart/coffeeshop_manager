import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "config" / "settings.env")

from core.accounting import run_daily_audit
from core.inventory import check_stock_levels
from agent.runner import start_openclaw_agent
from migrate import apply_migrations, get_connection, DEFAULT_DB
from notify import send_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("main")

_RECEIPT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".tiff", ".tif"}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CoffeeManager-OS gateway loop")
    p.add_argument(
        "--mode",
        choices=["audit", "inventory", "agent", "watch", "all"],
        default="all",
        help="Phase to run (default: all)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force DRY_RUN=true regardless of settings.env",
    )
    return p.parse_args()


def _scan_receipts(workspace: Path) -> list[str]:
    """Return absolute paths of receipt images/PDFs not yet processed in workspace."""
    found = []
    for f in workspace.iterdir():
        if f.name.startswith("."):
            continue
        if f.suffix.lower() in _RECEIPT_EXTENSIONS:
            marker = workspace / f".processed_{f.name}"
            if not marker.exists():
                found.append(str(f.resolve()))
    return found


def _detect_new_hires(conn, shifts_csv: Path) -> list[dict]:
    """
    Read barista_shifts.csv and return rows whose employee_name is not already
    in hr_actions and whose first_working_day is at least 24 hours from now.
    """
    if not shifts_csv.exists():
        log.info("barista_shifts.csv not found at %s — skipping new-hire detection", shifts_csv)
        return []

    known = {
        row["employee_name"]
        for row in conn.execute("SELECT employee_name FROM hr_actions").fetchall()
    }

    min_fwd = (datetime.now(timezone.utc) + timedelta(hours=24)).date()
    new_hires: list[dict] = []

    with open(shifts_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name    = row.get("employee_name", "").strip()
            fwd_str = row.get("first_working_day", "").strip()
            if not name or not fwd_str:
                continue
            try:
                fwd = date.fromisoformat(fwd_str)
            except ValueError:
                log.warning("barista_shifts.csv: invalid date '%s' for %s — skipped", fwd_str, name)
                continue
            if name not in known and fwd >= min_fwd:
                new_hires.append({
                    "name":              name,
                    "first_working_day": fwd_str,
                    "personal_code":     row.get("personal_code", "").strip() or None,
                    "position":          row.get("position", "").strip() or None,
                })

    return new_hires


async def main(args: argparse.Namespace) -> None:
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    dry_run   = os.getenv("DRY_RUN", "true").lower() == "true"
    db_path   = Path(os.getenv("DB_PATH", str(DEFAULT_DB)))
    workspace = Path(os.getenv("WORKSPACE_PATH", "data/workspace"))
    workspace.mkdir(parents=True, exist_ok=True)

    log.info("Starting CoffeeManager-OS | mode=%s dry_run=%s", args.mode, dry_run)

    apply_migrations(db_path)

    # Phase 1 — deterministic core (audit + stock). Connection closed before agent phase
    # so start_openclaw_agent can open its own without hitting a lock.
    audit_result: dict = {}
    stock_alerts: dict = {}
    receipts:     list = []
    new_hires:    list = []

    conn = get_connection(db_path)
    try:
        if args.mode in ("audit", "all"):
            audit_result = run_daily_audit(conn)
            _send_audit_notifications(conn, audit_result)

        if args.mode in ("inventory", "all"):
            stock_alerts = check_stock_levels(conn)
            _send_stock_notifications(conn, stock_alerts)

        if args.mode in ("agent", "all"):
            shifts_csv = Path(os.getenv("SHIFTS_CSV", "data/barista_shifts.csv"))
            receipts   = _scan_receipts(workspace)
            new_hires  = _detect_new_hires(conn, shifts_csv)
    finally:
        conn.close()

    # Phase 2 — agent dispatch. start_openclaw_agent opens its own connection.
    if args.mode in ("agent", "all"):
        log.info("Receipts pending:   %d", len(receipts))
        log.info("New hires detected: %d", len(new_hires))

        context = {
            "audit":     audit_result,
            "stock":     stock_alerts,
            "workspace": str(workspace),
            "receipts":  receipts,
            "new_hires": new_hires,
        }
        await start_openclaw_agent(context)


def _send_audit_notifications(conn, audit: dict) -> None:
    if audit.get("status") == "MISMATCH":
        send_telegram(
            conn, "AUDIT_MISMATCH",
            f"AUDIT_MISMATCH:{audit['date']}",
            f"⚠️ Z-report mismatch detected\n"
            f"Date: {audit['date']}\n"
            f"POS total:    {audit['pos_total']} EUR\n"
            f"i.EKA total:  {audit['ieka_total']} EUR\n"
            f"Discrepancy:  {audit['discrepancy']} EUR\n\n"
            f"Action required before 23:59 to avoid VMI fine.",
        )
    elif audit.get("status") == "ERROR":
        send_telegram(
            conn, "AUDIT_ERROR",
            f"AUDIT_ERROR:{audit['date']}",
            f"🔴 Audit error on {audit['date']}\n{audit.get('notes', 'Unknown error')}",
        )


def _send_stock_notifications(conn, stock: dict) -> None:
    week = date.today().strftime("%Y-%W")
    for item in stock.get("low_stock", []):
        send_telegram(
            conn, "PO_DRAFTED",
            f"PO_DRAFTED:{item['sku']}:{week}",
            f"📦 Low stock — purchase order drafted\n"
            f"Item:      {item['name']}\n"
            f"Current:   {item['current_kg']} {item['unit']}\n"
            f"Threshold: {item['threshold_kg']} {item['unit']}\n"
            f"Order qty: {item['order_qty']} {item['unit']}\n"
            f"Supplier:  {item['supplier_name']}\n\n"
            f"Reply GO to send the order email.\n"
            f"Reply SKIP to dismiss for this week.",
        )


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
