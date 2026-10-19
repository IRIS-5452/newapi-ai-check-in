#!/usr/bin/env python3
"""
Report check-in results to the Cloudflare Worker dashboard.

Reads ACCOUNTS / PROVIDERS (same JSON as main.py), queries each site's
check-in status + balance, then POSTs a summary to the Worker's /report.

Env:
    REPORT_URL    e.g. https://checkin-cron.xxx.workers.dev
    STATUS_TOKEN  shared secret
    ACCOUNTS      JSON array
    PROVIDERS     JSON object
"""

import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BJ = timezone(timedelta(hours=8))
QUOTA_PER_UNIT = 500000


def curl_json(url: str, token: str, uid: str) -> dict | None:
    cmd = [
        "curl", "-sS", "-m", "25", url,
        "-H", f"Authorization: Bearer {token}",
        "-H", f"new-api-user: {uid}",
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {UA}",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def check_site(origin: str, token: str, uid: str, month: str) -> dict:
    info = curl_json(f"{origin}/api/user/self", token, uid) or {}
    data = info.get("data") or {}
    quota = data.get("quota")
    balance = round(quota / QUOTA_PER_UNIT, 2) if isinstance(quota, (int, float)) else None

    st = curl_json(f"{origin}/api/user/checkin?month={month}", token, uid) or {}
    stats = (st.get("data") or {}).get("stats") or {}
    checked = stats.get("checked_in_today")

    ok = bool(info.get("success")) and (checked is not False)
    return {
        "ok": ok,
        "balance": balance,
        "checked_in": checked,
        "count": stats.get("checkin_count"),
        "reachable": bool(info.get("success")),
    }


def main() -> int:
    base = (os.environ.get("REPORT_URL") or "").rstrip("/")
    token = os.environ.get("STATUS_TOKEN") or ""
    if not base or not token:
        print("[!] REPORT_URL / STATUS_TOKEN not set, skip reporting")
        return 0

    try:
        accounts = json.loads(os.environ.get("ACCOUNTS") or "[]")
        providers = json.loads(os.environ.get("PROVIDERS") or "{}")
    except json.JSONDecodeError as exc:
        print(f"[!] invalid ACCOUNTS/PROVIDERS json: {exc}")
        return 2

    month = datetime.now(BJ).strftime("%Y-%m")
    date = datetime.now(BJ).strftime("%Y-%m-%d")

    rows = []
    for a in accounts:
        name = a.get("name") or a.get("provider")
        p = providers.get(a.get("provider"), {})
        origin = p.get("origin")
        tok = a.get("system_access_token")
        uid = a.get("api_user")
        if not origin or not tok or not uid:
            rows.append({"name": name, "ok": False, "balance": None, "checked_in": None})
            continue
        r = check_site(origin, tok, str(uid), month)
        r["name"] = name
        rows.append(r)
        print(f"  {name:12} ok={r['ok']} checked_in={r['checked_in']} balance={r['balance']}")

    payload = {
        "date": date,
        "workflow": "checkin.yml",
        "ok": all(r["ok"] for r in rows) if rows else False,
        "accounts": rows,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    cmd = [
        "curl", "-sS", "-m", "25", "-X", "POST",
        f"{base}/report?k={token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload, ensure_ascii=False),
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    print(f"[report] {out.stdout.strip()[:200]} {out.stderr.strip()[:200]}")

    push_if_failed(payload)
    return 0


def push_if_failed(payload: dict) -> None:
    """只在有站点失败时推送，避免每天骚扰。"""
    bark = (os.environ.get("BARK_URL") or "").strip().rstrip("/")
    if not bark or payload.get("ok"):
        return

    bad = [r for r in payload.get("accounts", []) if not r.get("ok")]
    if not bad:
        return

    title = f"签到异常 {len(bad)}/{len(payload.get('accounts', []))}"
    lines = []
    for r in payload.get("accounts", []):
        mark = "OK " if r.get("ok") else "FAIL"
        bal = f"${r['balance']:.2f}" if isinstance(r.get("balance"), (int, float)) else "-"
        lines.append(f"[{mark}] {r.get('name')} {bal}")
    body = "\n".join(lines)

    url = (
        f"{bark}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        f"?group=checkin&level=timeSensitive"
    )
    res = subprocess.run(
        ["curl", "-sS", "-m", "15", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(f"[bark] {res.stdout.strip()[:160]} {res.stderr.strip()[:160]}")


if __name__ == "__main__":
    sys.exit(main())
