#!/usr/bin/env python3
"""
ENI :: Multi-Threaded Conol Autoreg v2.0
Reads email_queue.jsonl, registers N accounts per email in parallel.
Each registration → auto-saved to pool → immediately available via gateway API.

Usage:
  python multi_reg.py [accounts_per_email=3] [max_parallel=2]
"""

import asyncio, json, os, sys, time, re, email as email_mod, imaplib, quopri, ssl
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
ACCOUNTS_FILE = ROOT / "conol_accounts_pool.jsonl"
QUEUE_FILE = ROOT / "email_queue.jsonl"
STATE_FILE = ROOT / "multi_reg_state.json"

with open(CFG_PATH) as f:
    CFG = json.load(f)

PASSWORD = CFG["conol"]["password"]
CDP_URL = CFG["cdp"]["url"]
BASE_URL = CFG["conol"]["base_url"]
SITE_KEY = CFG["conol"]["site_key"]
NAME_PREFIX = CFG["registration"]["name_prefix"]
EMAIL_PREFIX = CFG["registration"]["email_prefix"]
EMAIL_DOMAIN = CFG["registration"]["email_domain"]
VERIFY_TIMEOUT = CFG["registration"]["verify_timeout_sec"]


def update_queue_status(email: str, status: str) -> None:
    if not QUEUE_FILE.exists():
        return
    entries = []
    with QUEUE_FILE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e["email"] == email:
                    e["status"] = status
                    e["updated_at"] = time.time()
                entries.append(e)
            except Exception:
                pass
    QUEUE_FILE.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def append_to_pool(account: dict) -> None:
    """Append account to pool + trigger gateway reload."""
    with ACCOUNTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(account) + "\n")
    # Notify gateway to reload
    try:
        import httpx
        httpx.post(f"http://127.0.0.1:9999/pool/reload", timeout=5)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  GMAIL VERIFICATION (thread-pool — parallel across clients)
