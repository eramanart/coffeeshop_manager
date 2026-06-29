# CoffeeManager-OS — Claude Code context

> **STATUS: FROZEN — pre-live portfolio piece (frozen 2026-06-29).** This project was
> deliberately stopped and archived. It was **never run in live production**: the POS is
> stubbed (zero sales), so the compliance core has never been validated against a real
> transaction. **Do not resume the build** — do not start "Pass 3", the VAT/i.SAF
> workflow, or wire a real POS; that reopens the exact build loop the freeze closed.
> The human-facing overview, honest limitations, and the rationale for freezing are in
> `README.md`; known bugs (including one compliance-visible data-integrity issue) are in
> `KNOWN_ISSUES.md`. Treat every "completed / closed" claim below as a historical
> snapshot of the build, **not** a statement that the system is production-ready.

## Working root
All commands run from:
    cd C:\Users\eligi\Desktop\coffee_agent\python

Activate venv first:
    cd C:\Users\eligi\Desktop\coffee_agent
    .venv\Scripts\activate
    cd python

## Project structure (coffee_agent\python\ is the root)
```
python\
├── main_gateway_loop_.py          # entry point
├── migrate.py                     # SQLite schema — v6 migrations, helper functions
├── make_test_receipt.py           # generates synthetic invoice images
├── notify.py                      # Telegram notify helper
├── runner.py                      # top-level runner shim
├── agent\
│   ├── __init__.py
│   ├── runner.py                  # clawdbot task dispatcher — production
│   ├── soul.md                    # clawdbot system prompt (v1.1)
│   ├── scheduler_bot.py           # barista Telegram scheduling bot (v1)
│   └── skills\
│       ├── __init__.py
│       └── scan_receipt.py        # docTR/EasyOCR OCR pipeline (v2)
├── api\
│   ├── __init__.py
│   └── main.py                    # FastAPI dashboard + APScheduler + Telegram webhooks
├── core\
│   ├── __init__.py
│   ├── accounting.py              # Decimal PVM calc, Z-report audit, P&L
│   ├── inventory.py               # stock levels, POS pull model
│   ├── isaf_generator.py          # VMI i.SAF XML builder + XSD validation
│   ├── labor_forecast.py          # weighted DOW + weather forecasting engine
│   ├── shift_suggester.py         # converts forecast to roster suggestion
│   └── scheduling_integration_guide.py  # wiring instructions (read-only reference)
├── config\
│   ├── settings.env               # credentials (never commit)
│   └── schemas\
│       └── isaf_v1.2.xsd          # live XSD, validation passes
└── data\
    ├── memory.sqlite              # all persistent state
    └── workspace\
        └── manual_review\         # OCR Tier 3 files land here
```

## Clawdbot agent workspace
    C:\Users\eligi\.clawdbot\agents\main\

    workspace\COFFEESHOP_PORTALS.md  — hard rules read by agent every session
    skills\vmi_submit.md             — VMI i.MAS portal navigation skill
    skills\sodra_draft.md            — Sodra draudejai portal skill

---

## Build status — snapshot at freeze (NOT production-validated — see README.md)

### Core pipeline
- DB migrations v1–v6 applied, memory.sqlite live
- Z-report audit, inventory check, purchase order drafting all working
- config\schemas\isaf_v1.2.xsd present, XSD validation passes
- OCR: docTR + EasyOCR installed, all 3 confidence tiers verified
- clawdbot stubs replaced — _vmi_draft_isaf and _sodra_draft_1sd live
- FastAPI dashboard running, HITL /confirm and /dismiss endpoints active
- Dashboard 500 fixed (2026-06-09): migrate.py get_connection() now uses check_same_thread=False — SQLite connection was created in FastAPI's thread pool but used in the async event loop thread

### Barista scheduling (Phase 5 enhancement — live)
- agent\scheduler_bot.py wired to api\main.py webhook and Sunday 18:xx cron
- Sunday sequence confirmed working:
    18:00  stock check
    18:05  7 per-day shift suggestions → owner private chat
    18:10  full weekly roster (forecast + fairness + availability) → owner APPROVE/SKIP
