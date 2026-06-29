# Known Issues

A candid catalogue of bugs, design risks, and tech debt identified **before** freezing this project. They are documented intentionally rather than hidden: knowing the failure modes of your own system is part of the work, and a reviewer should be able to see that the gaps were understood, not missed.

Severity reflects impact *if the system were run in live production*. Since the project is frozen pre-live (POS stubbed, no real data), none of these is currently causing harm — but several would, the moment real transactions flowed through.

| # | Issue | Severity | Class |
|---|-------|----------|-------|
| 1 | Stock checks pollute the audit log and can hide a real mismatch | **High** | Compliance / data integrity |
| 2 | New-hire confirmation keyed on a non-unique employee name | **High** | Compliance / data integrity |
| 3 | Personal national ID was in plaintext config — removed at freeze | Resolved | Security / GDPR |
| 4 | Tests and smoke scripts mutate the live compliance database | **Medium** | Data integrity / tooling |
| 5 | Deduplication keys use non-ISO week numbers | **Low** | Correctness (edge case) |
| 6 | Mini-App dev bypass silently impersonates a fixed user ID | **Low** | Security (dev-only, inert in prod) |
| 7 | Dead `DB_PATH` environment variable | **Low** | Tech debt |
| 8 | Deprecated FastAPI startup/shutdown event handlers | **Low** | Tech debt |

---

## 1. Stock checks pollute the audit log and can hide a real mismatch — **High**

**What:** `check_stock_levels()` writes `LOW_STOCK` rows into the `audit_log` table with `status=OK`. The dashboard's "Today's Audit" card and the `/status` endpoint both read the *latest* `audit_log` row (`_last_audit()` → `SELECT * FROM audit_log ORDER BY run_at DESC LIMIT 1`).

**Why it matters:** This violates the project's own hard rule — "`NEVER write non-audit rows to audit_log`." It is harmless today because the POS is stubbed and every row is `0.00 / OK`. But once a real POS is wired, the inventory check runs every 15 minutes, so a stock-check `OK` row written seconds after a genuine `AUDIT_MISMATCH` will become the latest row and flip the audit card green — **silently hiding the mismatch the system exists to catch.** Given the i.EKA fine exposure (~€4,300), this is a compliance-visible data-integrity bug, not a cosmetic one.

**Fix (not taken):** Stock checks must not write to `audit_log` at all — they already have `notifications_sent` for the alert and `inventory` for state. `audit_log` should contain only genuine Z-report reconciliations. Side benefit: log growth drops from ~96 rows/day to ~1/day.

**Where:** `core/inventory.py` (`check_stock_levels`); read path in `api/main.py` (`_last_audit`).

---

## 2. New-hire confirmation keyed on a non-unique employee name — **High**

**What:** When the owner confirms a signed Sodra 1-SD, the dispatcher updates the HR record by **name**:

```python
# api/main.py, _dispatch_confirmed_action(), NEW_HIRE_DRAFTED branch
UPDATE hr_actions SET sodra_status = 'signed', signed_at = ?
WHERE employee_name = ?
```

**Why it matters:** `employee_name` is not unique. A rehire (the same person onboarded twice) or two employees who share a name will both be flipped to `signed` by a single confirmation. In a legal-compliance table tracking statutory new-hire registration, marking the wrong record as filed is a correctness failure that could mask a genuinely unregistered employee — exactly the 24-hour-deadline obligation Sodra enforces.

**Fix (not taken):** Key the update on a unique identifier — the `hr_actions` row id carried in the event key — rather than the display name.

**Where:** `api/main.py`, `_dispatch_confirmed_action()`, `NEW_HIRE_DRAFTED` branch (~line 850).

---

## 3. Personal national ID in plaintext config — **Resolved (was Medium)**

**What:** `SMART_ID_PERSONAL_CODE` previously sat in `settings.env` holding a real person's Lithuanian national identification code in plaintext, read by **no code path** — orphaned configuration.

**Why it mattered:** A national ID tied to a real individual is personal data under GDPR. Storing it in plaintext config — especially config that risks being committed to version control — was a privacy exposure with no offsetting benefit, since nothing used it.

