"""
core/sentiment_loop.py — Google Business Profile review monitor & sentiment engine

Polls GBP for new reviews, escalates low-star reviews to the owner,
builds weekly digests, and drafts monthly winback posts.

Usage:
    python core/sentiment_loop.py --poll
    python core/sentiment_loop.py --digest
    python core/sentiment_loop.py --winback
    python core/sentiment_loop.py --discover   # print GBP account/location IDs

GBP_STUB_MODE=true (default) returns synthetic reviews without hitting the API.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "config" / "settings.env")

# migrate imports are deferred to avoid circular import:
# migrate.py __import__s this module while building MIGRATIONS, so
# top-level 'from migrate import ...' would see a partially-initialized module.

log = logging.getLogger("sentiment_loop")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Config ─────────────────────────────────────────────────────────────────────

STUB_MODE   = os.getenv("GBP_STUB_MODE", "true").lower() == "true"
ALERT_STARS = int(os.getenv("GBP_ALERT_STARS", "2"))
TREND_DROP  = float(os.getenv("GBP_TREND_DROP", "0.3"))
ACCOUNT_ID  = os.getenv("GBP_ACCOUNT_ID", "")
LOCATION_ID = os.getenv("GBP_LOCATION_ID", "")
SHOP_NAME   = os.getenv("SHOP_NAME", "the shop")
SHOP_URL    = os.getenv("SHOP_WEBSITE_URL", "")
DB_PATH     = Path(os.getenv("DB_PATH", "data/memory.sqlite"))

# ── DB migration SQL (imported by migrate.py as v7) ────────────────────────────

MIGRATION_V7_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS google_reviews (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id    TEXT    NOT NULL UNIQUE,
        reviewer     TEXT,
        stars        INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
        comment      TEXT,
        review_time  TEXT    NOT NULL,
        reply_text   TEXT    DEFAULT NULL,
        replied_at   TEXT    DEFAULT NULL,
        fetched_at   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gr_stars ON google_reviews (stars)",
    "CREATE INDEX IF NOT EXISTS idx_gr_time  ON google_reviews (review_time)",
    """
    CREATE TABLE IF NOT EXISTS winback_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        month        TEXT    NOT NULL UNIQUE,
        draft_text   TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'approved', 'published', 'skipped')),
        published_at TEXT    DEFAULT NULL,
        created_at   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wp_month  ON winback_posts (month)",
    "CREATE INDEX IF NOT EXISTS idx_wp_status ON winback_posts (status)",
]


# ── Lazy migrate helpers (avoid circular import at module load) ────────────────

def _notify_if_new(conn, event_type: str, event_key: str, msg: str) -> bool:
    from migrate import notify_if_new as _nif
    return _nif(conn, event_type, event_key, msg)


# ── GBP OAuth2 / API helpers ───────────────────────────────────────────────────

