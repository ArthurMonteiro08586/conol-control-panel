#!/usr/bin/env python3
"""
ENI :: Conol Full Auto-Reg + API Gateway + Quest Farmer
========================================================
Один скрипт — регистрация, сбор квестов, запуск API-шлюза.

Usage:
  python eni_conol.py reg 5       — зарегистрировать 5 аккаунтов
  python eni_conol.py quests      — сфармить квесты на всех аккаунтах
  python eni_conol.py gateway     — запустить API-шлюз
  python eni_conol.py all 10      — зарегистрировать 10, сфармить, запустить шлюз
  python eni_conol.py status      — показать статус пула
  python eni_conol.py test        — протестировать все аккаунты
"""

import asyncio, json, os, sys, time, uuid
from pathlib import Path

# ─── paths ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
ACCOUNTS_FILE = ROOT / "conol_accounts_pool.jsonl"
STATE_FILE = ROOT / "conol_pool_state.json"
QUEST_STATE_FILE = ROOT / "conol_quests_state.json"

# ─── config ─────────────────────────────────────────────────────────────
with open(CFG_PATH) as f:
    CFG = json.load(f)

PASSWORD = CFG["conol"]["password"]
GMAIL = CFG["gmail"]["address"]
GMAIL_APP = CFG["gmail"]["app_password"]
CDP_URL = CFG["cdp"]["url"]
BASE_URL = CFG["conol"]["base_url"]
SITE_KEY = CFG["conol"]["site_key"]
GW_PORT = CFG["gateway"]["port"]
API_KEY = os.environ.get("ENI_POOL_KEY", CFG["gateway"]["api_key"])
NAME_PREFIX = CFG["registration"]["name_prefix"]
EMAIL_PREFIX = CFG["registration"]["email_prefix"]
EMAIL_DOMAIN = CFG["registration"]["email_domain"]
REG_DELAY_BASE = CFG["registration"]["base_delay_sec"]
REG_DELAY_PER = CFG["registration"]["delay_per_account_sec"]
VERIFY_TIMEOUT = CFG["registration"]["verify_timeout_sec"]
QUEST_COOLDOWN_ACCOUNT = CFG["quests"]["cooldown_between_accounts_sec"]
QUEST_COOLDOWN_QUEST = CFG["quests"]["cooldown_between_quests_sec"]


# ═══════════════════════════════════════════════════════════════════════
#  STATUS
# ═══════════════════════════════════════════════════════════════════════
def cmd_status():
    """Показать состояние пула."""
    if not ACCOUNTS_FILE.exists():
        print("❌ Нет файла с аккаунтами. Сначала: python eni_conol.py reg 5")
        return

    accounts = []
    with ACCOUNTS_FILE.open() as f:
        for line in f:
            if line.strip():
                accounts.append(json.loads(line))

    total_cr = sum(a.get("credits", 0) for a in accounts)
    with_cookies = sum(1 for a in accounts if Path(a.get("cookies_path", "")).exists())

    print(f"╔══════════════════════════════╗")
    print(f"║  ENI CONOL AUTOREG STATUS   ║")
    print(f"╠══════════════════════════════╣")
    print(f"║ Аккаунтов в пуле:  {len(accounts):>4}      ║")
    print(f"║ С cookies:          {with_cookies:>4}      ║")
    print(f"║ Общий баланс:       {total_cr:>5} cr  ║")
    print(f"║ Средний баланс:     {total_cr//max(1,len(accounts)):>5} cr  ║")
    print(f"╚══════════════════════════════╝")

    if not with_cookies:
        print("\n⚠️  Нет cookies — запусти регистрацию или перелогинься.")
        return

    # Gateway check
    import httpx
    try:
        r = httpx.get(f"http://127.0.0.1:{GW_PORT}/health", timeout=5)
        if r.status_code == 200:
            d = r.json()
            print(f"\n✅ Шлюз работает: {d['active']}/{d['total']} active, {d['total_requests']} запросов")
            print(f"   API Key: {d['api_key_prefix']}")
            print(f"   Endpoint: http://127.0.0.1:{GW_PORT}/v1/chat/completions")
        else:
            print(f"\n⚠️  Шлюз отвечает но статус {r.status_code}")
    except Exception:
        print(f"\n❌ Шлюз НЕ запущен. Запусти: python eni_conol.py gateway")


