"""conol_captcha.py — reCAPTCHA v3 solver chain for conol.ai registration.

Providers, tried in order:
  1. chrome_cdp  — FREE. Local Chrome with a warm profile renders conol.ai and
     executes window.grecaptcha.execute(siteKey, {action}) over CDP. A real
     browser + aged profile typically scores >= 0.3, which is all conol's
     minScore gate requires. No third-party service, no per-solve cost.
  2. anticaptcha — PAID fallback (existing behaviour), keys from config.json.

Public API (matches what conol_register.py needs):
    solve(action) -> Optional[str]      # token or None
    warmup() -> None                    # optional: pre-launch Chrome + load page

Chrome path/profile/port come from config.json -> captcha section:
    {"chrome_path": "...", "cdp_port": 9228, "profile_dir": "...", "providers": ["chrome_cdp","anticaptcha"]}
Defaults are sane; env overrides: CONOL_CAPTCHA_PROVIDERS (comma list), CONOL_CDP_PORT.

Stdlib + playwright only. The browser is launched ONCE and reused across solves
(page kept on conol.ai); the token is minted per solve with the right action.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent

def _cfg() -> dict:
    p = _ROOT / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}

_C = _cfg().get("captcha", {}) or {}
CONOL = _cfg().get("conol", {}) or {}

SITE_KEY = os.environ.get("CONOL_SITE_KEY") or CONOL.get("site_key", "")
BASE_URL = os.environ.get("CONOL_BASE_URL") or CONOL.get("base_url", "https://conol.ai")

DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PATH = os.environ.get("CONOL_CHROME_PATH") or _C.get("chrome_path") or DEFAULT_CHROME
CDP_PORT = int(os.environ.get("CONOL_CDP_PORT") or _C.get("cdp_port") or 9228)
PROFILE_DIR = _C.get("profile_dir") or str(_ROOT / ".chrome_cdp_profile")

_env_providers = os.environ.get("CONOL_CAPTCHA_PROVIDERS")
if _env_providers:
    PROVIDERS = [p.strip() for p in _env_providers.split(",") if p.strip()]
else:
    PROVIDERS = list(_C.get("providers") or ["chrome_cdp", "anticaptcha"])

ANTICAPTCHA_KEYS = list(_C.get("anticaptcha_keys") or [])

# ---------------------------------------------------------------- chrome_cdp

_pw = None          # playwright instance
_browser = None     # CDP browser handle
_page = None        # tab parked on conol.ai
LAST_PROVIDER = None  # which solver minted the last successful token


def _log(msg: str) -> None:
    print("[captcha] %s" % msg, flush=True)


def _cdp_alive() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _launch_chrome() -> bool:
    """Launch Chrome with remote debugging on CDP_PORT. Reuses a warm profile dir."""
    if _cdp_alive():
        return True
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    cmd = [
        CHROME_PATH,
        "--remote-debugging-port=%d" % CDP_PORT,
        "--user-data-dir=%s" % PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                                       getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except FileNotFoundError:
        _log("chrome not found at %s" % CHROME_PATH)
        return False
    for _ in range(30):
        time.sleep(1)
        if _cdp_alive():
            _log("chrome CDP up on :%d" % CDP_PORT)
            return True
    return False


def _connect():
    """Connect playwright over CDP and park a page on BASE_URL. Lazy + reused."""
    global _pw, _browser, _page
    if _page is not None:
        try:
            _ = _page.url  # cheap liveness probe
            return _page
        except Exception:
            _page = None
    if not _launch_chrome():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log("playwright not installed — chrome_cdp provider unavailable")
        return None
    try:
        if _pw is None:
            _pw = sync_playwright().start()
        if _browser is None:
            # timeout is mandatory here: connect_over_cdp without it can hang
            # forever when Chrome's WS endpoint accepts but never completes the
            # handshake (observed 12-min wedge in an unattended run).
            _browser = _pw.chromium.connect_over_cdp(
                "http://127.0.0.1:%d" % CDP_PORT, timeout=30000)
        ctx = _browser.contexts[0] if _browser.contexts else _browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        _log("tab url: %s" % (page.url or "?")[:80])
        if "conol.ai" not in (page.url or ""):
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
        # make sure grecaptcha is present (conol loads it lazily on auth pages)
        page.evaluate("""(siteKey) => new Promise((resolve, reject) => {
            if (window.grecaptcha) return resolve('already');
            const old = document.getElementById('eni-recaptcha');
            if (old) old.remove();
            const s = document.createElement('script');
            s.id = 'eni-recaptcha';
            s.src = 'https://www.recaptcha.net/recaptcha/api.js?render=' + encodeURIComponent(siteKey);
            s.onload = () => resolve('loaded');
            s.onerror = () => reject(new Error('recaptcha load failed'));
            document.head.appendChild(s);
        })""", SITE_KEY)
        page.wait_for_function("typeof window.grecaptcha !== 'undefined'", timeout=20000)
        _log("grecaptcha ready on %s" % (page.url or "?")[:60])
        _page = page
        return page
    except Exception as e:
        _log("connect failed: %s" % str(e)[:160])
        _page = None
        return None


def solve_chrome_cdp(action: str) -> Optional[str]:
    """Mint one reCAPTCHA v3 token in the live Chrome tab. Returns token or None."""
    page = _connect()
    if page is None:
        return None
    try:
        token = page.evaluate("""([siteKey, action]) => new Promise((resolve, reject) => {
            window.grecaptcha.ready(() => {
                window.grecaptcha.execute(siteKey, {action: action}).then(resolve).catch(reject);
            });
        })""", [SITE_KEY, action])
        if isinstance(token, str) and len(token) > 50:
            return token
        _log("chrome_cdp returned a short/odd token (%r)" % str(token)[:40])
    except Exception as e:
        _log("chrome_cdp solve error: %s" % str(e)[:160])
    return None


def warmup() -> None:
    """Pre-launch Chrome + load conol.ai so the first solve is fast."""
    _connect()


# -------------------------------------------------------------- anticaptcha

def solve_anticaptcha(action: str) -> Optional[str]:
    """Existing paid path: RecaptchaV3TaskProxyless via api.anti-captcha.com."""
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
                _log("createTask fail (%s...): %s" % (key[:8], data.get("errorDescription", "?")))
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
                    _log("getTaskResult error: %s" % r.get("errorDescription", ""))
                    break
                time.sleep(3)
        except Exception as e:
            _log("anticaptcha exception (%s...): %s" % (key[:8], str(e)[:120]))
    return None


# ------------------------------------------------------------------- chain

_SOLVERS = {"chrome_cdp": solve_chrome_cdp, "anticaptcha": solve_anticaptcha}


def solve(action: str, skip: "set|None" = None) -> Optional[str]:
    """Try each configured provider in order; first non-empty token wins.

    `skip` names providers to bypass this round — used to ESCALATE when the
    server rejects a token (CAPTCHA_VERIFICATION_FAILED): a freshly-created
    Chrome profile mints a low reCAPTCHA-v3 score, so we skip chrome_cdp and
    fall through to the paid AntiCaptcha path instead of burning attempts on
    tokens the server keeps rejecting. The provider that produced the last
    token is exposed via LAST_PROVIDER so the caller knows whom to skip.
    """
    skip = skip or set()
    for name in PROVIDERS:
        if name in skip:
            continue
        fn = _SOLVERS.get(name)
        if fn is None:
            _log("unknown captcha provider %r — skipped" % name)
            continue
        tok = fn(action)
        if tok:
            global LAST_PROVIDER
            LAST_PROVIDER = name
            return tok
        _log("provider %s returned nothing for action=%s" % (name, action))
    # every non-skipped provider failed — retry ignoring the skip list once
    if skip:
        _log("all non-skipped providers exhausted; retrying full chain")
        return solve(action, skip=None)
    return None


if __name__ == "__main__":
    # self-test: python conol_captcha.py [action]
    act = sys.argv[1] if len(sys.argv) > 1 else "sign_up"
    t0 = time.time()
    tok = solve(act)
    if tok:
        print("OK action=%s len=%d time=%.1fs providers=%s token=%s...%s"
              % (act, len(tok), time.time() - t0, PROVIDERS, tok[:24], tok[-12:]))
    else:
        print("FAIL action=%s time=%.1fs providers=%s" % (act, time.time() - t0, PROVIDERS))
