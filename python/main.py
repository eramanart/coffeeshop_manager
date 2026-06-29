"""
api/main.py — CoffeeManager-OS FastAPI dashboard
Phase 4: web interface, HITL confirmation, POS webhook receiver, cron scheduler.

Endpoints:
  GET  /                          → dashboard HTML
  GET  /status                    → last audit result + system health
  GET  /receipts                  → receipt processing queue
  GET  /alerts                    → unacknowledged notifications
  GET  /stock                     → current inventory levels
  GET  /pl/{month}                → P&L draft for YYYY-MM
  POST /confirm/{event_key}       → HITL: owner confirms a gated action
  POST /dismiss/{event_key}       → owner dismisses an alert
  POST /webhook/pos               → receive POS push events (optional)
  POST /webhook/telegram          → receive Telegram reply callbacks
  GET  /audit/history             → last 30 days of audit_log
  GET  /portal/actions            → last 50 portal_actions for monitoring
  GET  /health                    → liveness probe

Run:
  uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

Or via main.py:
  python main.py --mode api
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import secrets
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ── FastAPI and related ───────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Request, Depends
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError as exc:
    print(f"[ERROR] Missing dependency: {exc}")
    print("Install with: pip install 'fastapi[standard]' apscheduler uvicorn")
    sys.exit(1)

# ── Project imports ───────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from migrate import apply_migrations, get_connection, DEFAULT_DB, notify_if_new
from core.accounting import run_daily_audit, generate_pl_draft
from core.inventory import check_stock_levels
from api.helpers import (
    validate_telegram_secret_token,
    validate_bearer_token,
    validate_pos_secret,
    atomic_acknowledge,
)

log = logging.getLogger("api.main")

# ── Load environment (settings.env) ───────────────────────────────────────────
# The API is typically started via `uvicorn api.main:app`, which does NOT pass
# through main_gateway_loop_.py — so load settings.env here too, or every secret
# (dashboard Basic auth, API_BEARER_TOKEN, Telegram) reads empty and the app fails
# closed (dashboard 503, /confirm "not configured"). Existing process env wins:
# load_dotenv does not override variables already set (override=False default).
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "config" / "settings.env")

# ── Observability: Pydantic Logfire ───────────────────────────────────────────
# Additive and fail-open. Without the package installed, or without LOGFIRE_TOKEN,
# the app behaves exactly as before: send_to_logfire="if-token-present" makes it a
# silent no-op with no token, and default scrubbing redacts the Authorization /
# Basic-auth header. clawdbot runs as a --json subprocess, so its LLM reasoning is
# a black box here; what you get is the layer you own — FastAPI + every SQLite query.
try:
    import logfire
    logfire.configure(
        service_name="coffeemanager-os",
        environment=os.getenv("LOGFIRE_ENVIRONMENT", "production"),
        send_to_logfire="if-token-present",
        console=False,
    )
    try:
        logfire.instrument_sqlite3()        # one span per memory.sqlite query
    except Exception as exc:                # optional; needs the OTel sqlite3 pkg
        log.warning("Logfire sqlite3 tracing skipped: %s", exc)
    _LOGFIRE = logfire
except Exception as exc:                    # telemetry must never break the app
    _LOGFIRE = None
    log.warning("Logfire disabled (%s) — app continues without tracing", exc)

# ── Feature flags ─────────────────────────────────────────────────────────────
FEATURE_SENTIMENT = os.getenv("FEATURE_SENTIMENT_LOOP", "false").lower() == "true"
FEATURE_HR = os.getenv("FEATURE_HR_MANAGER", "false").lower() == "true"

# ── Circuit breaker ───────────────────────────────────────────────────────────
# Mirrors runner.CIRCUIT_BREAKER_THRESHOLD. Enforced here against the PERSISTED
# portal_actions log (the runner's counter is in-memory per agent instance and
# does not survive across HITL confirmations), so a portal that has failed its
# last N actions cannot be driven again by a dashboard/Telegram confirmation.
CIRCUIT_BREAKER_THRESHOLD = 3

# Which portal each confirmed-action prefix drives. Prefixes absent here trigger
# no portal interaction (internal DB updates, Telegram posts) and are never gated.
_PORTAL_FOR_PREFIX = {
    "RECEIPT_DRAFTED":   "vmi_imas",
    "ISAF_MONTHLY":      "vmi_imas",
    "NEW_HIRE_DRAFTED":  "sodra",
    "SODRA_RATE":        "sodra",
    "REVIEW_ESCALATION": "google",
    "WINBACK_POST":      "google",
}

# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    apply_migrations(DEFAULT_DB)

    app = FastAPI(
        title="CoffeeManager-OS",
        description="Hybrid operations agent dashboard",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # Request traces (method, route, status, timing). /health is excluded so the
    # uptime probe doesn't flood traces; bodies/headers aren't captured by default,
    # so the bearer token never lands in telemetry.
    if _LOGFIRE is not None:
        try:
            _LOGFIRE.instrument_fastapi(app, excluded_urls=["health"])
        except Exception as exc:
            log.warning("Logfire FastAPI tracing skipped: %s", exc)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[os.getenv("CORS_ALLOW_ORIGINS", "127.0.0.1")],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "X-Init-Data"],
    )

    # ── Basic Auth Middleware (protect dashboard and business data) ──────────────
    # GET / embeds API_BEARER_TOKEN in the page so the confirm/dismiss fetch() calls
    # can authenticate. That makes page-level auth mandatory: anyone who can load the
    # dashboard can read the token from view-source and call /confirm and /dismiss
    # directly. So every GET that renders the page or business data sits behind HTTP
    # Basic auth here, BEFORE the token is handed out.
    #
    # Excluded on purpose:
    #   /health              — unauthenticated liveness probe for uptime monitors
    #   /confirm, /dismiss   — POST, own bearer-token auth (validate_bearer_token)
    #   /webhook/*           — POST, own shared-secret auth (Telegram / POS)
    dashboard_user = os.getenv("DASHBOARD_USER", "")
    dashboard_pass = os.getenv("DASHBOARD_PASSWORD", "")

    _protected_prefixes = (
        "/status", "/receipts", "/alerts", "/stock", "/shifts",
        "/pl", "/audit", "/portal", "/docs", "/openapi", "/redoc",
    )

    def _is_protected(path: str) -> bool:
        if path == "/":
            return True
        return any(path == p or path.startswith(p + "/") or path.startswith(p + ".")
                   for p in _protected_prefixes)

    def _auth_challenge(detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="CoffeeManager-OS"'},
            content={"detail": detail},
        )

    async def dashboard_auth_middleware(request: Request, call_next):
        if _is_protected(request.url.path):
            auth_header = request.headers.get("Authorization", "")

            # The single-page dashboard's fetch() calls authenticate with the API
            # bearer token (the page already carries it, injected behind Basic auth).
            # Accept that bearer for the protected GETs so data loads don't depend on
            # the browser re-sending Basic auth to background fetches — which it does
            # not do reliably. The page itself (GET /) is still Basic-gated on initial
            # navigation, because the browser sends no bearer there; so this does not
            # widen who can obtain the token.
            api_token = os.getenv("API_BEARER_TOKEN", "")
            if auth_header.startswith("Bearer "):
                if api_token and secrets.compare_digest(auth_header[7:], api_token):
                    return await call_next(request)
                return _auth_challenge("Invalid credentials")

            # Fail CLOSED: a protected page must never be served without configured
            # credentials, or the embedded bearer token leaks to anyone who can reach it.
            if not (dashboard_user and dashboard_pass):
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Dashboard auth not configured "
                                       "(set DASHBOARD_USER and DASHBOARD_PASSWORD in settings.env)"},
                )

            if not auth_header.startswith("Basic "):
                return _auth_challenge("Authentication required")

            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                user, _, passwd = decoded.partition(":")
            except Exception:
                return _auth_challenge("Invalid authorization header")

            # Constant-time comparison to avoid leaking credentials via timing.
            user_ok = secrets.compare_digest(user, dashboard_user)
            pass_ok = secrets.compare_digest(passwd, dashboard_pass)
            if not (user_ok and pass_ok):
                return _auth_challenge("Invalid credentials")

        return await call_next(request)

    app.middleware("http")(dashboard_auth_middleware)

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")

    @app.on_event("startup")
    async def startup() -> None:
        _register_scheduled_jobs(scheduler)
        scheduler.start()
        log.info("CoffeeManager-OS API started. Scheduler running.")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")

    # ── Routes ────────────────────────────────────────────────────────────────
    _register_routes(app)

    # Telegram Mini App (barista calendar) — own initData auth, intentionally
    # outside the dashboard Basic-auth prefixes above.
    from api.miniapp import register_miniapp_routes
    register_miniapp_routes(app)

    return app


# ── Dependency: DB connection per request ────────────────────────────────────

def get_db():
    conn = get_connection(DEFAULT_DB)
    try:
        yield conn
    finally:
        conn.close()


# ── Scheduler jobs ────────────────────────────────────────────────────────────

def _register_scheduled_jobs(scheduler: AsyncIOScheduler) -> None:
    """
    Register all cron jobs as defined in soul.md operational protocols.
    All times are UTC.
    """

    # Daily audit — 07:00 UTC every day
    @scheduler.scheduled_job(CronTrigger(hour=7, minute=0))
    async def scheduled_daily_audit() -> None:
        log.info("Cron: daily audit triggered")
        _ping_healthcheck("HEALTHCHECK_AUDIT_URL", "/start")
        conn = get_connection(DEFAULT_DB)
        try:
            result = run_daily_audit(conn)
            await _handle_audit_result(conn, result)
            # The job RAN — that's what Healthchecks watches. A MISMATCH is a healthy
            # run with a business alert (sent above), so it still pings success; only
            # a script-level ERROR or an exception pings /fail.
            if result.get("status") == "ERROR":
                _ping_healthcheck("HEALTHCHECK_AUDIT_URL", "/fail")
            else:
                _ping_healthcheck("HEALTHCHECK_AUDIT_URL")
        except Exception as exc:
            log.error("Scheduled audit failed: %s", exc, exc_info=True)
            _ping_healthcheck("HEALTHCHECK_AUDIT_URL", "/fail")
        finally:
            conn.close()

    # Inventory check — every day at 18:00 UTC (also Sunday deep-check)
    @scheduler.scheduled_job(CronTrigger(hour=18, minute=0))
    async def scheduled_inventory_check() -> None:
        log.info("Cron: inventory check triggered")
        conn = get_connection(DEFAULT_DB)
        try:
            result = check_stock_levels(conn)
            await _handle_stock_result(conn, result)
        except Exception as exc:
            log.error("Scheduled inventory check failed: %s", exc, exc_info=True)
        finally:
            conn.close()

    # P&L draft — 2nd of each month at 20:00 UTC
    @scheduler.scheduled_job(CronTrigger(day=2, hour=20, minute=0))
    async def scheduled_pl_draft() -> None:
        log.info("Cron: P&L draft triggered")
        conn = get_connection(DEFAULT_DB)
        try:
            pl = generate_pl_draft(conn)
            await _handle_pl_result(conn, pl)
        except Exception as exc:
            log.error("Scheduled P&L draft failed: %s", exc, exc_info=True)
        finally:
            conn.close()

    # i.SAF monthly reminder — 12th of each month at 09:00 UTC
    @scheduler.scheduled_job(CronTrigger(day=12, hour=9, minute=0))
    async def scheduled_isaf_reminder() -> None:
        log.info("Cron: i.SAF monthly reminder triggered")
        _ping_healthcheck("HEALTHCHECK_ISAF_URL", "/start")
        conn = get_connection(DEFAULT_DB)
        try:
            await _trigger_isaf_compilation(conn)
            _ping_healthcheck("HEALTHCHECK_ISAF_URL")   # ran = up (a breaker-skip still counts)
        except Exception as exc:
            log.error("Scheduled i.SAF trigger failed: %s", exc, exc_info=True)
            _ping_healthcheck("HEALTHCHECK_ISAF_URL", "/fail")
        finally:
            conn.close()

    # POS poll — every 15 minutes (pull model, blind-spot amendment #3)
    poll_minutes = int(os.getenv("POS_POLL_MINUTES", "15"))

    @scheduler.scheduled_job(
        CronTrigger(minute=f"*/{poll_minutes}")
    )
    async def scheduled_pos_poll() -> None:
        """Pull POS data on schedule. Same queue as webhook — downstream is agnostic."""
        conn = get_connection(DEFAULT_DB)
        try:
            result = check_stock_levels(conn)
            # Only alert if something newly crossed the threshold
            for item in result.get("low_stock", []):
                week = datetime.now(timezone.utc).strftime("%Y-W%W")
                key  = f"STOCK_LOW:{item['sku']}:{week}"
                msg  = _stock_alert_message(item)
                await _send_telegram("STOCK_LOW", key, msg, conn, fields=_stock_alert_fields(item))
        except Exception as exc:
            log.error("POS poll failed: %s", exc)
        finally:
            conn.close()

    # Barista HR promotion check — daily at 09:00 UTC
    if FEATURE_HR:
        @scheduler.scheduled_job(CronTrigger(hour=9, minute=0))
        async def scheduled_hr_promotion_check() -> None:
            log.info("Cron: HR promotion check triggered")
            conn = get_connection(DEFAULT_DB)
            try:
                from core.hr_manager import check_promotions
                check_promotions(conn)
            except Exception as exc:
                log.error("HR promotion check failed: %s", exc, exc_info=True)
            finally:
                conn.close()
    else:
        log.info("HR feature disabled; skipping promotion check job")

    # Hourly sales collection — every hour at :55
    @scheduler.scheduled_job(CronTrigger(minute=55))
    async def scheduled_collect_sales() -> None:
        conn = get_connection(DEFAULT_DB)
        try:
            from core.labor_forecast import backfill_from_pos
            backfill_from_pos(conn, days_back=1)
        except Exception as exc:
            log.error("Sales collection failed: %s", exc, exc_info=True)
        finally:
            conn.close()

    # Weekly shift suggestions — Sunday 18:05 UTC
    @scheduler.scheduled_job(CronTrigger(day_of_week="sun", hour=18, minute=5))
    async def scheduled_shift_suggestions() -> None:
        log.info("Cron: weekly shift suggestions triggered")
        from core.labor_forecast import forecast_day
        from core.shift_suggester import suggest_shifts
        conn = get_connection(DEFAULT_DB)
        try:
            from datetime import timedelta
            next_week = [date.today() + timedelta(days=i) for i in range(1, 8)]
            for target in next_week:
                forecast   = forecast_day(conn, target)
                suggestion = suggest_shifts(forecast)
                await _send_telegram(
                    "SHIFT_SUGGESTION", suggestion["event_key"], suggestion["telegram_msg"], conn
                )
        except Exception as exc:
            log.error("Shift suggestions failed: %s", exc, exc_info=True)
        finally:
            conn.close()

    # Weekly roster — Sunday 18:10 UTC
    @scheduler.scheduled_job(CronTrigger(day_of_week="sun", hour=18, minute=10))
    async def scheduled_roster() -> None:
        log.info("Cron: weekly roster triggered")
        conn = get_connection(DEFAULT_DB)
        try:
            from agent.scheduler_bot import run_weekly_roster_job
            from notify import send_telegram as _send_tg
            await run_weekly_roster_job(conn, _send_tg)
        except Exception as exc:
            log.error("Weekly roster failed: %s", exc, exc_info=True)
        finally:
            conn.close()

    # Review poll — every 4 hours
    if FEATURE_SENTIMENT:
        @scheduler.scheduled_job(CronTrigger(hour="*/4"))
        async def scheduled_poll_reviews() -> None:
            conn = get_connection(DEFAULT_DB)
            try:
                from core.sentiment_loop import poll_reviews
                poll_reviews(conn)
            except Exception as exc:
                log.error("Review poll failed: %s", exc, exc_info=True)
            finally:
                conn.close()
    else:
        log.info("Sentiment feature disabled; skipping review poll job")

    # Weekly review digest — Monday 08:00 UTC
    if FEATURE_SENTIMENT:
        @scheduler.scheduled_job(CronTrigger(day_of_week="mon", hour=8, minute=0))
        async def scheduled_weekly_digest() -> None:
            log.info("Cron: weekly review digest triggered")
            conn = get_connection(DEFAULT_DB)
            try:
                from core.sentiment_loop import build_weekly_digest
                build_weekly_digest(conn)
            except Exception as exc:
                log.error("Weekly digest failed: %s", exc, exc_info=True)
            finally:
                conn.close()
    else:
        log.info("Sentiment feature disabled; skipping weekly digest job")

    # Monthly winback post — 1st of month 09:05 UTC
    if FEATURE_SENTIMENT:
        @scheduler.scheduled_job(CronTrigger(day=1, hour=9, minute=5))
        async def scheduled_winback() -> None:
            log.info("Cron: monthly winback post triggered")
            conn = get_connection(DEFAULT_DB)
            try:
                from core.sentiment_loop import draft_winback_post
                draft_winback_post(conn)
            except Exception as exc:
                log.error("Winback draft failed: %s", exc, exc_info=True)
            finally:
                conn.close()
    else:
        log.info("Sentiment feature disabled; skipping monthly winback job")

    log.info(
        "Scheduled jobs registered: audit@07:00, hr@09:00, isaf@12th, "
        "inventory@18:00, pl@2nd-20:00, pos@every-%dmin, sales@:55, "
        "shifts@Sun18:05, roster@Sun18:10, reviews@*/4h, "
        "digest@Mon08:00, winback@1st-09:05 (all UTC)", poll_minutes
    )


# ── Route registration ────────────────────────────────────────────────────────

def _register_routes(app: FastAPI) -> None:

    # ── Dashboard HTML ────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
    async def dashboard(conn=Depends(get_db)):
        """Owner-facing dashboard — single-page HTML, no JS framework needed."""
        audit  = _last_audit(conn)
        alerts = _unacked_alerts(conn)
        stock  = check_stock_levels(conn)
        roster = _today_roster(conn)
        bearer_token = os.getenv("API_BEARER_TOKEN", "")
        return HTMLResponse(_render_dashboard(audit, alerts, stock, roster, bearer_token))

    # ── Status ────────────────────────────────────────────────────────────────
    @app.get("/status", tags=["Monitoring"])
    async def status(conn=Depends(get_db)) -> dict:
        """System health: last audit result, unacknowledged alert count, scheduler state."""
        audit        = _last_audit(conn)
        unacked      = _unacked_alerts(conn)
        open_breakers = _open_circuit_breakers(conn)
        return {
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "audit":                audit,
            "unacknowledged_alerts": len(unacked),
            "open_circuit_breakers": open_breakers,
            "scheduler":            "running",
        }

    # ── Receipts queue ────────────────────────────────────────────────────────
    @app.get("/receipts", tags=["Operations"])
    async def receipts(conn=Depends(get_db)) -> dict:
        """All receipts and their processing status."""
        rows = conn.execute(
            """SELECT filename, ocr_status, ocr_raw_json, supplier_vat,
                      doc_date, net_amount, pvm_amount, pvm_code,
                      vmi_status, vmi_draft_ref, notes
               FROM receipt_processing
               ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        return {
            "receipts": [dict(r) for r in rows],
            "pending":  sum(1 for r in rows if r["vmi_status"] == "pending"),
            "drafted":  sum(1 for r in rows if r["vmi_status"] == "drafted"),
            "signed":   sum(1 for r in rows if r["vmi_status"] == "signed"),
        }

    # ── Alerts ────────────────────────────────────────────────────────────────
    @app.get("/alerts", tags=["Operations"])
    async def alerts(conn=Depends(get_db)) -> dict:
        """All notifications sent, with acknowledgement status."""
        rows = conn.execute(
            """SELECT id, sent_at, channel, event_type, event_key,
                      message_preview, fields_json, acknowledged_at
               FROM notifications_sent
               ORDER BY sent_at DESC LIMIT 100"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Parse structured fields for the dashboard cards; None → card falls
            # back to message_preview.
            raw = d.pop("fields_json", None)
            try:
                d["fields"] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                d["fields"] = None
            out.append(d)
        return {
            "alerts":        out,
            "unacknowledged": sum(1 for r in out if r["acknowledged_at"] is None),
        }

    # ── Stock ─────────────────────────────────────────────────────────────────
    @app.get("/stock", tags=["Operations"])
    async def stock(conn=Depends(get_db)) -> dict:
        """Current inventory levels against thresholds."""
        return check_stock_levels(conn)

    # ── Shifts today ──────────────────────────────────────────────────────────
    @app.get("/shifts/today", tags=["Scheduling"])
    async def shifts_today(conn=Depends(get_db)) -> dict:
        """Published shifts for today with barista names."""
        return {"date": str(date.today()), "roster": _today_roster(conn)}

    # ── P&L ───────────────────────────────────────────────────────────────────
    @app.get("/pl/{month}", tags=["Accounting"])
    async def pl_draft(month: str, conn=Depends(get_db)) -> dict:
        """
        Return P&L draft for a given month (YYYY-MM).
        Generates on the fly if not cached.
        """
        if not _valid_month(month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        cached = Path(f"data/pl_draft_{month}.json")
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        return generate_pl_draft(conn, month)

    # ── Audit history ─────────────────────────────────────────────────────────
    @app.get("/audit/history", tags=["Accounting"])
    async def audit_history(days: int = 30, conn=Depends(get_db)) -> dict:
        """Last N days of Z-report audit results."""
        rows = conn.execute(
            """SELECT audit_date, pos_total, ieka_total, discrepancy,
                      status, notes, run_at
               FROM audit_log
               ORDER BY audit_date DESC LIMIT ?""",
            (days,)
        ).fetchall()
        mismatches = sum(1 for r in rows if r["status"] == "MISMATCH")
        return {
            "days":       days,
            "records":    [dict(r) for r in rows],
            "mismatches": mismatches,
            "ok_rate":    f"{((len(rows) - mismatches) / max(len(rows), 1) * 100):.1f}%",
        }

    # ── Portal actions ────────────────────────────────────────────────────────
    @app.get("/portal/actions", tags=["Monitoring"])
    async def portal_actions(limit: int = 50, conn=Depends(get_db)) -> dict:
        """Recent portal interactions for monitoring and debugging."""
        rows = conn.execute(
            """SELECT acted_at, portal, action_type, url, description,
                      outcome, error_detail, session_id
               FROM portal_actions
               ORDER BY acted_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        failures = sum(1 for r in rows if r["outcome"] == "failure")
        return {
            "actions":  [dict(r) for r in rows],
            "failures": failures,
            "total":    len(rows),
        }

    # ── HITL: confirm ─────────────────────────────────────────────────────────
    @app.post("/confirm/{event_key}", tags=["HITL"])
    async def confirm_action(
        event_key: str,
        request: Request,
        conn=Depends(get_db)
    ) -> dict:
        """
        Owner confirms a gated action (GO).
        Marks the notification as acknowledged and queues the next agent step.

        Requires: Authorization: Bearer <API_BEARER_TOKEN>

        Examples:
          POST /confirm/PO_DRAFTED:BEANS_ESPRESSO:2025-W19
          POST /confirm/RECEIPT_DRAFTED:invoice_2025_05_01.jpg
        """
        validate_bearer_token(request.headers.get("Authorization"))

        row = conn.execute(
            "SELECT * FROM notifications_sent WHERE event_key = ?",
            (event_key,)
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No notification found for event_key: {event_key}"
            )

        # Use atomic acknowledge to prevent double-dispatch
        ack_ok = atomic_acknowledge(conn, event_key)
        if not ack_ok:
            return {
                "status":  "already_confirmed",
                "event_key": event_key,
                "acknowledged_at": row["acknowledged_at"],
            }

        # ── Dispatch post-confirmation actions ────────────────────────────────
        next_action = await _dispatch_confirmed_action(event_key, conn)

        now = datetime.now(timezone.utc).isoformat()
        log.info("Owner confirmed: %s → %s", event_key, next_action)
        return {
            "status":       "confirmed",
            "event_key":    event_key,
            "confirmed_at": now,
            "next_action":  next_action,
        }

    # ── HITL: dismiss ─────────────────────────────────────────────────────────
    @app.post("/dismiss/{event_key}", tags=["HITL"])
    async def dismiss_alert(
        event_key: str,
        request: Request,
        conn=Depends(get_db)
    ) -> dict:
        """
        Owner dismisses an alert without taking action.
        Marks acknowledged without triggering any downstream step.

        Requires: Authorization: Bearer <API_BEARER_TOKEN>
        """
        validate_bearer_token(request.headers.get("Authorization"))

        atomic_acknowledge(conn, event_key)
        log.info("Owner dismissed: %s", event_key)
        return {"status": "dismissed", "event_key": event_key}

    # ── POS webhook (optional push model) ─────────────────────────────────────
    @app.post("/webhook/pos", tags=["Webhooks"])
    async def pos_webhook(request: Request, conn=Depends(get_db)) -> dict:
        """
        Optional: receive real-time POS events if the provider supports webhooks.
        Writes events into the same internal queue as the 15-min poller.
        Downstream code never knows the source — pull and push are equivalent.

        Requires: Authorization: Bearer <WEBHOOK_POS_SECRET> (if configured)
        """
        validate_pos_secret(request.headers.get("Authorization"))

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        event_type = payload.get("type", "unknown")
        log.info("POS webhook received: type=%s", event_type)

        # Extract POS total and period from payload (format depends on POS provider)
        pos_total = payload.get("total", "0.00")
        period_start = payload.get("period_start", date.today().isoformat())
        period_end = payload.get("period_end", date.today().isoformat())

        # Write to pos_events table (isolated from audit_log)
        conn.execute(
            """INSERT INTO pos_events
               (received_at, pos_total, period_start, period_end, source, notes)
               VALUES (?, ?, ?, ?, 'webhook', ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                str(pos_total),
                period_start,
                period_end,
                f"type:{event_type}",
            )
        )
        conn.commit()
        return {"status": "received", "event_type": event_type}

    # ── Telegram webhook ──────────────────────────────────────────────────────
    @app.post("/webhook/telegram", tags=["Webhooks"])
    async def telegram_webhook(request: Request, conn=Depends(get_db)) -> dict:
        """
        Receive Telegram message callbacks (owner replies).
        Maps reply text to confirm/dismiss actions.

        Requires: X-Telegram-Bot-Api-Secret-Token header with configured secret

        GO {event_key}   → calls confirm_action
        SKIP {event_key} → calls dismiss_alert

        Case is preserved: "GO RECEIPT_DRAFTED:invoice_2025_05_01.jpg" preserves lowercase key.
        """
        validate_telegram_secret_token(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        )

        try:
            update = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid Telegram update")

        message = (update.get("message") or update.get("callback_query", {})
                   .get("message", {}))
        text = (message.get("text") or "").strip()

        # Only uppercase the command verb, preserve event_key case.
        # APPROVE is an alias for GO (owner-facing copy uses "APPROVE" for rosters).
        upper = text.upper()
        if upper.startswith("GO ") or upper.startswith("APPROVE "):
            # Extract event_key preserving its original case
            verb_len = 3 if upper.startswith("GO ") else len("APPROVE ")
            event_key = text[verb_len:].strip()
            row = conn.execute(
                "SELECT event_key FROM notifications_sent WHERE event_key = ?",
                (event_key,)
            ).fetchone()
            if row:
                # Use atomic acknowledge to prevent double-dispatch
                ack_ok = atomic_acknowledge(conn, event_key)
                if ack_ok:
                    next_action = await _dispatch_confirmed_action(event_key, conn)
                    log.info("Telegram GO: %s → %s", event_key, next_action)
                    return {"status": "confirmed", "event_key": event_key, "next_action": next_action}
                else:
                    log.info("Telegram GO (already ack'd): %s", event_key)
                    return {"status": "already_confirmed", "event_key": event_key}

        elif upper.startswith("SKIP "):
            # Extract event_key preserving its original case
            event_key = text[5:].strip()
            atomic_acknowledge(conn, event_key)
            log.info("Telegram SKIP: %s", event_key)
            return {"status": "dismissed", "event_key": event_key}

        return {"status": "ignored", "text": text[:50]}

    # ── Health probe ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["Monitoring"])
    async def health() -> dict:
        """Liveness probe for uptime monitoring."""
        db_ok = False
        try:
            conn = get_connection(DEFAULT_DB)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            db_ok = True
        except Exception:
            pass
        status = "ok" if db_ok else "degraded"
        return {
            "status":    status,
            "db":        "ok" if db_ok else "error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ── Post-confirmation dispatcher ──────────────────────────────────────────────

async def _dispatch_confirmed_action(event_key: str, conn) -> str:
    """
    After an owner GO confirmation, determine and trigger the next step.
    Returns a human-readable description of the action taken.

    This is the bridge between the HITL endpoint and the OpenClaw agent.
    In production: instantiate OpenClawAgent and call run_task().
    Here: documented stubs so the wiring is explicit.
    """
    prefix = event_key.split(":")[0]

    # ── Circuit-breaker gate ──────────────────────────────────────────────────
    # If this action drives a portal whose breaker is open, refuse to perform it.
    # Re-open the acknowledgement (acknowledged_at=NULL) so the owner can retry the
    # SAME confirmation once the portal is healthy — otherwise the earlier
    # atomic_acknowledge() would have permanently consumed the event.
    portal = _PORTAL_FOR_PREFIX.get(prefix)
    if portal and _is_breaker_open(conn, portal):
        conn.execute(
            "UPDATE notifications_sent SET acknowledged_at = NULL WHERE event_key = ?",
            (event_key,),
        )
        conn.commit()
        log.warning("Dispatch BLOCKED: '%s' breaker open — %s NOT performed", portal, event_key)
        bkey = f"BREAKER_OPEN:{portal}:{date.today().isoformat()}"
        await _send_telegram(
            "BREAKER_OPEN", bkey,
            f"⛔ *Action blocked* — `{portal}` circuit breaker is open "
            f"({CIRCUIT_BREAKER_THRESHOLD} consecutive failures).\n"
            f"`{event_key}` was NOT performed. Investigate the portal, then re-confirm.",
            conn,
        )
        return "blocked_circuit_breaker_open"

    if prefix == "PO_DRAFTED":
        # Owner approved a purchase order — send the supplier email
        # agent = OpenClawAgent(...); await agent.run_task({"task": "send_po", "key": event_key})
        log.info("Dispatching: send purchase order for %s", event_key)
        return "purchase_order_queued_for_sending"

    elif prefix == "RECEIPT_DRAFTED":
        # Owner confirmed a Tier-2 OCR receipt — generate XML and draft in VMI
        log.info("Dispatching: resume i.SAF draft for %s", event_key)
        return "isaf_draft_queued"

    elif prefix == "NEW_HIRE_DRAFTED":
        # Owner confirmed they've signed the Sodra 1-SD (update status)
        name_part = event_key.replace("NEW_HIRE_DRAFTED:", "").split(":")[0]
        conn.execute(
            """UPDATE hr_actions SET sodra_status = 'signed',
               signed_at = ? WHERE employee_name = ?""",
            (datetime.now(timezone.utc).isoformat(), name_part)
        )
        conn.commit()
        log.info("hr_actions updated: signed for %s", name_part)
        return "hr_action_marked_signed"

    elif prefix == "ISAF_MONTHLY":
        # Owner confirmed they've signed the monthly i.SAF submission
        log.info("Monthly i.SAF acknowledged: %s", event_key)
        return "isaf_submission_acknowledged"

    elif prefix == "BARISTA_PROMOTE":
        # Owner confirmed promotion: BARISTA_PROMOTE:{barista_id}:{new_level}
        if not FEATURE_HR:
            log.warning("HR feature disabled; BARISTA_PROMOTE dispatch skipped")
            return "hr_feature_disabled"
        parts = event_key.split(":")
        if len(parts) == 3:
            from core.hr_manager import promote_barista
            barista_id = int(parts[1])
            new_level  = parts[2]
            promote_barista(conn, barista_id, new_level)
            log.info("Barista %d promoted to %s", barista_id, new_level)
            return f"barista_promoted_to_{new_level.lower()}"
        return "barista_promote_parse_error"

    elif prefix == "SODRA_RATE":
        # Owner confirmed Sodra rate-change submission
        parts = event_key.split(":")
        if len(parts) >= 2:
            barista_id = int(parts[1])
            conn.execute(
                """UPDATE sodra_rate_drafts SET status='submitted', submitted_at=?
                   WHERE id = (SELECT id FROM sodra_rate_drafts
                               WHERE barista_id=? AND status='draft'
                               ORDER BY created_at DESC LIMIT 1)""",
                (_now_utc(), barista_id),
            )
            conn.commit()
            log.info("Sodra rate draft marked submitted for barista %d", barista_id)
            return "sodra_rate_marked_submitted"
        return "sodra_rate_parse_error"

    elif prefix == "SHIFT_SUGGESTION":
        date_part = event_key[len("SHIFT_SUGGESTION:"):]
        conn.execute(
            "UPDATE shift_suggestions SET status='approved', responded_at=? WHERE suggestion_date=?",
            (_now_utc(), date_part),
        )
        conn.commit()
        log.info("Shift suggestion approved: %s", date_part)
        return "shift_suggestion_approved"

    elif prefix == "ROSTER_DRAFT":
        iso_week = event_key[len("ROSTER_DRAFT:"):]
        from agent.scheduler_bot import publish_approved_roster
        from notify import send_telegram as _send_tg
        publish_approved_roster(conn, iso_week, _send_tg)
        log.info("Roster published: %s", iso_week)
        return "roster_published"

    elif prefix == "REVIEW_ESCALATION":
        if not FEATURE_SENTIMENT:
            log.warning("Sentiment feature disabled; REVIEW_ESCALATION dispatch skipped")
            return "sentiment_feature_disabled"
        review_id = event_key[len("REVIEW_ESCALATION:"):]
        from core.sentiment_loop import post_reply as _post_reply
        row = conn.execute(
            "SELECT reply_text FROM google_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row and row["reply_text"]:
            _post_reply(review_id, row["reply_text"], conn)
            return "review_reply_posted"
        log.error("No draft reply found for review %s", review_id)
        return "review_reply_not_found"

    elif prefix == "WINBACK_POST":
        if not FEATURE_SENTIMENT:
            log.warning("Sentiment feature disabled; WINBACK_POST dispatch skipped")
            return "sentiment_feature_disabled"
        month = event_key[len("WINBACK_POST:"):]
        from core.sentiment_loop import publish_winback_post as _publish
        _publish(month, conn)
        return "winback_post_published"

    return "no_downstream_action"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ping_healthcheck(env_var: str, suffix: str = "") -> None:
    """Ping a Healthchecks.io check (dead man's switch). Best-effort: never raises.

    suffix=""        → success ("up")
    suffix="/start"  → job started (lets Healthchecks time the run)
    suffix="/fail"   → job failed (triggers an alert)

    Set <env_var> to the check's ping URL in settings.env to enable; unset = no-op.
    """
    import urllib.request as _urllib
    base = os.getenv(env_var, "").strip()
    if not base:
        return
    url = base.rstrip("/") + suffix
    try:
        with _urllib.urlopen(url, timeout=10) as resp:
            log.debug("Healthcheck ping %s → %s", url, resp.status)
    except Exception as exc:
        log.warning("Healthcheck ping failed (%s): %s", env_var, exc)


# ── Scheduled job helpers ─────────────────────────────────────────────────────

async def _handle_audit_result(conn, result: dict) -> None:
    if result["status"] == "MISMATCH":
        key = f"AUDIT_MISMATCH:{result['date']}"
        msg = (
            f"⚠️ *Z-report mismatch*\n"
            f"Date: `{result['date']}`\n"
            f"POS:  `{result['pos_total']} EUR`\n"
            f"i.EKA: `{result['ieka_total']} EUR`\n"
            f"Diff: `{result['discrepancy']} EUR`\n\n"
            f"Confirm once resolved: POST /confirm/{key}"
        )
        await _send_telegram("AUDIT_MISMATCH", key, msg, conn, fields={
            "Date":       result["date"],
            "POS":        f"{result['pos_total']} EUR",
            "i.EKA":      f"{result['ieka_total']} EUR",
            "Difference": f"{result['discrepancy']} EUR",
        })

    elif result["status"] == "ERROR":
        key = f"AUDIT_ERROR:{result['date']}"
        msg = (
            f"🔴 *Audit script error*\n"
            f"Date: `{result['date']}`\n"
            f"Error: `{result.get('notes', 'unknown')}`"
        )
        await _send_telegram("AUDIT_ERROR", key, msg, conn)


async def _handle_stock_result(conn, result: dict) -> None:
    for item in result.get("low_stock", []):
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        key  = f"STOCK_LOW:{item['sku']}:{week}"
        msg  = _stock_alert_message(item)
        await _send_telegram("STOCK_LOW", key, msg, conn, fields=_stock_alert_fields(item))


async def _handle_pl_result(conn, pl: dict) -> None:
    key = f"PL_DRAFT:{pl['month']}"
    warnings = "\n".join(pl.get("notes", [])) or "No warnings."
    msg = (
        f"📊 *P&L draft ready — {pl['month']}*\n"
        f"Gross revenue: `{pl['revenue']['gross']} EUR`\n"
        f"Net profit:    `{pl['net_profit']} EUR`\n"
        f"Labor %:       `{pl['labor_pct']}%`\n"
        f"COGS %:        `{pl['cogs_pct']}%`\n\n"
        f"{warnings}\n\n"
        f"Full draft: `data/pl_draft_{pl['month']}.json`\n"
        f"Please review before the 3rd."
    )
    await _send_telegram("PL_DRAFT_READY", key, msg, conn, fields={
        "Month":         pl["month"],
        "Gross revenue": f"{pl['revenue']['gross']} EUR",
        "Net profit":    f"{pl['net_profit']} EUR",
        "Labor %":       f"{pl['labor_pct']}%",
        "COGS %":        f"{pl['cogs_pct']}%",
    })


async def _trigger_isaf_compilation(conn) -> None:
    """Notify owner to trigger the monthly i.SAF compilation."""
    # Scheduled portal job: do not even prompt for i.SAF work if the VMI breaker
    # is open — the compilation drives vmi_imas and would fail again.
    if _is_breaker_open(conn, "vmi_imas"):
        bkey = f"BREAKER_OPEN:vmi_imas:{date.today().isoformat()}"
        await _send_telegram(
            "BREAKER_OPEN", bkey,
            f"⛔ Monthly i.SAF reminder skipped — `vmi_imas` circuit breaker is open "
            f"({CIRCUIT_BREAKER_THRESHOLD} consecutive failures). "
            f"Resolve the VMI portal failures before compiling.",
            conn,
        )
        log.warning("i.SAF reminder skipped: vmi_imas breaker open")
        return
    month = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    key   = f"ISAF_MONTHLY_REMINDER:{month}"
    msg   = (
        f"📋 *Monthly i.SAF due in 3 days*\n"
        f"Period: `{month}`\n"
        f"Deadline: 15th of this month\n\n"
        f"The agent will compile all receipts from {month} and "
        f"draft the i.SAF submission in VMI i.MAS.\n"
        f"Reply GO {key} to trigger now."
    )
    await _send_telegram("ISAF_MONTHLY_REMINDER", key, msg, conn, fields={
        "Period":   month,
        "Deadline": "15th of this month",
    })


def _stock_alert_message(item: dict) -> str:
    key = f"PO_DRAFTED:{item['sku']}:{datetime.now(timezone.utc).strftime('%Y-W%W')}"
    return (
        f"📦 *Low stock alert*\n"
        f"Item:      `{item['name']}`\n"
        f"Current:   `{item['current_kg']} {item['unit']}`\n"
        f"Threshold: `{item['threshold_kg']} {item['unit']}`\n\n"
        f"Purchase order drafted.\n"
        f"Reply *GO {key}* to send to {item.get('supplier_name', 'supplier')}.\n"
        f"Reply *SKIP {key}* to dismiss for this week."
    )


def _stock_alert_fields(item: dict) -> dict:
    """Structured fields for the dashboard low-stock card (Step 2)."""
    unit = item.get("unit", "")
    return {
        "Item":      item.get("name", item.get("sku", "")),
        "Current":   f"{item.get('current_kg', '?')} {unit}".strip(),
        "Threshold": f"{item.get('threshold_kg', '?')} {unit}".strip(),
        "Supplier":  item.get("supplier_name", ""),
    }


# ── Telegram helper ───────────────────────────────────────────────────────────

async def _send_telegram(
    event_type: str, event_key: str, message: str, conn, fields: dict | None = None
) -> bool:
    """Send Telegram message with deduplication after successful send.

    `fields` is optional structured key→value data stored alongside the
    notification for the dashboard cards (falls back to message_preview if omitted).
    """
    import urllib.request as _urllib

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    if dry_run:
        log.info("[DRY RUN] Telegram %s: %s", event_type, message[:80])
        # Do NOT record notification in dry-run — allow full replay in production
        return True

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram not configured — skipping notification: %s", event_key)
        return False

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "Markdown",
    }).encode()
    req = _urllib.Request(url, data=payload,
                          headers={"Content-Type": "application/json"})
    try:
        with _urllib.urlopen(req, timeout=10) as resp:
            log.info("Telegram sent [%s]: %s", resp.status, event_key)
            # Record notification AFTER successful send
            is_new = notify_if_new(conn, event_type, event_key, message, fields=fields)
            if not is_new:
                # Already sent in a concurrent or prior request (expected race)
                log.info("Telegram duplicate recorded (concurrent send): %s", event_key)
            return True
    except Exception as exc:
        log.error("Telegram send failed for %s: %s", event_key, exc)
        # Do NOT record notification on failure — allow retry
        return False


# ── DB query helpers ──────────────────────────────────────────────────────────

def _last_audit(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM audit_log ORDER BY run_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else {"status": "NO_DATA"}


def _unacked_alerts(conn) -> list:
    rows = conn.execute(
        """SELECT event_key, event_type, sent_at, message_preview
           FROM notifications_sent
           WHERE acknowledged_at IS NULL
           ORDER BY sent_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _is_breaker_open(conn, portal: str) -> bool:
    """
    True if this portal's last CIRCUIT_BREAKER_THRESHOLD actions were ALL failures.
    Single source of truth for both the /status display and dispatch enforcement.
    """
    rows = conn.execute(
        """SELECT outcome FROM portal_actions
           WHERE portal = ?
           ORDER BY acted_at DESC LIMIT ?""",
        (portal, CIRCUIT_BREAKER_THRESHOLD),
    ).fetchall()
    return (len(rows) == CIRCUIT_BREAKER_THRESHOLD
            and all(r["outcome"] == "failure" for r in rows))


def _open_circuit_breakers(conn) -> list[str]:
    """Return all portals whose circuit breaker is currently open."""
    portals = [
        r["portal"]
        for r in conn.execute("SELECT DISTINCT portal FROM portal_actions").fetchall()
    ]
    return [p for p in portals if _is_breaker_open(conn, p)]


def _valid_month(month: str) -> bool:
    import re
    return bool(re.match(r"^\d{4}-\d{2}$", month))


# ── Dashboard helpers ─────────────────────────────────────────────────────────

def _esc(value) -> str:
    """HTML-escape any value (incl. quotes) for safe interpolation into markup."""
    return html.escape(str(value), quote=True)


def _js_arg(value) -> str:
    """
    Render a value as a JS string-literal argument that is ALSO safe inside a
    double-quoted HTML on* attribute. json.dumps does the JS-string escaping
    (double quotes, backslashes, control chars, non-ASCII); html.escape then
    neutralizes the characters that matter in attribute context (", ', &, <, >).
    The browser decodes the entities before the JS engine sees the string, so a
    key like  a'b"c\\d  round-trips intact and cannot break out of either layer.
    Emits its own surrounding quotes — do not wrap the call site in quotes.
    """
    return html.escape(json.dumps(str(value)), quote=True)


def _today_roster(conn) -> list[dict]:
    """Return published shifts for today joined with barista names."""
    try:
        today = date.today().isoformat()
        rows = conn.execute(
            """SELECT ps.slot_label, ps.slot_name, b.name
               FROM published_shifts ps
               JOIN baristas b ON b.id = ps.barista_id
               WHERE ps.shift_date = ?
               ORDER BY ps.slot_name, ps.rowid""",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _alert_border(event_type: str) -> str:
    t = event_type.upper()
    if any(k in t for k in ("MISMATCH", "ESCALATION", "FAILED", "CIRCUIT", "BREAKER")):
        return "#993C1D"
    if any(k in t for k in ("LOW", "WARN", "SWAP_COST", "PROMOTE")):
        return "#BA7517"
    return "#1A6FA8"


def _alert_label(event_type: str) -> str:
    labels = {
        "AUDIT_MISMATCH":        "Z-report mismatch — action required",
        "AUDIT_ERROR":           "Audit script error",
        "RECEIPT_DRAFTED":       "i.SAF draft ready — awaiting e-signature",
        "REVIEW_ESCALATION":     "Review needs response",
        "STOCK_LOW":             "Low stock — purchase order drafted",
        "PO_DRAFTED":            "Purchase order ready to send",
        "NEW_HIRE_DRAFTED":      "Sodra 1-SD draft ready",
        "ROSTER_DRAFT":          "Weekly roster draft — awaiting approval",
        "SHIFT_SUGGESTION":      "Shift suggestion for review",
        "WINBACK_POST":          "Monthly win-back post draft",
        "PL_DRAFT_READY":        "P&L draft ready for review",
        "ISAF_MONTHLY_READY":    "Monthly i.SAF draft ready",
        "ISAF_MONTHLY_REMINDER": "Monthly i.SAF due in 3 days",
        "SMARTID_TIMEOUT":       "Smart-ID login timed out",
        "BREAKER_OPEN":          "Portal circuit breaker open — action blocked",
    }
    return labels.get(event_type, event_type.replace("_", " ").title())


def _primary_btn(event_key: str, event_type: str) -> str:
    external = {
        "RECEIPT_DRAFTED":    ("Sign in EDS",   "https://deklaravimas.vmi.lt"),
        "ISAF_MONTHLY_READY": ("Sign in EDS",   "https://deklaravimas.vmi.lt"),
        "NEW_HIRE_DRAFTED":   ("Sign in Sodra", "https://draudejai.sodra.lt"),
    }
    if event_type in external:
        label, url = external[event_type]  # both fixed constants
        return (
            f"<a href='{_esc(url)}' target='_blank' style='display:inline-block;"
            f"background:#0F6E56;color:#fff;text-decoration:none;"
            f"padding:5px 12px;border-radius:5px;font-size:12px'>{_esc(label)} &#8599;</a>"
        )
    action_labels = {
        "REVIEW_ESCALATION": "Post reply",
        "ROSTER_DRAFT":      "Approve roster",
        "SHIFT_SUGGESTION":  "Approve",
        "PO_DRAFTED":        "Send order",
        "STOCK_LOW":         "Send order",
        "WINBACK_POST":      "Publish",
        "PL_DRAFT_READY":    "Mark reviewed",
    }
    label = action_labels.get(event_type, "Confirm")
    # Use JavaScript fetch to send Authorization header (HTML forms cannot).
    # _js_arg emits its own quotes and is safe in both the JS-string and
    # HTML-attribute layers (keys may contain ', ", \, :, spaces, etc.).
    return (
        f"<button onclick=\"_dashboard_confirm({_js_arg(event_key)})\" "
        f"style='background:#0F6E56;color:#fff;border:none;"
        f"padding:5px 12px;border-radius:5px;cursor:pointer;font-size:12px'>"
        f"{_esc(label)}</button>"
    )


# ── Dashboard HTML renderer ───────────────────────────────────────────────────

def _serve_dashboard_html(bearer_token: str) -> str | None:
    """
    Serve the standalone single-page dashboard (api/dashboard.html) if present.
    Returns None when the file is absent, so _render_dashboard falls back to the
    legacy server-rendered HTML — dropping the file in swaps the UI, removing it
    reverts, with zero risk to a running server.

    The file ships in DEMO mode with an empty token (safe to open directly/share).
    We flip it to live and inject the bearer token HERE, server-side, so the token
    only reaches the browser behind the Basic-auth layer — same trust model as the
    legacy page. json.dumps keeps it a safe JS string literal.
    """
    path = Path(__file__).resolve().parent / "api" / "dashboard.html"
    if not path.exists():
        return None
    html = path.read_text(encoding="utf-8")
    html = html.replace('mode: "demo"', 'mode: "live"')
    html = html.replace('token: "",', f'token: {json.dumps(bearer_token)},')
    return html


def _render_dashboard(audit: dict, alerts: list, stock: dict, roster: list | None = None, bearer_token: str = "") -> str:
    _spa = _serve_dashboard_html(bearer_token)
    if _spa is not None:
        return _spa
    # ── Legacy server-rendered dashboard (fallback when api/dashboard.html absent) ──
    audit_status = audit.get("status", "NO_DATA")
    audit_color  = "#0F6E56" if audit_status == "OK" else "#993C1D"
    alert_count  = len(alerts)
    low_count    = len(stock.get("low_stock", []))

    # Alert cards with colored left border
    def _alert_card(a: dict) -> str:
        border = _alert_border(a["event_type"])  # fixed color constant
        preview = (a.get("message_preview") or "")[:240]
        return (
            f"<div style='background:#fff;border-radius:8px;border-left:4px solid {border};"
            f"padding:12px 16px;margin-bottom:10px;display:flex;align-items:center;gap:12px'>"
            f"<div style='flex:1'>"
            f"<div style='font-size:12px;font-weight:700;color:{border}'>{_esc(_alert_label(a['event_type']))}</div>"
            f"<div style='font-size:11px;color:#5F5E5A;margin:2px 0'>{_esc(a['event_key'])}</div>"
            f"<div style='font-size:12px;color:#1A1A1A'>{_esc(preview)}</div>"
            f"</div>"
            f"<div style='display:flex;gap:6px;flex-shrink:0'>"
            + _primary_btn(a['event_key'], a['event_type']) +
            f"<button onclick=\"_dashboard_dismiss({_js_arg(a['event_key'])})\""
            f" style='background:#993C1D;color:#fff;border:none;"
            f"padding:5px 12px;border-radius:5px;cursor:pointer;font-size:12px'>SKIP</button>"
            f"</div></div>"
        )

    alerts_html = (
        "<h3 style='color:#1A2E4A;margin:20px 0 10px;font-size:15px'>Alerts requiring action</h3>"
        + "".join(_alert_card(a) for a in alerts[:10])
        if alerts else
        "<p style='color:#0F6E56;margin-bottom:24px;font-size:13px'>No pending alerts.</p>"
    )

    # Inventory horizontal bars
    def _stock_bar(item: dict) -> str:
        try:
            cur = float(item["current_kg"])
            thr = float(item["threshold_kg"])
            pct = min(100, int(cur / max(thr * 2, 0.001) * 100))
        except (TypeError, ValueError, ZeroDivisionError):
            pct = 0
        bar_color = "#993C1D" if item["is_low"] else "#0F6E56"
        status_label = "LOW" if item["is_low"] else "OK"
        return (
            f"<div style='margin-bottom:12px'>"
            f"<div style='display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px'>"
            f"<span style='font-weight:600'>{_esc(item['name'])}</span>"
            f"<span style='color:{bar_color};font-weight:700'>{status_label} &nbsp;"
            f"{_esc(item['current_kg'])} / {_esc(item['threshold_kg'])} {_esc(item['unit'])}</span>"
            f"</div>"
            f"<div style='background:#eee;border-radius:4px;height:8px'>"
            f"<div style='background:{bar_color};width:{pct}%;height:8px;border-radius:4px'></div>"
            f"</div></div>"
        )

    stock_bars = "".join(_stock_bar(i) for i in stock.get("items", []))
    inventory_html = (
        f"<div style='background:#fff;border-radius:10px;padding:16px;margin-bottom:24px'>"
        f"{stock_bars or '<p style=\"color:#5F5E5A;font-size:13px\">No data</p>'}"
        f"</div>"
    )

    # Today's roster panel
    def _avatar(name: str) -> str:
        parts = name.strip().split()
        initials = (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()
        return (
            f"<div style='width:36px;height:36px;border-radius:50%;background:#1A2E4A;"
            f"color:#fff;display:flex;align-items:center;justify-content:center;"
            f"font-size:13px;font-weight:700;flex-shrink:0'>{_esc(initials)}</div>"
        )

    if roster:
        roster_cards = "".join(
            f"<div style='display:flex;align-items:center;gap:10px;padding:8px 0;"
            f"border-bottom:0.5px solid #eee'>"
            f"{_avatar(r['name'])}"
            f"<div><div style='font-size:13px;font-weight:600'>{_esc(r['name'])}</div>"
            f"<div style='font-size:11px;color:#5F5E5A'>{_esc(r['slot_label'])}</div></div>"
            f"</div>"
            for r in roster
        )
        roster_html = (
            "<h3 style='color:#1A2E4A;margin:20px 0 10px;font-size:15px'>Today's roster</h3>"
            f"<div style='background:#fff;border-radius:10px;padding:12px 16px;"
            f"margin-bottom:24px'>{roster_cards}</div>"
        )
    else:
        roster_html = (
            "<h3 style='color:#1A2E4A;margin:20px 0 10px;font-size:15px'>Today's roster</h3>"
            "<div style='background:#fff;border-radius:10px;padding:12px 16px;"
            "margin-bottom:24px;color:#5F5E5A;font-size:13px'>No shifts published today.</div>"
        )

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8'>
  <meta name='viewport' content='width=device-width,initial-scale=1'>
  <meta http-equiv='refresh' content='120'>
  <title>CoffeeManager-OS</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Arial,sans-serif;background:#F1EFE8;color:#1A1A1A;padding:24px}}
    h1{{color:#1A2E4A;margin-bottom:4px;font-size:22px}}
    .sub{{color:#5F5E5A;font-size:13px;margin-bottom:20px}}
    .nav{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
    .nav a{{color:#0F6E56;text-decoration:none;font-size:13px;font-weight:500}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}}
    .card{{background:#fff;border-radius:10px;padding:16px;border:0.5px solid #ddd}}
    .card h2{{font-size:12px;color:#5F5E5A;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}}
    .card .val{{font-size:28px;font-weight:700;color:#1A2E4A}}
    .card .sub2{{font-size:12px;color:#5F5E5A;margin-top:4px}}
    h3{{color:#1A2E4A;margin:20px 0 10px;font-size:15px}}
  </style>
</head>
<body>
  <h1>CoffeeManager-OS</h1>
  <p class='sub'>Last refreshed: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
  <nav class='nav'>
    <a href='/audit/history'>Audit history</a>
    <a href='/portal/actions'>Portal log</a>
    <a href='/receipts'>Receipts</a>
    <a href='/docs'>API docs</a>
    <a href='/health'>Health</a>
  </nav>

  <div class='grid'>
    <div class='card'>
      <h2>Today's Audit</h2>
      <div class='val' style='color:{audit_color}'>{_esc(audit_status)}</div>
      <div class='sub2'>Discrepancy: {_esc(audit.get('discrepancy', '—'))} EUR</div>
    </div>
    <div class='card'>
      <h2>Pending Alerts</h2>
      <div class='val' style='color:{"#993C1D" if alert_count else "#0F6E56"}'>{alert_count}</div>
      <div class='sub2'>Require your confirmation</div>
    </div>
    <div class='card'>
      <h2>Low Stock Items</h2>
      <div class='val' style='color:{"#BA7517" if low_count else "#0F6E56"}'>{low_count}</div>
      <div class='sub2'>Below reorder threshold</div>
    </div>
    <div class='card'>
      <h2>System</h2>
      <div class='val' style='color:#0F6E56;font-size:18px'>Running</div>
      <div class='sub2'><a href='/health' style='color:#0F6E56'>Health probe</a></div>
    </div>
  </div>

  {alerts_html}

  <h3>Inventory</h3>
  {inventory_html}

  {roster_html}

  <p style='color:#5F5E5A;font-size:12px'>
    CoffeeManager-OS v1.0 &nbsp;·&nbsp; All tax submissions require owner e-signature.
  </p>

  <script>
  const API_TOKEN = {json.dumps(bearer_token)};

  async function _dashboard_confirm(eventKey) {{
    try {{
      const response = await fetch(`/confirm/${{eventKey}}`, {{
        method: 'POST',
        headers: {{
          'Authorization': `Bearer ${{API_TOKEN}}`,
          'Content-Type': 'application/json'
        }}
      }});
      if (response.ok) {{
        const data = await response.json();
        alert(`Confirmed: ${{data.next_action || data.status}}`);
        location.reload();
      }} else {{
        alert(`Error: ${{response.status}} - ${{response.statusText}}`);
      }}
    }} catch(e) {{
      alert(`Request failed: ${{e.message}}`);
    }}
  }}

  async function _dashboard_dismiss(eventKey) {{
    try {{
      const response = await fetch(`/dismiss/${{eventKey}}`, {{
        method: 'POST',
        headers: {{
          'Authorization': `Bearer ${{API_TOKEN}}`,
          'Content-Type': 'application/json'
        }}
      }});
      if (response.ok) {{
        alert('Dismissed');
        location.reload();
      }} else {{
        alert(`Error: ${{response.status}} - ${{response.statusText}}`);
      }}
    }} catch(e) {{
      alert(`Request failed: ${{e.message}}`);
    }}
  }}
  </script>
</body>
</html>"""


# ── App instance (for uvicorn) ────────────────────────────────────────────────
app = create_app()

# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
    uvicorn.run(
        app,
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