# ═══════════════════════════════════════════════════════════════════════
def wait_for_verify_link(gmail_addr: str, gmail_app: str,
                         target_alias: str, since_epoch: float) -> str:
    """Blocking IMAP poll for Conol verification email."""
    imap_host = CFG["gmail"]["imap_host"]
    imap_port = CFG["gmail"]["imap_port"]

    deadline = time.time() + VERIFY_TIMEOUT
    while time.time() < deadline:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port,
                                     ssl_context=ssl.create_default_context(),
                                     timeout=20)
            mail.login(gmail_addr, gmail_app)
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
                    email_mod.utils.parsedate_tz(str(msg.get("Date", ""))))
                if msg_ts and msg_ts < since_epoch - 30:
                    continue
                decoded = quopri.decodestring(raw).decode("utf-8", errors="replace")
                match = re.search(
                    r"https://conol\.ai/api/auth/verify-email\?token=([^&\s\"'<>]+)"
                    r"(?:&amp;|&)callbackURL=([^\s\"'<>]+)",
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
                try:
                    mail.logout()
                except Exception:
                    pass
        time.sleep(4)
    raise TimeoutError(f"Verification email not found for {target_alias} after {VERIFY_TIMEOUT}s")


# ═══════════════════════════════════════════════════════════════════════
#  REGISTRATION WORKER (one per client email)
# ═══════════════════════════════════════════════════════════════════════
async def register_batch(gmail_addr: str, gmail_app: str,
                         count: int, sem: asyncio.Semaphore) -> list[dict]:
    """
    Register `count` accounts using one Gmail for verification.
    Uses shared Chrome CDP browser. Handles recaptcha v3.
    """
    from playwright.async_api import async_playwright

    async def get_recaptcha_token(page, action: str = "sign_up") -> str:
        """Execute recaptcha v3 and return token."""
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

    async def api_fetch(page, path: str, *, method: str = "GET",
                        body=None, captcha: str = None) -> dict:
        """Fetch conol.ai API from browser context."""
        return await page.evaluate(
            """async ({path, method, body, captcha}) => {
                const headers = {Accept: 'application/json'};
                if (body !== null) headers['Content-Type'] = 'application/json';
                if (captcha) headers['x-captcha-response'] = captcha;
                const r = await fetch(path, {
                    method, headers, credentials: 'include',
                    body: body === null ? undefined : JSON.stringify(body)
                });
                const text = await r.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: r.status, url: r.url, text: text.slice(0, 2000), data};
            }""",
            {"path": path, "method": method, "body": body, "captcha": captcha},
        )

    results = []

    async with sem:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)

            for i in range(count):
                import random
                suffix = int(time.time() * 1000) % 10000000000 + i + random.randint(100, 999999)
                alias = f"{EMAIL_PREFIX}{suffix}@{EMAIL_DOMAIN}"
                name = f"{NAME_PREFIX}{str(suffix)[-6:]}"
                result = {
                    "email": alias, "name": name, "started_at": time.time(),
                    "ok": False, "attempt": 0,
                }
                print(f"  [{gmail_addr}] Reg {i+1}/{count}: {alias} ...")

                # FRESH incognito context per registration
                context = await browser.new_context(
                    user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.{random.randint(1000,9999)}.{random.randint(10,99)} Safari/537.36",
                    viewport={"width": 1920, "height": random.randint(900, 1080)},
                    locale="en-US",
                )
                page = await context.new_page()
                try:
                    # Navigate and warmup — reCAPTCHA v3 needs time
                    await page.goto(f"{BASE_URL}/sign-up",
                                    wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                    await page.wait_for_function(
                        "typeof window.grecaptcha !== 'undefined' || document.querySelector('script[src*=\"recaptcha\"]')",
                        timeout=15000,
                    )
                    await page.wait_for_timeout(2000 + random.randint(500, 2000))

                    # Step 1: Sign Up with recaptcha
                    signup_token = await get_recaptcha_token(page, "sign_up")
                    signup = await api_fetch(page, "/api/invites/register",
                                             method="POST", captcha=signup_token, body={
                        "token": None, "email": alias, "password": PASSWORD,
                        "name": name, "referrer_share_id": None,
                    })

                    if signup["status"] not in (200, 201):
                        err = signup.get("data", {}).get("error",
                            signup.get("data", {}).get("message",
                            signup["text"][:200]))
                        is_cap = any(k in str(err).lower() for k in ["captcha", "recaptcha", "robot", "challenge"])
                        if is_cap and result.get("attempt", 0) < 3:
                            result["attempt"] = result.get("attempt", 0) + 1
                            wait = 30 * result["attempt"] + random.randint(5, 15)
                            print(f"    ⚠️ captcha fail, retry {result['attempt']}/3 after {wait}s...")
                            await page.close()
                            await context.close()
                            await asyncio.sleep(wait)
                            # Retry — decrement i loop counter manually via recursion-ish
                            # Instead just re-run registration with same index
                            result["error"] = f"captcha retry needed"
                            results.append(result)
                            # Re-attempt inline
                            context = await browser.new_context(
                                user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.{random.randint(1000,9999)}.{random.randint(10,99)} Safari/537.36",
                                viewport={"width": 1920, "height": random.randint(900, 1080)},
                                locale="en-US",
                            )
                            page = await context.new_page()
                            await page.goto(f"{BASE_URL}/sign-up", wait_until="domcontentloaded", timeout=45000)
                            await page.wait_for_timeout(3000)
                            await page.wait_for_function("typeof window.grecaptcha !== 'undefined' || document.querySelector('script[src*=\"recaptcha\"]')", timeout=15000)
                            await page.wait_for_timeout(3000)
                            signup_token = await get_recaptcha_token(page, "sign_up")
                            signup = await api_fetch(page, "/api/invites/register", method="POST", captcha=signup_token, body={
                                "token": None, "email": alias, "password": PASSWORD, "name": name, "referrer_share_id": None,
                            })
                            if signup["status"] not in (200, 201):
                                result["error"] = f"SIGNUP HTTP {signup['status']}: {err}"
                                results[-1] = result
                                await page.close()
                                await context.close()
                                continue
                        else:
                            result["error"] = f"SIGNUP HTTP {signup['status']}: {err}"
                            results.append(result)
                            await page.close()
                            await context.close()
                            continue

                    # Step 2: Send verification email
                    verify_token = await get_recaptcha_token(page, "send_verification_email")
                    verify_send = await api_fetch(
                        page, "/api/auth/send-verification-email",
                        method="POST", captcha=verify_token,
                        body={"email": alias, "callbackURL": "/home"},
                    )

                    if verify_send["status"] not in (200, 201):
                        err = verify_send.get("data", {}).get("error", verify_send["text"][:200])
                        result["error"] = f"VERIFY-SEND HTTP {verify_send['status']}: {err}"
                        results.append(result)
                        await page.close()
                        await context.close()
                        continue

                    # Step 3: Wait for email + click verify link
                    loop = asyncio.get_event_loop()
                    verify_url = await loop.run_in_executor(
                        None, wait_for_verify_link,
                        gmail_addr, gmail_app, alias, result["started_at"],
                    )
                    await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)

                    # Step 4: Login
                    await page.goto(f"{BASE_URL}/login",
                                    wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(1500 + random.randint(500, 1500))
                    signin_token = await get_recaptcha_token(page, "sign_in")
                    await api_fetch(page, "/api/auth/sign-in/email",
                                    method="POST", captcha=signin_token, body={
                        "email": alias, "password": PASSWORD,
                        "callbackURL": "/home", "rememberMe": True,
                    })
                    await page.wait_for_timeout(2000)

                    # Step 5: Verify session
                    session = await api_fetch(page, "/api/auth/get-session")
                    user = (session.get("data") or {}).get("user") or {}
                    result["session_ok"] = user.get("email") == alias

                    if not result["session_ok"]:
                        result["error"] = f"SESSION check failed: got {user.get('email','?')}"
                        results.append(result)
                        await page.close()
                        await context.close()
                        continue

                    # Step 6: Get balance
                    balance = await api_fetch(page, "/api/billing/balance")
                    if balance.get("data"):
                        result["credits"] = balance["data"].get("total", 0)

                    # Step 7: Save cookies
                    cookies = await context.cookies([BASE_URL])
                    cookies_path = ROOT / f"cookies_{name.lower()}.json"
                    cookies_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
                    result["cookies_path"] = str(cookies_path)
                    result["ok"] = True

                    # Step 8: Append to pool + notify gateway
                    pool_entry = {
                        "email": alias, "name": name,
                        "cookies_path": str(cookies_path),
                        "created_at": time.time(),
                        "credits": result.get("credits", 0),
                    }
                    append_to_pool(pool_entry)

                except TimeoutError as e:
                    result["error"] = f"TIMEOUT: {e}"
                except Exception as e:
                    result["error"] = f"{type(e).__name__}: {e}"[:300]
                finally:
                    try:
                        await page.close()
                        await context.close()
                    except Exception:
                        pass

                results.append(result)
                status = "✅ OK" if result.get("ok") else f"❌ {result.get('error','?')[:80]}"
                cr = result.get('credits', '?')
                print(f"    → {status} | credits={cr}")
                await asyncio.sleep(1.5 + random.randint(1, 5))

    return results


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════
async def main():
    accounts_per = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    max_parallel = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    if not QUEUE_FILE.exists():
        print("No email queue. Add via: curl -X POST http://127.0.0.1:9999/queue/add "
              "-H 'Authorization: Bearer test' -H 'Content-Type: application/json' "
              "-d '{\"email\":\"your@gmail.com\",\"password\":\"app-password\"}'")
        return

    with QUEUE_FILE.open(encoding="utf-8") as f:
        queue = [json.loads(l) for l in f if l.strip()]

    pending = [q for q in queue if q.get("status") in (None, "queued")]
    if not pending:
        print("All emails already processed.")
        return

    print("╔════════════════════════════════════════╗")
    print("║  ENI MULTI-REG v2.0                   ║")
    print(f"║  Emails in queue: {len(pending):>3}                 ║")
    print(f"║  Accounts/email:  {accounts_per:>3}                 ║")
    print(f"║  Max parallel:    {max_parallel:>3}                 ║")
    print("╚════════════════════════════════════════╝\n")

    sem = asyncio.Semaphore(max_parallel)
    all_results = {}

    for q in pending[:max_parallel * 2]:
        gmail_addr = q["email"]
        gmail_app = q.get("password") or CFG["gmail"]["app_password"]

        print(f"\n{'='*50}")
        print(f"Client: {gmail_addr}")
        print(f"{'='*50}")
        update_queue_status(gmail_addr, "registering")

        batch_results = await register_batch(gmail_addr, gmail_app, accounts_per, sem)

        ok = sum(1 for r in batch_results if r.get("ok"))
        total_cr = sum(r.get("credits", 0) for r in batch_results if r.get("ok"))
        failed = [r for r in batch_results if not r.get("ok")]

        print(f"\n  Done: {ok}/{accounts_per} OK, +{total_cr} cr")
        if failed:
            print(f"  Failed {len(failed)}:")
            for f in failed:
                print(f"    ❌ {f['email']}: {f.get('error','unknown')[:100]}")

        all_results[gmail_addr] = {"ok": ok, "total": accounts_per, "credits": total_cr}
        update_queue_status(gmail_addr, "registered" if ok > 0 else "failed")

    # Save state
    STATE_FILE.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\n✅ State saved: {STATE_FILE}")

    # Summary
    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    total_ok = sum(r["ok"] for r in all_results.values())
    total_cr = sum(r["credits"] for r in all_results.values())
    total_acc = sum(r["total"] for r in all_results.values())
    for email, r in all_results.items():
        print(f"  {email}: {r['ok']}/{r['total']} accounts | {r['credits']} cr")
    print(f"\n  TOTAL: {total_ok}/{total_acc} accounts | +{total_cr} credits")

    if total_ok > 0:
        print(f"\n  🔑 Gateway API: http://127.0.0.1:9999/v1/chat/completions")
        print(f"  🔑 API Key: test")
        print(f"  🏥 Health: http://127.0.0.1:9999/health")


if __name__ == "__main__":
    asyncio.run(main())
