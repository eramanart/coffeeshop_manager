"""
core/labor_forecast.py — Predictive labor scheduling forecasting engine

Model: weighted day-of-week (DOW) regression with weather adjustment.
No external ML library required — runs on Python stdlib statistics only.
Fits from as little as 4 weeks of history; confidence improves up to ~16 weeks.

Two data sources:
  1. POS hourly sales pulled from existing inventory.py POS integration
     and stored in hourly_sales table (added by migration v5).
  2. Open-Meteo API — free, no key required, covers Vilnius precisely.
     https://open-meteo.com/en/docs

Output: a dict of {hour_str: forecast_eur} for a future date, plus
a confidence score (0–100) and the weather multiplier applied.

CLI:
    python core/labor_forecast.py --date 2026-05-16
    python core/labor_forecast.py --backfill       # pull last 30 days from POS
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

log = logging.getLogger("core.labor_forecast")

# ── Constants ─────────────────────────────────────────────────────────────────
SHOP_OPEN_HOUR  = int(os.getenv("SHOP_OPEN_HOUR",  "8"))
SHOP_CLOSE_HOUR = int(os.getenv("SHOP_CLOSE_HOUR", "21"))
SHOP_LAT        = float(os.getenv("SHOP_LAT", "54.6872"))   # Vilnius default
SHOP_LON        = float(os.getenv("SHOP_LON", "25.2797"))

# Weather code → revenue multiplier (WMO weather codes from Open-Meteo)
# Clear = 1.0, light cloud = 0.95, overcast = 0.88,
# drizzle = 0.80, rain = 0.72, heavy rain = 0.62
WEATHER_MULTIPLIERS: dict[int, float] = {
    0:  1.00,   # Clear sky
    1:  0.97,   # Mainly clear
    2:  0.93,   # Partly cloudy
    3:  0.88,   # Overcast
    45: 0.85,   # Fog
    48: 0.85,
    51: 0.80,   # Drizzle light
    53: 0.76,
    55: 0.72,   # Drizzle dense
    61: 0.75,   # Rain slight
    63: 0.68,
    65: 0.62,   # Rain heavy
    71: 0.82,   # Snow (quieter but not as bad as rain in Lithuania)
    73: 0.78,
    75: 0.72,
    80: 0.70,   # Showers slight
    81: 0.65,
    82: 0.60,
    95: 0.58,   # Thunderstorm
}
DEFAULT_WEATHER_MULT = 0.85  # used when API is unavailable


# ═════════════════════════════════════════════════════════════════════════════
# MAIN FORECAST FUNCTION
# ═════════════════════════════════════════════════════════════════════════════

def forecast_day(
    conn,
    target_date: date,
    min_weeks: int = 2,
) -> dict:
    """
    Forecast hourly revenue for target_date.

    Args:
        conn:        SQLite connection from migrate.get_connection().
        target_date: The future date to forecast.
        min_weeks:   Minimum weeks of history before generating a forecast.
                     Returns a low-confidence stub below this threshold.

    Returns:
        {
          "date":           "YYYY-MM-DD",
          "dow":            int (0=Mon, 6=Sun),
          "dow_name":       str,
          "hours":          {"08": 82.50, "09": 95.00, ...},  # EUR per hour
          "total_forecast": "850.00",   # Decimal string
          "weather_code":   int,
          "weather_mult":   float,
          "weather_desc":   str,
          "confidence":     int,        # 0–100
          "weeks_of_data":  int,
          "model":          "dow_weighted_regression",
        }
    """
    dow      = target_date.weekday()
    dow_name = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][dow]
    log.info("Forecasting %s (%s)", target_date, dow_name)

    # ── 1. Pull historical hourly data for the same DOW ───────────────────────
    history = _fetch_dow_history(conn, dow, weeks_back=16)
    weeks_available = len(history)
    log.info("Historical DOW data: %d weeks available", weeks_available)

    if weeks_available < min_weeks:
        log.warning("Insufficient history (%d weeks) — returning stub forecast", weeks_available)
        return _stub_forecast(target_date, dow, dow_name, weeks_available)

    # ── 2. Compute base hourly means from history ─────────────────────────────
    base_hours = _compute_hourly_means(history)

    # ── 3. Get weather forecast for target_date ───────────────────────────────
    weather_code, weather_mult, weather_desc = _get_weather(target_date)

    # ── 4. Apply weather adjustment ───────────────────────────────────────────
    adjusted = {
        h: round(v * weather_mult, 2)
        for h, v in base_hours.items()
    }
    total = sum(adjusted.values())

    confidence = _compute_confidence(weeks_available)
    log.info(
        "Forecast complete: total=%.2f EUR, weather=%s (x%.2f), confidence=%d%%",
        total, weather_desc, weather_mult, confidence
    )

    # ── 5. Persist forecast ───────────────────────────────────────────────────
    _save_forecast(conn, target_date, dow, adjusted, weather_code, weather_mult, confidence)

    return {
        "date":           target_date.isoformat(),
        "dow":            dow,
        "dow_name":       dow_name,
        "hours":          {h: str(Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP))
                           for h, v in adjusted.items()},
        "total_forecast": str(Decimal(str(total)).quantize(Decimal("0.01"), ROUND_HALF_UP)),
        "weather_code":   weather_code,
        "weather_mult":   weather_mult,
        "weather_desc":   weather_desc,
        "confidence":     confidence,
        "weeks_of_data":  weeks_available,
        "model":          "dow_weighted_regression",
    }


# ═════════════════════════════════════════════════════════════════════════════
# HISTORY COLLECTION
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_dow_history(conn, dow: int, weeks_back: int) -> list[dict]:
    """
    Pull hourly sales records for a specific day-of-week from SQLite.
    Returns list of {date, hours: {hour_str: revenue_float}} dicts.
    """
    rows = conn.execute(
        """SELECT sale_date, hour, revenue_eur
           FROM hourly_sales
           WHERE day_of_week = ?
             AND sale_date >= date('now', ?)
           ORDER BY sale_date DESC, hour ASC""",
        (dow, f"-{weeks_back * 7} days")
    ).fetchall()

    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        d = row["sale_date"]
        h = f"{row['hour']:02d}"
        by_date.setdefault(d, {})[h] = float(row["revenue_eur"])

    return [{"date": d, "hours": hrs} for d, hrs in by_date.items()]


def _compute_hourly_means(history: list[dict]) -> dict[str, float]:
    """
    Compute weighted mean revenue per hour across all history weeks.
    More recent weeks get higher weight (recency bias 1.5x for last 4 weeks).
    """
    n = len(history)
    hourly_values: dict[str, list[tuple[float, float]]] = {}

    for i, week in enumerate(history):
        # Weight: most recent week = n, oldest = 1 → normalise
        recency_weight = (n - i) / n
        # Extra 1.5x boost for the most recent 4 weeks
        weight = recency_weight * (1.5 if i < 4 else 1.0)

        for h, rev in week["hours"].items():
            hourly_values.setdefault(h, []).append((rev, weight))

    result: dict[str, float] = {}
    for hour in [f"{h:02d}" for h in range(SHOP_OPEN_HOUR, SHOP_CLOSE_HOUR)]:
        if hour in hourly_values:
            vals, weights = zip(*hourly_values[hour])
            weighted_mean = sum(v * w for v, w in zip(vals, weights)) / sum(weights)
            result[hour] = round(weighted_mean, 2)
        else:
            # Fallback: interpolate from neighbors or use 0
            result[hour] = 0.0

    return result


# ═════════════════════════════════════════════════════════════════════════════
# WEATHER
# ═════════════════════════════════════════════════════════════════════════════

def _get_weather(target_date: date) -> tuple[int, float, str]:
    """
    Fetch the dominant weather code for target_date from Open-Meteo API.
    Free, no key required, covers Vilnius precisely.
    Returns (weather_code, multiplier, description).
    Falls back gracefully if the API is unreachable.
    """
    # Check cache first
    conn_cache = None
    try:
        from migrate import get_connection, DEFAULT_DB
        conn_cache = get_connection(DEFAULT_DB)
        cached = conn_cache.execute(
            "SELECT weather_code, weather_mult, weather_desc FROM weather_cache WHERE forecast_date = ?",
            (target_date.isoformat(),)
        ).fetchone()
        if cached:
            log.debug("Weather cache hit for %s", target_date)
            return cached["weather_code"], cached["weather_mult"], cached["weather_desc"]
    except Exception as exc:
        log.debug("Weather cache unavailable: %s", exc)

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={SHOP_LAT}&longitude={SHOP_LON}"
            f"&daily=weathercode"
            f"&timezone=Europe/Vilnius"
            f"&start_date={target_date}&end_date={target_date}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "CoffeeManager-OS/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        code = int(data["daily"]["weathercode"][0])
        mult = WEATHER_MULTIPLIERS.get(code, DEFAULT_WEATHER_MULT)
        desc = _weather_description(code)
        log.info("Weather for %s: code=%d (%s) mult=%.2f", target_date, code, desc, mult)

        if conn_cache:
            conn_cache.execute(
                """INSERT OR REPLACE INTO weather_cache
                   (forecast_date, weather_code, weather_mult, weather_desc, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (target_date.isoformat(), code, mult, desc,
                 datetime.now(timezone.utc).isoformat())
            )
            conn_cache.commit()

        return code, mult, desc

    except Exception as exc:
        log.warning("Weather API unavailable (%s) — using default multiplier", exc)
        return -1, DEFAULT_WEATHER_MULT, "forecast unavailable"
    finally:
        if conn_cache:
            conn_cache.close()