# ═══════════════════════════════════════════════════════════════════════
#  TEST — проверка всех аккаунтов
# ═══════════════════════════════════════════════════════════════════════
async def cmd_test():
    """Протестировать ВСЕ аккаунты — кто реально отвечает."""
    import httpx

    if not ACCOUNTS_FILE.exists():
        print("❌ Нет аккаунтов.")
        return

    with ACCOUNTS_FILE.open() as f:
        accounts = [json.loads(l) for l in f if l.strip()]

    print(f"🧪 Тест {len(accounts)} аккаунтов...\n")

    async def test_one(acc, i, total):
        name = acc.get("name", "?")
        email = acc["email"]
        cp = acc.get("cookies_path", "")
        if not cp or not Path(cp).exists():
            print(f"[{i}/{total}] {name} — ❌ no cookies")
            return (name, email, False, "no cookies")

        try:
            with open(cp) as f:
                cookies = {c["name"]: c["value"] for c in json.load(f)}

            async with httpx.AsyncClient(cookies=cookies, timeout=httpx.Timeout(30)) as client:
                r = await client.post(f"{BASE_URL}/api/sessions", json={
                    "source": {"type": "home"},
                    "messages": [{"type": "text", "content": "Say OK"}],
                    "timezone": "Europe/Kyiv",
                    "agentModel": "gpt-5.5",
                    "agentEffort": "low",
                })

                if r.status_code != 201:
                    print(f"[{i}/{total}] {name} — ❌ HTTP {r.status_code}")
                    return (name, email, False, f"HTTP {r.status_code}")

                sid = r.json()["sessionId"]
                full = ""
                async with client.stream("GET", f"{BASE_URL}/api/sessions/{sid}/messages?logDeltas=1") as stream:
                    async for line in stream.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                            if ev.get("type") == "done":
                                break
                            if ev.get("type") == "history_delta":
                                for stage in ev.get("stages", []):
                                    for log in stage.get("logs", []):
                                        for c in log.get("content", []):
                                            if c.get("text"):
                                                full += c["text"]
                        except:
                            pass

                print(f"[{i}/{total}] {name} — ✅ OK ({acc.get('credits',0)} cr)")
                return (name, email, True, "ok")
        except Exception as e:
            print(f"[{i}/{total}] {name} — 💥 {str(e)[:40]}")
            return (name, email, False, str(e)[:60])

    sem = asyncio.Semaphore(3)
    async def bounded(acc, i):
        async with sem:
            return await test_one(acc, i, len(accounts))

    results = await asyncio.gather(*[bounded(acc, i+1) for i, acc in enumerate(accounts)])

    ok = [r for r in results if r[2]]
    fail = [r for r in results if not r[2]]
    print(f"\n✅ {len(ok)}/{len(accounts)} работают")
    if fail:
        print(f"❌ {len(fail)} сломаны:")
        for f in fail:
            print(f"   {f[0]} ({f[1]}) — {f[3]}")


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ═══════════════════════════════════════════════════════════════════════
async def cmd_reg(count: int):
    """Массовая регистрация через Chrome CDP."""
    import email as email_mod, imaplib, quopri, re, ssl
    from playwright.async_api import async_playwright

    print(f"🚀 Регистрация {count} аккаунтов через Chrome CDP ({CDP_URL})")
    print(f"   Email: {EMAIL_PREFIX}XXX@{EMAIL_DOMAIN}")
    print(f"   Пароль: {'*'*len(PASSWORD)}")
    print()

    def wait_for_verify_link(target_alias: str, since_epoch: float) -> str:
        deadline = time.time() + VERIFY_TIMEOUT
        while time.time() < deadline:
            mail = None
            try:
                mail = imaplib.IMAP4_SSL(
                    CFG["gmail"]["imap_host"], CFG["gmail"]["imap_port"],
                    ssl_context=ssl.create_default_context(), timeout=20
                )
                mail.login(GMAIL, GMAIL_APP)
                mail.select("INBOX")
                status, data = mail.search(None, 'FROM "Conol"')
                if status != "OK" or not data or not data[0]:
                    time.sleep(4)
                    continue
                for mid in reversed(data[0].split()[-30:]):
                    status, rows = mail.fetch(mid, "(RFC822)")
                    if status != "OK" or not rows or not isinstance(rows[0], tuple):
                        continue
                    raw = rows[0][1]
                    msg = email_mod.message_from_bytes(raw)
                    to_hdr = str(msg.get("To", ""))
                    if target_alias.lower() not in to_hdr.lower():
                        continue
                    msg_ts = email_mod.utils.mktime_tz(
                        email_mod.utils.parsedate_tz(str(msg.get("Date", "")))
                    )
                    if msg_ts and msg_ts < since_epoch - 30:
                        continue
                    decoded = quopri.decodestring(raw).decode("utf-8", errors="replace")
                    match = re.search(
                        r"https://conol\.ai/api/auth/verify-email\?token=([^&\s\"'<>]+)(?:&amp;|&)callbackURL=([^\s\"'<>]+)",
                        decoded, flags=re.IGNORECASE,
                    )
                    if match:
                        token = match.group(1).replace("=\r\n", "").replace("=\n", "")
                        callback = match.group(2).replace("=\r\n", "").replace("=\n", "")
                        return f"https://conol.ai/api/auth/verify-email?token={token}&callbackURL={callback}"
            except Exception:
                pass
            finally:
                if mail is not None:
                    try: mail.logout()
                    except Exception: pass
            time.sleep(4)
        raise TimeoutError(f"Верификационное письмо не найдено для {target_alias}")

    async def recaptcha(page, action: str) -> str:
        return await page.evaluate(
            """async ({siteKey, action}) => {
                if (!window.grecaptcha) {
                    await new Promise((resolve, reject) => {
                        const old = document.getElementById('eni-recaptcha');
                        if (old) old.remove();
                        const script = document.createElement('script');
                        script.id = 'eni-recaptcha';
                        script.src = `https://www.recaptcha.net/recaptcha/api.js?render=${encodeURIComponent(siteKey)}`;
                        script.onload = resolve;
                        script.onerror = () => reject(new Error('recaptcha load failed'));
                        document.head.appendChild(script);
                    });
                }
                await new Promise((resolve) => window.grecaptcha.ready(resolve));
                return await window.grecaptcha.execute(siteKey, {action});
            }""",
            {"siteKey": SITE_KEY, "action": action},
        )

    async def json_fetch(page, path: str, *, method: str = "GET", body=None, captcha=None):
        return await page.evaluate(
            """async ({path, method, body, captcha}) => {
                const headers = {Accept: 'application/json'};
                if (body !== null) headers['Content-Type'] = 'application/json';
                if (captcha) headers['x-captcha-response'] = captcha;
                const r = await fetch(path, {method, headers, credentials: 'include',
                    body: body === null ? undefined : JSON.stringify(body)});
                const text = await r.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: r.status, url: r.url, text: text.slice(0, 2000), data};
            }""",
            {"path": path, "method": method, "body": body, "captcha": captcha},
        )

    import random

    async def register_one(browser, idx: int, attempt: int = 0) -> dict:
        suffix = int(time.time() * 1000) % 10000000000 + random.randint(100, 999999)
        alias = f"{EMAIL_PREFIX}{suffix}@{EMAIL_DOMAIN}"
        name = f"{NAME_PREFIX}{str(suffix)[-6:]}"
        result = {"idx": idx, "alias": alias, "name": name, "started_at": time.time(), "attempt": attempt}

        # FRESH incognito context per registration — clean fingerprint for reCAPTCHA v3
        context = await browser.new_context(
            user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.{random.randint(1000,9999)}.{random.randint(10,99)} Safari/537.36",
            viewport={"width": 1920, "height": random.randint(900, 1080)},
            locale="en-US",
        )
        page = await context.new_page()
        try:
            # Navigate to sign-up
            await page.goto(f"{BASE_URL}/sign-up", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            # Fill form to trigger reCAPTCHA v3 load
            try:
                await page.fill("#name", name)
                await page.fill("#email", alias)
                await page.fill("#password", PASSWORD)
                await page.fill("#confirmPassword", PASSWORD)
            except Exception:
                pass
            await page.wait_for_timeout(1000 + random.randint(500, 1500))

            # Click Sign up button — this triggers reCAPTCHA v3 script injection
            # (reCAPTCHA loads on submit attempt, not on page load or form fill)
            try:
                btn = page.locator("button:has-text('Sign up')")
                await btn.click(timeout=5000)
            except Exception:
                pass

            # Wait for reCAPTCHA script to load (triggered by submit click)
            await page.wait_for_function("typeof window.grecaptcha !== 'undefined'", timeout=20000)
            await page.wait_for_timeout(1500 + random.randint(500, 1500))

            # Sign Up with retry on captcha
            signup_token = await recaptcha(page, "sign_up")
            result["signup_token_len"] = len(signup_token)
            signup = await json_fetch(page, "/api/invites/register", method="POST", captcha=signup_token, body={
                "token": None, "email": alias, "password": PASSWORD, "name": name, "referrer_share_id": None,
            })
            result["signup"] = signup

            # Detect captcha-specific errors
            signup_err = (signup.get("data") or {}).get("error", "") or (signup.get("data") or {}).get("message", "") or signup.get("text", "")
            is_captcha_err = any(k in str(signup_err).lower() for k in ["captcha", "recaptcha", "robot", "challenge", "unusual"])

            if signup["status"] not in (200, 201):
                if is_captcha_err and attempt < 3:
                    wait = 30 * (attempt + 1) + random.randint(5, 15)
                    print(f"    ⚠️ captcha fail (HTTP {signup['status']}), retry {attempt+1}/3 after {wait}s...")
                    await page.close()
                    await context.close()
                    await asyncio.sleep(wait)
                    return await register_one(browser, idx, attempt + 1)
                result["error"] = f"signup HTTP {signup['status']}: {signup['text'][:300]}"
                return result

            # Send Verification
            verify_token = await recaptcha(page, "send_verification_email")
            verify_send = await json_fetch(page, "/api/auth/send-verification-email", method="POST", captcha=verify_token, body={
                "email": alias, "callbackURL": "/home",
            })
            result["verify_send"] = verify_send
            if verify_send["status"] not in (200, 201):
                verify_err = (verify_send.get("data") or {}).get("error", "") or verify_send.get("text", "")
                is_vcaptcha = any(k in str(verify_err).lower() for k in ["captcha", "recaptcha", "robot"])
                if is_vcaptcha and attempt < 3:
                    wait = 25 * (attempt + 1) + random.randint(5, 10)
                    print(f"    ⚠️ captcha fail on verify, retry {attempt+1}/3 after {wait}s...")
                    await page.close()
                    await context.close()
                    await asyncio.sleep(wait)
                    return await register_one(browser, idx, attempt + 1)
                result["error"] = f"verify-send HTTP {verify_send['status']}: {verify_send['text'][:200]}"
                return result

            # Wait for email + verify
            verify_url = await asyncio.to_thread(wait_for_verify_link, alias, result["started_at"])
            vr = await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
            result["verify_http"] = vr.status if vr else None

            # Login
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500 + random.randint(500, 1500))
            signin_token = await recaptcha(page, "sign_in")
            signin = await json_fetch(page, "/api/auth/sign-in/email", method="POST", captcha=signin_token, body={
                "email": alias, "password": PASSWORD, "callbackURL": "/home", "rememberMe": True,
            })
            result["signin"] = signin

            # Verify session
            session = await json_fetch(page, "/api/auth/get-session")
            result["session_ok"] = session.get("data", {}).get("user", {}).get("email") == alias

            # Balance
            balance = await json_fetch(page, "/api/billing/balance")
            if balance.get("data"):
                result["credits"] = balance["data"].get("total", 0)

            # Save cookies
            cookies = await context.cookies([BASE_URL])
            cookies_path = ROOT / f"cookies_{name.lower()}.json"
            cookies_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
            result["cookies_path"] = str(cookies_path)

            # Save to pool
            account_entry = {
                "email": alias, "password": PASSWORD, "name": name,
                "cookies_path": str(cookies_path), "created_at": time.time(),
                "credits": result.get("credits", 0),
            }
            with ACCOUNTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(account_entry) + "\n")

            result["ok"] = True
        except Exception as e:
            err_str = str(e)[:500]
            # Retry on Playwright connection errors
            if attempt < 2 and any(k in err_str.lower() for k in ["target closed", "connection", "browser disconnected"]):
                print(f"    ⚠️ browser error, retry {attempt+1}/2 after 10s...")
                await asyncio.sleep(10)
                return await register_one(browser, idx, attempt + 1)
            result["error"] = err_str
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass
        return result

    # ── Main loop ──
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        for i in range(count):
            delay = REG_DELAY_BASE + i * REG_DELAY_PER + random.randint(3, 12)
            print(f"[{i+1}/{count}] задержка {delay}с...")
            await asyncio.sleep(delay)
            r = await register_one(browser, i + 1)
            results.append(r)
            ok = "✅ OK" if r.get("ok") else f"❌ FAIL: {r.get('error', '?')}"
            print(f"  [{i+1}/{count}] {r['alias']} → {ok} | credits={r.get('credits','?')}")
            print()

    ok_count = sum(1 for r in results if r.get("ok"))
    total_cr = sum(r.get("credits", 0) for r in results if r.get("ok"))
    STATE_FILE.write_text(json.dumps({
        "target": count, "ok": ok_count, "failed": len(results) - ok_count,
        "total_credits": total_cr, "results": results,
    }, indent=2, ensure_ascii=False))
    print(f"✅ Готово: {ok_count}/{count} OK, +{total_cr} cr")
    print(f"   Файл: {ACCOUNTS_FILE} ({ACCOUNTS_FILE.stat().st_size} байт)")


