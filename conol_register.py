#!/usr/bin/env python3
"""
conol_register.py — Pilot registration of new conol.ai accounts (max 5).

Strategy: HTTP-only registration (AntiCaptcha reCAPTCHA v3 + IMAP verification).
No browser needed. Creates N new accounts via plus-alias.

Usage:
    python conol_register.py --count 5    # pilot 5 accounts (default)
    python conol_register.py --count 1    # single account
"""

import atexit
import subprocess
import json
import os
import time
import logging
import re
import imaplib
import email as email_module
import quopri
import ssl
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import requests

# Log lines carry ✅/❌. When stdout is a file rather than a tty the encoding comes from
# the locale (cp1251/cp866 on this box) and the first emoji raises UnicodeEncodeError,
# killing an unattended run. Setting PYTHONIOENCODING from inside the process is too late
# — the interpreter reads it at startup — so reconfigure the streams directly. The
# supervisor also passes -X utf8 and PYTHONIOENCODING; this covers every other launcher.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("conol_register")

# ─── constants ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"
PID_FILE = BASE_DIR / "conol_register_run.pid"

# credentials load from config.json / env — see conol_secrets.py
from conol_secrets import (  # noqa: E402
    GMAIL, GMAIL_APP, PASSWORD, SITE_KEY, BASE_URL, ANTICAPTCHA_KEYS,
)


def solve_captcha(action: str) -> Optional[str]:
    """Solve reCAPTCHA v3 via AntiCaptcha. Returns token or None."""
    import requests

    for key in ANTICAPTCHA_KEYS:
        try:
            resp = requests.post("https://api.anti-captcha.com/createTask", json={
                "clientKey": key,
                "task": {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": BASE_URL,
                    "websiteKey": SITE_KEY,
                    "pageAction": action,
                    "minScore": 0.3,
                }
            }, timeout=30)
            data = resp.json()
            if data.get("errorId") != 0:
                log.warning("createTask fail (%s...): %s", key[:8], data.get("errorDescription", "?"))
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
                    log.warning("getTaskResult error: %s", r.get("errorDescription", "?"))
                    break
                time.sleep(3)
        except Exception as e:
            log.warning("AntiCaptcha exception (%s...): %s", key[:8], e)
    return None


def new_client() -> "requests.Session":
    """One HTTP session for the WHOLE registration chain.

    better-auth sets a session cookie on POST /api/invites/register and
    /api/auth/send-verification-email only succeeds when that cookie is
    presented. The previous code used a fresh `requests.post` per step, so the
    cookie was dropped and 3/5 pilot accounts died with
    "send verification email failed" even though the account had been created.
    """
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"),
        "Origin": BASE_URL,
    })
    return session


def _log_failure(step: str, resp) -> None:
    """Never swallow a non-2xx: the body is the only diagnostic conol gives us."""
    log.warning("%s failed: HTTP %s %s", step, resp.status_code, resp.text[:220].replace("\n", " "))


def sign_in(email: str, captcha_token: str, client=None) -> Optional[dict]:
    """POST /api/auth/sign-in/email. Returns session data dict or None."""
    http = client or requests
    resp = http.post(f"{BASE_URL}/api/auth/sign-in/email", json={
        "email": email, "password": PASSWORD,
        "callbackURL": "/home", "rememberMe": True,
    }, headers={
        "x-captcha-response": captcha_token,
        "Content-Type": "application/json",
        "Referer": f"{BASE_URL}/login",
    }, timeout=20)

    if resp.status_code != 200:
        _log_failure(f"sign-in {email[:34]}", resp)
        return None

    data = resp.json()
    # The usable credential is the COOKIE value: "<token>.<base64 signature>" (~85
    # chars). The response body's "token" is the bare 32-char session id and
    # get-session rejects it, so it must never be treated as a fallback.
    # With a shared requests.Session the cookie often lands in the session jar
    # rather than resp.cookies, so check both.
    session_token = resp.cookies.get("__Secure-better-auth.session_token")
    if not session_token:
        jar = getattr(http, "cookies", None)
        if jar is not None:
            session_token = jar.get("__Secure-better-auth.session_token")
    if not session_token:
        body_token = data.get("token") or ""
        log.warning("sign-in 200 for %s but no session cookie (body token %d chars is "
                    "unusable for get-session)", email[:34], len(body_token))
        return None

    return {"session_token": session_token, "user": data.get("user", {})}


