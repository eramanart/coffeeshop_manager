"""
dashboard_streamlit.py — CoffeeManager-OS local, READ-ONLY dashboard.

A nicer view over data/memory.sqlite than the FastAPI page, intended ONLY for
local use (`streamlit run`) or LAN behind an HTTPS reverse proxy. It is NOT for
public hosting (Streamlit Community Cloud etc.) — the system handles VMI/Sodra
financial data, GDPR PII, and credentials. See CLAUDE.md.

Design guarantees:
  * Opens SQLite in read-only URI mode (mode=ro) — it CANNOT mutate the DB.
  * Does NOT import the app's write path (migrate.get_connection opens rw).
  * Money columns are shown as the stored Decimal strings, never reparsed as
    float. The revenue chart uses hourly_sales.revenue_eur (already REAL) — a
    visualization, not a tax figure.

Run (from python/, with the stenv conda env active):
    streamlit run dashboard_streamlit.py
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import streamlit as st

# DB anchored to this file's location (python/data/), matching migrate.DEFAULT_DB,
# so the dashboard reads the same canonical DB regardless of launch directory.
DB_PATH = Path(__file__).resolve().parent / "data" / "memory.sqlite"


# ---------------------------------------------------------------------------
# Read-only data access
# ---------------------------------------------------------------------------
def _connect_ro() -> sqlite3.Connection:
    """Open the DB strictly read-only. Raises if the file is missing."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=15)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame. Cached briefly so reruns are cheap."""
    conn = _connect_ro()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


@st.cache_data(ttl=15)
def scalar(sql: str, params: tuple = ()) -> object:
    conn = _connect_ro()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def fmt_eur(value: object) -> str:
    """Format a stored Decimal-string (or number) as euros without float drift."""
    if value is None:
        return "—"
    try:
        return f"€{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(page_title="CoffeeManager-OS", page_icon="☕", layout="wide")

st.title("☕ CoffeeManager-OS — Dashboard")
st.caption("Read-only view over data/memory.sqlite · local use only · not for public hosting")

# Sidebar
with st.sidebar:
    st.header("Status")
    if not DB_PATH.exists():
        st.error(f"DB not found:\n{DB_PATH}")
        st.stop()
    st.success("Connected (read-only)")
    st.code(str(DB_PATH), language=None)
    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Data auto-caches for 15s.")

    st.divider()
    st.subheader("Date filter")
    st.caption("Applies to the Audit and Revenue tabs.")

    def _parse_date(value: object, fallback: dt.date) -> dt.date:
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return fallback

    _today = dt.date.today()
    _dmin = scalar(
        "SELECT MIN(d) FROM (SELECT MIN(audit_date) d FROM audit_log "
        "UNION SELECT MIN(sale_date) FROM hourly_sales)"
    )
    _dmax = scalar(
        "SELECT MAX(d) FROM (SELECT MAX(audit_date) d FROM audit_log "
        "UNION SELECT MAX(sale_date) FROM hourly_sales)"
    )
    bound_lo = _parse_date(_dmin, _today - dt.timedelta(days=30))
    bound_hi = _parse_date(_dmax, _today)
    picked = st.date_input(
        "Range",
        value=(bound_lo, bound_hi),
        min_value=bound_lo,
        max_value=bound_hi,
        format="YYYY-MM-DD",
    )
    if isinstance(picked, (tuple, list)) and len(picked) == 2:
        start_date, end_date = picked
    else:  # mid-selection: only one date chosen so far
        start_date = end_date = picked if isinstance(picked, dt.date) else bound_lo
    start_s, end_s = start_date.isoformat(), end_date.isoformat()