- migrate.py v6: baristas, barista_availability, published_shifts, swap_requests
- Commands live: AVAIL, OFF, MY SHIFTS, ROSTER, SWAP, SWAP CONFIRM, SWAP DECLINE, HELP
- Rolling 5-day lock enforced: slots within 5 days are read-only
- Fairness algorithm: fewer monthly hours = higher priority for peak slots
- Auto-registration on first barista message confirmed working
- PINNED_INFO card: manual step remaining — post PINNED_INFO from
  scheduler_bot.PINNED_INFO to #scheduling and #announcements and pin it

### Remaining manual step
Post and pin the info card in both Telegram channels:
    python -c "from agent.scheduler_bot import PINNED_INFO; print(PINNED_INFO)"
    Copy output → paste into #scheduling channel → pin the message
    Repeat for #announcements channel

---

## Telegram channel structure
Group: [AGENT_NAME] — Coffee Team

  #announcements   read-only, bot posts only
                   weekly roster, weather alerts, shop news
  #scheduling      interactive, baristas submit availability
                   AVAIL / OFF / MY SHIFTS / ROSTER / SWAP commands
  #team-chat       open group chat, bot monitors for SWAP commands

Owner receives roster draft in private chat every Sunday 18:10 UTC.
Reply APPROVE ROSTER_DRAFT:{date} to publish to #announcements.

---

## Run commands (from coffee_agent\python\)

    python main_gateway_loop_.py --dry-run
    python main_gateway_loop_.py --mode audit
    python main_gateway_loop_.py --mode inventory
    python main_gateway_loop_.py --mode agent
    python main_gateway_loop_.py --mode watch

    python core\accounting.py --test               # 15 tests
    python core\isaf_generator.py --test           # 14 tests
    python core\labor_forecast.py --backfill --days 60
    python core\labor_forecast.py --date 2026-05-23
    python core\shift_suggester.py --date 2026-05-23

    python agent\skills\scan_receipt.py data\workspace\test_invoice_clean.png

    uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

---

## Critical rules — enforce on every edit, no exceptions

- NEVER use float for money. Always Python Decimal with ROUND_HALF_UP.
- NEVER submit a VMI or Sodra form. Draft + notify_owner only.
- NEVER calculate tax in agent\runner.py or soul.md. Use core\ only.
- NEVER store credentials in source. Read from config\settings.env.
- ALWAYS log every portal interaction via migrate.log_portal_action().
- ALWAYS call notify_if_new() before any Telegram message to owner.
- NEVER retry Smart-ID autonomously. Stop and notify owner.
- NEVER generate i.SAF XML from OCR confidence below 70%.
- NEVER post to #announcements without owner APPROVE confirmation.
- NEVER write non-audit rows to audit_log. Use pos_events for webhook data.
- NEVER expose the dashboard beyond localhost over plain HTTP. The dashboard page
  embeds API_BEARER_TOKEN and uses HTTP Basic auth (DASHBOARD_USER/PASSWORD); both
  the token and Basic credentials are plaintext on the wire. If API_HOST=0.0.0.0
  (e.g. to reach it from your phone), it MUST sit behind an HTTPS reverse proxy.

---

## Pass 1 Deployment Checklist (Required Before Go-Live)

### Authentication & Telegram Setup
- [ ] Re-register Telegram webhook with secret_token parameter (setWebhook API call)
  - Telegram must send X-Telegram-Bot-Api-Secret-Token header, or webhook auth will fail
- [ ] Populate config/settings.env:
  - TELEGRAM_SECRET_TOKEN (matches secret passed to Telegram setWebhook)
  - API_BEARER_TOKEN (40+ random chars for /confirm and /dismiss)
  - WEBHOOK_POS_SECRET (optional, leave empty if POS is internal)
  - API_HOST (127.0.0.1 for localhost, 0.0.0.0 for docker)
  - CORS_ALLOW_ORIGINS (127.0.0.1 by default)