def register_account(email: str, name: str, captcha_token: str, client=None) -> Optional[dict]:
    """POST /api/invites/register. Returns response dict or {"error": ...}."""
    http = client or requests
    resp = http.post(f"{BASE_URL}/api/invites/register", json={
        "token": None, "email": email, "password": PASSWORD,
        "name": name, "referrer_share_id": None,
    }, headers={
        "x-captcha-response": captcha_token,
        "Content-Type": "application/json",
        "Referer": f"{BASE_URL}/sign-up",
    }, timeout=20)

    if resp.status_code in (200, 201):
        try:
            return resp.json()
        except ValueError:
            return {"ok": True, "raw": resp.text[:200]}
    _log_failure(f"register {email[:34]}", resp)
    try:
        err = resp.json()
        return {"error": err.get("error") or err.get("code") or resp.text[:200]}
    except ValueError:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}


def send_verification_email(email: str, captcha_token: str, client=None) -> bool:
    """POST /api/auth/send-verification-email. Requires the register session cookie."""
    http = client or requests
    resp = http.post(f"{BASE_URL}/api/auth/send-verification-email", json={
        "email": email, "callbackURL": "/home",
    }, headers={
        "x-captcha-response": captcha_token,
        "Content-Type": "application/json",
        "Referer": f"{BASE_URL}/sign-up",
    }, timeout=20)

    if resp.status_code in (200, 201):
        return True
    _log_failure(f"send-verification-email {email[:34]}", resp)
    return False