def _get_access_token() -> str:
    client_id     = os.getenv("GBP_CLIENT_ID", "")
    client_secret = os.getenv("GBP_CLIENT_SECRET", "")
    refresh_token = os.getenv("GBP_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError("GBP OAuth credentials not configured in settings.env")
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def _gbp_headers() -> dict:
    return {"Authorization": f"Bearer {_get_access_token()}", "Content-Type": "application/json"}


def _fetch_reviews_real() -> list[dict]:
    url = (
        f"https://mybusiness.googleapis.com/v4/"
        f"accounts/{ACCOUNT_ID}/locations/{LOCATION_ID}/reviews"
        f"?pageSize=50&orderBy=updateTime+desc"
    )
    req = urllib.request.Request(url, headers=_gbp_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    star_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    return [
        {
            "review_id":   r["reviewId"],
            "reviewer":    r.get("reviewer", {}).get("displayName", "Anonymous"),
            "stars":       star_map.get(r.get("starRating", "THREE"), 3),
            "comment":     r.get("comment", ""),
            "review_time": r.get("updateTime", datetime.now(timezone.utc).isoformat()),
        }
        for r in data.get("reviews", [])
    ]


def _fetch_reviews_stub() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"review_id": "stub_r1", "reviewer": "Marius K.",  "stars": 5,
         "comment": "Best espresso in Vilnius!",
         "review_time": (now - timedelta(hours=2)).isoformat()},
        {"review_id": "stub_r2", "reviewer": "Agnė L.",    "stars": 2,
         "comment": "Waited 15 min for a latte, staff unhelpful.",
         "review_time": (now - timedelta(hours=5)).isoformat()},
        {"review_id": "stub_r3", "reviewer": "Jonas P.",   "stars": 4,
         "comment": "Nice atmosphere, slightly overpriced.",
         "review_time": (now - timedelta(days=1)).isoformat()},
        {"review_id": "stub_r4", "reviewer": "Rasa V.",    "stars": 1,
         "comment": "Cold coffee, rude barista.",
         "review_time": (now - timedelta(days=2)).isoformat()},
        {"review_id": "stub_r5", "reviewer": "Tomas B.",   "stars": 5,
         "comment": "Great place to work remotely.",
         "review_time": (now - timedelta(days=3)).isoformat()},
    ]


def _fetch_reviews() -> list[dict]:
    return _fetch_reviews_stub() if STUB_MODE else _fetch_reviews_real()


def _post_reply_real(review_id: str, reply_text: str) -> None:
    url = (
        f"https://mybusiness.googleapis.com/v4/"
        f"accounts/{ACCOUNT_ID}/locations/{LOCATION_ID}/reviews/{review_id}/reply"
    )
    data = json.dumps({"comment": reply_text}).encode()
    req  = urllib.request.Request(url, data=data, headers=_gbp_headers(), method="PUT")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _post_reply_stub(review_id: str, reply_text: str) -> None:
    log.info("[STUB] post_reply review_id=%s reply=%s…", review_id, reply_text[:60])