### Test with DRY_RUN=true (Before Production)
```bash
# Load dashboard, click confirm/skip buttons → should work
# Send Telegram webhook with secret header → should work
# Send same webhook twice → should return already_confirmed
# Send webhook with wrong secret → should return 401
```

Once all four tests pass, proceed to production with DRY_RUN=false.

---

## Key design decisions

Entry point: main_gateway_loop_.py
  Bootstrap: config load → DB migration → Phase 1 Python core → Phase 2 agent dispatch.

Clawdbot integration (agent\runner.py):
  _run_clawdbot_agent() runs: clawdbot agent --agent main --message ... --json --timeout 300
  Async subprocess avoids session lock contention.
  Smart-ID pause handled inside agent skill files, not in Python.
  Circuit breaker: 3 failures per portal → CircuitBreakerOpen, paused until reset.

Barista scheduling algorithm (agent\scheduler_bot.py):
  1. forecast_day() per day from labor_forecast.py (DOW + weather)
  2. suggest_shifts() per day from shift_suggester.py (slot intensity → barista count)
  3. Fetch all barista_availability for the week
  4. Fairness score: fewer monthly hours = higher priority for peak slots
  5. Assign, flag uncovered slots as warnings
  6. Owner APPROVE required before posting to #announcements

Rolling 5-day lock (SCHEDULE_LOCK_DAYS=5):
  Baristas cannot change availability within 5 days of a shift.
  Owner can unlock manually. Enforced in set_availability() and remove_availability().

Three-tier OCR confidence gate (agent\skills\scan_receipt.py v3):
  Tier is set by confidence BUT capped at 2 unless amounts are validated AND all
  required fields present — confidence_tier(fields) is the single source, used by
  both the CLI and runner.py. Required: supplier_vat, doc_date, net_amount,
  pvm_amount, pvm_code.
  >= 90%  Tier 1  automatic XML → VMI draft   (only if validated + complete)
  70-89%  Tier 2  owner confirms fields before XML generation
  < 70%   Tier 3  file moved to manual_review\, never generates XML
  _extract_amounts() (v3, fixed 2026-06-17): finds the net+VAT=gross triple that
  actually satisfies net+VAT==gross AND VAT==round(net×rate) for rate in {21,9,5}%,
  drawing candidates from all printed amounts (excluding dates/rates/ids). Layout-
  independent, self-validating. Returns None (validated=False) when no valid triple
  exists — it does NOT fabricate. Replaced the v2 position-parser whose "Pass 3 gross
  fallback" derived amounts from the LAST number on the receipt, which was the DATE
  (e.g. gross "2026.05" → net 1674.42) — silently producing wrong tax numbers at
  Tier-1 confidence. Found during dry-run OCR testing on real Lidl/IKI receipts.

Deduplication (migrate.py → notify_if_new()):
  UNIQUE on notifications_sent.event_key. Keys reset by date or ISO week.

HITL confirmation (api\main.py):
  POST /confirm/{event_key} → _dispatch_confirmed_action() routes to next step.
  Telegram replies "GO {key}" / "APPROVE {key}" / "SKIP {key}" hit /webhook/telegram.
  APPROVE is a literal alias for GO in the webhook verb parser.

Circuit-breaker enforcement at dispatch (api\main.py):
  _is_breaker_open(conn, portal): portal's last CIRCUIT_BREAKER_THRESHOLD (3)
  portal_actions rows all 'failure'. Read from the PERSISTED log, not the runner's
  in-memory counter. _PORTAL_FOR_PREFIX maps event prefixes → portal (vmi_imas,
  sodra, google). _dispatch_confirmed_action() refuses any portal action whose
  breaker is open, re-opens the acknowledgement (acknowledged_at=NULL) so the owner
  can retry after the portal recovers, and alerts BREAKER_OPEN. The scheduled i.SAF
  job is gated the same way. Non-portal actions (rosters, shifts, HR) are never gated.

---