# --- Top metrics ------------------------------------------------------------
latest_audit = q(
    "SELECT * FROM audit_log ORDER BY date(audit_date) DESC, id DESC LIMIT 1"
)
latest_rev_date = scalar("SELECT MAX(sale_date) FROM hourly_sales")
day_rev = scalar(
    "SELECT SUM(revenue_eur) FROM hourly_sales WHERE sale_date = ?",
    (latest_rev_date,) if latest_rev_date else ("",),
)
open_receipts = scalar(
    "SELECT COUNT(*) FROM receipt_processing WHERE vmi_status IN ('pending','drafted')"
)
unack = scalar(
    "SELECT COUNT(*) FROM notifications_sent WHERE acknowledged_at IS NULL"
)
active_baristas = scalar("SELECT COUNT(*) FROM baristas WHERE is_active = 1")

c1, c2, c3, c4, c5 = st.columns(5)
if not latest_audit.empty:
    a = latest_audit.iloc[0]
    c1.metric(
        f"Audit · {a['audit_date']}",
        a["status"],
        delta=f"Δ {fmt_eur(a['discrepancy'])}",
        delta_color="inverse" if a["status"] == "MISMATCH" else "off",
    )
else:
    c1.metric("Audit", "—")
c2.metric(f"Revenue · {latest_rev_date or '—'}", fmt_eur(day_rev))
c3.metric("Open receipts", open_receipts or 0)
c4.metric("Unack. alerts", unack or 0)
c5.metric("Active baristas", active_baristas or 0)

st.divider()

# --- Tabs -------------------------------------------------------------------
tab_audit, tab_rev, tab_roster, tab_ocr, tab_alerts = st.tabs(
    ["Audit", "Revenue", "Roster", "Receipts (OCR)", "Alerts"]
)

with tab_audit:
    st.subheader("Z-report reconciliation")
    if not latest_audit.empty:
        a = latest_audit.iloc[0]
        if a["status"] == "MISMATCH":
            st.error(f"⚠ MISMATCH on {a['audit_date']} — discrepancy {fmt_eur(a['discrepancy'])}")
        else:
            st.success(f"OK on {a['audit_date']} — POS and i.EKA reconcile")
        m1, m2, m3 = st.columns(3)
        m1.metric("POS total", fmt_eur(a["pos_total"]))
        m2.metric("i.EKA total", fmt_eur(a["ieka_total"]))
        m3.metric("Discrepancy", fmt_eur(a["discrepancy"]))
    st.markdown(f"**Audit runs · {start_s} → {end_s}**")
    audits = q(
        "SELECT audit_date, status, pos_total, ieka_total, discrepancy, notes "
        "FROM audit_log WHERE date(audit_date) BETWEEN date(?) AND date(?) "
        "ORDER BY date(audit_date) DESC, id DESC",
        (start_s, end_s),
    )
    if audits.empty:
        st.info("No audit runs in the selected date range.")
    else:
        # Raw (unformatted) export first, so the CSV keeps clean Decimal strings.
        st.download_button(
            "⬇ Download audit log (CSV)",
            data=audits.to_csv(index=False).encode("utf-8"),
            file_name=f"audit_log_{start_s}_to_{end_s}.csv",
            mime="text/csv",
        )
        display = audits.copy()
        for col in ("pos_total", "ieka_total", "discrepancy"):
            display[col] = display[col].map(fmt_eur)
        st.dataframe(display, use_container_width=True, hide_index=True)

with tab_rev:
    st.subheader(f"Hourly sales · {start_s} → {end_s}")
    last_days = q(
        "SELECT sale_date, SUM(revenue_eur) AS revenue FROM hourly_sales "
        "WHERE date(sale_date) BETWEEN date(?) AND date(?) "
        "GROUP BY sale_date ORDER BY date(sale_date)",
        (start_s, end_s),
    )
    if last_days.empty:
        st.info("No sales in the selected date range.")
    else:
        rng_total = sum(Decimal(str(v)) for v in last_days["revenue"])
        st.metric("Revenue in range", fmt_eur(rng_total))
        st.bar_chart(last_days.set_index("sale_date")["revenue"], height=260)
    st.markdown("**Average revenue by hour of day** (within range)")
    by_hour = q(
        "SELECT hour, AVG(revenue_eur) AS avg_revenue FROM hourly_sales "
        "WHERE date(sale_date) BETWEEN date(?) AND date(?) "
        "GROUP BY hour ORDER BY hour",
        (start_s, end_s),
    )
    if not by_hour.empty:
        st.line_chart(by_hour.set_index("hour")["avg_revenue"], height=240)