def wait_for_verify_link(target_alias: str, since_epoch: float, timeout: int = 180) -> Optional[str]:
    """Poll Gmail IMAP for verification link. Returns URL or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                                     ssl_context=ssl.create_default_context(), timeout=20)
            mail.login(GMAIL, GMAIL_APP)
            mail.select("INBOX")
            status, data = mail.search(None, 'FROM "Conol"')
            if status != "OK" or not data or not data[0]:
                time.sleep(4)
                continue
            for message_id in reversed(data[0].split()[-30:]):
                status, rows = mail.fetch(message_id, "(RFC822)")
                if status != "OK" or not rows or not isinstance(rows[0], tuple):
                    continue
                raw = rows[0][1]
                message = email_module.message_from_bytes(raw)
                to_header = str(message.get("To", ""))
                if target_alias.lower() not in to_header.lower():
                    continue
                message_ts = email_module.utils.mktime_tz(
                    email_module.utils.parsedate_tz(str(message.get("Date", ""))))
                if message_ts and message_ts < since_epoch - 30:
                    continue
                decoded = quopri.decodestring(raw).decode("utf-8", errors="replace")
                match = re.search(
                    r"https://conol\.ai/api/auth/verify-email\?token=([^&\s\"'<>]+)(?:&amp;|&)callbackURL=([^\s\"'<>]+)",
                    decoded, flags=re.IGNORECASE)
                if match:
                    token = match.group(1).replace("=\r\n", "").replace("=\n", "")
                    callback = match.group(2).replace("=\r\n", "").replace("=\n", "")
                    return f"https://conol.ai/api/auth/verify-email?token={token}&callbackURL={callback}"
        except:
            pass
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except:
                    pass
        time.sleep(4)
    return None


def verify_email(verify_url: str, client=None) -> bool:
    """GET verify-email URL. Returns True if verification succeeded."""
    http = client or requests
    resp = http.get(verify_url, headers={"Accept": "application/json"},
                    timeout=20, allow_redirects=False)
    if resp.status_code in (200, 302, 307):
        return True
    _log_failure("verify-email", resp)
    return False


def verify_session(session_token: str) -> Optional[dict]:
    """GET /api/auth/get-session. Returns user or None."""
    import requests

    resp = requests.get(f"{BASE_URL}/api/auth/get-session",
                        cookies={"__Secure-better-auth.session_token": session_token},
                        headers={"Accept": "application/json"}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("user")
    return None


def get_balance(session_token: str) -> Optional[float]:
    """GET /api/billing/balance. Returns total or None."""
    import requests

    resp = requests.get(f"{BASE_URL}/api/billing/balance",
                        cookies={"__Secure-better-auth.session_token": session_token},
                        headers={"Accept": "application/json"}, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("total")
    return None


def save_cookies(name: str, session_token: str) -> str:
    """Save cookies atomically; never truncate an existing file on empty input.

    Same defect that was fixed in conol_refresh.py: opening the target with "w"
    before validating anything turned a working cookie file into `[]` on a failed
    refresh (observed: cookies_conol337718.json 629 B -> 2 B).
    """
    cookies_path = BASE_DIR / f"cookies_{name.lower()}.json"
    if not session_token:
        log.warning("save_cookies: empty token for %s — leaving %s untouched", name, cookies_path.name)
        return str(cookies_path)

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


def append_to_pool(entry: dict):
    """Append one account to pool file."""
    with open(POOL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def register_one(suffix: int) -> dict:
    """Register one account. Returns result dict."""
    email = f"baradok609+conol{suffix}@gmail.com"
    # 10 digits, not 6: save_cookies keys the file on this name, so a 6-digit space
    # is only 10^6 and a ~370-attempt campaign has a ~6-7% birthday chance of two
    # pool rows pointing at one cookies_conol*.json (the second silently overwrites
    # the first, and it surfaces later as a token belonging to another account).
    name = f"Conol{str(suffix).zfill(10)[-10:]}"
    started = time.time()
    result = {"name": name, "email": email, "started_at": started}
    captcha_cost = 0

    log.info("=" * 50)
    log.info("Registering %s (%s)", name, email)

    client = new_client()

    # Step 1: register. Retry with a FRESH token per attempt: AntiCaptcha v3
    # tokens are a score lottery, and the same endpoint reliably passes on
    # attempt 2-3. Evidence: sign-in succeeds 32/42 ONLY because it retries,
    # while the single-shot steps failed with 403 CAPTCHA_VERIFICATION_FAILED.
    reg = None
    for attempt in range(1, 4):
        captcha = solve_captcha("sign_up")
        if not captcha:
            log.warning("sign_up captcha solve failed (attempt %d/3)", attempt)
            time.sleep(3)
            continue
        captcha_cost += 1
        candidate = register_account(email, name, captcha, client)
        if candidate is not None and "error" not in candidate:
            reg = candidate
            break
        err_text = str((candidate or {}).get("error", ""))
        log.warning("register attempt %d/3 rejected: %s", attempt, err_text[:160] or "no response")
        if err_text and "CAPTCHA" not in err_text.upper():
            # Registration is not idempotent. A 403 CAPTCHA_VERIFICATION_FAILED happens
            # BEFORE the account exists, so retrying is safe; a 400/409/422 ("email
            # already exists", validation) or a 429 does not benefit from another
            # solve and retrying it just burns budget while masking real state.
            log.error("non-captcha rejection — not retrying: %s", err_text[:160])
            return {**result, "captcha_solves": captcha_cost, "error": err_text[:200]}
        time.sleep(5)
    if reg is None:
        return {**result, "captcha_solves": captcha_cost,
                "error": "register failed after 3 attempts"}

    log.info("✅ Register OK")

    # Step 2: send verification email — same retry discipline, fresh token each time.
    sent = False
    for attempt in range(1, 4):
        captcha = solve_captcha("send_verification_email")
        if not captcha:
            log.warning("send_verification_email captcha solve failed (attempt %d/3)", attempt)
            time.sleep(3)
            continue
        captcha_cost += 1
        if send_verification_email(email, captcha, client):
            sent = True
            break
        log.warning("send-verification attempt %d/3 rejected", attempt)
        time.sleep(5)
    if not sent:
        return {**result, "captcha_solves": captcha_cost,
                "error": "send verification email failed after 3 attempts"}

    log.info("✅ Verification email sent")

    # Step 3: Wait for verify link in Gmail (up to 3 min)
    verify_url = wait_for_verify_link(email, started, 180)
    if not verify_url:
        return {**result, "captcha_solves": captcha_cost,
                "error": "no verify email found in Gmail (3 min timeout)"}

    log.info("✅ Got verify link: %s...", verify_url[:80])

    # Step 4: Verify email via HTTP
    if not verify_email(verify_url, client):
        return {**result, "captcha_solves": captcha_cost, "error": "email verification failed"}

    # Not "Email verified": verify_email() accepts any 200/302/307 without inspecting
    # Location, and better-auth 302s on failure too. The authoritative gate is
    # user.emailVerified from get-session at Step 6 — in a 14-hour unattended log a
    # premature success line is how a batch of unusable accounts goes unnoticed.
    log.info("✅ verify-email responded (authoritative emailVerified check follows at sign-in)")

    # Step 5: Sign in (with retry)
    session_data = None
    for retry in range(3):
        captcha = solve_captcha("sign_in")
        if not captcha:
            continue
        captcha_cost += 1
        session_data = sign_in(email, captcha, client)
        if session_data:
            break
        time.sleep(5)

    if not session_data:
        return {**result, "captcha_solves": captcha_cost,
                "error": "sign-in failed after 3 retries"}

    session_token = session_data["session_token"]

    # Step 6: Verify session. `verify_email()` accepts any 302 without checking
    # Location, so emailVerified here is the only real proof the account is usable
    # rather than merely able to sign in.
    user = verify_session(session_token)
    if not user:
        return {**result, "captcha_solves": captcha_cost,
                "error": "session invalid after sign-in"}

    if user.get("email", "").lower() != email.lower():
        return {**result, "captcha_solves": captcha_cost,
                "error": f"email mismatch: {user.get('email')}"}

    if not user.get("emailVerified"):
        # Registration AND sign-in both worked, so this alias is real and
        # recoverable — dropping it would manufacture another orphan on conol.ai
        # that nobody can find later. Record it as a work-queue row instead:
        # no session_token and no cookie file, so the importer's
        # is_live = bool(session_token and expires > now) keeps it inactive and it
        # never reaches ExtremeRouter as a usable account. A later pass can re-issue
        # send-verification-email for exactly these rows and promote them.
        append_to_pool({
            "name": name, "email": email, "password": PASSWORD,
            "cookies_path": "", "created_at": started,
            "session_token": None, "token_expires": None,
            "last_login": time.time(), "status": "unverified",
            "credits": 0,
        })
        log.warning("%s: session ok but emailVerified=false — queued as 'unverified'", name)
        return {**result, "captcha_solves": captcha_cost,
                "error": "emailVerified=false — queued as unverified for a later pass"}

    # Step 7: Get balance
    credits = get_balance(session_token)
    elapsed = time.time() - started

    # Step 8: Save cookies + pool
    cookies_path = save_cookies(name, session_token)
    pool_entry = {
        "name": name, "email": email, "password": PASSWORD,
        "cookies_path": cookies_path, "created_at": started,
        "session_token": session_token, "token_expires": time.time() + 604800,
        "last_login": time.time(), "status": "live",
        "credits": credits or 0,
    }
    append_to_pool(pool_entry)

    log.info("✅ %s: email=%s, credits=%s, time=%.0fs, captcha_solves=%d",
             name, email, credits, elapsed, captcha_cost)

    return {
        **result, "ok": True, "credits": credits,
        "elapsed_s": round(elapsed, 1), "captcha_solves": captcha_cost,
    }

# ─── unattended-run guards ────────────────────────────────────────────────────
# Budget exhaustion, a dead solver key, Gmail IMAP throttling and an IP block all
# present identically: solve_captcha returns None or the endpoint 403s, per
# account, forever. Without these guards a multi-hour run produces nothing while
# still looking alive.
MAX_CONSECUTIVE_FAILURES = 10
MIN_BUDGET_USD = 1.0
RESULTS_FILE = BASE_DIR / "conol_register_results.jsonl"


def solver_balances() -> dict:
    """Live AntiCaptcha balances — the log must report what can actually be spent,
    not a hardcoded figure from whenever the script was written."""
    out = {}
    for idx, key in enumerate(ANTICAPTCHA_KEYS, start=1):
        try:
            resp = requests.post("https://api.anti-captcha.com/getBalance",
                                 json={"clientKey": key}, timeout=20).json()
            out[f"key{idx}"] = resp.get("balance") if resp.get("errorId") == 0 \
                else f"error:{resp.get('errorCode')}"
        except Exception as exc:  # noqa: BLE001 - a probe must not kill the run
            out[f"key{idx}"] = f"unreachable:{type(exc).__name__}"
    return out


def append_result_line(result: dict):
    """One JSON line per attempt, written as it happens.

    The in-memory `results` list is only summarised after the loop, so a death at
    account 90 used to leave no record of which attempts failed or why.
    """
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({**result, "ts": round(time.time(), 1)}, ensure_ascii=False) + "\n")

# A slot claim is authoritative only while the file is younger than this. The window
# must exceed the LONGEST legitimate hold: a 200-attempt round at ~70-140 s per attempt
# runs up to ~8 h, and conol_refresh rewrites the whole pool over a similar span. A
# window shorter than that would read a legitimate multi-hour holder as stale and
# green-light a second writer — the exact threat the slot exists to stop. A window this
# long is also what caps the worst case of a wedged claim (a pid Windows recycled onto a
# long-lived unrelated process, plus the deliberately fail-closed probe): refusal can
# never outlast the window, so an unattended campaign cannot be bricked by its own guard.
# Project-wide source of truth: conol_scale.slot_holder imports it lazily inside the
# function — a module-level import would let this module's logging.basicConfig win the
# root-logger configuration of whoever imports it.
SLOT_MAX_HOLD_SEC = 12 * 3600


def _pid_alive(pid: int) -> bool:
    """True if `pid` exists. Fail-closed: any probe error means "assume alive".

    Do NOT reach for os.kill(pid, 0) here — on Windows CPython implements that as
    OpenProcess(PROCESS_TERMINATE) + TerminateProcess(handle, sig), so signal 0 would
    KILL the process it was meant to probe. tasklist is the only safe read-only check.
    Its output is decoded defensively because this box emits non-UTF-8 bytes (0xff) in
    some locales, which previously dropped a reader thread and yielded stdout=None; a
    broken probe that reads as "dead" would let a second registrar start next to a
    live one, which is the exact failure this guard exists to prevent.
    """
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                              capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 — probe failure must not look like "dead"
        log.warning("pid probe for %s failed (%s) — assuming alive", pid, exc)
        return True
    out = (proc.stdout or b"").decode("utf-8", "replace")
    # Whole-token match: the Mem Usage column can contain bare digits ("516 K") that a
    # substring search would mistake for a pid.
    return str(pid) in out.split()


def _write_pid_atomic(pid: int) -> None:
    tmp = PID_FILE.with_suffix(".pid.tmp")
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(tmp, PID_FILE)


def acquire_pid_slot(owner: str = "registrar") -> bool:
    """Claim conol_register_run.pid; refuse to start if a live foreign pid holds it.

    This file used to be written only by whoever launched the registrar, so a manual
    launch left a stale pid behind and the supervisor's pid-oracle probed a long-dead
    process forever. That left a SINGLE oracle — the run log's mtime — guarding against
    two registrars appending to one JSONL pool and reading one Gmail mailbox over IMAP.
    Owning the slot here makes the second writer impossible no matter who launches the
    process, which the supervisor-side write cannot do. A pid left behind by taskkill /F
    (no job object, so the child survives its parent, and atexit never runs) is detected
    as stale on the next start and taken over.

    Refusal also requires the claim to be FRESH (mtime < SLOT_MAX_HOLD_SEC): _pid_alive
    fails closed on purpose and Windows recycles pids, so without the age term one stale
    file whose pid got reused by a long-lived unrelated process would refuse every round
    forever — an unattended campaign bricked by its own guard. The holder keeps its claim
    fresh by rewriting the file once per account (see main()).
    """
    me = os.getpid()
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip())
        except (ValueError, OSError):
            other = 0
        try:
            age = time.time() - PID_FILE.stat().st_mtime
        except OSError:
            age = float("inf")  # cannot prove freshness -> treat as abandoned
        if other == me:
            log.info("re-acquiring own pid slot (pid %s)", me)
        elif other and age < SLOT_MAX_HOLD_SEC and _pid_alive(other):
            log.error("another %s (pid %s) already holds the slot (%.0fs old) — refusing "
                      "to start a second writer on %s", owner, other, age, POOL_FILE.name)
            return False
        else:
            reason = (f"held longer than the {SLOT_MAX_HOLD_SEC}s max-hold window"
                      if other and age >= SLOT_MAX_HOLD_SEC else "pid not alive")
            log.warning("taking over pid slot from pid %s (%s)", other, reason)
    _write_pid_atomic(me)
    return True


def release_pid_slot() -> None:
    """Drop the pid file, but only while it still holds OUR pid."""
    me = os.getpid()
    try:
        if (PID_FILE.exists()
                and PID_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip() == str(me)):
            PID_FILE.unlink()
    except OSError as exc:
        log.warning("could not clear pid file: %s", exc)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pilot conol.ai registration")
    parser.add_argument("--count", type=int, default=5, help="Number of accounts to register")
    args = parser.parse_args()

    # Claim the slot before anything touches the pool. atexit (not try/finally around
    # the whole body) keeps the diff to two lines and still covers every clean exit
    # path, including the early `return 2` budget refusal below.
    if not acquire_pid_slot():
        return 3
    atexit.register(release_pid_slot)

    # Cap raised from 5 (pilot) to 200 after the retry fix was measured end-to-end:
    # 1/2 pilot accounts registered fully, 6 solves = $0.06, ~83 s per attempt.
    if args.count > 200:
        log.warning("Capping requested %d accounts to 200 per run.", args.count)
        args.count = 200

    log.info("Pilot registration: %d accounts", args.count)
    budgets = solver_balances()
    log.info("Captcha budget (live getBalance): %s", budgets)
    spendable = sum(v for v in budgets.values() if isinstance(v, (int, float)))
    # Scale the pre-flight floor with the round size: at the measured ~$0.03/attempt
    # (worst case ~$0.09 with every retry firing) a flat $1 floor lets a 200-attempt
    # round start, create aliases, and die on the consecutive-failure breaker — which
    # is exactly the wasted work the floor exists to prevent.
    floor = max(MIN_BUDGET_USD, 0.09 * args.count)
    if spendable < floor:
        log.error("Solver budget $%.4f is below the $%.2f floor for %d attempts — refusing to start",
                  spendable, floor, args.count)
        return 2

    results = []
    consecutive_failures = 0
    t0 = time.time()

    for i in range(args.count):
        # Millisecond resolution (no randomness): collision-free unless two attempts
        # land in the same millisecond, which the 30 s inter-account gap rules out.
        # `name` above keeps 10 digits so the cookie filename space matches.
        suffix = int(time.time() * 1000) % 10_000_000_000
        result = register_one(suffix)
        result["batch_index"] = i
        results.append(result)
        append_result_line(result)
        # Keep the slot claim fresh: acquire_pid_slot's refusal is age-gated, so a long
        # round that never touches this file would eventually read as abandoned and let a
        # second writer in. Refresh the MTIME only, and only while we still own the file:
        # os.utime opens with FILE_WRITE_ATTRIBUTES (no replace), so it does not race the
        # concurrent slot_holder() reader the way an os.replace would raise PermissionError
        # on Windows, and it does not stomp a new holder's claim if we were aged out and
        # someone legitimately took over. A missed refresh is harmless — the age window
        # is 2 orders of magnitude wider than one attempt.
        try:
            if (PID_FILE.exists()
                    and PID_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip() == str(os.getpid())):
                os.utime(PID_FILE, None)
        except OSError as exc:
            log.warning("could not refresh the slot claim: %s", exc)

        if result.get("ok"):
            consecutive_failures = 0
            log.info("  ✅ Account %d/%d registered", i+1, args.count)
        else:
            consecutive_failures += 1
            log.warning("  ❌ Account %d/%d failed: %s", i+1, args.count, result.get("error", "?"))
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("CIRCUIT BREAKER: %d consecutive failures — aborting instead of "
                          "burning budget for hours. Last error: %s",
                          consecutive_failures, result.get("error", "?"))
                break

        if i < args.count - 1:
            delay = 30
            log.info("  Waiting %ds before next account...", delay)
            time.sleep(delay)

    elapsed = time.time() - t0
    ok_count = sum(1 for r in results if r.get("ok"))
    captcha_total = sum(r.get("captcha_solves", 0) for r in results)
    # Divide by what was actually attempted: after a circuit-breaker abort that is
    # fewer than args.count, and the per-account cost matters most in that run.
    avg_time = round(elapsed / len(results), 1) if results else 0

    print("\n" + "=" * 60)
    print(f"PILOT COMPLETE: {ok_count}/{args.count} accounts registered")
    print(f"Total time: {elapsed:.0f}s (avg {avg_time}s per account)")
    print(f"Total captcha solves: {captcha_total}")

    if ok_count > 0:
        live_with_credits = [r for r in results if r.get("ok")]
        total_credits = sum(r.get("credits", 0) or 0 for r in live_with_credits)
        # These are conol credit units, not dollars — the old label rendered a
        # 22k-credit pool as "$22268.00".
        print(f"Total credits earned: {total_credits:.2f} conol credits")
        print(f"Captcha solves: {captcha_total} (estimate at $0.01/solve: ~${captcha_total * 0.01:.2f})")

    print("=" * 60)

    # Detailed failures
    failed = [r for r in results if not r.get("ok")]
    if failed:
        print("\nFAILURES:")
        for r in failed:
            print(f"  ❌ {r['name']} ({r['email']}): {r.get('error', '?')}")
        print()

    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())