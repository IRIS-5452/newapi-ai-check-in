#!/usr/bin/env python3
"""
NewAPI site check-in with PoW (Proof of Work) challenge support.

适用于签到接口要求 pow_challenge / pow_nonce 的站点
（如 runanytime.hxi.me）。

流程：
  1. GET  /api/user/checkin?month=YYYY-MM     查状态
  2. GET  /api/user/pow/challenge?action=checkin   拿挑战
  3. 本地算 nonce：SHA256(prefix + hex8(nonce)) 前 difficulty 位为 0
  4. POST /api/user/checkin?pow_challenge=..&pow_nonce=..

Env:
    POW_ACCOUNTS = [{"name":"..","origin":"https://..","token":"..","api_user":".."}]
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
QUOTA_PER_UNIT = 500000


def api(url: str, token: str, api_user: str = "", method: str = "GET"):
    cmd = [
        "curl", "-sS", "-m", "30", "-X", method, url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {UA}",
    ]
    if api_user:
        cmd += ["-H", f"new-api-user: {api_user}"]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:200]}


def leading_zero_bits_ok(digest: bytes, difficulty: int) -> bool:
    full, rem = difficulty // 8, difficulty % 8
    for i in range(full):
        if digest[i] != 0:
            return False
    if rem > 0:
        if (digest[full] & (0xFF << (8 - rem)) & 0xFF) != 0:
            return False
    return True


def solve_pow(prefix: str, difficulty: int):
    s = 0
    while True:
        nonce = format(s, "08x")
        digest = hashlib.sha256((prefix + nonce).encode()).digest()
        if leading_zero_bits_ok(digest, difficulty):
            return nonce, s + 1
        s += 1
        if s > 0xFFFFFFFF:
            raise RuntimeError("max attempts exceeded")


def run_one(acc: dict, month: str) -> dict:
    name = acc.get("name") or "unnamed"
    origin = (acc.get("origin") or "").rstrip("/")
    token = acc.get("token") or ""
    api_user = str(acc.get("api_user") or "")

    if not origin or not token:
        print(f"[{name}] missing origin/token")
        return {"name": name, "ok": False}

    info = api(f"{origin}/api/user/self", token, api_user)
    quota = (info.get("data") or {}).get("quota")
    balance = round(quota / QUOTA_PER_UNIT, 2) if isinstance(quota, (int, float)) else None

    st = api(f"{origin}/api/user/checkin?month={month}", token, api_user)
    stats = (st.get("data") or {}).get("stats") or {}
    checked = stats.get("checked_in_today")
    print(f"[{name}] balance={balance} checked_in_today={checked}")

    if checked is True:
        print(f"[{name}] already checked in, skip")
        return {"name": name, "ok": True, "balance": balance, "checked_in": True}

    ch = api(f"{origin}/api/user/pow/challenge?action=checkin", token, api_user)
    data = ch.get("data") or {}
    cid, prefix, diff = data.get("challenge_id"), data.get("prefix"), data.get("difficulty")
    if not (cid and prefix and isinstance(diff, int)):
        print(f"[{name}] challenge failed: {json.dumps(ch, ensure_ascii=False)[:200]}")
        return {"name": name, "ok": False, "balance": balance}

    t0 = time.time()
    nonce, attempts = solve_pow(prefix, diff)
    print(f"[{name}] pow solved: nonce={nonce} attempts={attempts} {(time.time()-t0)*1000:.0f}ms")

    url = f"{origin}/api/user/checkin?pow_challenge={cid}&pow_nonce={nonce}"
    res = api(url, token, api_user, method="POST")
    print(f"[{name}] submit: {json.dumps(res, ensure_ascii=False)[:250]}")

    ok = bool(res.get("success"))
    if not ok and "已签到" in str(res.get("message", "")):
        ok = True

    award = (res.get("data") or {}).get("quota_awarded") if ok else None
    return {
        "name": name,
        "ok": ok,
        "balance": balance,
        "checked_in": ok,
        "today_award": round(award / QUOTA_PER_UNIT, 2) if isinstance(award, (int, float)) else None,
    }


def report(results: list, workflow: str) -> None:
    """把结果回报给 Cloudflare Worker 看板。"""
    base = (os.environ.get("REPORT_URL") or "").rstrip("/")
    token = os.environ.get("STATUS_TOKEN") or ""
    if not base or not token or not results:
        return
    body = {
        "date": datetime.now(BJ).strftime("%Y-%m-%d"),
        "workflow": workflow,
        "accounts": results,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    res = subprocess.run(
        ["curl", "-sS", "-m", "20", "-X", "POST", f"{base}/report?k={token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(body, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(f"[report] {res.stdout.strip()[:160]}")


def main() -> int:
    raw = os.environ.get("POW_ACCOUNTS", "").strip()
    if not raw:
        print("[!] POW_ACCOUNTS not set")
        return 2
    try:
        accounts = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[!] POW_ACCOUNTS invalid json: {exc}")
        return 2

    month = datetime.now(BJ).strftime("%Y-%m")
    print(f"[*] accounts={len(accounts)} month={month}")

    ok = 0
    results = []
    for acc in accounts:
        try:
            r = run_one(acc, month)
            results.append(r)
            if r.get("ok"):
                ok += 1
        except Exception as exc:
            print(f"[{acc.get('name')}] exception: {exc}")
            results.append({"name": acc.get("name") or "unnamed", "ok": False})
        print("-" * 50)

    print(f"[=] success {ok}/{len(accounts)}")
    report(results, "pow.yml")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