**Resolution (done):** Removed from `settings.env`; verified absent at freeze (2026-06-29). If Smart-ID auth ever needs it programmatically, source it from an OS keyring or an interactive prompt — never plaintext. (Note: the unrelated `personal_code` field elsewhere is new-hire data from the barista intake used for Sodra drafts, not the owner's Smart-ID code.)

**Where:** `config/settings.env` (no longer present).

---

## 4. Tests and smoke scripts mutate the live compliance database — **Medium**

**What:** `DEFAULT_DB` is hard-anchored to `python/data/memory.sqlite`, and the `DB_PATH` environment variable that looks like it should override it is dead (see issue #7). As a result, test and smoke scripts run against the **live** compliance database. A Mini-App smoke test on 2026-06-25 auto-created a `Dev Tester` barista plus an availability row directly in `memory.sqlite`.

**Why it matters:** The system of record for legally-relevant compliance and HR data should never be mutated by a test run. Test artifacts polluting production data undermines the integrity of every downstream read.

**Fix (not taken):** Make `DEFAULT_DB` honour `DB_PATH` (or a dedicated `CM_DB_OVERRIDE`) so tests point at a throwaway copy. A `reset_barista_data.py` script exists as manual cleanup until then.

**Where:** `migrate.py` (`DEFAULT_DB` resolution).

---

## 5. Deduplication keys use non-ISO week numbers — **Low**

**What:** Several event keys derive a week stamp via `datetime.strftime("%Y-W%W")`. `%W` is not the ISO-8601 week number (`%V` with `%G` as the year); the two calendars disagree around year boundaries.

**Why it matters:** Notification dedup relies on these keys being stable and consistent. Near a year boundary, `%W` weeks can drift from any ISO-week logic used elsewhere, producing either a missed dedup (duplicate alert) or an unintended collision. It is an edge case, but a silent one.

**Fix (not taken):** Standardise on ISO week keys (`%G-W%V`) everywhere week-based keys are generated, and audit existing keys for the boundary case.

**Where:** `api/main.py` (stock alert / POS poll key construction) and any other week-keyed events.

---

## 6. Mini-App dev bypass silently impersonates a fixed user ID — **Low**

**What:** The Mini-App developer auth bypass (`_dev_bypass_barista`) accepts `MINIAPP_DEV_USER`. If the value is a non-numeric name it does not recognise, it falls back to a fixed ID (`-9999`) rather than rejecting — silently impersonating whoever holds that ID instead of the name typed. (Caught 2026-06-25: `MINIAPP_DEV_USER=TestBarista` resolved to `Dev Tester`, not `TestBarista`.)

**Why it matters:** Acting as the wrong identity is a security smell. It is low-stakes here because the bypass is **dev-only and inert in production** — it is ignored whenever `DRY_RUN=false` — but the fail-open behaviour is the wrong default.

**Fix (not taken):** Reject an unrecognised name (raise / return `401`) instead of falling back to a placeholder identity. Numeric IDs that map to an existing barista already resolve exactly and are fine.

**Where:** `api/miniapp.py` (`_dev_bypass_barista`).

---

## 7. Dead `DB_PATH` environment variable — **Low**

**What:** `DB_PATH` is documented and present in `settings.env` but read by nothing; `DEFAULT_DB` is the real path.

**Why it matters:** Dead config is misleading — it implies an override that does not exist, and is the direct cause of issue #4 (tests hitting the live DB).

**Fix (not taken):** Either wire it up (which also resolves #4) or remove it from the config and docs.

**Where:** `config/settings.env`, `migrate.py`.

---

## 8. Deprecated FastAPI startup/shutdown event handlers — **Low**

**What:** The app uses `@app.on_event("startup")` and `@app.on_event("shutdown")` to start and stop the scheduler. These decorators are deprecated in current FastAPI in favour of a `lifespan` context manager.

**Why it matters:** Not breaking today, but emits deprecation warnings and will eventually stop working on a future FastAPI major version. Pure tech debt.

**Fix (not taken):** Migrate to a `lifespan` handler. (This was on the project's own "Pass 3" backlog.)

**Where:** `api/main.py` (`create_app()`).

---

*These issues were known at the time the project was frozen. They are recorded here rather than silently carried, so the state of the system is fully and honestly represented.*
