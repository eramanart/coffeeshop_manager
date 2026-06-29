"""
Pass-2 acceptance gate (#10): a tripped circuit breaker must BLOCK dispatch,
not merely appear on /status. Also covers retry-after-reset, non-portal actions
staying ungated, and the #7 escaping of a hostile event key.

Run:  python tests/test_breaker_blocks_dispatch.py
"""
import os
import sys
from pathlib import Path

os.environ["DRY_RUN"] = "true"
os.environ["DASHBOARD_USER"] = "owner"
os.environ["DASHBOARD_PASSWORD"] = "test-dash-pass"
os.environ["API_BEARER_TOKEN"] = "good-bearer-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
os.environ.setdefault("FEATURE_SENTIMENT_LOOP", "false")
os.environ.setdefault("FEATURE_HR_MANAGER", "false")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from main import app, _is_breaker_open, _render_dashboard  # noqa: E402
from migrate import get_connection, DEFAULT_DB, notify_if_new, log_portal_action  # noqa: E402

BEARER = "good-bearer-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def acked_at(conn, key):
    r = conn.execute("SELECT acknowledged_at FROM notifications_sent WHERE event_key=?", (key,)).fetchone()
    return r["acknowledged_at"] if r else "MISSING"


def trip(conn, portal, n=3):
    for i in range(n):
        log_portal_action(conn, portal=portal, action_type="draft",
                          description=f"forced failure {i}", outcome="failure")


with TestClient(app) as client:
    conn = get_connection(DEFAULT_DB)

    # Clean slate for the test portal and keys.
    conn.execute("DELETE FROM portal_actions WHERE portal IN ('vmi_imas','sodra')")
    KEY = "RECEIPT_DRAFTED:breaker_test_invoice.jpg"   # → vmi_imas
    SHIFT = "SHIFT_SUGGESTION:2026-06-20"               # non-portal, never gated
    for k in (KEY, SHIFT):
        conn.execute("DELETE FROM notifications_sent WHERE event_key=?", (k,))
    conn.commit()
    notify_if_new(conn, "RECEIPT_DRAFTED", KEY, "breaker test receipt")
    notify_if_new(conn, "SHIFT_SUGGESTION", SHIFT, "shift suggestion")

    print("Breaker closed initially:")
    check("vmi_imas breaker closed", not _is_breaker_open(conn, "vmi_imas"))

    print("Trip the breaker (3 consecutive vmi_imas failures):")
    trip(conn, "vmi_imas", 3)
    check("vmi_imas breaker now OPEN", _is_breaker_open(conn, "vmi_imas"))

    print("Breaker shows on /status:")
    r = client.get("/status", headers={"Authorization": "Basic b3duZXI6dGVzdC1kYXNoLXBhc3M="})
    check("/status lists vmi_imas", r.status_code == 200 and "vmi_imas" in r.json().get("open_circuit_breakers", []),
          f"got {r.status_code} {r.text[:200]}")

    print("Dispatch is BLOCKED (the acceptance gate):")
    r = client.post(f"/confirm/{KEY}", headers={"Authorization": f"Bearer {BEARER}"})
    body = r.json()
    check("confirm returns blocked_circuit_breaker_open",
          r.status_code == 200 and body.get("next_action") == "blocked_circuit_breaker_open",
          f"got {r.status_code} {body}")
    check("notification re-opened (acknowledged_at back to NULL, retryable)",
          acked_at(conn, KEY) is None, f"acked_at={acked_at(conn, KEY)!r}")

    print("Reset the breaker (one success), dispatch proceeds:")
    log_portal_action(conn, portal="vmi_imas", action_type="draft",
                      description="recovered", outcome="success")
    check("vmi_imas breaker closed after success", not _is_breaker_open(conn, "vmi_imas"))
    r = client.post(f"/confirm/{KEY}", headers={"Authorization": f"Bearer {BEARER}"})
    body = r.json()
    check("confirm now dispatches (isaf_draft_queued)",
          r.status_code == 200 and body.get("next_action") == "isaf_draft_queued",
          f"got {r.status_code} {body}")

    print("Non-portal action is never gated even with portal failures:")
    trip(conn, "vmi_imas", 3)  # re-open vmi breaker
    r = client.post(f"/confirm/{SHIFT}", headers={"Authorization": f"Bearer {BEARER}"})
    body = r.json()
    check("SHIFT_SUGGESTION dispatches despite open vmi breaker",
          r.status_code == 200 and body.get("next_action") == "shift_suggestion_approved",
          f"got {r.status_code} {body}")

    # Cleanup
    conn.execute("DELETE FROM portal_actions WHERE portal='vmi_imas'")
    conn.commit()
    conn.close()

print("\n#7 escaping — hostile event key cannot break out of HTML/JS:")
EVIL = "x');alert(document.cookie)//"
alerts = [{
    "event_type": "PO_DRAFTED",
    "event_key": EVIL,
    "sent_at": "2026-06-15T00:00:00Z",
    "message_preview": "<script>alert('xss')</script>",
}]
stock = {"items": [{"name": "<img src=x onerror=alert(1)>", "current_kg": "5", "threshold_kg": "3",
                    "unit": "kg", "is_low": False}], "low_stock": []}
hostroster = [{"name": "<b>Bob</b>", "slot_label": "Morning</div><script>x</script>"}]
out = _render_dashboard({"status": "OK"}, alerts, stock, hostroster, bearer_token="tok")
check = None  # local rebinding guard
ok1 = "<script>alert('xss')</script>" not in out          # preview escaped
ok2 = "');alert(document.cookie)//" not in out             # raw JS-break absent
ok3 = "<img src=x onerror=alert(1)>" not in out            # stock name escaped
ok4 = "<b>Bob</b>" not in out and "<script>x</script>" not in out
ok5 = "&lt;script&gt;" in out                              # proof escaping happened
for name, ok in [("message_preview escaped", ok1), ("hostile key not raw in JS", ok2),
                 ("stock name escaped", ok3), ("roster name/slot escaped", ok4),
                 ("entities present (escaping ran)", ok5)]:
    if ok:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1; print(f"  FAIL  {name}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