## SQLite tables (data\memory.sqlite)

  audit_log              daily Z-report comparison results
  portal_actions         every clawdbot portal step, timestamped
  notifications_sent     Telegram messages with dedup guard
  receipt_processing     OCR → XML → VMI draft state per file
  hr_actions             Sodra 1-SD workflow state
  schema_migrations      migration version tracking
  hourly_sales           POS hourly revenue for scheduling model
  weather_cache          Open-Meteo forecast cache
  shift_suggestions      weekly roster drafts and approval status
  baristas               registered barista profiles
  barista_availability   submitted availability (rolling, 5-day lock)
  published_shifts       approved confirmed shifts per barista per day
  swap_requests          shift swap requests between baristas

## Migration notes (migrate.py)

  v1–v6  core pipeline + barista scheduling (see structure above)
  v7     Customer sentiment loop — google_reviews, winback_posts tables.
         SQL lives in core\sentiment_loop.py (MIGRATION_V7_SQL). Tables are created
         unconditionally, but all v7 code paths are gated behind FEATURE_SENTIMENT_LOOP
         (default false). Was previously undocumented.
  v8     Barista HR — level/rate columns on baristas, barista_level_history,
         sodra_rate_drafts. SQL in core\hr_manager.py (MIGRATION_V8_SQL). Gated behind
         FEATURE_HR_MANAGER (default false). Was previously undocumented.
  v9     pos_events table for webhook-originated POS events (isolated from audit_log).

---

