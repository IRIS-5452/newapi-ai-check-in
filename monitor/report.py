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


def compute_streak(records: list) -> int:
    """从签到记录算连续天数（仅限本月，跨月会断）。"""
    dates = sorted(
        {r.get("checkin_date") for r in records if r.get("checkin_date")},
        reverse=True,
    )
    if not dates:
        return 0
    streak = 1
    prev = datetime.strptime(dates[0], "%Y-%m-%d").date()
    for ds in dates[1:]:
        cur = datetime.strptime(ds, "%Y-%m-%d").date()
        if (prev - cur).days == 1:
            streak += 1
            prev = cur
        else:
            break
    return streak


def check_site(origin: str, token: str, uid: str, month: str, today: str) -> dict:
    info = curl_json(f"{origin}/api/user/self", token, uid) or {}
    data = info.get("data") or {}
    quota = data.get("quota")
    used = data.get("used_quota")
    balance = round(quota / QUOTA_PER_UNIT, 2) if isinstance(quota, (int, float)) else None

    # 消耗只能用 used_quota 的增量算：签到只加 quota，不影响 used
    used_ever = round(used / QUOTA_PER_UNIT, 4) if isinstance(used, (int, float)) else None
    quota_ever = round(quota / QUOTA_PER_UNIT, 4) if isinstance(quota, (int, float)) else None

    st = curl_json(f"{origin}/api/user/checkin?month={month}", token, uid) or {}
    stats = (st.get("data") or {}).get("stats") or {}
    checked = stats.get("checked_in_today")
    records = stats.get("records") or []

    # 今日增量：优先取今天那条，取不到就取最近一条（各站日期边界不同）
    today_award = None
    for r in records:
        if r.get("checkin_date") == today:
            today_award = r.get("quota_awarded")
            break
    if today_award is None and records:
        today_award = records[0].get("quota_awarded")

    # 本月签到收益
    month_award = sum(
        r.get("quota_awarded", 0) for r in records
        if isinstance(r.get("quota_awarded"), (int, float))
    )

    ok = bool(info.get("success")) and (checked is not False)
    return {
        "ok": ok,
        "balance": balance,
        "checked_in": checked,
        "count": stats.get("checkin_count"),
        "today_award": (
            round(today_award / QUOTA_PER_UNIT, 2)
            if isinstance(today_award, (int, float))
            else None
        ),
        "month_award": round(month_award / QUOTA_PER_UNIT, 2) if month_award else 0,
        "streak": compute_streak(records),
        "used_ever": used_ever,
        "quota_ever": quota_ever,
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
        r = check_site(origin, tok, str(uid), month, date)
        r["name"] = name
        rows.append(r)
        print(
            f"  {name:12} ok={r['ok']} checked={r['checked_in']} "
            f"bal={r['balance']} award={r.get('today_award')} streak={r.get('streak')}"
        )

    # 取历史快照，算「今日消耗」「7 天消耗」
    hist = curl_json(f"{base}/status?k={token}", "", "") or {}
    snaps = hist.get("snapshots") or []
    now_bj = datetime.now(BJ)
    y_date = (now_bj - timedelta(days=1)).strftime("%Y-%m-%d")
    w_date = (now_bj - timedelta(days=7)).strftime("%Y-%m-%d")

    def snap_map(d):
        for s in snaps:
            if s.get("date") == d:
                return {a["name"]: a for a in s.get("accounts", [])}
        return {}

    y_map, w_map = snap_map(y_date), snap_map(w_date)

    for r in rows:
        used = r.get("used_ever")
        quota = r.get("quota_ever")
        py = y_map.get(r["name"]) or {}
        pw = w_map.get(r["name"]) or {}

        # 消耗 = used 的增量（签到不影响 used）
        r["today_used"] = (
            round(used - py["used"], 2)
            if isinstance(used, (int, float)) and isinstance(py.get("used"), (int, float))
            else None
        )
        r["week_used"] = (
            round(used - pw["used"], 2)
            if isinstance(used, (int, float)) and isinstance(pw.get("used"), (int, float))
            else None
        )
        # 余额变化（7 天）
        r["week_balance_delta"] = (
            round(quota - pw["quota"], 2)
            if isinstance(quota, (int, float)) and isinstance(pw.get("quota"), (int, float))
            else None
        )

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