def _weather_description(code: int) -> str:
    descriptions = {
        0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "icy fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        80: "light showers", 81: "showers", 82: "heavy showers",
        95: "thunderstorm",
    }
    return descriptions.get(code, f"code {code}")


# ═════════════════════════════════════════════════════════════════════════════
# CONFIDENCE AND PERSISTENCE
# ═════════════════════════════════════════════════════════════════════════════

def _compute_confidence(weeks: int) -> int:
    """
    Model confidence grows with weeks of history.
    Below 4 weeks: too noisy to trust. Above 16: diminishing returns.
    """
    if weeks < 2:  return 40
    if weeks < 4:  return 55
    if weeks < 6:  return 68
    if weeks < 8:  return 75
    if weeks < 12: return 82
    if weeks < 16: return 88
    return 93


def _save_forecast(conn, target_date, dow, hours, weather_code, weather_mult, confidence) -> None:
    total = sum(hours.values())
    conn.execute(
        """INSERT OR REPLACE INTO shift_suggestions
           (suggestion_date, dow, forecast_json, total_forecast_eur,
            weather_code, weather_mult, confidence, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            target_date.isoformat(), dow, json.dumps(hours),
            round(total, 2), weather_code, weather_mult, confidence,
            datetime.now(timezone.utc).isoformat(),
        )
    )
    conn.commit()


def _stub_forecast(target_date, dow, dow_name, weeks) -> dict:
    """Return a low-confidence stub when history is insufficient."""
    base = [32, 55, 72, 78, 82, 84, 80, 75, 68, 58, 50, 44, 38]
    hours = {
        f"{SHOP_OPEN_HOUR + i:02d}": round(v * 0.9, 2)
        for i, v in enumerate(base[:SHOP_CLOSE_HOUR - SHOP_OPEN_HOUR])
    }
    return {
        "date": target_date.isoformat(), "dow": dow, "dow_name": dow_name,
        "hours": hours, "total_forecast": str(round(sum(hours.values()), 2)),
        "weather_code": -1, "weather_mult": 1.0, "weather_desc": "not applied",
        "confidence": 40, "weeks_of_data": weeks, "model": "stub_insufficient_history",
    }


# ═════════════════════════════════════════════════════════════════════════════
# POS BACKFILL — populate hourly_sales from existing POS history
# ═════════════════════════════════════════════════════════════════════════════

def backfill_from_pos(conn, days_back: int = 30) -> int:
    """
    Pull historical hourly sales from POS API and write to hourly_sales.
    Call once after deploying the module; thereafter the cron keeps it current.
    Returns number of rows written.
    """
    from core.inventory import _fetch_paysera, _fetch_robolabs
    provider = os.getenv("POS_PROVIDER", "stub").lower()
    rows_written = 0

    for i in range(days_back, 0, -1):
        target = date.today() - timedelta(days=i)
        date_str = target.isoformat()

        try:
            if provider == "stub":
                hourly = _stub_hourly(target)
            elif provider == "paysera":
                hourly = _fetch_paysera_hourly(date_str)
            elif provider == "robolabs":
                hourly = _fetch_robolabs_hourly(date_str)
            else:
                continue

            for hour, revenue in hourly.items():
                conn.execute(
                    """INSERT OR IGNORE INTO hourly_sales
                       (sale_date, hour, day_of_week, revenue_eur, recorded_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (date_str, int(hour), target.weekday(),
                     round(revenue, 2), datetime.now(timezone.utc).isoformat())
                )
                rows_written += 1
            conn.commit()

        except Exception as exc:
            log.warning("Backfill failed for %s: %s", date_str, exc)

    log.info("Backfill complete: %d rows written", rows_written)
    return rows_written


def _stub_hourly(d: date) -> dict[str, float]:
    """Generate realistic stub hourly revenue for testing."""
    import random
    random.seed(d.toordinal())
    dow_mult = [0.72, 0.74, 0.78, 0.82, 0.95, 1.28, 1.18][d.weekday()]
    base = [28, 52, 68, 74, 78, 82, 85, 88, 84, 76, 62, 54, 48]
    return {
        f"{SHOP_OPEN_HOUR + i:02d}": round(v * dow_mult * (0.9 + random.random() * 0.2), 2)
        for i, v in enumerate(base)
    }


def _fetch_paysera_hourly(date_str: str) -> dict[str, float]:
    """Fetch hourly breakdown from Paysera. Implement when POS is live."""
    raise NotImplementedError("Paysera hourly endpoint — implement per their API docs")


def _fetch_robolabs_hourly(date_str: str) -> dict[str, float]:
    """Fetch hourly breakdown from RoboLabs. Implement when POS is live."""
    raise NotImplementedError("RoboLabs hourly endpoint — implement per their API docs")


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse, sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from migrate import apply_migrations, get_connection, DEFAULT_DB

    parser = argparse.ArgumentParser()
    parser.add_argument("--date",     default=str(date.today() + timedelta(days=1)))
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--days",     type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    apply_migrations(DEFAULT_DB)
    conn = get_connection(DEFAULT_DB)

    if args.backfill:
        n = backfill_from_pos(conn, args.days)
        print(f"Backfilled {n} rows.")
    else:
        result = forecast_day(conn, date.fromisoformat(args.date))
        print(f"\nForecast for {result['date']} ({result['dow_name']})")
        print(f"Weather: {result['weather_desc']} (x{result['weather_mult']})")
        print(f"Confidence: {result['confidence']}%  ({result['weeks_of_data']} weeks data)")
        print(f"\nHourly forecast:")
        for h, v in result["hours"].items():
            bar = "█" * int(float(v) / 10)
            print(f"  {h}:00  €{v:>7}  {bar}")
        print(f"\nTotal: €{result['total_forecast']}")

    conn.close()
