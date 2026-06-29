# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: Add migration v5 to migrate.py
# ═══════════════════════════════════════════════════════════════════════════
#
# In migrate.py, append this tuple to the MIGRATIONS list:
#
#   (
#       5,
#       "Predictive labor scheduling: hourly_sales, weather_cache, shift_suggestions",
#       [
#           """
#           CREATE TABLE IF NOT EXISTS hourly_sales (
#               id            INTEGER PRIMARY KEY AUTOINCREMENT,
#               sale_date     TEXT    NOT NULL,
#               hour          INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
#               day_of_week   INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
#               revenue_eur   REAL    NOT NULL,
#               recorded_at   TEXT    NOT NULL
#           )
#           """,
#           "CREATE UNIQUE INDEX IF NOT EXISTS idx_hs_date_hour ON hourly_sales (sale_date, hour)",
#           "CREATE INDEX IF NOT EXISTS idx_hs_dow ON hourly_sales (day_of_week)",
#
#           """
#           CREATE TABLE IF NOT EXISTS weather_cache (
#               forecast_date TEXT    PRIMARY KEY,
#               weather_code  INTEGER NOT NULL,
#               weather_mult  REAL    NOT NULL,
#               weather_desc  TEXT    NOT NULL,
#               fetched_at    TEXT    NOT NULL
#           )
#           """,
#
#           """
#           CREATE TABLE IF NOT EXISTS shift_suggestions (
#               id                  INTEGER PRIMARY KEY AUTOINCREMENT,
#               suggestion_date     TEXT    NOT NULL UNIQUE,
#               dow                 INTEGER NOT NULL,
#               forecast_json       TEXT    NOT NULL,
#               total_forecast_eur  REAL    NOT NULL,
#               weather_code        INTEGER,
#               weather_mult        REAL,
#               confidence          INTEGER NOT NULL,
#               status              TEXT    NOT NULL DEFAULT 'pending'
#                   CHECK (status IN ('pending','approved','edited','skipped')),
#               owner_response      TEXT,
#               created_at          TEXT    NOT NULL,
#               responded_at        TEXT
#           )
#           """,
#           "CREATE INDEX IF NOT EXISTS idx_ss_date ON shift_suggestions (suggestion_date)",
#           "CREATE INDEX IF NOT EXISTS idx_ss_status ON shift_suggestions (status)",
#       ],
#   ),
#
# Then run: python migrate.py
# The migration is idempotent — safe to run multiple times.


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2: Add to config/settings.env
# ═══════════════════════════════════════════════════════════════════════════
#
# Shop location (Open-Meteo uses these for the weather forecast)
# SHOP_LAT=54.6872
# SHOP_LON=25.2797
#
# Trading hours
# SHOP_OPEN_HOUR=8
# SHOP_CLOSE_HOUR=21
#
# Labor scheduling thresholds
# BARISTA_HOURLY_RATE=9.50
# LABOR_TARGET_PCT=0.32
# SCHEDULE_PEAK_THRESH=0.80
# SCHEDULE_NORMAL_THRESH=0.55
# SCHEDULE_PEAK_COUNT=3
# SCHEDULE_NORMAL_COUNT=2
# SCHEDULE_QUIET_COUNT=1


# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: Add scheduling cron jobs to api/main.py
# ═══════════════════════════════════════════════════════════════════════════
#
# Inside _register_scheduled_jobs(), add:
#
#   # Hourly POS data collection for scheduling model
#   @scheduler.scheduled_job(CronTrigger(minute=55))   # top of every hour
#   async def collect_hourly_sales():
#       from core.labor_forecast import backfill_from_pos
#       conn = get_connection(DEFAULT_DB)
#       try:
#           # Write just the last 2 hours (current + previous)
#           backfill_from_pos(conn, days_back=1)
#       except Exception as exc:
#           log.error("Hourly sales collection failed: %s", exc)
#       finally:
#           conn.close()
#
#   # Weekly shift suggestion — Sunday 18:00 UTC, suggests next week
#   @scheduler.scheduled_job(CronTrigger(day_of_week='sun', hour=18, minute=0))
#   async def weekly_shift_suggestion():
#       from core.labor_forecast import forecast_day
#       from core.shift_suggester import suggest_shifts
#       conn = get_connection(DEFAULT_DB)
#       try:
#           next_week_dates = [
#               date.today() + timedelta(days=i)
#               for i in range(1, 8)
#           ]
#           for target in next_week_dates:
#               forecast   = forecast_day(conn, target)
#               suggestion = suggest_shifts(forecast)
#               await _send_telegram(
#                   "SHIFT_SUGGESTION",
#                   suggestion["event_key"],
#                   suggestion["telegram_msg"],
#                   conn,
#               )
#       except Exception as exc:
#           log.error("Weekly shift suggestion failed: %s", exc)
#       finally:
#           conn.close()
#
#   # HITL: add APPROVE/EDIT/SKIP routing to _dispatch_confirmed_action()
#   # In the function, add:
#   elif prefix == "SHIFT_SUGGESTION":
#       date_part = event_key.replace("SHIFT_SUGGESTION:", "")
#       conn.execute(
#           "UPDATE shift_suggestions SET status='approved', responded_at=? WHERE suggestion_date=?",
#           (datetime.now(timezone.utc).isoformat(), date_part)
#       )
#       conn.commit()
#       return "shift_suggestion_approved"


# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: First-run backfill (run once after deploying)
# ═══════════════════════════════════════════════════════════════════════════
#
# cd coffee_agent\python
# python core/labor_forecast.py --backfill --days 60
#
# This populates hourly_sales with 60 days of stub (or real POS) data.
# With 60 days you get 8-9 data points per day-of-week → confidence ~82%.
# With real POS data this will be accurate immediately.


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5: Test the full pipeline
# ═══════════════════════════════════════════════════════════════════════════
#
# python core/labor_forecast.py --backfill --days 60
# python core/labor_forecast.py --date 2026-05-23
# python core/shift_suggester.py --date 2026-05-23
#
# Expected output:
#   Forecast for 2026-05-23 (Saturday)
#   Weather: clear sky (x1.00)
#   Confidence: 82%  (8 weeks data)
#   Total: €XXX.XX
#
#   Suggested roster — Saturday 2026-05-23
#   08:00–11:00  █  1 barista  €28.50  [quiet]
#   11:00–14:00  ███  3 baristas  €85.50  [peak]
#   14:00–17:00  ██  2 baristas  €57.00  [normal]
#   17:00–21:00  ██  2 baristas  €76.00  [normal]
#   Total: €247.00  (within budget)
print("Integration guide loaded — follow steps 1-5 above.")
