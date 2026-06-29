"""
Smoke test for the Pass-1 dashboard-auth fix.

Exercises the path the reviewer flagged as never-executed:
  - GET / with no creds            → 401 (token NOT served)
  - GET / with wrong creds         → 401
  - GET / with right creds         → 200 AND the bearer token is gated behind auth
  - GET /status with no creds      → 401 (business data protected, was open before)
  - GET /health with no creds      → 200 (liveness stays public)
  - POST /confirm wrong bearer     → 401
  - POST /confirm right bearer     → confirmed
  - POST /confirm again (dedup)    → already_confirmed
  - /webhook/telegram wrong secret → 401
  - /webhook/telegram GO x2        → confirmed then already_confirmed

Run:  python tests/smoke_dashboard_auth.py
"""
import base64
import os
import sys
from pathlib import Path

# ── Test environment (set BEFORE importing the app) ──────────────────────────
os.environ["DRY_RUN"] = "true"
os.environ["DASHBOARD_USER"] = "owner"
os.environ["DASHBOARD_PASSWORD"] = "test-dash-pass"
os.environ["API_BEARER_TOKEN"] = "test-bearer-token-aaaaaaaaaaaaaaaaaaaaaaaa"
os.environ["TELEGRAM_SECRET_TOKEN"] = "test-tg-secret"
os.environ.setdefault("FEATURE_SENTIMENT_LOOP", "false")
os.environ.setdefault("FEATURE_HR_MANAGER", "false")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from migrate import get_connection, DEFAULT_DB, notify_if_new  # noqa: E402

GOOD = base64.b64encode(b"owner:test-dash-pass").decode()
BAD = base64.b64encode(b"owner:wrong").decode()
BEARER = "test-bearer-token-aaaaaaaaaaaaaaaaaaaaaaaa"

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


with TestClient(app) as client:
    # Seed a notification so /confirm has something to acknowledge.
    EVENT_KEY = "PO_DRAFTED:SMOKE_TEST:2026-W24"
    conn = get_connection(DEFAULT_DB)
    conn.execute("DELETE FROM notifications_sent WHERE event_key = ?", (EVENT_KEY,))
    conn.commit()
    notify_if_new(conn, "PO_DRAFTED", EVENT_KEY, "smoke test PO")
    conn.close()

    print("Dashboard page auth (browser path):")
    r = client.get("/", headers={})
    check("GET / no creds → 401", r.status_code == 401, f"got {r.status_code}")
    check("GET / no creds does NOT leak token",
          BEARER not in r.text, "token found in unauth response body!")

    r = client.get("/", headers={"Authorization": f"Basic {BAD}"})
    check("GET / wrong creds → 401", r.status_code == 401, f"got {r.status_code}")

    r = client.get("/", headers={"Authorization": f"Basic {GOOD}"})
    check("GET / good creds → 200", r.status_code == 200, f"got {r.status_code}")
    check("GET / good creds serves token to authed owner",
          BEARER in r.text, "token missing from authed dashboard")

    print("Business-data endpoints:")
    r = client.get("/status")
    check("GET /status no creds → 401", r.status_code == 401, f"got {r.status_code}")
    r = client.get("/shifts/today")
    check("GET /shifts/today no creds → 401", r.status_code == 401, f"got {r.status_code}")

    print("Liveness probe stays public:")
    r = client.get("/health")
    check("GET /health no creds → 200", r.status_code == 200, f"got {r.status_code}")

    # Fail-closed (creds unset → 503, no token served) is verified separately by
    # building a fresh app with blank DASHBOARD_USER/PASSWORD; see the one-liner in
    # the PR notes. It can't run in-process here because creds are captured at
    # create_app() time and this app was built with creds present.

    print("HITL /confirm bearer auth:")
    r = client.post(f"/confirm/{EVENT_KEY}", headers={"Authorization": "Bearer wrong"})
    check("POST /confirm wrong bearer → 401", r.status_code == 401, f"got {r.status_code}")

    r = client.post(f"/confirm/{EVENT_KEY}", headers={"Authorization": f"Bearer {BEARER}"})
    check("POST /confirm good bearer → confirmed",
          r.status_code == 200 and r.json().get("status") == "confirmed",
          f"got {r.status_code} {r.text}")

    r = client.post(f"/confirm/{EVENT_KEY}", headers={"Authorization": f"Bearer {BEARER}"})
    check("POST /confirm again → already_confirmed",
          r.status_code == 200 and r.json().get("status") == "already_confirmed",
          f"got {r.status_code} {r.text}")

    print("Telegram webhook secret + dedup:")
    # Re-seed a fresh key for the webhook GO path.
    KEY2 = "PO_DRAFTED:SMOKE_TG:2026-W24"
    conn = get_connection(DEFAULT_DB)
    conn.execute("DELETE FROM notifications_sent WHERE event_key = ?", (KEY2,))
    conn.commit()
    notify_if_new(conn, "PO_DRAFTED", KEY2, "smoke tg PO")
    conn.close()

    r = client.post("/webhook/telegram",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "WRONG"},
                    json={"message": {"text": f"GO {KEY2}"}})
    check("telegram wrong secret → 401", r.status_code == 401, f"got {r.status_code}")

    r = client.post("/webhook/telegram",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-tg-secret"},
                    json={"message": {"text": f"GO {KEY2}"}})
    check("telegram GO good secret → confirmed",
          r.status_code == 200 and r.json().get("status") == "confirmed",
          f"got {r.status_code} {r.text}")

    r = client.post("/webhook/telegram",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-tg-secret"},
                    json={"message": {"text": f"GO {KEY2}"}})
    check("telegram GO again → already_confirmed",
          r.status_code == 200 and r.json().get("status") == "already_confirmed",
          f"got {r.status_code} {r.text}")

    # #6: APPROVE is an alias for GO (used in roster copy)
    KEY3 = "ROSTER_DRAFT:2026-W25"
    conn = get_connection(DEFAULT_DB)
    conn.execute("DELETE FROM notifications_sent WHERE event_key = ?", (KEY3,))
    conn.commit()
    notify_if_new(conn, "ROSTER_DRAFT", KEY3, "roster draft")
    conn.close()
    r = client.post("/webhook/telegram",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-tg-secret"},
                    json={"message": {"text": f"APPROVE {KEY3}"}})
    check("telegram APPROVE alias → confirmed",
          r.status_code == 200 and r.json().get("status") == "confirmed",
          f"got {r.status_code} {r.text}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