def _publish_post_real(text: str) -> None:
    url = (
        f"https://mybusiness.googleapis.com/v4/"
        f"accounts/{ACCOUNT_ID}/locations/{LOCATION_ID}/localPosts"
    )
    data = json.dumps({
        "languageCode": "lt",
        "summary":      text,
        "callToAction": {"actionType": "LEARN_MORE", "url": SHOP_URL},
        "topicType":    "STANDARD",
    }).encode()
    req = urllib.request.Request(url, data=data, headers=_gbp_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _publish_post_stub(text: str) -> None:
    log.info("[STUB] publish_post: %s…", text[:80])


# ── Draft reply template ───────────────────────────────────────────────────────

def _draft_reply(stars: int, comment: str) -> str:
    if stars <= 2:
        contact = f" at {SHOP_URL}" if SHOP_URL else ""
        return (
            f"Thank you for your honest feedback. We're sorry your experience "
            f"didn't meet our standards. We'd love to make it right — "
            f"please reach out to us directly{contact}."
        )
    return (
        f"Thank you for visiting {SHOP_NAME}! We appreciate you taking the "
        f"time to share your experience."
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def poll_reviews(conn) -> None:
    """Fetch new reviews, persist them, and escalate low-star alerts."""
    reviews = _fetch_reviews()
    now     = datetime.now(timezone.utc).isoformat()
    new_low: list[dict] = []

    for r in reviews:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO google_reviews
                   (review_id, reviewer, stars, comment, review_time, fetched_at)
                   VALUES (?,?,?,?,?,?)""",
                (r["review_id"], r["reviewer"], r["stars"],
                 r["comment"], r["review_time"], now),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                if r["stars"] <= ALERT_STARS:
                    new_low.append(r)
        except Exception as exc:
            log.error("DB write error for review %s: %s", r["review_id"], exc)

    conn.commit()
    log.info("poll_reviews: %d fetched, %d new low-star", len(reviews), len(new_low))

    for r in new_low:
        draft     = _draft_reply(r["stars"], r.get("comment", ""))
        event_key = f"REVIEW_ESCALATION:{r['review_id']}"
        stars_str = "★" * r["stars"] + "☆" * (5 - r["stars"])
        msg = (
            f"⭐ Low-star review alert\n"
            f"Reviewer: {r['reviewer']}\n"
            f"Rating:   {stars_str} ({r['stars']}/5)\n"
            f"Review:   {r.get('comment', '(no comment)')}\n\n"
            f"Draft reply:\n{draft}\n\n"
            f"Reply GO {event_key} to post this reply.\n"
            f"Reply SKIP {event_key} to dismiss."
        )
        conn.execute(
            "UPDATE google_reviews SET reply_text=? WHERE review_id=?",
            (draft, r["review_id"]),
        )
        conn.commit()
        _notify_if_new(conn,"REVIEW_ESCALATION", event_key, msg)

    _check_trend(conn)


def _check_trend(conn) -> None:
    cutoff_14 = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    cutoff_7  = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    avg_prior = conn.execute(
        "SELECT AVG(stars) FROM google_reviews WHERE review_time >= ? AND review_time < ?",
        (cutoff_14, cutoff_7),
    ).fetchone()[0]
    avg_now = conn.execute(
        "SELECT AVG(stars) FROM google_reviews WHERE review_time >= ?",
        (cutoff_7,),
    ).fetchone()[0]

    if avg_prior is None or avg_now is None:
        return

    drop = avg_prior - avg_now
    if drop >= TREND_DROP:
        event_key = f"REVIEW_TREND_DROP:{datetime.now(timezone.utc).date().isoformat()}"
        msg = (
            f"📉 Review rating trend drop detected\n"
            f"Prior 7-day avg: {avg_prior:.2f} ★\n"
            f"This 7-day avg:  {avg_now:.2f} ★\n"
            f"Drop: {drop:.2f} stars (threshold: {TREND_DROP})\n\n"
            f"Consider reviewing recent shift notes and checking service quality."
        )
        _notify_if_new(conn,"REVIEW_TREND_DROP", event_key, msg)


def build_weekly_digest(conn) -> None:
    """Compile the last 7 days of reviews into a digest and notify the owner."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows   = conn.execute(
        """SELECT stars, comment, reviewer, review_time
           FROM google_reviews WHERE review_time >= ? ORDER BY review_time DESC""",
        (cutoff,),
    ).fetchall()

    if not rows:
        log.info("build_weekly_digest: no reviews in the last 7 days")
        return

    total   = len(rows)
    avg     = sum(r["stars"] for r in rows) / total
    by_star = {s: 0 for s in range(1, 6)}
    for r in rows:
        by_star[r["stars"]] += 1

    highlights = [r for r in rows if r["stars"] >= 4][:3]
    lowlights  = [r for r in rows if r["stars"] <= 2][:3]

    def _fmt(rs):
        lines = [f"  {r['stars']}★ {r['reviewer']}: {(r['comment'] or '')[:80]}" for r in rs]
        return "\n".join(lines) or "  (none)"

    dist_str = "  ".join(f"{s}★:{by_star[s]}" for s in range(5, 0, -1))
    week_str = datetime.now(timezone.utc).strftime("%Y-W%W")

    msg = (
        f"📊 Weekly review digest — {week_str}\n"
        f"Total: {total}   Average: {avg:.2f} ★\n"
        f"Distribution: {dist_str}\n\n"
        f"Top reviews:\n{_fmt(highlights)}\n\n"
        f"Needs attention:\n{_fmt(lowlights)}"
    )
    _notify_if_new(conn,"REVIEW_WEEKLY_DIGEST", f"REVIEW_DIGEST:{week_str}", msg)
    log.info("build_weekly_digest: sent digest %s (%d reviews, avg %.2f)", week_str, total, avg)


def draft_winback_post(conn) -> None:
    """Draft a monthly winback post and send to owner for GO/SKIP confirmation."""
    month      = datetime.now(timezone.utc).strftime("%Y-%m")
    prev_month = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    if conn.execute("SELECT id FROM winback_posts WHERE month=?", (month,)).fetchone():
        log.info("draft_winback_post: already drafted for %s", month)
        return

    low_rows = conn.execute(
        """SELECT stars, comment FROM google_reviews
           WHERE review_time >= ? AND review_time < ? AND stars <= ?
           ORDER BY stars ASC LIMIT 5""",
        (f"{prev_month}-01", f"{month}-01", ALERT_STARS),
    ).fetchall()

    themes: list[str] = []
    for r in low_rows:
        c = (r["comment"] or "").lower()
        if any(w in c for w in ["wait", "slow", "long"]):
            themes.append("service speed")
        if any(w in c for w in ["cold", "temperature"]):
            themes.append("drink temperature")
        if any(w in c for w in ["rude", "staff", "barista", "unfriendly"]):
            themes.append("staff friendliness")
        if any(w in c for w in ["price", "expensive", "cost"]):
            themes.append("value for money")
    themes = list(dict.fromkeys(themes)) or ["your recent experience"]

    theme_str  = " and ".join(themes[:2])
    draft_text = (
        f"Dear {SHOP_NAME} community,\n\n"
        f"Thank you for your reviews in {prev_month}. "
        f"We have been working on {theme_str} — "
        f"your feedback directly shapes how we run our café.\n\n"
        f"We look forward to welcoming you back and showing you the improvements. "
        f"Stop by for a complimentary coffee upgrade on your next visit — just mention this post.\n\n"
        f"☕ See you soon!"
    )

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO winback_posts (month, draft_text, status, created_at) VALUES (?,?,?,?)",
        (month, draft_text, "draft", now),
    )
    conn.commit()

    event_key = f"WINBACK_POST:{month}"
    msg = (
        f"📝 Monthly winback post draft ready — {month}\n\n"
        f"Draft:\n{draft_text}\n\n"
        f"Reply GO {event_key} to publish to Google Business Profile.\n"
        f"Reply SKIP {event_key} to skip this month."
    )
    _notify_if_new(conn,"WINBACK_POST", event_key, msg)
    log.info("draft_winback_post: draft created for %s", month)


