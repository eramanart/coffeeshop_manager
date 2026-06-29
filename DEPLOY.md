# CoffeeManager-OS — Deployment Checklist

Go-live tasks that are **operational, not engineering**. These are owner-driven and
must be completed before the system runs against real VMI/Sodra workflows. They are
deliberately kept out of the numbered engineering passes so they don't get lost.

Owner: **shop owner** (elas.ramanauskas@gmail.com). Claude Code can assist but cannot
complete these (they need real credentials, a browser session, or a policy decision).

---

## Status as of 2026-06-16

- [x] **0. Reproducible env** — manifests exist; `import main` + suites verified.
- [x] **1. Three secrets** — generated and in settings.env (DASHBOARD_USER=owner).
- [~] **2. Telegram webhook** — tunnel tool chosen (cloudflared, via start_tunnel.bat).
      Works, but is per-session: currently DOWN (webhook url empty, 2 pending). Fix =
      run the daily routine (stop_all → start_server → start_tunnel).
- [ ] **3. PINNED_INFO** — card generated (see step 3); still needs posting + pinning.
- [x] **4. Browser smoke test** — owner logged into dashboard; smoke_tunnel 9/9.
- [ ] **5. DRY_RUN → false** — correctly NOT done. Deferred until ready + accountant.

Running state: server up on :8000, scheduler active, DRY_RUN=true. Pipeline runs
against the real DB (real inventory + 1300+ audit rows). **POS feed is `stub`** —
sales/Z-report totals read 0.00 until a real POS provider is configured.

---

## 0. Reproducible environment (precondition for everything below)

"Can a new machine run this?" Until recently the answer was no — the venv was
reproducible only by remembering what to install. Manifests now exist:

- [ ] From a clean checkout, recreate the venv and install:
        python -m venv .venv
        .venv\Scripts\activate
        pip install -r python/requirements.txt
        pip install -r python/requirements-dev.txt   # only if running tests
- [ ] Verify the app imports and the suites pass:
        cd python
        python -c "import main"                       # should print no error
        python -m pytest tests/test_pass1_fixes.py -q
- [ ] If you change runtime deps, re-pin: `pip freeze`, then update
      `python/requirements.txt` (direct deps only, exact pins).

NOTE: `requirements.txt` is hand-curated to DIRECT dependencies, pinned to the
versions in the working venv. `httpx` is intentionally dev-only today and moves to
runtime in Pass 3 (async telegram). Keep this file honest — it is the only thing
standing between a fresh machine and a working install.

## 1. Fill the three required secrets in `config/settings.env`

- [x] `API_BEARER_TOKEN` — generated (token_urlsafe(40)), written to settings.env.
- [x] `TELEGRAM_SECRET_TOKEN` — generated; use this exact value in step 2's setWebhook.
- [x] `DASHBOARD_PASSWORD` — generated; username is `owner`. Rotate any of these with
      `python -c "import secrets; print(secrets.token_urlsafe(40))"` if you prefer your own.

NOTE (fix, 2026-06-15): the API app (`uvicorn api.main:app`) previously did not load
settings.env at all — only the gateway entry point did. main.py now calls
`load_dotenv(config/settings.env)` at import, so the dashboard/API reads these secrets
regardless of how it is launched. Verified from a clean process: GET / returns 401
(creds loaded & enforced), and 200 with the real password.

## 2. Register the Telegram webhook via a local tunnel (ngrok / cloudflared)

Telegram only delivers to a public HTTPS URL and only sends the
`X-Telegram-Bot-Api-Secret-Token` header if `secret_token` was passed to setWebhook.
A tunnel exposes the local app over HTTPS; API_HOST stays 127.0.0.1.

Secrets are NOT written here — the commands read them from config/settings.env.