## Environment variables (config\settings.env)

  TELEGRAM_BOT_TOKEN      from @BotFather
  TELEGRAM_CHAT_ID        owner private chat ID
  TELEGRAM_GROUP_ID       barista group chat ID (negative number)
  TELEGRAM_ANNOUNCE_THREAD  thread ID for #announcements
  TELEGRAM_SCHEDULE_THREAD  thread ID for #scheduling
  TELEGRAM_TEAMCHAT_THREAD  thread ID for #team-chat
  AGENT_NAME              bot display name (placeholder until decided)
  OPENCLAW_API_KEY        from clawdbot dashboard
  POS_PROVIDER            stub | paysera | robolabs
  POS_POLL_MINUTES        15
  VMI_VAT_CODE            LT + 9 digits
  VMI_COMPANY_CODE        9 digits
  DRY_RUN                 true | false
  CLAWDBOT_AGENT_NAME     main
  CLAWDBOT_TASK_TIMEOUT   300
  TELEGRAM_SECRET_TOKEN   webhook secret (validate in /webhook/telegram)
  API_BEARER_TOKEN        strong token for /confirm and /dismiss endpoints
  WEBHOOK_POS_SECRET      optional secret for /webhook/pos (can be empty)
  API_HOST                127.0.0.1 (localhost only, 0.0.0.0 for all interfaces)
  CORS_ALLOW_ORIGINS      127.0.0.1 (comma-separated list of allowed origins). Does
                          NOT apply to the Mini App: /miniapp serves the page and it
                          fetches relative /miniapp/* (same-origin), so no preflight.
  MINIAPP_URL             Public HTTPS URL of the /miniapp page
                          (https://coffee.agnestudio.lt/miniapp). Load-bearing in THREE
                          places — keep them identical: this var, the cloudflared
                          config.yml ingress hostname, and the setWebhook target.
                          Blank = miniapp_button() yields no button and
                          set_menu_button.py refuses to run (nothing breaks).
  MINIAPP_DEV_USER        DEV-ONLY auth bypass for testing the live /miniapp fetch
                          path on localhost without Telegram. Barista name (throwaway)
                          or existing numeric telegram_id. INERT in production:
                          ignored whenever DRY_RUN=false. Leave blank in prod.
  DASHBOARD_USER          HTTP Basic auth username for the dashboard (REQUIRED)
  DASHBOARD_PASSWORD      HTTP Basic auth password — guards GET / and all business
                          data GETs. GET / embeds API_BEARER_TOKEN, so the app fails
                          CLOSED (503) if these are unset. /health stays public.
  FEATURE_SENTIMENT_LOOP  true | false (customer sentiment module, false by default)
  FEATURE_HR_MANAGER      true | false (barista HR module, false by default)
  LOGFIRE_TOKEN           Pydantic Logfire token; empty = tracing is a silent no-op
  LOGFIRE_ENVIRONMENT     production (Logfire environment tag)
  HEALTHCHECK_AUDIT_URL   Healthchecks.io ping URL; 07:00 daily audit pings it
                          (/start, success, /fail). Empty = no-op. Dead-man's switch.
  HEALTHCHECK_ISAF_URL    Healthchecks ping URL; 12th-09:00 i.SAF reminder pings it.
                          Configure check in CRON mode (0 9 12 * * UTC, grace 1 day),
                          NOT period mode. Empty = no-op.
  SHOP_LAT                54.6872 (Vilnius default)
  SHOP_LON                25.2797
  SHOP_OPEN_HOUR          8
  SHOP_CLOSE_HOUR         21
  BARISTA_HOURLY_RATE     9.50
  LABOR_TARGET_PCT        0.32
  SCHEDULE_LOCK_DAYS      5
  SCHEDULE_PEAK_THRESH    0.80
  SCHEDULE_NORMAL_THRESH  0.55
  SCHEDULE_PEAK_COUNT     3
  SCHEDULE_NORMAL_COUNT   2
  SCHEDULE_QUIET_COUNT    1

---

## Lithuanian compliance

VMI i.EKA  daily sync. Offline >2h risks EUR 4300 fine. Audit at 07:00 UTC.
VMI i.SAF  monthly. Due by 15th. XSD validated before upload.
Sodra 1-SD new employee. Min 24h before first day. Auto-enforced.
All forms  draft only. Owner e-signature in EDS always required.

---

## Enhancement modules built (Phase 5)

  Predictive labor scheduling   core\labor_forecast.py + core\shift_suggester.py
  Barista Telegram scheduler    agent\scheduler_bot.py
  Barista Mini App (calendar)   api\miniapp.py + api\barista_calendar.html
                                Telegram Mini App: own initData (X-Init-Data) auth via
                                require_barista, outside the dashboard Basic-auth prefixes.
                                /miniapp serves the page; /miniapp/schedule returns the
                                Mini-App contract (shifts[]/me/swaps_*) — api\miniapp.py is
                                the single storage->display adapter (days map + joined
                                slot_label -> shifts[] with start/end). Opened via the
                                "My shifts" menu button (set_menu_button.py, run once
                                after MINIAPP_URL is live).

  Not yet built (designed, ready to implement):
    Waste analytics per barista
    Customer sentiment loop (Google Reviews)
    Machine self-diagnosis
    Inline DM "Open my calendar" web_app button (miniapp_button()): a pushed
      affordance vs. the menu button's standing one. No private-chat send site exists
      in scheduler_bot today (its only sendMessage is the group post, and web_app
      buttons are rejected in groups). Attach it the day per-barista DM reminders land
      (e.g. "you open tomorrow") — that is the natural send site. Not before.

---

## Spec drift / cleanup backlog — ROADMAP NOT TAKEN (project frozen)

These are not an active TODO list. The project is frozen (see the banner at the top),
so nothing here will be actioned. They are recorded as the known state at freeze. The
human-readable, severity-ranked subset of the genuinely important ones (audit_log
purity, the name-keyed new-hire UPDATE, tests hitting the live DB, the dead DB_PATH,
the Mini-App dev bypass) lives in `KNOWN_ISSUES.md`.

- **[HIGH — compliance-visible, was fix-BEFORE-real-POS] audit_log purity.**
  check_stock_levels writes LOW_STOCK rows into audit_log with status=OK — violating
  the "NEVER write non-audit rows to audit_log" critical rule (same class as Pass-1
  Critical #4). The dashboard "Today's Audit" card and /status read the LATEST
  audit_log row. Harmless now (POS=stub, all 0.00/OK), but once real POS is wired, a
  stock-check OK row written seconds after a real AUDIT_MISMATCH would flip the audit
  card green and HIDE the mismatch — a €4,300 failure mode. Fix: stock checks must not
  write to audit_log (they have notifications_sent for the alert + inventory for
  state); audit_log holds only genuine Z-report reconciliations. Side benefit: drops
  audit_log growth from ~96 rows/day to ~1/day (the 15-min rows are stock artifacts).
  Found during dry-run on 2026-06-19. On the Pass 3 list.
- SMART_ID_PERSONAL_CODE (settings.env): GDPR PII (personal code tied to a real
  person) stored in plaintext config, and read by NO code path — a tree-wide search
  finds the var only in settings.env. It is orphaned config. Action: remove it from
  settings.env. If Smart-ID auth ever needs it programmatically, source it from an OS
  keyring or an interactive prompt, never plaintext. (Note: the `personal_code` field
  in runner.py / main_gateway_loop_.py is unrelated — that is new-hire data from the
  barista CSV used for Sodra 1-SD drafts, not the owner's Smart-ID code.)
- Shadow files at python\ root that duplicated the core\ / agent\ source-of-truth
  modules have been REMOVED (2026-06-15): accounting.py, inventory.py (byte-identical),
  isaf_generator.py, scan_receipt.py, scheduler_bot.py (divergent older drafts — pre-v2
  terminology, mojibake encoding), and two unimportable scratch exports. All were proven
  dead by grep before deletion. Canonical versions remain in core\ and agent\. The last
  stray, gap2_stubs_replacement.py, was also grep-proven dead and deleted. Lesson: keep
  one copy; do not let top-level forks shadow core\.
- Working directory: the app MUST run from python\ (the `api` package lives there;
  `uvicorn api.main:app` from the repo root fails with No module named 'api'). DEFAULT_DB
  is now anchored to python\data\ (was cwd-relative — a wrong-dir launch silently created
  an empty DB). Other relative paths still assume cwd=python\ but fail loudly (XSD at
  config\schemas\, data\pl_draft_*.json) — lower risk. Note: the DB_PATH env var in
  settings.env is DEAD — nothing reads it; DEFAULT_DB is the real path. Remove or wire up.
  Wiring it up has a concrete payoff: tests/smoke scripts currently run against the LIVE
  compliance DB because DEFAULT_DB is hard-anchored (a 2026-06-25 miniapp smoke test
  auto-created a `Dev Tester` barista + an availability row in memory.sqlite this way).
  Make DEFAULT_DB honour DB_PATH (or a CM_DB_OVERRIDE) so tests point at a throwaway copy
  — the compliance DB should never be mutated by a smoke test. reset_barista_data.py is
  the manual cleanup until then.
- Mini App dev bypass (api\miniapp.py _dev_bypass_barista) footgun: a non-numeric
  MINIAPP_DEV_USER falls back to a fixed id (-9999), silently impersonating whoever holds
  that id rather than the name you typed (caught 2026-06-25: MINIAPP_DEV_USER=TestBarista
  maps to Dev Tester, not TestBarista — use the numeric telegram_id to impersonate). Dev-
  only, low stakes, but it should REJECT an unrecognised name (raise/401) instead of
  falling back to a wrong identity. Numeric ids that map to an existing barista are exact.
- Dependency manifests: RESOLVED (2026-06-15). python\requirements.txt (direct runtime
  deps, exact pins, verified against import sites) and python\requirements-dev.txt
  (pytest, httpx for TestClient) now exist. Reproducing the venv is documented in
  DEPLOY.md step 0. httpx is dev-only until Pass 3 moves telegram to async.
- Secrets hygiene: the project is NOT yet a git repo, so settings.env has never been
  committed (no history exists to expose it). Before the first `git init`/commit:
  root .gitignore now excludes config/settings.env; config/settings.env.example holds
  the version-controlled schema. If the repo is ever initialized and the live secrets
  land in a commit, rotate ALL of them (Telegram token, OpenClaw/Anthropic key, Google
  client secret + refresh token) and remove the Smart-ID code.

---

## Architecture history

Full design, all files, roadmap, budget, RACI in Claude.ai conversation.
For compliance rules, clawdbot integration rationale, or algorithm design
decisions, ask there rather than inferring from code alone.
