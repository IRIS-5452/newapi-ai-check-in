#!/usr/bin/env python3
"""
MCII node-sharing site (proxy.mcii.cc) daily check-in. Multi-account.

Auth: POST /api/v1/auth/login -> nodepool_session cookie (30 days)
Check-in: POST /api/v1/me/checkin {confirm_partial: bool}
Status: GET /api/v1/me  (checkin_on / checkin_streak / subscription_expires_at)

Config (env), two supported forms:
  1) MCII_ACCOUNTS  = [{"username":"..","password":".."}, {...}]   (preferred)
  2) MCII_USERNAME / MCII_PASSWORD                                  (legacy single account)

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


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        for k, v in {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
            "Origin": BASE,
            "Referer": BASE + "/",
        }.items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return exc.code, {"error": raw[:300]}


def run_one(username: str, password: str, today: str) -> bool:
    label = f"[{username}]"
    if not username or not password:
        print(f"{label} missing username/password, skipped")
        return False

    s = Session()
    status, data = s.call(
        "POST", "/api/v1/auth/login",
        {"username": username, "password": password, "remember": True},
    )
    if status != 200 or not data.get("authenticated"):
        print(f"{label} login failed (HTTP {status}): {data.get('error') or data}")
        return False
    uid = (data.get("user") or {}).get("id")
    print(f"{label} logged in as {data.get('username')} (id={uid})")

    status, me = s.call("GET", "/api/v1/me")
    if status != 200:
        print(f"{label} cannot read profile (HTTP {status}): {me.get('error') or me}")
        return False
    user = me.get("user") or me
    checkin_on = user.get("checkin_on") or ""
    print(
        f"{label} xp={user.get('xp')} points={user.get('points')} "
        f"level={user.get('level')} streak={user.get('checkin_streak')} "
        f"checkin_on={checkin_on or 'never'} expires={user.get('subscription_expires_at')}"
    )

    if checkin_on == today:
        print(f"{label} already checked in today, skip")
        return True

    status, res = s.call("POST", "/api/v1/me/checkin", {"confirm_partial": False})
    if status != 200:
        print(f"{label} check-in failed (HTTP {status}): {res.get('error') or res}")
        return False

    if res.get("needs_confirmation"):
        print(f"{label} partial confirmation required, resending")
        status, res = s.call("POST", "/api/v1/me/checkin", {"confirm_partial": True})
        if status != 200:
            print(f"{label} confirmed check-in failed (HTTP {status}): {res.get('error') or res}")
            return False

    print(f"{label} check-in OK: {json.dumps(res, ensure_ascii=False)[:220]}")

    status, me = s.call("GET", "/api/v1/me")
    if status == 200:
        user = me.get("user") or me
        print(
            f"{label} after: xp={user.get('xp')} streak={user.get('checkin_streak')} "
            f"expires={user.get('subscription_expires_at')}"
        )
    return True


def main() -> int:
    accounts = []
    raw = os.environ.get("MCII_ACCOUNTS", "").strip()
    if raw:
        try:
            accounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[!] MCII_ACCOUNTS is not valid JSON: {exc}")
            return 2
    else:
        accounts = [
            {
                "username": os.environ.get("MCII_USERNAME", ""),
                "password": os.environ.get("MCII_PASSWORD", ""),
            }
        ]

    if not accounts:
        print("[!] no accounts configured")
        return 2

    today = datetime.now(BJ).strftime("%Y-%m-%d")
    print(f"[*] accounts={len(accounts)}  target date (UTC+8)={today}")

    ok = 0
    for acc in accounts:
        try:
            if run_one(acc.get("username", ""), acc.get("password", ""), today):
                ok += 1
        except Exception as exc:
            print(f"[{acc.get('username')}] exception: {exc}")
        print("-" * 50)

    print(f"[=] success {ok}/{len(accounts)}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