# ═══════════════════════════════════════════════════════════════════════
#  QUESTS
# ═══════════════════════════════════════════════════════════════════════
def _quest_request(s, method, path, *, json_body=None, timeout=30, max_retries=4):
    """HTTP request with 429 retry + exponential backoff. Returns (response, ok)."""
    for attempt in range(max_retries):
        try:
            if method == "GET":
                r = s.get(f"{BASE_URL}{path}", timeout=timeout)
            else:
                r = s.post(f"{BASE_URL}{path}", json=json_body, timeout=timeout)
            if r.status_code != 429:
                return r, True
            wait = 20 * (2 ** attempt)
            print(f"⏳ 429 rate-limited, wait {wait}s (retry {attempt+1}/{max_retries})...", end=" ", flush=True)
            time.sleep(wait)
        except TimeoutError:
            if attempt == max_retries - 1:
                return None, False
            time.sleep(10)
        except Exception:
            if attempt == max_retries - 1:
                return None, False
            time.sleep(10)
    return None, False


def _run_quest(s, quest_id, prompt, effort="low"):
    """Run one quest via conol agent session. Returns dict result."""
    print(f"    🎯 {quest_id}...", end=" ", flush=True)
    r, ok = _quest_request(s, "POST", "/api/sessions", json_body={
        "source": {"type": "home"},
        "messages": [{"type": "text", "content": prompt}],
        "timezone": "Europe/Kyiv",
        "agentModel": "gpt-5.6-luna",
        "agentEffort": effort,
    }, timeout=30)
    if not ok or r.status_code != 201:
        print(f"❌ session {r.status_code if r else 'timeout'}")
        return {"status": f"session {r.status_code if r else 'timeout'}"}

    sid = r.json()["sessionId"]
    r2, ok = _quest_request(s, "GET", f"/api/sessions/{sid}/messages?logDeltas=1", timeout=180)
    if not ok:
        print("💥 stream timeout")
        return {"status": "stream timeout"}

    full = ""
    for line in r2.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
            if ev.get("type") == "done":
                break
            if ev.get("type") == "history_delta":
                for stage in ev.get("stages", []):
                    for log in stage.get("logs", []):
                        for c in log.get("content", []):
                            if c.get("text"):
                                full = c["text"]
        except Exception:
            pass

    print(f"✅ ({len(full)} chars)")
    return {"status": "ran", "len": len(full), "preview": full[:80]}


