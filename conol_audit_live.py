#!/usr/bin/env python3
"""Full per-account liveness audit of the conol pool.

Why this exists: `test_conol_pool.py` samples ONE session (its Test 4) and its
Test 3 prints PASS unconditionally, while `save_cookies` fabricates
`expires = now + 7d` regardless of the token's real validity — and the ER importer
derives `isActive` from that fabricated expiry. So `isActive=1` means "a cookie
file was written after a sign-in", NOT "this token authenticates today".

This script is the real oracle: for every pool row it calls
GET https://conol.ai/api/auth/get-session with that row's token and counts 200s
whose user.email matches the row. No captcha, no spend, one cheap request per row.

Usage:
    python conol_audit_live.py            # audit, write conol_audit_live.json
    python conol_audit_live.py --workers 4
"""
import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"
OUT_FILE = BASE_DIR / "conol_audit_live.json"
COOKIE_NAME = "__Secure-better-auth.session_token"
SESSION_URL = "https://conol.ai/api/auth/get-session"
# A single 200-with-no-user is NOT proof of death. conol returned an empty user for a
# token that answered with the real email ~90 seconds later (Conol982918, 2026-10-03),
# and this script's verdicts drive `isActive` in ExtremeRouter — a false "dead" parks a
# working account, and 21 of 72 rows were marked dead in one pass. Confirm before recording.
DEAD_CONFIRM_ATTEMPTS = 2
DEAD_CONFIRM_DELAY_SEC = 2.0
# conol answers 429 when this script outruns it, and a 429 says nothing about the
# account. Recording it as death deactivated 11 working accounts in ExtremeRouter on
# 2026-10-03 (4 workers x 68 rows, twice in an hour). Back off instead, and space
# requests globally so the workers cannot stampede.
RATE_LIMIT_ATTEMPTS = 5
BACKOFF_BASE_SEC = 3.0
BACKOFF_MAX_SEC = 45.0
MIN_REQUEST_GAP_SEC = 1.2  # the limiter is window-based, so --workers 1 alone does not help

_throttle_lock = threading.Lock()
_last_request = [0.0]


def _throttle():
    """Global minimum spacing between get-session calls, across all workers."""
    with _throttle_lock:
        wait = MIN_REQUEST_GAP_SEC - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("conol_audit")

_print_lock = threading.Lock()


def load_pool():
    """Tolerant JSONL read: the registrar appends while we may be reading."""
    rows, torn = [], 0
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            torn += 1
    if torn:
        log.warning("skipped %d torn/incomplete pool line(s)", torn)
    return rows


def token_from_row(row):
    """Prefer the pool's own token, fall back to the cookie file."""
    token = (row.get("session_token") or "").strip()
    if token:
        return token
    path = row.get("cookies_path") or ""
    candidates = [Path(path), BASE_DIR / Path(path).name] if path else []
    for candidate in candidates:
        try:
            if candidate.exists():
                for cookie in json.loads(candidate.read_text(encoding="utf-8")):
                    if isinstance(cookie, dict) and cookie.get("name") == COOKIE_NAME:
                        return (cookie.get("value") or "").strip()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return ""