with tab_roster:
    st.subheader("Published shifts (upcoming)")
    shifts = q(
        "SELECT ps.shift_date, ps.slot_label, b.name AS barista, ps.published_at "
        "FROM published_shifts ps LEFT JOIN baristas b ON b.id = ps.barista_id "
        "ORDER BY date(ps.shift_date), ps.slot_label"
    )
    st.dataframe(shifts, use_container_width=True, hide_index=True)

    st.markdown("**Forecast / suggestion status**")
    sugg = q(
        "SELECT suggestion_date, total_forecast_eur, confidence, status, owner_response "
        "FROM shift_suggestions ORDER BY date(suggestion_date) DESC LIMIT 14"
    )
    if not sugg.empty:
        sugg["total_forecast_eur"] = sugg["total_forecast_eur"].map(fmt_eur)
    st.dataframe(sugg, use_container_width=True, hide_index=True)

    st.markdown("**Swap requests**")
    swaps = q(
        "SELECT sr.shift_date, sr.slot_name, "
        "       r.name AS requester, t.name AS target, "
        "       sr.status, sr.created_at, sr.resolved_at "
        "FROM swap_requests sr "
        "LEFT JOIN baristas r ON r.id = sr.requester_id "
        "LEFT JOIN baristas t ON t.id = sr.target_id "
        "ORDER BY datetime(sr.created_at) DESC LIMIT 50"
    )
    if swaps.empty:
        st.info("No swap requests.")
    else:
        st.dataframe(swaps, use_container_width=True, hide_index=True)

    st.markdown("**Baristas**")
    # PII-light: name + level + active flag only. No telegram_id / personal data.
    baristas = q(
        "SELECT name, level, hourly_rate, is_active, hire_date "
        "FROM baristas ORDER BY name"
    )
    if not baristas.empty:
        baristas["hourly_rate"] = baristas["hourly_rate"].map(fmt_eur)
    st.dataframe(baristas, use_container_width=True, hide_index=True)

with tab_ocr:
    st.subheader("Receipt → OCR → i.SAF pipeline")
    receipts = q(
        "SELECT filename, ocr_status, supplier_vat, doc_date, net_amount, "
        "pvm_amount, pvm_code, vmi_status, vmi_draft_ref, notes "
        "FROM receipt_processing ORDER BY date(created_at) DESC, id DESC"
    )
    for col in ("net_amount", "pvm_amount"):
        if col in receipts:
            receipts[col] = receipts[col].map(fmt_eur)
    st.dataframe(receipts, use_container_width=True, hide_index=True)
    st.caption(
        "Tier gate: ≥90% auto → VMI draft · 70–89% owner confirm · <70% manual_review. "
        "This view never triggers OCR or XML — read-only."
    )

with tab_alerts:
    st.subheader("Notifications sent (dedup log)")
    alerts = q(
        "SELECT sent_at, event_type, event_key, message_preview, "
        "CASE WHEN acknowledged_at IS NULL THEN 'no' ELSE 'yes' END AS acknowledged "
        "FROM notifications_sent ORDER BY datetime(sent_at) DESC LIMIT 50"
    )
    st.dataframe(alerts, use_container_width=True, hide_index=True)

    pa_count = scalar("SELECT COUNT(*) FROM portal_actions")
    st.markdown(f"**Portal actions log** ({pa_count or 0} rows)")
    if pa_count:
        portal = q(
            "SELECT acted_at, portal, action_type, outcome, description, error_detail "
            "FROM portal_actions ORDER BY datetime(acted_at) DESC LIMIT 50"
        )
        st.dataframe(portal, use_container_width=True, hide_index=True)
    else:
        st.info("No portal actions logged yet.")