def cmd_quests():
    """Сфармить квесты на всех аккаунтах (easy 4 + optional hard 4)."""
    import requests

    EASY_QUESTS = {
        "note_written_by_agent": "Please create a new note titled 'ENI Log' with this content: 'Quest marker — note created by ENI automation.'",
        "memory_written": "Save this fact to your persistent memory: 'ENI automation: quest farming system initialized successfully.' Use the memory tool.",
        "note_edited_by_agent": "Edit the note titled 'ENI Log' — append this line: '\\nUpdated: quest edit complete.'",
        "timer_scheduled": "Schedule a timer/reminder for 5 minutes from now with the message 'ENI quest timer check'. Use your scheduling tool.",
    }
    HARD_QUESTS = {
        "note_shared": "Share the note 'ENI Log' publicly and give me the share link.",
        "skill_installed": "Browse the skill marketplace and install any useful skill. Then confirm what you installed.",
    }
    ALL_QUESTS = {**EASY_QUESTS, **HARD_QUESTS}

    if not ACCOUNTS_FILE.exists():
        print("❌ Нет аккаунтов. Зарегистрируй сначала.")
        return

    with ACCOUNTS_FILE.open() as f:
        accounts = [json.loads(l) for l in f if l.strip()]

    valid = []
    for a in accounts:
        cp = Path(a["cookies_path"])
        if cp.exists() and cp.stat().st_size > 10:
            valid.append(a)
        else:
            print(f"⏭ Пропущен (нет cookies): {a['email']}")

    if not valid:
        print("❌ Нет аккаунтов с cookies.")
        return

    print(f"🎯 Квест-фармер: {len(valid)} аккаунтов\n")

    all_results = []
    total_credits = 0
    total_quests_done = 0

    for i, acc in enumerate(valid):
        email = acc["email"]
        print(f"[{i+1}/{len(valid)}] {email}")

        s = requests.Session()
        with open(acc["cookies_path"]) as cf:
            for c in json.load(cf):
                s.cookies.set(c["name"], c["value"], domain=c.get("domain", "conol.ai"))

        r, ok = _quest_request(s, "GET", "/api/auth/get-session", timeout=15)
        if not ok or r.status_code != 200:
            print(f"  ❌ auth fail: HTTP {r.status_code if r else 'timeout'}")
            all_results.append({"email": email, "error": f"auth {r.status_code if r else 'timeout'}"})
            time.sleep(QUEST_COOLDOWN_ACCOUNT)
            continue
        user = r.json().get("user", {})
        print(f"  Auth: {user.get('name','?')}")

        r, _ = _quest_request(s, "GET", "/api/billing/balance", timeout=15)
        bal_before = r.json().get("total", 0) if r and r.status_code == 200 else 0

        r, _ = _quest_request(s, "GET", "/api/quests", timeout=15)
        quests_data = r.json() if r and r.status_code == 200 else {}
        done_before = sum(1 for q in quests_data.get("quests", []) if q.get("completed"))
        print(f"  Квестов до: {done_before}/{len(ALL_QUESTS)} | Баланс: {bal_before:.0f} cr")

        quest_results = {}
        quests_done_here = 0
        for quest_id, prompt in ALL_QUESTS.items():
            r, _ = _quest_request(s, "GET", "/api/quests", timeout=15)
            qd = r.json() if r and r.status_code == 200 else {}
            if any(q.get("id") == quest_id and q.get("completed") for q in qd.get("quests", [])):
                quest_results[quest_id] = "already_done"
                continue

            effort = "high" if quest_id in HARD_QUESTS else "low"
            result = _run_quest(s, quest_id, prompt, effort=effort)
            quest_results[quest_id] = result

            r, _ = _quest_request(s, "GET", "/api/quests", timeout=15)
            qd = r.json() if r and r.status_code == 200 else {}
            if any(q.get("id") == quest_id and q.get("completed") for q in qd.get("quests", [])):
                quest_results[quest_id] = {"status": "completed", **result} if isinstance(result, dict) else "completed"
                quests_done_here += 1
                total_quests_done += 1

            time.sleep(QUEST_COOLDOWN_QUEST)

        r, _ = _quest_request(s, "GET", "/api/billing/balance", timeout=15)
        bal_after = r.json().get("total", 0) if r and r.status_code == 200 else bal_before
        earned = bal_after - bal_before
        total_credits += earned
        print(f"  +{quests_done_here} квестов | +{earned:.0f} cr | Баланс: {bal_after:.0f} cr\n")

        all_results.append({
            "email": email, "credits_earned": earned,
            "quests_done": quests_done_here, "quests": quest_results,
        })
        time.sleep(QUEST_COOLDOWN_ACCOUNT)

    print(f"\n✅ Фарминг завершён: +{total_credits:.0f} cr | +{total_quests_done} квестов всего")
    QUEST_STATE_FILE.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"   Состояние: {QUEST_STATE_FILE} ({QUEST_STATE_FILE.stat().st_size} байт)")


