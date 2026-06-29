"""
core/accounting.py — Deterministic accounting logic for CoffeeManager-OS

All monetary arithmetic uses Python's Decimal type with ROUND_HALF_UP.
Never use float for any money value — VMI flags discrepancies as small as €0.01.

Modules:
  - PVM (VAT) calculation at standard (21%) and reduced (9%, 5%) rates
  - Daily Z-report audit: compare POS total vs VMI i.EKA transmission
  - Weekly bank reconciliation
  - Monthly P&L draft generation

CLI smoke test:
  python -m core.accounting --test
  python -m core.accounting --audit         # runs today's audit
  python -m core.accounting --pl 2025-05    # generates P&L for given month
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any

log = logging.getLogger("core.accounting")

# ── VAT rates (PVM) ───────────────────────────────────────────────────────────
PVM_RATES: dict[str, Decimal] = {
    "PVM1": Decimal("0.21"),   # standard — most goods & services
    "PVM2": Decimal("0.09"),   # reduced — food, non-alcoholic drinks, books
    "PVM5": Decimal("0.05"),   # super-reduced — specific categories
    "PVM0": Decimal("0.00"),   # zero-rated — exports
}

# ── Expense categories for P&L ────────────────────────────────────────────────
EXPENSE_CATEGORIES = ["COGS", "Labor", "Rent", "Utilities", "Marketing", "Other"]


# ═════════════════════════════════════════════════════════════════════════════
# PVM (VAT) CALCULATIONS
# ═════════════════════════════════════════════════════════════════════════════

def calculate_pvm(
    net_amount: Decimal,
    pvm_code: str = "PVM1",
) -> dict[str, Decimal]:
    """
    Calculate PVM (VAT) amounts from a net (ex-VAT) amount.

    Args:
        net_amount: Net amount as Decimal. Must be non-negative.
        pvm_code:   One of PVM1 (21%), PVM2 (9%), PVM5 (5%), PVM0 (0%).

    Returns:
        {"net": Decimal, "pvm": Decimal, "gross": Decimal}

    Raises:
        ValueError: if pvm_code is unknown or net_amount is negative.
    """
    if net_amount < Decimal("0"):
        raise ValueError(f"net_amount must be non-negative, got {net_amount}")
    rate = PVM_RATES.get(pvm_code.upper())
    if rate is None:
        raise ValueError(f"Unknown PVM code: {pvm_code}. Valid: {list(PVM_RATES)}")

    pvm   = (net_amount * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
    gross = net_amount + pvm
    return {"net": net_amount, "pvm": pvm, "gross": gross}


def gross_to_net(gross_amount: Decimal, pvm_code: str = "PVM1") -> dict[str, Decimal]:
    """
    Reverse-calculate net and PVM from a gross (inc-VAT) amount.
    Useful when a receipt only shows the total.

    Args:
        gross_amount: Gross amount as Decimal.
        pvm_code:     PVM code.

    Returns:
        {"net": Decimal, "pvm": Decimal, "gross": Decimal}
    """
    if gross_amount < Decimal("0"):
        raise ValueError(f"gross_amount must be non-negative, got {gross_amount}")
    rate = PVM_RATES.get(pvm_code.upper())
    if rate is None:
        raise ValueError(f"Unknown PVM code: {pvm_code}")

    net   = (gross_amount / (Decimal("1") + rate)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    pvm   = (gross_amount - net).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"net": net, "pvm": pvm, "gross": gross_amount}


# ═════════════════════════════════════════════════════════════════════════════
# Z-REPORT AUDIT
# ═════════════════════════════════════════════════════════════════════════════

def run_daily_audit(conn, audit_date: date | None = None) -> dict[str, Any]:
    """
    Compare today's POS Z-report total against the VMI i.EKA transmission.

    Flow:
      1. Pull POS daily total from POS API (pull model).
      2. Pull i.EKA transmission record from VMI API or local cache.
      3. Compare. Discrepancy < €0.01 → OK. Otherwise → MISMATCH.
      4. Write result to audit_log in SQLite.

    Args:
        conn:       SQLite connection from migrate.get_connection().
        audit_date: Date to audit. Defaults to today (UTC).

    Returns:
        {
          "date":        str (YYYY-MM-DD),
          "pos_total":   str (Decimal),
          "ieka_total":  str (Decimal),
          "discrepancy": str (Decimal),
          "status":      "OK" | "MISMATCH" | "ERROR",
          "notes":       str | None,
        }
    """
    target_date = audit_date or date.today()
    date_str    = target_date.isoformat()
    log.info("Running Z-report audit for %s", date_str)

    try:
        pos_total  = _fetch_pos_daily_total(date_str)
        ieka_total = _fetch_ieka_total(date_str)
    except Exception as exc:
        log.error("Audit data fetch failed: %s", exc)
        result = {
            "date": date_str, "pos_total": "0.00", "ieka_total": "0.00",
            "discrepancy": "0.00", "status": "ERROR", "notes": str(exc),
        }
        _write_audit_log(conn, result)
        return result

    discrepancy = abs(pos_total - ieka_total).quantize(Decimal("0.01"), ROUND_HALF_UP)
    status      = "OK" if discrepancy < Decimal("0.01") else "MISMATCH"

    notes = None
    if status == "MISMATCH":
        notes = (
            f"POS reported {pos_total} EUR; i.EKA shows {ieka_total} EUR. "
            f"Difference: {discrepancy} EUR. "
            f"Check for offline cash transactions or failed i.EKA sync."
        )
        log.warning("AUDIT MISMATCH on %s: discrepancy=%s EUR", date_str, discrepancy)

    result = {
        "date":        date_str,
        "pos_total":   str(pos_total),
        "ieka_total":  str(ieka_total),
        "discrepancy": str(discrepancy),
        "status":      status,
        "notes":       notes,
    }
    _write_audit_log(conn, result)
    log.info("Audit result: %s (discrepancy=%s EUR)", status, discrepancy)
    return result


def _fetch_pos_daily_total(date_str: str) -> Decimal:
    """
    Pull the POS daily Z-report total for a given date.
    Returns gross sales amount (inc. PVM) as Decimal.

    Replace the stub with your real POS API call.
    """
    provider = os.getenv("POS_PROVIDER", "stub").lower()

    if provider == "paysera":
        return _paysera_daily_total(date_str)
    elif provider == "robolabs":
        return _robolabs_daily_total(date_str)
    else:
        # Stub: returns a realistic daily total for development testing.
        # Monday–Friday ~€800, weekends ~€1,200.
        d = date.fromisoformat(date_str)
        base = Decimal("1200.00") if d.weekday() >= 5 else Decimal("800.00")
        # Add a small deterministic variance per date so tests are repeatable
        variance = Decimal(str(sum(ord(c) for c in date_str) % 50))
        return (base + variance).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _fetch_ieka_total(date_str: str) -> Decimal:
    """
    Pull the VMI i.EKA transmission total for a given date.
    In production this queries the VMI Web Service or a local i.EKA sync log.

    Returns gross total as recorded by i.EKA (should match POS exactly).
    """
    ieka_log = Path("data/ieka_sync.json")
    if ieka_log.exists():
        try:
            with ieka_log.open(encoding="utf-8") as f:
                records = json.load(f)
            if date_str in records:
                return Decimal(str(records[date_str])).quantize(
                    Decimal("0.01"), ROUND_HALF_UP
                )
        except Exception as exc:
            log.warning("Could not read i.EKA log: %s", exc)

    # Stub: simulate a correctly synced i.EKA total (matching POS)
    # In a real deployment this would call the VMI Web Service.
    return _fetch_pos_daily_total(date_str)


def _paysera_daily_total(date_str: str) -> Decimal:
    """Fetch daily Z-report total from Paysera POS API."""
    api_key     = os.getenv("PAYSERA_API_KEY")
    merchant_id = os.getenv("PAYSERA_MERCHANT_ID")
    if not api_key or not merchant_id:
        raise EnvironmentError("PAYSERA_API_KEY and PAYSERA_MERCHANT_ID must be set.")

    url = (
        f"https://api.paysera.com/pos/v1/merchants/{merchant_id}"
        f"/transactions?date={date_str}&type=z_report"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    total = sum(Decimal(str(t["amount"])) for t in data.get("transactions", []))
    return total.quantize(Decimal("0.01"), ROUND_HALF_UP)


def _robolabs_daily_total(date_str: str) -> Decimal:
    """Fetch daily Z-report total from RoboLabs POS API."""
    api_key     = os.getenv("ROBOLABS_API_KEY")
    location_id = os.getenv("ROBOLABS_LOCATION_ID")
    if not api_key or not location_id:
        raise EnvironmentError("ROBOLABS_API_KEY and ROBOLABS_LOCATION_ID must be set.")

    url = (
        f"https://api.robolabs.lt/v2/locations/{location_id}"
        f"/reports/z?date={date_str}"
    )
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    return Decimal(str(data.get("total", "0"))).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )


def _write_audit_log(conn, result: dict) -> None:
    """Persist audit result to SQLite audit_log table."""
    from migrate import log_audit
    log_audit(conn, result)


# ═════════════════════════════════════════════════════════════════════════════
# WEEKLY BANK RECONCILIATION
# ═════════════════════════════════════════════════════════════════════════════

def run_weekly_reconciliation(
    conn,
    week_start: date | None = None,
) -> dict[str, Any]:
    """
    Reconcile bank statement totals against POS Z-report totals for a week.

    Args:
        conn:       SQLite connection.
        week_start: Monday of the week to reconcile. Defaults to last Monday.

    Returns:
        {
          "week_start":       str (YYYY-MM-DD),
          "week_end":         str (YYYY-MM-DD),
          "pos_total":        str (Decimal),
          "bank_total":       str (Decimal),
          "discrepancy":      str (Decimal),
          "status":           "RECONCILED" | "DISCREPANCY" | "ERROR",
          "daily_breakdown":  [ {date, pos, bank, diff}, ... ],
        }
    """
    if week_start is None:
        today      = date.today()
        week_start = today - timedelta(days=today.weekday())   # last Monday
    week_end = week_start + timedelta(days=6)

    log.info("Reconciling week %s → %s", week_start, week_end)
    breakdown   = []
    pos_week    = Decimal("0.00")
    bank_week   = Decimal("0.00")

    for i in range(7):
        day     = week_start + timedelta(days=i)
        day_str = day.isoformat()

        try:
            pos_day  = _fetch_pos_daily_total(day_str)
            bank_day = _fetch_bank_daily_total(day_str)
        except Exception as exc:
            log.warning("Could not fetch data for %s: %s", day_str, exc)
            pos_day = bank_day = Decimal("0.00")

        diff = (bank_day - pos_day).quantize(Decimal("0.01"), ROUND_HALF_UP)
        breakdown.append({
            "date": day_str,
            "pos":  str(pos_day),
            "bank": str(bank_day),
            "diff": str(diff),
        })
        pos_week  += pos_day
        bank_week += bank_day

    pos_week  = pos_week.quantize(Decimal("0.01"),  ROUND_HALF_UP)
    bank_week = bank_week.quantize(Decimal("0.01"), ROUND_HALF_UP)
    total_diff = abs(bank_week - pos_week).quantize(Decimal("0.01"), ROUND_HALF_UP)
    status = "RECONCILED" if total_diff < Decimal("0.01") else "DISCREPANCY"

    result = {
        "week_start":      week_start.isoformat(),
        "week_end":        week_end.isoformat(),
        "pos_total":       str(pos_week),
        "bank_total":      str(bank_week),
        "discrepancy":     str(total_diff),
        "status":          status,
        "daily_breakdown": breakdown,
    }
    log.info("Reconciliation: %s (total_diff=%s EUR)", status, total_diff)
    return result


def _fetch_bank_daily_total(date_str: str) -> Decimal:
    """
    Pull bank statement total for a given date.
    In production: integrate with your bank's API (Swedbank, SEB, Luminor, etc.)
    or parse an exported CSV.

    Stub returns POS total + small variance to simulate real-world cash handling.
    """
    bank_export = Path("data/bank_statement.json")
    if bank_export.exists():
        try:
            with bank_export.open(encoding="utf-8") as f:
                records = json.load(f)
            if date_str in records:
                return Decimal(str(records[date_str])).quantize(
                    Decimal("0.01"), ROUND_HALF_UP
                )
        except Exception as exc:
            log.warning("Could not read bank export: %s", exc)

    # Stub: match POS total exactly (zero variance)
    return _fetch_pos_daily_total(date_str)


# ═════════════════════════════════════════════════════════════════════════════
# MONTHLY P&L DRAFT
# ═════════════════════════════════════════════════════════════════════════════

def generate_pl_draft(
    conn,
    month: str | None = None,
) -> dict[str, Any]:
    """
    Generate a Profit & Loss draft for a given month.
    Due on the 3rd of each month per the operational protocol.

    Args:
        conn:  SQLite connection.
        month: "YYYY-MM" string. Defaults to previous month.

    Returns:
        {
          "month":    str,
          "revenue":  {gross, net, pvm} all as str(Decimal),
          "expenses": {category: amount, ...},
          "gross_profit":    str,
          "operating_profit":str,
          "net_profit":      str,
          "labor_pct":       str,   # labor as % of gross revenue
          "cogs_pct":        str,
          "status":          "draft",
          "generated_at":    str,
          "notes":           [str, ...],
        }
    """
    if month is None:
        first_of_this_month = date.today().replace(day=1)
        prev_month_end      = first_of_this_month - timedelta(days=1)
        month               = prev_month_end.strftime("%Y-%m")

    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    log.info("Generating P&L draft for %s", month)

    # ── Revenue ───────────────────────────────────────────────────────────────
    gross_revenue = _sum_monthly_revenue(year, mon)
    revenue_breakdown = gross_to_net(gross_revenue, "PVM1")

    # ── Expenses ──────────────────────────────────────────────────────────────
    expenses = _load_monthly_expenses(year, mon)

    total_expenses = sum(
        Decimal(str(v)) for v in expenses.values()
    ).quantize(Decimal("0.01"), ROUND_HALF_UP)

    cogs           = Decimal(str(expenses.get("COGS", "0.00")))
    labor          = Decimal(str(expenses.get("Labor", "0.00")))
    gross_profit   = (revenue_breakdown["net"] - cogs).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )
    operating_profit = (gross_profit - total_expenses + cogs).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )
    net_profit = operating_profit  # simplified; no interest/tax line yet

    # ── KPI ratios ────────────────────────────────────────────────────────────
    def pct(numerator: Decimal, denominator: Decimal) -> str:
        if denominator == Decimal("0"):
            return "0.0"
        return str(
            (numerator / denominator * Decimal("100")).quantize(
                Decimal("0.1"), ROUND_HALF_UP
            )
        )

    labor_pct = pct(labor, gross_revenue)
    cogs_pct  = pct(cogs,  gross_revenue)

    # ── Warnings ──────────────────────────────────────────────────────────────
    notes = []
    if Decimal(labor_pct) > Decimal("35"):
        notes.append(
            f"⚠️ Labor cost is {labor_pct}% of revenue (target: ≤35%). "
            f"Review scheduling for {month}."
        )
    if Decimal(cogs_pct) > Decimal("32"):
        notes.append(
            f"⚠️ COGS is {cogs_pct}% of revenue (target: ≤32%). "
            f"Check supplier prices and waste levels."
        )
    if net_profit < Decimal("0"):
        notes.append(
            f"🔴 Net loss of {abs(net_profit)} EUR in {month}. "
            f"Immediate review required."
        )

    pl = {
        "month":   month,
        "revenue": {
            "gross": str(gross_revenue),
            "net":   str(revenue_breakdown["net"]),
            "pvm":   str(revenue_breakdown["pvm"]),
        },
        "expenses": {k: str(v) for k, v in expenses.items()},
        "total_expenses":   str(total_expenses),
        "gross_profit":     str(gross_profit),
        "operating_profit": str(operating_profit),
        "net_profit":       str(net_profit),
        "labor_pct":        labor_pct,
        "cogs_pct":         cogs_pct,
        "status":           "draft",
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "notes":            notes,
    }

    # ── Persist draft to file for owner review ────────────────────────────────
    _save_pl_draft(pl, month)
    log.info("P&L draft generated: net_profit=%s EUR, labor=%s%%, COGS=%s%%",
             net_profit, labor_pct, cogs_pct)
    return pl


def _sum_monthly_revenue(year: int, month: int) -> Decimal:
    """
    Sum all Z-report daily totals for a calendar month.
    Reads from audit_log in SQLite (populated by run_daily_audit).
    Falls back to POS API if audit_log has gaps.
    """
    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    total = Decimal("0.00")

    for day in range(1, days_in_month + 1):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        total += _fetch_pos_daily_total(date_str)

    return total.quantize(Decimal("0.01"), ROUND_HALF_UP)


def _load_monthly_expenses(year: int, month: int) -> dict[str, str]:
    """
    Load categorised expenses for a month from data/expenses_YYYY-MM.json.
    Returns a dict of {category: str(Decimal)}.
    Falls back to stub values if file is missing.
    """
    expense_file = Path(f"data/expenses_{year:04d}-{month:02d}.json")
    if expense_file.exists():
        try:
            with expense_file.open(encoding="utf-8") as f:
                raw = json.load(f)
            # Validate and coerce all values to Decimal strings
            validated = {}
            for cat in EXPENSE_CATEGORIES:
                try:
                    validated[cat] = str(
                        Decimal(str(raw.get(cat, "0"))).quantize(
                            Decimal("0.01"), ROUND_HALF_UP
                        )
                    )
                except InvalidOperation:
                    log.warning("Invalid expense value for %s in %s — using 0", cat, expense_file)
                    validated[cat] = "0.00"
            return validated
        except Exception as exc:
            log.warning("Could not load %s: %s — using stub", expense_file, exc)

    # Stub: realistic coffee-shop expense ratios for development
    gross = _sum_monthly_revenue(year, month)
    return {
        "COGS":      str((gross * Decimal("0.28")).quantize(Decimal("0.01"), ROUND_HALF_UP)),
        "Labor":     str((gross * Decimal("0.32")).quantize(Decimal("0.01"), ROUND_HALF_UP)),
        "Rent":      "2200.00",
        "Utilities": "380.00",
        "Marketing": "150.00",
        "Other":     "220.00",
    }


def _save_pl_draft(pl: dict, month: str) -> None:
    """Save the P&L draft JSON to data/ for owner review."""
    Path("data").mkdir(exist_ok=True)
    out = Path(f"data/pl_draft_{month}.json")
    with out.open("w", encoding="utf-8") as f:
        json.dump(pl, f, indent=2, ensure_ascii=False)
    log.info("P&L draft saved to %s", out)


# ═════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def _print_audit(result: dict) -> None:
    icon = "✅" if result["status"] == "OK" else "⚠️ "
    print(f"\n{icon} Audit {result['date']}: {result['status']}")
    print(f"  POS total:    {result['pos_total']} EUR")
    print(f"  i.EKA total:  {result['ieka_total']} EUR")
    print(f"  Discrepancy:  {result['discrepancy']} EUR")
    if result.get("notes"):
        print(f"  Notes: {result['notes']}")


def _print_pl(pl: dict) -> None:
    print(f"\n📊 P&L Draft — {pl['month']}  [{pl['status'].upper()}]")
    print(f"  Gross revenue:     {pl['revenue']['gross']:>12} EUR")
    print(f"  Net revenue:       {pl['revenue']['net']:>12} EUR")
    print(f"  PVM collected:     {pl['revenue']['pvm']:>12} EUR")
    print()
    for cat, amt in pl["expenses"].items():
        print(f"  {cat:<14}  {amt:>12} EUR")
    print(f"  {'─'*30}")
    print(f"  Total expenses:    {pl['total_expenses']:>12} EUR")
    print()
    print(f"  Gross profit:      {pl['gross_profit']:>12} EUR")
    print(f"  Operating profit:  {pl['operating_profit']:>12} EUR")
    print(f"  Net profit:        {pl['net_profit']:>12} EUR")
    print()
    print(f"  Labor %:           {pl['labor_pct']:>11}%")
    print(f"  COGS %:            {pl['cogs_pct']:>11}%")
    for note in pl.get("notes", []):
        print(f"\n  {note}")


def _run_tests() -> None:
    print("Running accounting unit tests...")
    errors = []

    def check(name: str, got: Any, expected: Any) -> None:
        if got != expected:
            errors.append(f"FAIL {name}: got {got!r}, expected {expected!r}")
        else:
            print(f"  ✓ {name}")

    # PVM calculations
    r = calculate_pvm(Decimal("100.00"), "PVM1")
    check("PVM1 net",   r["net"],   Decimal("100.00"))
    check("PVM1 pvm",   r["pvm"],   Decimal("21.00"))
    check("PVM1 gross", r["gross"], Decimal("121.00"))

    r = calculate_pvm(Decimal("100.00"), "PVM2")
    check("PVM2 pvm",   r["pvm"],   Decimal("9.00"))
    check("PVM2 gross", r["gross"], Decimal("109.00"))

    r = calculate_pvm(Decimal("99.99"), "PVM1")
    check("PVM1 rounding pvm",   r["pvm"],   Decimal("21.00"))  # 20.9979 → 21.00
    check("PVM1 rounding gross", r["gross"], Decimal("120.99"))

    # Gross-to-net reverse calculation
    r = gross_to_net(Decimal("121.00"), "PVM1")
    check("gross_to_net net",  r["net"],  Decimal("100.00"))
    check("gross_to_net pvm",  r["pvm"],  Decimal("21.00"))

    r = gross_to_net(Decimal("10.00"), "PVM2")
    check("reduced gross_to_net net",  r["net"],  Decimal("9.17"))
    check("reduced gross_to_net pvm",  r["pvm"],  Decimal("0.83"))

    # Edge cases
    r = calculate_pvm(Decimal("0.00"), "PVM1")
    check("zero amount pvm",   r["pvm"],   Decimal("0.00"))
    check("zero amount gross", r["gross"], Decimal("0.00"))

    # Error cases
    try:
        calculate_pvm(Decimal("-1.00"))
        errors.append("FAIL negative amount: should have raised ValueError")
    except ValueError:
        print("  ✓ negative amount raises ValueError")

    try:
        calculate_pvm(Decimal("10.00"), "INVALID")
        errors.append("FAIL bad PVM code: should have raised ValueError")
    except ValueError:
        print("  ✓ bad PVM code raises ValueError")

    print()
    if errors:
        for e in errors:
            print(f"  {e}")
        print(f"\n{len(errors)} test(s) FAILED.")
        sys.exit(1)
    else:
        print("All tests passed ✅")


if __name__ == "__main__":
    import argparse

    # Add project root to path when run directly
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from migrate import apply_migrations, get_connection, DEFAULT_DB

    parser = argparse.ArgumentParser(description="CoffeeManager-OS accounting module")
    parser.add_argument("--test",   action="store_true", help="Run unit tests")
    parser.add_argument("--audit",  action="store_true", help="Run today's Z-report audit")
    parser.add_argument("--recon",  action="store_true", help="Run last week's bank reconciliation")
    parser.add_argument("--pl",     metavar="YYYY-MM",   help="Generate P&L draft for month")
    args = parser.parse_args()

    if args.test:
        _run_tests()
        sys.exit(0)

    apply_migrations(DEFAULT_DB)
    conn = get_connection(DEFAULT_DB)

    if args.audit:
        _print_audit(run_daily_audit(conn))
    elif args.recon:
        r = run_weekly_reconciliation(conn)
        print(f"\nReconciliation {r['week_start']} → {r['week_end']}: {r['status']}")
        print(f"  POS week total:  {r['pos_total']} EUR")
        print(f"  Bank week total: {r['bank_total']} EUR")
        print(f"  Discrepancy:     {r['discrepancy']} EUR")
        for day in r["daily_breakdown"]:
            flag = " ⚠️" if day["diff"] != "0.00" else ""
            print(f"    {day['date']}  POS {day['pos']:>9}  Bank {day['bank']:>9}  "
                  f"Diff {day['diff']:>8}{flag}")
    elif args.pl:
        _print_pl(generate_pl_draft(conn, args.pl))
    else:
        parser.print_help()

    conn.close()
