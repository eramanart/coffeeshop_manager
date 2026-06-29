"""
End-to-end go-live smoke test against a LIVE server (through the cloudflared/ngrok
tunnel, or against http://127.0.0.1:8000 directly).

Exercises all three auth layers + reachability over real HTTP, with NO state
mutation: the Telegram check sends a non-command message ("ignored"), and the
bearer check targets a nonexistent event key (404 after auth passes). Safe to run
against a live, even production, instance.

Reads secrets from config/settings.env — nothing is hardcoded.

Usage (from coffee_agent/python):
    python tests/smoke_tunnel.py https://<your-tunnel>.trycloudflare.com
    python tests/smoke_tunnel.py http://127.0.0.1:8000
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def request(method: str, url: str, headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")

    env = load_env(Path(__file__).resolve().parent.parent / "config" / "settings.env")
    user = env.get("DASHBOARD_USER", "")
    pw = env.get("DASHBOARD_PASSWORD", "")
    bearer = env.get("API_BEARER_TOKEN", "")
    tg_secret = env.get("TELEGRAM_SECRET_TOKEN", "")
    basic = base64.b64encode(f"{user}:{pw}".encode()).decode()

    passed = failed = 0

    def check(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1; print(f"  PASS  {name}")
        else:
            failed += 1; print(f"  FAIL  {name}  {extra}")

    print(f"Target: {base}\n")

    print("Reachability + liveness:")
    s, _ = request("GET", f"{base}/health")
    check("GET /health -> 200", s == 200, f"got {s}")

    print("Dashboard Basic auth (the tunnel-exposure guard):")
    s, b = request("GET", f"{base}/")
    check("GET / no creds -> 401", s == 401, f"got {s}")
    check("token not leaked in 401 body", bearer and bearer not in b, "bearer found unauth!")
    s, _ = request("GET", f"{base}/", headers={"Authorization": f"Basic {basic}"})
    check("GET / real creds -> 200", s == 200, f"got {s}")
    s, _ = request("GET", f"{base}/status")
    check("GET /status no creds -> 401 (business data guarded)", s == 401, f"got {s}")

    print("Telegram webhook secret (no state change — non-command message):")
    payload = json.dumps({"message": {"text": "hello (not a command)"}}).encode()
    hdr_json = {"Content-Type": "application/json"}
    s, _ = request("POST", f"{base}/webhook/telegram",
                   headers={**hdr_json, "X-Telegram-Bot-Api-Secret-Token": "WRONG"}, body=payload)
    check("telegram wrong secret -> 401", s == 401, f"got {s}")
    s, b = request("POST", f"{base}/webhook/telegram",
                   headers={**hdr_json, "X-Telegram-Bot-Api-Secret-Token": tg_secret}, body=payload)
    check("telegram correct secret -> 200 ignored", s == 200 and '"ignored"' in b, f"got {s} {b[:120]}")

    print("Bearer auth on /confirm (no state change — nonexistent key):")
    s, _ = request("POST", f"{base}/confirm/NONEXISTENT_KEY",
                   headers={"Authorization": "Bearer WRONG"})
    check("confirm wrong bearer -> 401", s == 401, f"got {s}")
    s, _ = request("POST", f"{base}/confirm/NONEXISTENT_KEY",
                   headers={"Authorization": f"Bearer {bearer}"})
    check("confirm correct bearer -> 404 (auth passed, key absent)", s == 404, f"got {s}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