# ═══════════════════════════════════════════════════════════════════════
#  GATEWAY
# ═══════════════════════════════════════════════════════════════════════
def cmd_gateway():
    """Запустить API-шлюз (foreground)."""
    import uvicorn
    from gateway import app, pool

    print(f"🚀 ENI Conol Pool Gateway")
    print(f"   Порт: {GW_PORT}")
    print(f"   API Key: {API_KEY}")
    print(f"   Аккаунтов: {pool.stats()['active']}/{pool.stats()['total']}")
    print(f"   Endpoint: http://127.0.0.1:{GW_PORT}/v1/chat/completions")
    print()

    uvicorn.run(app, host="127.0.0.1", port=GW_PORT, log_level="info")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════
def print_help():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print_help()
        return

    cmd = sys.argv[1].lower()

    if cmd == "status":
        cmd_status()
    elif cmd == "test":
        asyncio.run(cmd_test())
    elif cmd == "reg":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        asyncio.run(cmd_reg(count))
    elif cmd == "quests":
        cmd_quests()
    elif cmd == "gateway":
        cmd_gateway()
    elif cmd == "all":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        print("═" * 50)
        print("  PHASE 1: REGISTRATION")
        print("═" * 50)
        asyncio.run(cmd_reg(count))
        print("\n" + "═" * 50)
        print("  PHASE 2: QUEST FARMING")
        print("═" * 50)
        cmd_quests()
        print("\n" + "═" * 50)
        print("  PHASE 3: GATEWAY")
        print("═" * 50)
        cmd_gateway()
    else:
        print(f"Неизвестная команда: {cmd}")
        print_help()


if __name__ == "__main__":
    main()
