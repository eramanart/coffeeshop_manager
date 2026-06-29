"""
start_tunnel.py — start a Cloudflare quick tunnel to the local CoffeeManager-OS
server and register the Telegram webhook AUTOMATICALLY.

Run this AFTER the server is running (start_server.bat, or uvicorn on port 8000).
The free Cloudflare tunnel gets a NEW address every time it starts, so this script
re-registers the Telegram webhook with the new address each run — you don't have to
copy/paste anything.

Leave the window OPEN while you want the bot to receive your replies (GO/APPROVE/SKIP).
Close the window to stop the tunnel.
"""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLOUDFLARED = ROOT / "cloudflared.exe"
SETTINGS = ROOT / "python" / "config" / "settings.env"
LOCAL_URL = "http://localhost:8000"
HEALTH = "http://127.0.0.1:8000/health"


def load_settings() -> dict:
    env = {}
    for line in SETTINGS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def server_running() -> bool:
    try:
        urllib.request.urlopen(HEALTH, timeout=3)
        return True
    except Exception:
        return False


def registered_webhook_url(bot: str) -> str:
    """Return the URL Telegram currently has registered (or '' if none/unknown)."""
    api = f"https://api.telegram.org/bot{bot}/getWebhookInfo"
    try:
        with urllib.request.urlopen(api, timeout=15) as r:
            return json.loads(r.read().decode()).get("result", {}).get("url", "")
    except Exception:  # noqa: BLE001
        return ""


def webhook_watchdog(bot: str, secret: str, public_url: str, stop: threading.Event) -> None:
    """Every 60s, make sure Telegram still points at THIS tunnel; re-register if not.

    Closes the silent failure mode where the tunnel is up and the bot is up, but
    Telegram is still trying to deliver to a stale/empty URL (e.g. the first
    registration raced the tunnel coming online, or another run overwrote it).
    Only acts when the URLs actually differ, so it costs one cheap getWebhookInfo
    per minute in the normal case."""
    expected = f"{public_url}/webhook/telegram"
    while not stop.wait(60):
        if registered_webhook_url(bot) != expected:
            print("\n   [watchdog] Telegram is not pointing at this tunnel — re-registering...")
            resp = set_webhook(bot, secret, public_url)
            print("   [watchdog] re-registered OK.\n" if '"ok":true' in resp
                  else f"   [watchdog] re-register response: {resp}\n")


def set_webhook(bot: str, secret: str, public_url: str) -> str:
    """Register the webhook with Telegram. Returns Telegram's response text.

    A brand-new tunnel hostname takes a few seconds to become globally resolvable.
    We let TELEGRAM'S resolver be the judge (it's what actually delivers) and retry
    while it reports the host isn't resolvable yet. We deliberately do NOT probe the
    URL from this machine first: doing so negative-caches the not-yet-existing name
    in the LOCAL DNS resolver, which then fails for minutes even though Telegram can
    reach it fine. Also retries on 429 (Telegram rate-limits setWebhook)."""
    api = f"https://api.telegram.org/bot{bot}/setWebhook"
    data = urllib.parse.urlencode(
        {"url": f"{public_url}/webhook/telegram", "secret_token": secret}
    ).encode()
    last = ""
    # ~3 minute budget. A new tunnel usually resolves in well under a minute, but
    # propagation is occasionally slow; better to wait than to fail and confuse.
    for i in range(45):
        try:
            with urllib.request.urlopen(api, data=data, timeout=15) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            last = f"HTTP {e.code}: {body}"
            low = body.lower()
            if e.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:  # noqa: BLE001
                    wait = 2
                time.sleep(wait + 1)
                continue
            if "resolve host" in low or "bad webhook" in low:
                if i % 5 == 0:  # calm status every ~20s instead of every 4s
                    print(f"   (waiting for the tunnel to come online... ~{i * 4}s)")
                time.sleep(4)
                continue
            return last
        except Exception as e:  # noqa: BLE001
            last = f"request error: {e}"
            time.sleep(3)
            continue
    return last or "failed after retries"


def main() -> int:
    print("=" * 64)
    print(" CoffeeManager-OS — Telegram tunnel")
    print("=" * 64)

    if not CLOUDFLARED.exists():
        print(f"\nERROR: cloudflared.exe not found at {CLOUDFLARED}")
        input("\nPress Enter to close...")
        return 1

    env = load_settings()
    bot = env.get("TELEGRAM_BOT_TOKEN", "")
    secret = env.get("TELEGRAM_SECRET_TOKEN", "")
    if not bot or not secret:
        print("\nERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_SECRET_TOKEN missing in settings.env")
        input("\nPress Enter to close...")
        return 1

    if not server_running():
        print("\nThe server is NOT running on http://127.0.0.1:8000.")
        print("Start it first (double-click start_server.bat), then run this again.")
        input("\nPress Enter to close...")
        return 1

    print("\nServer is running. Starting the tunnel (this takes a few seconds)...\n")
    proc = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--url", LOCAL_URL],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    registered = False
    stop = threading.Event()
    try:
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not registered:
                public_url = m.group(0)
                print("=" * 64)
                print(f" Tunnel address: {public_url}")
                print(" Registering with Telegram (waits for the tunnel to come online)...")
                resp = set_webhook(bot, secret, public_url)
                if '"ok":true' in resp:
                    print(" Telegram webhook registered. Your bot replies will")
                    print(" now reach the app.")
                else:
                    print(f" setWebhook response: {resp}")
                # Keep Telegram pointed at THIS tunnel for as long as we run.
                threading.Thread(
                    target=webhook_watchdog, args=(bot, secret, public_url, stop),
                    daemon=True,
                ).start()
                print("")
                print(" >>> LEAVE THIS WINDOW OPEN while you use the bot. <<<")
                print(" >>> Close it to stop the tunnel.                  <<<")
                print("=" * 64)
                registered = True
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