- [ ] Start the app. The `api` package lives in python/, and uvicorn must be the
      venv's. This one line handles both (works in any terminal, venv activated or not):
        cd C:\Users\eligi\Desktop\coffee_agent\python; ..\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
      Common failures if you deviate:
        - bare `uvicorn api.main:app` from repo root → "No module named 'api'" (wrong dir)
        - `uvicorn ...` in a terminal without the venv → "uvicorn is not recognized" (use
          the ..\.venv\Scripts\python.exe -m uvicorn form above, or activate first with
          ..\.venv\Scripts\Activate.ps1)
      (DB path is anchored, so a wrong-dir launch can't silently use an empty DB.)
- [ ] Start a tunnel to port 8000 and copy the https URL it prints:
        ngrok http 8000            # or: cloudflared tunnel --url http://localhost:8000
- [ ] Register the webhook (run from coffee_agent/python, POSIX shell):
        BOT=$(grep '^TELEGRAM_BOT_TOKEN='   config/settings.env | cut -d= -f2)
        SECRET=$(grep '^TELEGRAM_SECRET_TOKEN=' config/settings.env | cut -d= -f2)
        curl "https://api.telegram.org/bot$BOT/setWebhook" \
             --data-urlencode "url=https://<YOUR-TUNNEL-URL>/webhook/telegram" \
             --data-urlencode "secret_token=$SECRET"
- [ ] Verify registration (should show your url, no last_error):
        curl "https://api.telegram.org/bot$BOT/getWebhookInfo"
- [ ] Run the end-to-end smoke test against the tunnel URL (no state change — safe):
        python tests/smoke_tunnel.py https://<YOUR-TUNNEL-URL>
      Exercises liveness + all three auth layers (dashboard Basic, Telegram secret,
      bearer) over real HTTP. Expect "9 passed, 0 failed". Validated locally already;
      running it through the tunnel additionally proves Telegram can reach the app.
- [ ] Send a real bot command in Telegram (e.g. SKIP <some_key>) and confirm the app
      logs a 200, not a 401. A 401 means a secret_token mismatch — re-check settings.env.

NOTE: the tunnel URL changes each restart on free ngrok; re-run setWebhook after a
restart. A reserved domain (paid) or cloudflared named tunnel avoids this.

## 3. Post and pin the scheduling info card

- [ ] Print the card:
        python -c "from agent.scheduler_bot import PINNED_INFO; print(PINNED_INFO)"
- [ ] Paste into the `#scheduling` channel → pin the message.
- [ ] Repeat for the `#announcements` channel.

## 4. Manual browser smoke test of the dashboard confirm path

Automated tests cover the server side; this confirms the in-browser fetch path the
owner actually uses (the one failure mode that turns "I approved it" into "I thought
I approved it").

- [ ] Load the dashboard, authenticate via Basic auth (DASHBOARD_USER / PASSWORD).
- [ ] Click a "Confirm"/"Approve" button on an alert → expect a "Confirmed: …" alert
      and the page to reload with the alert gone.
- [ ] Force a failure (e.g. temporarily set a wrong API_BEARER_TOKEN) and click again
      → expect an "Error: 401 …" alert, NOT a silent success.

## 5. Production toggle

- [ ] Run the four DRY_RUN tests from CLAUDE.md's Pass 1 checklist (confirm button,
      webhook with secret, double webhook → already_confirmed, wrong secret → 401).
- [ ] Only then set `DRY_RUN=false` in `config/settings.env`.

### The real go-live path (DRY_RUN is plumbing-validation, not compliance-validation)

The five items above are only the ENTRY to go-live. Important nuance: with POS=stub,
POS and i.EKA both read 0.00, so every audit returns OK trivially — you are proving
the plumbing (scheduler fires, audit runs, dedup holds, dashboard renders, breaker
stays closed), NOT the compliance logic (the AUDIT_MISMATCH path only does anything
when POS and i.EKA disagree). Real POS integration is the gate that unlocks compliance
testing. Ordered path:

1. **Now:** stub POS, get the webhook stable (start_tunnel.bat), run in DRY_RUN.
2. Run 1–2 weeks; see what surfaces (plumbing validation).
3. **Integrate the real POS** (paysera or robolabs — whichever the shop uses). The hook
   already exists: `backfill_from_pos` in core/labor_forecast.py + the poll cron. Likely
   ~half a day once the API key is in hand.
4. Run another stub→real transition week; confirm AUDIT_MISMATCH fires on real disagreement.
5. **Named Cloudflare tunnel** (reliability — see below). Do it here, not now.
6. **Accountant conversation** (#11 i.EKA offline policy, #12 Sodra short-notice).
7. **Then** flip `DRY_RUN=false`.

POS choice (paysera vs robolabs) is the owner's, based on the shop's actual POS. If
there's no POS contract yet, that's a separate conversation. Decision recorded 2026-06-16:
**stub is fine for the current dry-run stage; real POS before DRY_RUN=false.**

**Named tunnel (pre-DRY_RUN=false, reliability + simplification).** The free trycloudflare
quick tunnel has slow/flaky DNS propagation to Telegram (1–3+ min); the start_tunnel.py
watchdog makes that *safe* during dry-run (self-heals re-registration), but it doesn't make
it *good*. A named Cloudflare tunnel gives a **stable URL** that is always in DNS and
registers **instantly** — and crucially it **removes code, not just adds reliability**: the
whole "wait for propagation → check if the URL changed → re-register if so" branch
(set_webhook retry loop + webhook_watchdog) goes away, because the URL never changes. So
it's a reliability win AND a simplification win. Requires a free Cloudflare account + a
domain you control. Why gated on DRY_RUN=false: a delayed/dropped webhook costs nothing
while DRY_RUN holds everything; the moment it can cost a missed AUDIT_MISMATCH (€4,300) is
exactly DRY_RUN=false.

---

## 6. Barista Mini App + named tunnel domain (live: coffee.agnestudio.lt)

The named Cloudflare tunnel domain is **`coffee.agnestudio.lt`**. It is load-bearing in
THREE independent places that MUST stay identical — if one drifts, the failure is silent
and annoying to trace:

  1. cloudflared ingress hostname in `C:\Users\eligi\.cloudflared\config.yml`
  2. `MINIAPP_URL` in `config/settings.env`  (= `https://coffee.agnestudio.lt/miniapp`)
  3. the `setWebhook` target              (= `https://coffee.agnestudio.lt/webhook/telegram`)

Go-live wiring (pure config now — no code left):

- [ ] Route DNS to the named tunnel (zone must be active):
        cloudflared tunnel route dns coffeemanager coffee.agnestudio.lt
- [ ] Set `MINIAPP_URL=https://coffee.agnestudio.lt/miniapp` in `config/settings.env`.
      (Also copy `MINIAPP_DEV_USER=` — blank in prod — from settings.env.example.)
- [ ] Give every barista the "My shifts" menu button (run ONCE, after MINIAPP_URL is set):
        cd C:\Users\eligi\Desktop\coffee_agent\python
        ..\.venv\Scripts\python.exe set_menu_button.py
      Refuses to run (no Telegram call) until MINIAPP_URL is a live https URL.
- [ ] Re-point the webhook at the STABLE url (this retires the per-session re-register
      ritual in step 2 — the URL never changes again):
        setWebhook url=https://coffee.agnestudio.lt/webhook/telegram (secret from settings.env)
- [ ] Reboot survival: NSSM uvicorn service (AppDirectory = python\) + `cloudflared
      service install`. Together they retire the manual start_server/start_tunnel dance.

First phone-open test (the real one): a barista taps "My shifts" → Telegram hands the page
their signed initData → `require_barista` validates the HMAC → they see THEIR own shifts.
Reading a failure by symptom:

  - page loads, `/miniapp/schedule` → **401 Invalid or expired init data** = the
    `TELEGRAM_BOT_TOKEN` that set the menu button differs from the one the backend
    validates with. Same token both sides.
  - `/miniapp/schedule` → **503 token not configured** = backend env missing the token.
  - page itself won't load = tunnel/cert, not the backend (no app log line).

Notes that save debugging time:
  - CORS is N/A — the page and its `/miniapp/*` fetches are same-origin (relative paths).
  - Mini App auth is INDEPENDENT of the webhook (it uses the `X-Init-Data` HMAC), so the
    first open can be tested before the webhook re-point.
  - `MINIAPP_DEV_USER` (settings.env) bypasses initData for localhost testing but is INERT
    whenever `DRY_RUN=false`; keep it blank in production.

---

## Blockers that are policy decisions, not tasks (review #11, #12)

These gate real VMI/Sodra operation and are **owner decisions**, not code:

- [ ] **#11 i.EKA offline detection** — decide the monitoring/alerting policy for VMI
      i.EKA sync being offline (>2h risks a EUR 4300 fine).
- [ ] **#12 Sodra short-notice hires** — decide how to handle hires registered with
      less than the required 24h notice before first working day.

---

## Security reminders (from CLAUDE.md, repeated here for go-live)

- `config/settings.env` holds live secrets and is gitignored. Never commit it. If it
  ever lands in a git commit, rotate ALL secrets immediately.
- If `API_HOST=0.0.0.0` (e.g. to reach the dashboard from a phone), the app MUST sit
  behind an HTTPS reverse proxy — Basic auth and the bearer token are plaintext.
