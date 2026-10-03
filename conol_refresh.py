#!/usr/bin/env python3
"""
conol_refresh.py — Refresh stale session tokens for all accounts in conol_accounts_pool.jsonl.

Strategy: HTTP-only (no browser). Use AntiCaptcha reCAPTCHA v3 → captcha token → POST sign-in → extract cookie.
Fallback: CDP browser (Chrome on :9228) if HTTP fails.

Usage:
    python conol_refresh.py                          # refresh 42 accounts
    python conol_refresh.py --max 3                  # pilot 3 accounts
    python conol_refresh.py --max 3 --cdp            # force CDP mode
    python conol_refresh.py --account conol471446    # single account by name
"""

import json
import os
import sys
import time
import atexit
import logging
from pathlib import Path
from typing import Optional

from conol_register import acquire_pid_slot, release_pid_slot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("conol_refresh")

# ─── paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"
BACKUP_DIR = BASE_DIR / "backups"
CDP_PROFILE = BASE_DIR / "chrome_cdp_profile"
CAPTCHA_SOLVER = BASE_DIR.parent / "conol_github" / "captcha_solver.py"

# credentials load from config.json / env — see conol_secrets.py
from conol_secrets import (  # noqa: E402
    ANTICAPTCHA_KEY, ANTICAPTCHA_BACKUP, SITE_KEY, PASSWORD,
)

POOL_SCHEMA_KEYS = {
    "email", "password", "name", "cookies_path", "created_at",
    "credits", "status", "session_token", "token_expires", "last_login"
}


def solve_captcha(action: str = "sign_in", api_key: str = ANTICAPTCHA_KEY,
                  backup_key: str = ANTICAPTCHA_BACKUP) -> Optional[str]:
    """Solve reCAPTCHA v3 via AntiCaptcha. Returns token or None."""
    import requests

    keys = [api_key, backup_key]
    for key in keys:
        try:
            resp = requests.post("https://api.anti-captcha.com/createTask", json={
                "clientKey": key,
                "task": {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": "https://conol.ai",
                    "websiteKey": SITE_KEY,
                    "pageAction": action,
                    "minScore": 0.3,
                }
            }, timeout=30)
            data = resp.json()
            if data.get("errorId") != 0:
                log.warning("createTask failed (key %s...): %s", key[:8], data.get("errorDescription", "?"))
                continue
            task_id = data["taskId"]
            deadline = time.time() + 90
            while time.time() < deadline:
                r = requests.post("https://api.anti-captcha.com/getTaskResult", json={
                    "clientKey": key, "taskId": task_id
                }, timeout=15).json()
                if r.get("status") == "ready":
                    token = r["solution"].get("gRecaptchaResponse", "")
                    if token:
                        return token
                elif r.get("errorId", 0) != 0:
                    log.warning("getTaskResult error (key %s...): %s", key[:8],
                                r.get("errorDescription", "?"))
                    break
                time.sleep(3)
        except Exception as e:
            log.warning("AntiCaptcha call failed (key %s...): %s", key[:8], e)
    return None