def post_reply(review_id: str, draft_reply: str, conn) -> None:
    """Post an owner-approved reply to a GBP review."""
    if STUB_MODE:
        _post_reply_stub(review_id, draft_reply)
    else:
        _post_reply_real(review_id, draft_reply)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE google_reviews SET reply_text=?, replied_at=? WHERE review_id=?",
        (draft_reply, now, review_id),
    )
    conn.commit()
    log.info("post_reply: replied to review %s", review_id)


def publish_winback_post(month: str, conn) -> None:
    """Publish an owner-approved winback post to Google Business Profile."""
    row = conn.execute(
        "SELECT draft_text FROM winback_posts WHERE month=?", (month,)
    ).fetchone()
    if not row:
        log.error("publish_winback_post: no draft found for month %s", month)
        return

    if STUB_MODE:
        _publish_post_stub(row["draft_text"])
    else:
        _publish_post_real(row["draft_text"])

    conn.execute(
        "UPDATE winback_posts SET status='published', published_at=? WHERE month=?",
        (datetime.now(timezone.utc).isoformat(), month),
    )
    conn.commit()
    log.info("publish_winback_post: published post for %s", month)


# ── OAuth2 one-time setup ──────────────────────────────────────────────────────

def _run_auth_flow() -> None:
    """
    One-time OAuth2 flow to obtain a refresh token.

    Steps:
      1. This script prints an authorisation URL.
      2. Open it in your browser and approve the permissions.
      3. Google redirects to localhost:8080 — the page will show
         ERR_CONNECTION_REFUSED (that is expected and fine).
      4. Copy the full URL from your browser address bar and paste it here.
      5. The script extracts the code and exchanges it for a refresh token.
    """
    client_id     = os.getenv("GBP_CLIENT_ID", "")
    client_secret = os.getenv("GBP_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ERROR: GBP_CLIENT_ID and GBP_CLIENT_SECRET must be set in settings.env")
        sys.exit(1)

    REDIRECT_URI = "http://localhost:8080"
    SCOPE        = "https://www.googleapis.com/auth/business.manage"

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id":     client_id,
            "redirect_uri":  REDIRECT_URI,
            "response_type": "code",
            "scope":         SCOPE,
            "access_type":   "offline",
            "prompt":        "consent",
        })
    )

    print("\n-- Step 1 ----------------------------------------------------------")
    print("Open this URL in your browser:\n")
    print(f"  {auth_url}\n")
    print("-- Step 2 ----------------------------------------------------------")
    print("Approve the permissions. Google will redirect to localhost:8080.")
    print("You will see ERR_CONNECTION_REFUSED — that is expected.")
    print("Copy the FULL URL from your browser address bar.\n")

    print("-- Step 3 -- Paste the redirect URL (or just the code= value) back\n"
          "             to the assistant and ask to run --auth-code CODE.\n")
    sys.exit(0)