def _session_probe(token):
    """One get-session attempt. Returns (verdict, detail, user, status, retry_after).

    verdict is "ok" (authenticated), "dead" (conol answered and the credential is
    unusable), "rate_limited" (conol refused to answer — the auditor's own traffic,
    NOT the account's state) or "network_error" (no answer at all).

    Only a 200 whose body carries no user, or an explicit 401, counts as death.
    429/403/5xx and a 200 that is not JSON are transient: they must never move
    isActive, because that flag decides which accounts ExtremeRouter routes to.
    """
    _throttle()
    try:
        resp = requests.get(SESSION_URL, cookies={COOKIE_NAME: token},
                            headers={"Accept": "application/json"}, timeout=25)
    except requests.RequestException as exc:
        return "network_error", f"{type(exc).__name__}", {}, None, None
    code = resp.status_code
    try:
        retry_after = float(resp.headers.get("Retry-After") or 0) or None
    except (TypeError, ValueError):
        retry_after = None
    if code == 401:
        return "dead", f"HTTP 401: {resp.text[:80]}", {}, code, None
    if code != 200:
        return "rate_limited", f"HTTP {code}: {resp.text[:80]}", {}, code, retry_after
    try:
        user = (resp.json() or {}).get("user") or {}
    except ValueError:
        # A 200 that is not JSON is a proxy/WAF artefact, not an expired credential.
        return "rate_limited", "200 with non-JSON body", {}, code, None
    if not user.get("email"):
        # better-auth answers 200 with an empty/absent user for an unusable token, so
        # the status code alone is NOT liveness. Six dead July tokens used to land in a
        # misleading "email_mismatch" bucket because of this.
        return "dead", "200 with no user", {}, code, None
    return "ok", "", user, code, None


def audit_row(row):
    email = row.get("email", "")
    name = row.get("name", email.split("@")[0])
    token = token_from_row(row)
    if not token:
        return {"name": name, "email": email, "state": "no_token"}

    verdict, detail, user, code, retry_after = _session_probe(token)
    attempts = 1
    while verdict == "rate_limited" and attempts < RATE_LIMIT_ATTEMPTS:
        delay = retry_after or min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** (attempts - 1)))
        time.sleep(delay)
        attempts += 1
        verdict, detail, user, code, retry_after = _session_probe(token)
    if verdict == "dead" and attempts < DEAD_CONFIRM_ATTEMPTS:
        time.sleep(DEAD_CONFIRM_DELAY_SEC)
        attempts += 1
        verdict, detail, user, code, retry_after = _session_probe(token)

    if verdict == "rate_limited":
        return {"name": name, "email": email, "state": "rate_limited", "http": code,
                "detail": f"{detail} — still unanswered after {attempts} attempts"}
    if verdict == "network_error":
        return {"name": name, "email": email, "state": "network_error", "detail": detail}
    if verdict == "dead":
        return {"name": name, "email": email, "state": "dead", "http": code,
                "detail": f"{detail} — confirmed over {attempts} attempts"}
    if user["email"].lower() != email.lower():
        # Genuinely different account: usually a cookie-file name collision
        # (save_cookies keys the file on the account name), i.e. this row is holding
        # somebody else's credential. Needs re-pairing, not a refresh.
        return {"name": name, "email": email, "state": "email_mismatch",
                "detail": user["email"]}
    return {"name": name, "email": email, "state": "live", "attempts": attempts,
            "emailVerified": user.get("emailVerified"), "user_id": user.get("id")}


def main():
    parser = argparse.ArgumentParser(description="Audit every conol pool token against get-session")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel requests (default 1; conol rate-limits this endpoint by "
                             "window, and a 429 used to be misrecorded as a dead account)")
    args = parser.parse_args()

    rows = load_pool()
    log.info("auditing %d pool rows with %d workers", len(rows), args.workers)
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(audit_row, rows))

    counts = {}
    for item in results:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    live = [r for r in results if r["state"] == "live"]
    unverified = [r for r in live if r.get("emailVerified") is not True]

    summary = {
        "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.time() - started, 1),
        "rows": len(rows),
        "counts": counts,
        "live": len(live),
        "live_but_not_email_verified": len(unverified),
        "dead": [r["name"] for r in results if r["state"] == "dead"],
        "inconclusive": [r["name"] for r in results
                         if r["state"] in ("rate_limited", "network_error")],
        "no_token": [r["name"] for r in results if r["state"] == "no_token"],
        "details": results,
    }
    OUT_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log.info("LIVE %d / %d rows  | states: %s", len(live), len(rows), counts)
    if unverified:
        log.warning("%d live rows are NOT emailVerified: %s",
                    len(unverified), [r["name"] for r in unverified][:10])
    log.info("report -> %s", OUT_FILE)
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