def sign_in(email: str, captcha_token: str) -> Optional[dict]:
    """POST /api/auth/sign-in/email with captcha. Returns session data or None."""
    import requests

    resp = requests.post("https://conol.ai/api/auth/sign-in/email", json={
        "email": email,
        "password": PASSWORD,
        "callbackURL": "/home",
        "rememberMe": True,
    }, headers={
        "x-captcha-response": captcha_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://conol.ai",
        "Referer": "https://conol.ai/login",
    }, timeout=15)

    if resp.status_code != 200:
        log.warning("sign-in HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    data = resp.json()
    session_token = None
    if "__Secure-better-auth.session_token" in resp.cookies:
        session_token = resp.cookies["__Secure-better-auth.session_token"]
    if not session_token and data.get("token"):
        # Fallback: use body token
        session_token = data["token"]

    if not session_token:
        log.warning("No session token in response for %s", email[:30])
        return None

    return {
        "session_token": session_token,
        "user": data.get("user", {}),
        "token_expires": time.time() + 604800,  # 7 days from now
    }


def verify_session(session_token: str) -> Optional[dict]:
    """GET /api/auth/get-session. Returns user data if valid, None if expired."""
    import requests

    resp = requests.get("https://conol.ai/api/auth/get-session",
                        cookies={"__Secure-better-auth.session_token": session_token},
                        headers={"Accept": "application/json"}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("user")
    return None


def get_balance(session_token: str) -> Optional[float]:
    """GET /api/billing/balance. Returns total credits or None."""
    import requests

    resp = requests.get("https://conol.ai/api/billing/balance",
                        cookies={"__Secure-better-auth.session_token": session_token},
                        headers={"Accept": "application/json"}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("total")
    return None


# ─── concurrent-writer detection ────────────────────────────────────────────
REGISTER_LOG = BASE_DIR / "conol_register_run.log"
REGISTER_LOG_WINDOW_SEC = 240


def registrar_log_fresh(window_sec: int = REGISTER_LOG_WINDOW_SEC) -> bool:
    """True if a registrar wrote to its log within `window_sec`.

    Inlined rather than imported from conol_scale: that module calls
    logging.basicConfig() at import time, and importing it above this file's own
    basicConfig would win the root-logger configuration for the whole process.

    The pid slot alone cannot see every writer. A registrar launched before the slot
    protocol existed — or one orphaned by `taskkill /F` of its supervisor, which has no
    job object, so the child outlives the parent — never claims the slot. This script
    reads the WHOLE pool here and rewrites it from that snapshot after tens of minutes
    of token renewal, so overlapping such a registrar erases every row it appended in
    between, with no error anywhere. The log's mtime is the oracle that does see it: a
    registrar writes at least once per attempt (~70-140 s), well inside the window.
    """
    try:
        return (REGISTER_LOG.exists()
                and (time.time() - REGISTER_LOG.stat().st_mtime) < window_sec)
    except OSError:
        return True  # fail closed: an unreadable log must not read as "idle"


def load_pool() -> list[dict]:
    """Load accounts from pool file (tolerant of partial last line)."""
    return _read_pool_tolerant()


def _atomic_replace(tmp_path: Path, dest: Path, attempts: int = 4, delay: float = 0.25):
    """os.replace() over a file another process has open raises PermissionError on
    Windows (Python's open() shares read/write but not delete) — and the ER importer
    does read the pool while a refresh writes it. A failed replace that leaves only
    the .tmp behind is worse than the non-atomic write this replaces, so retry
    briefly and log loudly instead of crashing mid-run.
    """
    for attempt in range(1, attempts + 1):
        try:
            tmp_path.replace(dest)
            return
        except PermissionError as exc:
            if attempt == attempts:
                log.error("could not replace %s after %d attempts (%s) — data is safe in %s",
                          dest.name, attempts, exc, tmp_path.name)
                raise
            log.warning("%s is held by another process, retry %d/%d", dest.name, attempt, attempts)
            time.sleep(delay)


def _read_pool_tolerant() -> list[dict]:
    """Read the pool without dying on a line another writer is mid-append on."""
    rows = []
    if not POOL_FILE.exists():
        return rows
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Writer was mid-append when we read; this is the normal case.
            log.warning("Skipping unparseable line (likely concurrent writer): %s",
                        line[:80])
            continue
    return rows


def save_pool(accounts: list[dict]):
    """Merge-by-email, then write atomically.

    Two independent reasons this is not a plain rewrite:
      - a crash mid-save must not truncate the only copy of the pool;
      - conol_register.py APPENDS to this file while a refresh holds its own
        in-memory list, so writing that list verbatim silently destroys every
        account registered after the refresh started. That happened: a registered
        account's pool line disappeared while its cookie file survived.
    Merging makes lost updates UNLIKELY, not impossible: an append landing between
    the re-read above and the replace below is still discarded, a millisecond-wide
    window. The real invariant is one writer at a time — conol_scale.py enforces it
    with an idleness probe before every round. Incoming records win on field level.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{POOL_FILE}.bak-{ts}"
    if POOL_FILE.exists():
        import shutil
        shutil.copy2(POOL_FILE, backup_path)
        log.info("Backup saved: %s", backup_path)

    merged = {r["email"]: r for r in _read_pool_tolerant() if r.get("email")}
    incoming = {a["email"]: a for a in accounts if a.get("email")}
    rescued = [email for email in merged if email not in incoming]
    if rescued:
        log.warning("merge rescued %d row(s) written by another process: %s",
                    len(rescued), rescued[:4])
    merged.update(incoming)

    rows = list(merged.values())
    tmp_path = POOL_FILE.with_name(POOL_FILE.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for a in rows:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
        f.flush()
    _atomic_replace(tmp_path, POOL_FILE)
    log.info("Pool saved: %d accounts (%d incoming) -> %s", len(rows), len(incoming), POOL_FILE)


def save_cookies(name: str, session_token: str):
    """Save fresh cookies atomically; never wipe an existing file on empty input.

    The previous version opened the target with "w" before validating anything, so a
    failed refresh truncated a working cookie file to `[]` (observed:
    cookies_conol337718.json went 629 B -> 2 B).
    """
    cookies_path = BASE_DIR / f"cookies_{name.lower()}.json"
    if not session_token:
        log.warning("save_cookies: empty token for %s — leaving %s untouched", name, cookies_path.name)
        return str(cookies_path)

    # Store as-is: conol.ai accepts the raw and the percent-encoded form identically
    # (verified 2026-10-03 against get-session, /api/agent-servers and POST /api/sessions).
    cookies = [{
        "name": "__Secure-better-auth.session_token",
        "value": session_token,
        "domain": "conol.ai",
        "path": "/",
        "expires": int(time.time() + 604800),
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }]
    tmp_path = cookies_path.with_name(cookies_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
        f.flush()
    tmp_path.replace(cookies_path)
    return str(cookies_path)


def refresh_account(account: dict, use_cdp: bool = False, max_retries: int = 3) -> dict:
    """Refresh one account. Returns updated account dict or None on failure."""
    email = account["email"]
    name = account["name"]

    log.info("Refreshing %s (%s)...", name, email[:35])

    if use_cdp:
        log.warning("CDP mode not implemented, falling back to HTTP")
        return None

    for attempt in range(max_retries):
        # Alternate keys: try main key first, then backup
        key = ANTICAPTCHA_KEY if attempt % 2 == 0 else ANTICAPTCHA_BACKUP

        # 1. Solve captcha
        captcha_token = solve_captcha("sign_in", api_key=key)
        if not captcha_token:
            log.warning("Captcha solve failed for %s (attempt %d/%d)", name, attempt+1, max_retries)
            continue

        # 2. Sign in
        session_data = sign_in(email, captcha_token)
        if not session_data:
            log.warning("Sign-in failed for %s (attempt %d/%d)", name, attempt+1, max_retries)
            time.sleep(5)
            continue

        session_token = session_data["session_token"]

        # 3. Verify session
        user = verify_session(session_token)
        if not user:
            log.warning("Session verification failed for %s (attempt %d/%d)", name, attempt+1, max_retries)
            continue

        # 4. Check email match
        if user.get("email", "").lower() != email.lower():
            log.warning("Email mismatch: got %s, expected %s (attempt %d/%d)",
                        user.get("email"), email, attempt+1, max_retries)
            continue

        # 5. Get balance
        credits = get_balance(session_token)

        # 6. Save cookies
        cookies_path = save_cookies(name, session_token)

        updated = dict(account)
        updated.update({
            "session_token": session_token,
            "token_expires": time.time() + 604800,
            "last_login": time.time(),
            "status": "live",
            "cookies_path": cookies_path,
            "credits": credits or account.get("credits", 0),
        })

        log.info("✅ %s: session valid, email=%s, credits=%s, token_len=%d (attempt %d/%d)",
                 name, user.get("email"), credits, len(session_token), attempt+1, max_retries)
        return updated

    log.error("All %d attempts failed for %s", max_retries, name)
    return None


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Refresh conol.ai account sessions")
    parser.add_argument("--max", type=int, default=0, help="Max accounts to process (0=all)")
    parser.add_argument("--cdp", action="store_true", help="Use CDP browser mode")
    parser.add_argument("--account", type=str, help="Refresh specific account by name (substring)")
    parser.add_argument("--interval", type=int, default=8, help="Delay between accounts (seconds)")
    parser.add_argument("--force", action="store_true",
                        help="Skip BOTH pool-writer guards: the active-registrar log check "
                             "and the pid-slot claim. Only when you are certain nothing else "
                             "touches conol_accounts_pool.jsonl.")
    args = parser.parse_args()

    all_accounts = load_pool()
    if not all_accounts:
        log.error("No accounts in pool: %s", POOL_FILE)
        return 1

    log.info("Pool has %d accounts", len(all_accounts))

    # One-writer-at-a-time, via TWO independent checks, because the writers do not all
    # speak one protocol. save_pool() rewrites the entire file from the snapshot loaded
    # above, so an overlapping registrar's appends vanish silently.
    if not args.force:
        # 1. Log mtime — sees ANY registrar, including one that never claimed the slot
        #    (orphaned by taskkill /F of its supervisor, or predating the slot).
        if registrar_log_fresh():
            try:
                age = time.time() - REGISTER_LOG.stat().st_mtime
            except OSError:
                age = -1.0
            log.error("A registrar wrote to %s %.0fs ago (window %ds) and is still running. "
                      "It does not hold the pid slot, so the slot alone would not stop us, "
                      "and refreshing now would rewrite the pool from this snapshot and erase "
                      "every account it registers in the meantime. Run this after the current "
                      "round finishes, or pass --force to override both guards.",
                      REGISTER_LOG.name, age, REGISTER_LOG_WINDOW_SEC)
            return 20
        # 2. Pid slot — sees a concurrent refresh and any slot-aware writer.
        if not acquire_pid_slot(owner="token refresher"):
            log.error("Token-refresh slot is held by another pool writer. "
                      "Use --force if you are certain no other writer is active.")
            return 19
        atexit.register(release_pid_slot)

    # Select accounts to process (by index, never lose full list)
    selected_indices = list(range(len(all_accounts)))
    if args.account:
        selected_indices = [i for i, a in enumerate(all_accounts)
                           if args.account.lower() in a["name"].lower()]
        log.info("Filtered to %d accounts matching '%s'", len(selected_indices), args.account)

    max_accounts = args.max if args.max > 0 else len(selected_indices)
    selected_indices = selected_indices[:max_accounts]

    log.info("Processing %d accounts (use_cdp=%s, interval=%ds)...",
             len(selected_indices), args.cdp, args.interval)

    results = {"ok": 0, "failed": 0, "skipped": 0, "details": []}

    for rank, pool_idx in enumerate(selected_indices):
        account = all_accounts[pool_idx]

        # Check if already has a valid session
        existing_token = account.get("session_token")
        token_expires = account.get("token_expires", 0)
        if existing_token and token_expires > time.time() + 3600:
            # Verify existing token first
            user = verify_session(existing_token)
            if user:
                log.info("  ⏭️ %s: existing token still valid, skipping", account["name"])
                results["skipped"] += 1
                results["details"].append({
                    "name": account["name"], "status": "skipped_valid",
                    "email": user.get("email"),
                })
                continue

        result = refresh_account(account, args.cdp)
        if result:
            all_accounts[pool_idx] = result
            results["ok"] += 1
            results["details"].append({
                "name": result["name"], "status": "live",
                "email": result.get("session_email", ""),
                "credits": result.get("credits"),
            })
        else:
            all_accounts[pool_idx] = dict(account)
            all_accounts[pool_idx]["status"] = "failed"
            results["failed"] += 1
            results["details"].append({
                "name": account["name"], "status": "failed",
            })

        # Save progress after each account (full list)
        if rank < len(selected_indices) - 1:
            save_pool(all_accounts)

        # Delay between accounts
        if rank < len(selected_indices) - 1 and args.interval > 0:
            log.info("  Waiting %ds...", args.interval)
            time.sleep(args.interval)

    save_pool(all_accounts)

    # Summary
    log.info("=" * 50)
    log.info("REFRESH COMPLETE: %d OK, %d failed, %d skipped / %d processed",
             results["ok"], results["failed"], results["skipped"], len(selected_indices))
    log.info("=" * 50)

    if results["ok"] == 0 and results["skipped"] == 0 and len(selected_indices) > 0:
        log.error("ZERO accounts refreshed. Critical failure.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())