def _exchange_auth_code(code: str) -> None:
    client_id     = os.getenv("GBP_CLIENT_ID", "")
    client_secret = os.getenv("GBP_CLIENT_SECRET", "")
    REDIRECT_URI  = "http://localhost:8080"

    # Accept either the full redirect URL or just the bare code
    if code.startswith("http"):
        qs   = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        code = qs.get("code", [None])[0]
        if not code:
            print(f"ERROR: No 'code' parameter found in URL.")
            sys.exit(1)

    data = urllib.parse.urlencode({
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        tokens = json.loads(resp.read())

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("ERROR: No refresh_token in response — ensure prompt=consent was set.")
        print(json.dumps(tokens, indent=2))
        sys.exit(1)

    print("\nAuthorisation complete.\n")
    print("Add this line to config/settings.env:\n")
    print(f"GBP_REFRESH_TOKEN={refresh_token}\n")
    print("Then run:  python core/sentiment_loop.py --discover")


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from migrate import apply_migrations, get_connection
    apply_migrations(DB_PATH)

    parser = argparse.ArgumentParser(description="Google review sentiment loop")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--auth",      action="store_true", help="Print OAuth2 authorisation URL")
    grp.add_argument("--auth-code", metavar="CODE",      help="Exchange authorisation code for refresh token")
    grp.add_argument("--poll",      action="store_true", help="Poll reviews and escalate low-star alerts")
    grp.add_argument("--digest",    action="store_true", help="Build and send weekly review digest")
    grp.add_argument("--winback",   action="store_true", help="Draft monthly winback post")
    grp.add_argument("--discover",  action="store_true", help="Print GBP account/location IDs (real mode only)")
    args = parser.parse_args()

    if args.auth:
        _run_auth_flow()
        sys.exit(0)

    if args.auth_code:
        _exchange_auth_code(args.auth_code)
        sys.exit(0)

    conn = get_connection(DB_PATH)
    try:
        if args.poll:
            poll_reviews(conn)
        elif args.digest:
            build_weekly_digest(conn)
        elif args.winback:
            draft_winback_post(conn)
        elif args.discover:
            if STUB_MODE:
                print("GBP_STUB_MODE=true — set to false and configure OAuth to discover")
            else:
                token = _get_access_token()
                req   = urllib.request.Request(
                    "https://mybusiness.googleapis.com/v4/accounts",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    print(json.dumps(json.loads(resp.read()), indent=2))
    finally:
        conn.close()
