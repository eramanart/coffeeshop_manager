"""
core/inventory.py — Deterministic inventory management
Uses a pull-first polling architecture (blind-spot amendment #3).
Never uses webhooks as a primary data source.

Thresholds are configured per-SKU in config/inventory_thresholds.json.
If the config file is missing, sensible coffee-shop defaults are used.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

log = logging.getLogger("core.inventory")

DEFAULT_THRESHOLDS: dict[str, dict] = {
    "BEANS_ESPRESSO": {
        "name":           "Espresso beans",
        "threshold_kg":   5.0,
        "order_qty_kg":   25.0,
        "supplier_name":  "Kavos Tiekėjas UAB",
        "supplier_email": "orders@kavos-tiekėjas.lt",
        "unit":           "kg",
    },
    "BEANS_FILTER": {
        "name":           "Filter coffee beans",
        "threshold_kg":   2.0,
        "order_qty_kg":   10.0,
        "supplier_name":  "Kavos Tiekėjas UAB",
        "supplier_email": "orders@kavos-tiekėjas.lt",
        "unit":           "kg",
    },
    "MILK_WHOLE": {
        "name":           "Whole milk",
        "threshold_kg":   10.0,
        "order_qty_kg":   50.0,
        "supplier_name":  "Pieno Žvaigždės",
        "supplier_email": "orders@pienozvaigzdes.lt",
        "unit":           "L",
    },
    "MILK_OAT": {
        "name":           "Oat milk",
        "threshold_kg":   5.0,
        "order_qty_kg":   20.0,
        "supplier_name":  "Oatly",
        "supplier_email": "orders@oatly.com",
        "unit":           "L",
    },
    "CUPS_TAKEAWAY": {
        "name":           "Takeaway cups (12oz)",
        "threshold_kg":   200.0,   # unit: pieces
        "order_qty_kg":   1000.0,
        "supplier_name":  "Pakuočių Centras",
        "supplier_email": "orders@pakuociu-centras.lt",
        "unit":           "pcs",
    },
}


def check_stock_levels(conn) -> dict:
    """
    Pull current stock levels from the POS API (pull model).
    Compare against configured thresholds.
    Log all checks to SQLite.

    Returns:
        {
          "checked_at": str,         # ISO-8601 UTC
          "items":      [item, ...], # all items with current levels
          "low_stock":  [item, ...], # items below threshold (triggers PO)
          "status":     "ok" | "low_stock" | "error",
        }
    """
    log.info("Checking stock levels via POS API (pull model)...")
    checked_at = datetime.now(timezone.utc).isoformat()
    thresholds = _load_thresholds()

    # ── Pull stock data from POS ──────────────────────────────────────────────
    try:
        stock_data = _fetch_stock_from_pos()
    except Exception as exc:
        log.error("POS stock pull failed: %s", exc)
        return {"checked_at": checked_at, "status": "error", "error": str(exc),
                "items": [], "low_stock": []}

    items:     list[dict] = []
    low_stock: list[dict] = []

    for sku, cfg in thresholds.items():
        current = stock_data.get(sku, 0.0)
        threshold = cfg["threshold_kg"]
        order_qty = cfg["order_qty_kg"]

        item = {
            "sku":          sku,
            "name":         cfg["name"],
            "current_kg":   current,
            "threshold_kg": threshold,
            "order_qty":    order_qty,
            "unit":         cfg.get("unit", "kg"),
            "supplier_name":  cfg.get("supplier_name"),
            "supplier_email": cfg.get("supplier_email"),
            "is_low":       current < threshold,
        }
        items.append(item)

        if item["is_low"]:
            low_stock.append(item)
            log.warning(
                "LOW STOCK: %s — current=%.1f%s threshold=%.1f%s",
                cfg["name"], current, item["unit"], threshold, item["unit"]
            )
            _log_stock_event(conn, sku, cfg["name"], current, threshold, order_qty)
        else:
            log.debug("OK: %s — %.1f%s (threshold: %.1f)",
                      cfg["name"], current, item["unit"], threshold)

    status = "low_stock" if low_stock else "ok"
    log.info("Inventory check complete: %d items, %d low", len(items), len(low_stock))

    return {
        "checked_at": checked_at,
        "status":     status,
        "items":      items,
        "low_stock":  low_stock,
    }


# ── POS API client (pull model) ───────────────────────────────────────────────

def _fetch_stock_from_pos() -> dict[str, float]:
    """
    Pull current stock quantities from the POS API.
    Returns a dict of {SKU: quantity_as_float}.

    Implements poll-first architecture: this function is called by the
    APScheduler every 15 minutes (configured in main.py or api/main.py).
    The optional webhook endpoint in FastAPI writes to the same format,
    so downstream code never knows the difference.

    Supports Paysera and RoboLabs via environment variable POS_PROVIDER.
    Replace the stub with your real API calls once credentials are confirmed.
    """
    provider = os.getenv("POS_PROVIDER", "stub").lower()

    if provider == "paysera":
        return _fetch_paysera()
    elif provider == "robolabs":
        return _fetch_robolabs()
    else:
        # Stub: returns realistic development data so the full pipeline
        # can be tested before POS credentials are available.
        log.info("POS_PROVIDER=stub — using development fixture data")
        return {
            "BEANS_ESPRESSO": 3.5,    # below 5kg threshold → triggers alert
            "BEANS_FILTER":   4.2,    # above 2kg threshold → ok
            "MILK_WHOLE":     8.0,    # below 10L threshold → triggers alert
            "MILK_OAT":       6.0,    # above 5L threshold → ok
            "CUPS_TAKEAWAY":  350.0,  # above 200pcs threshold → ok
        }


def _fetch_paysera() -> dict[str, float]:
    """
    Pull stock data from the Paysera POS API.
    Requires: PAYSERA_API_KEY and PAYSERA_MERCHANT_ID in environment.

    Paysera documentation: https://developers.paysera.com/en/pos
    """
    import urllib.request

    api_key     = os.getenv("PAYSERA_API_KEY")
    merchant_id = os.getenv("PAYSERA_MERCHANT_ID")
    if not api_key or not merchant_id:
        raise EnvironmentError(
            "PAYSERA_API_KEY and PAYSERA_MERCHANT_ID must be set."
        )

    url = f"https://api.paysera.com/pos/v1/merchants/{merchant_id}/inventory"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    # Map Paysera item IDs to internal SKUs using POS_SKU_MAP env var
    sku_map = json.loads(os.getenv("POS_SKU_MAP", "{}"))
    return {
        sku_map.get(item["id"], item["id"]): float(item["quantity"])
        for item in data.get("items", [])
    }


def _fetch_robolabs() -> dict[str, float]:
    """
    Pull stock data from the RoboLabs POS API.
    Requires: ROBOLABS_API_KEY and ROBOLABS_LOCATION_ID in environment.

    RoboLabs documentation: https://docs.robolabs.lt
    """
    import urllib.request

    api_key     = os.getenv("ROBOLABS_API_KEY")
    location_id = os.getenv("ROBOLABS_LOCATION_ID")
    if not api_key or not location_id:
        raise EnvironmentError(
            "ROBOLABS_API_KEY and ROBOLABS_LOCATION_ID must be set."
        )

    url = f"https://api.robolabs.lt/v2/locations/{location_id}/stock"
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    sku_map = json.loads(os.getenv("POS_SKU_MAP", "{}"))
    return {
        sku_map.get(item["productCode"], item["productCode"]): float(item["qty"])
        for item in data.get("stock", [])
    }


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_thresholds() -> dict[str, dict]:
    """
    Load per-SKU thresholds from config/inventory_thresholds.json.
    Falls back to DEFAULT_THRESHOLDS if the file is missing.
    """
    config_path = Path("config/inventory_thresholds.json")
    if config_path.exists():
        try:
            with config_path.open(encoding="utf-8") as f:
                data = json.load(f)
            log.debug("Loaded thresholds from %s", config_path)
            return data
        except Exception as exc:
            log.warning("Could not load %s: %s — using defaults", config_path, exc)
    return DEFAULT_THRESHOLDS


def _log_stock_event(conn, sku: str, name: str,
                     current: float, threshold: float, order_qty: float) -> None:
    """Write a low-stock event to SQLite for audit and deduplication."""
    conn.execute(
        """INSERT OR IGNORE INTO audit_log
           (run_at, audit_date, pos_total, ieka_total, discrepancy, status, notes)
           VALUES (?, ?, ?, ?, ?, 'OK', ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "0.00", "0.00", "0.00",
            f"LOW_STOCK: {sku} ({name}) current={current} threshold={threshold} "
            f"order_qty={order_qty}",
        ),
    )
    conn.commit()


# ── CLI smoke test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from migrate import apply_migrations, get_connection, DEFAULT_DB

    apply_migrations(DEFAULT_DB)
    conn = get_connection(DEFAULT_DB)
    result = check_stock_levels(conn)
    conn.close()

    print(f"\nStatus: {result['status']}")
    print(f"Checked: {result['checked_at']}\n")
    for item in result["items"]:
        flag = "⚠️ LOW" if item["is_low"] else "✓ OK "
        print(f"  {flag}  {item['name']:<25} "
              f"{item['current_kg']:>6.1f} {item['unit']:<3}  "
              f"(threshold: {item['threshold_kg']} {item['unit']})")
    if result["low_stock"]:
        print(f"\n{len(result['low_stock'])} item(s) below threshold — "
              f"purchase orders will be drafted.")
