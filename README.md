# CoffeeManager-OS

A compliance-and-operations automation system for a single independent coffee shop in Vilnius, Lithuania. It reconciles daily fiscal records against the Lithuanian tax authority's register, drafts monthly VAT (i.SAF) and new-hire (Sodra) filings for owner e-signature, processes supplier invoices via OCR, and handles barista scheduling — coordinated through a FastAPI dashboard and a Telegram bot, with an LLM agent confined to navigating government web portals.

> **Status: Frozen (pre-live).** This project is archived as a portfolio piece at a deliberate stopping point. It was never put into live production. The compliance logic is implemented but **has never been validated against real transaction data** — see [Status & Limitations](#status--limitations) below. This document is written to be accurate rather than flattering; the limitations are part of what the project demonstrates.

---

## Why this exists (and why it stopped)

A small business in Lithuania faces real, deadline-driven compliance obligations with real financial penalties: the i.EKA fiscal register must stay in daily sync (an offline gap beyond ~2 hours risks a fine on the order of €4,300), i.SAF VAT reports are due monthly by the 15th, and Sodra requires a new employee to be registered at least 24 hours before their first shift. The original goal was to automate that burden for one owner-operated café.

Over time the project accreted well beyond that core — into inventory, predictive labor scheduling, P&L drafting, a Telegram Mini-App calendar, and (feature-flagged off) a customer-review sentiment loop and barista HR module. After a self-audit, the project was **consciously frozen** rather than pushed to production, for reasons documented honestly in [What I'd Do Differently](#what-id-do-differently). The decision to stop — built it, audited it, chose not to ship it, and recorded why — is itself the most useful artifact here.

---

## Status & Limitations

Read this before evaluating anything else in the repo.

- **The core compliance loop has never processed a real transaction.** The POS feed is stubbed (`POS_PROVIDER=stub`) and returns zero sales. Every accumulated daily-audit row compares €0.00 against €0.00 and records `OK`. The reconciliation, i.SAF generation, and Sodra drafting code paths are implemented and unit-tested in isolation, but the end-to-end compliance value proposition was **never proven against live data**. No filing produced by this system has been accepted by VMI or Sodra.
- **Single-tenant only.** Everything is wired for one specific shop. There is no multi-tenancy, no onboarding, no per-customer configuration.
- **Home-hardware deployment.** It was designed to run on a desktop PC behind a Cloudflare tunnel, kept alive by Windows services (NSSM). That is adequate for one café and inappropriate as a reliability story for a compliance product.
- **The LLM agent depends on government portal UIs that change without notice.** The `clawdbot` agent drives VMI i.MAS and Sodra by browser automation. Any portal redesign or form change breaks the relevant skill until it is manually repaired.
- **Known correctness issues remain open.** They are catalogued candidly in [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md), including one compliance-visible data-integrity bug. They were identified before freezing and intentionally left documented rather than hidden.

If you are reviewing this as a work sample: the value is in the architecture, the failure-mode handling, and the self-assessment — not in a claim that it runs a real business today.

---

## Architecture

The system is deliberately **not** "an LLM doing the work." It is a deterministic Python application that delegates exactly one fuzzy task — navigating government web portals — to an LLM agent. Everything that touches money or tax math is plain, testable Python.

```
                       ┌─────────────────────────────────────────────┐
                       │  FastAPI (api/main.py)                       │
   Telegram  ────────► │  • Owner dashboard (HTML)                    │
   (owner + baristas)  │  • HITL /confirm /dismiss endpoints          │
                       │  • Webhooks: Telegram, POS                   │
                       │  • APScheduler cron jobs (audit, i.SAF, …)   │
                       └───────────────┬─────────────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                               ▼
  core/  (deterministic)        agent/  (LLM-driven)            data/memory.sqlite
  • accounting.py  Decimal      • runner.py    clawdbot          single source of
    PVM math, Z-report audit      subprocess dispatcher          persistent state
  • isaf_generator.py  XML      • soul.md      system prompt
    + XSD validation            • skills/      VMI & Sodra
  • inventory, forecasting        portal-navigation skills
  • shift_suggester             • scan_receipt.py  OCR pipeline
```

### Design decisions worth noting

- **The LLM is quarantined to the edge.** It never calculates tax. A hard rule (`NEVER calculate tax in agent/runner.py`) keeps all monetary logic in `core/`, where it is deterministic and unit-tested. Money is always `Decimal` with `ROUND_HALF_UP`, never `float`.
- **Human-in-the-loop, draft-only.** The system never submits a tax or HR form autonomously. It prepares a draft and notifies the owner, who applies their e-signature in the official portal. Confirmation flows back through `/confirm` or a Telegram `GO`/`APPROVE` reply.
- **Fail-closed authentication.** The dashboard embeds an API bearer token in the page so its `fetch()` calls can authenticate; that makes page-level auth mandatory, so the app returns `503` rather than serving the page if Basic-auth credentials are unset. The token is only ever handed out behind that Basic-auth layer.
- **Circuit breaker reads persisted state, not memory.** A portal that has failed its last N actions (default 3) is blocked at dispatch. Crucially, the breaker is evaluated against the persisted `portal_actions` log — not an in-memory counter — so it survives restarts and applies across human confirmations.
- **OCR confidence tiering, self-validating.** Invoice OCR is gated into three tiers; amounts are only accepted when a `net + VAT == gross` triple actually validates against a known VAT rate, so the extractor returns nothing rather than fabricating a plausible-but-wrong number. (This replaced an earlier parser that could misread a printed date as the gross amount.)
- **Deduplicated, idempotent notifications.** Owner alerts are keyed (`UNIQUE` on an event key, reset by date or ISO week) so the same alert is not sent twice, and confirmations use an atomic acknowledge to prevent double-dispatch.

---

## Tech stack

- **Backend:** Python, FastAPI, APScheduler (cron scheduling), SQLite (system of record)
- **Agent:** `clawdbot` LLM agent invoked as a JSON subprocess; portal navigation skills as Markdown
- **OCR:** docTR + EasyOCR
- **Interfaces:** Telegram bot + Telegram Mini-App (barista calendar); server-rendered HTML owner dashboard
- **Compliance integrations (LT):** VMI i.EKA, VMI i.SAF (XML + XSD validation), Sodra 1-SD; supplier portals (Lidl / IKI / Maxima)
- **Infra (as designed):** Cloudflare tunnel, NSSM Windows services, Healthchecks.io dead-man's switches, Pydantic Logfire observability

---

## Repository layout

```
python/                          # application root — must run from here
├── main_gateway_loop_.py        # CLI entry point / bootstrap
├── migrate.py                   # SQLite schema (v1–v9) + helpers
├── notify.py                    # Telegram notify helper
├── agent/
│   ├── runner.py                # clawdbot task dispatcher
│   ├── soul.md                  # agent system prompt
│   ├── scheduler_bot.py         # barista scheduling bot
│   └── skills/scan_receipt.py   # OCR pipeline
├── api/
│   ├── main.py                  # FastAPI dashboard + scheduler + webhooks
│   ├── miniapp.py               # Telegram Mini-App routes
│   └── helpers.py               # auth validators, atomic acknowledge
├── core/
│   ├── accounting.py            # Decimal PVM math, Z-report audit, P&L
│   ├── inventory.py             # stock levels, POS pull model
│   ├── isaf_generator.py        # i.SAF XML builder + XSD validation
│   ├── labor_forecast.py        # demand forecasting
│   ├── shift_suggester.py       # roster suggestion
│   ├── sentiment_loop.py        # customer reviews (feature-flagged off)
│   └── hr_manager.py            # barista HR (feature-flagged off)
├── config/
│   ├── settings.env.example     # configuration schema (no secrets)
│   └── schemas/isaf_v1.2.xsd    # VMI XSD for validation
└── data/
    └── memory.sqlite            # NOT committed — see note below
```

---

## Running it (DRY_RUN)

The system is intended to be explored in `DRY_RUN=true` mode, where Telegram sends are logged rather than dispatched and no portal action is taken.

```bash
# from the python/ directory
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

cp config/settings.env.example config/settings.env   # then fill in values
# set DRY_RUN=true

uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

The dashboard is at `http://127.0.0.1:8000/` (behind HTTP Basic auth — set `DASHBOARD_USER` / `DASHBOARD_PASSWORD`). Core modules ship with self-tests, e.g. `python core/accounting.py --test`.

**Two things to know before cloning:**

- **No secrets are committed.** `config/settings.env` is git-ignored; only `settings.env.example` (the schema) is in version control. If you fork this, supply your own credentials.
- **The live database is not committed.** `data/memory.sqlite` contains operational data including employee personal data used for Sodra drafts, so it is excluded for GDPR reasons. The repo ships schema-only; run the migrations to create a fresh, empty DB.

---

## What I'd do differently

This section is the point of freezing it honestly. The engineering craft in the repo is, I think, sound. The product thinking behind it was not, and naming that is more valuable than hiding it.

1. **Pick one job.** The system spans roughly ten distinct workflow domains under a single "OS" banner. There is a coherent, valuable spine inside it — Lithuanian tax/HR compliance (daily reconciliation + i.SAF + Sodra) — but it is buried under inventory, scheduling, P&L, sentiment, and HR features that each compete with dedicated tools and none of which carry a penalty if they fail. I would have built only the compliance spine, well, and stopped.

2. **Aim the strong part at the right market.** The defensible, valuable core is *not coffee-specific at all* — it is generic Lithuanian small-business VMI/Sodra automation. The coffee-specific parts (bean inventory, barista scheduling) were simultaneously the weakest and the least defensible. The "coffee shop" framing limited the one genuinely good idea rather than enabling it.

3. **Validate before building outward.** Every compliance feature was built before the system processed a single real transaction, because the POS was stubbed. The correct order was: integrate one real POS, validate the core reconciliation loop against real money, *then* decide whether to expand. Building unvalidated tax automation manufactures false confidence — worse than no automation.

4. **Right-size the operations.** A home desktop, a tunnel, ~12 cron jobs, five auth schemes, and ~47 environment variables is disproportionate machinery for one café — the cure became heavier than the disease. A genuinely small tool, or a managed deployment, would have been more reliable than the bespoke infrastructure.

5. **Notice accretion early.** The project grew by "wouldn't it be useful if…" rather than "what is the one job." The clearest symptom was triumphant documentation ("ALL GAPS CLOSED") sitting on top of a core that had never run with real input. Catching that pattern sooner would have saved most of the scope.

What I would keep: the LLM-at-the-edge boundary, the Decimal-money discipline, the draft-only human-in-the-loop model, the fail-closed auth, and the persisted-state circuit breaker. Those decisions hold up.

---

## Known issues

A candid catalogue of bugs and risks identified before freezing — including one compliance-visible data-integrity issue — is maintained in [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md).

---

*Archived as a portfolio piece. Not maintained. Not in production.*
