#!/usr/bin/env python3
"""
MCII node-sharing site (proxy.mcii.cc) daily check-in.

Auth: POST /api/v1/auth/login -> nodepool_session cookie (30 days)
Check-in: POST /api/v1/me/checkin {confirm_partial: bool}
Status: GET /api/v1/me  (checkin_on / checkin_streak / subscription_expires_at)

Stdlib only, no external deps.
"""

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://proxy.mcii.cc"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BJ = timezone(timedelta(hours=8))

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method: str, path: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/")
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:300]}


def main() -> int:
    username = os.environ.get("MCII_USERNAME", "").strip()
    password = os.environ.get("MCII_PASSWORD", "").strip()
    if not username or not password:
        print("[!] MCII_USERNAME / MCII_PASSWORD not set")
        return 2

    today = datetime.now(BJ).strftime("%Y-%m-%d")
    print(f"[*] Target date (UTC+8): {today}")

    status, data = call("POST", "/api/v1/auth/login",
                        {"username": username, "password": password, "remember": True})
    if status != 200 or not data.get("authenticated"):
        print(f"[x] Login failed (HTTP {status}): {data.get('error') or data}")
        return 1
    print(f"[+] Logged in as {data.get('username')} (id={((data.get('user') or {}).get('id'))})")

    status, me = call("GET", "/api/v1/me")
    if status != 200:
        print(f"[x] Cannot read profile (HTTP {status}): {me.get('error') or me}")
        return 1
    user = me.get("user") or me
    checkin_on = user.get("checkin_on") or ""
    streak = user.get("checkin_streak", 0)
    expires = user.get("subscription_expires_at", "")
    print(f"[i] checkin_on={checkin_on or 'never'}  streak={streak}  expires={expires}")

    if checkin_on == today:
        print("[=] Already checked in today, nothing to do")
        return 0

    status, res = call("POST", "/api/v1/me/checkin", {"confirm_partial": False})
    if status != 200:
        print(f"[x] Check-in failed (HTTP {status}): {res.get('error') or res}")
        return 1

    if res.get("needs_confirmation"):
        print("[~] Partial confirmation required, resending with confirm_partial=true")
        status, res = call("POST", "/api/v1/me/checkin", {"confirm_partial": True})
        if status != 200:
            print(f"[x] Confirmed check-in failed (HTTP {status}): {res.get('error') or res}")
            return 1

    print(f"[+] Check-in OK: {json.dumps(res, ensure_ascii=False)[:300]}")

    status, me = call("GET", "/api/v1/me")
    if status == 200:
        user = me.get("user") or me
        print(f"[i] After: streak={user.get('checkin_streak')} "
              f"points={user.get('points')} expires={user.get('subscription_expires_at')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
