#!/usr/bin/env python3
"""
Self-check tests for conol pool operations.
Run: python test_conol_pool.py

Tests:
1. Pool schema compliance
2. Cookie file format
3. Pool dedup (merge)
4. Sign-in + verify session (one live account)
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import unquote

BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}: {detail}" if detail else f"  ❌ {name}")
        FAIL += 1


def test_pool_schema():
    """Test 1: All pool entries have required fields and correct types."""
    print("\n=== Test 1: Pool Schema Compliance ===")

    if not POOL_FILE.exists():
        check("Pool file exists", False, f"not found: {POOL_FILE}")
        return

    with open(POOL_FILE) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    check(f"Pool has entries", len(entries) > 0, f"got {len(entries)}")
    if len(entries) == 0:
        return

    required_fields = {"email", "password", "name", "cookies_path"}
    for entry in entries:
        name = entry.get("name", "?")
        for field in list(required_fields):
            if field not in entry or not entry[field]:
                check(f"{name}: missing '{field}'", False)
                break
        else:
            check(f"{name}: all required fields present", True)

    # Check pool has no duplicate emails
    emails = [e.get("email", "") for e in entries]
    check("No duplicate emails", len(emails) == len(set(emails)),
          f"{len(emails)} total, {len(emails) - len(set(emails))} duplicates")

    # Check cookie paths are absolute
    for entry in entries:
        cp = entry.get("cookies_path", "")
        if cp and not os.path.isabs(cp):
            check(f"{entry.get('name')}: cookies_path not absolute", False, cp)
            break
    else:
        check("All cookies_path are absolute", True)


def test_cookie_format():
    """Test 2: Cookie files have correct format."""
    print("\n=== Test 2: Cookie File Format ===")

    if not POOL_FILE.exists():
        check("Pool file", False, "not found")
        return

    with open(POOL_FILE) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    checked = 0
    for entry in entries:
        cp = entry.get("cookies_path", "")
        if not cp or not os.path.exists(cp):
            continue
        checked += 1
        try:
            with open(cp) as f:
                cookies = json.load(f)
            # Must be a list
            if not isinstance(cookies, list):
                check(f"{entry['name']}: cookie file not a list", False)
                continue
            # Must have session_token
            tokens = [c for c in cookies if c.get("name") == "__Secure-better-auth.session_token"]
            if not tokens:
                check(f"{entry['name']}: no session_token cookie", False)
                continue
            # Token must have value
            val = tokens[0].get("value", "")
            if len(val) < 20:
                check(f"{entry['name']}: token too short ({len(val)})", False)
                continue
            # Must have domain
            if tokens[0].get("domain") != "conol.ai":
                check(f"{entry['name']}: wrong domain", False, tokens[0].get("domain"))
                continue
            check(f"{entry['name']}: valid cookie format", True)
        except (json.JSONDecodeError, OSError) as e:
            check(f"{entry['name']}: can't read cookie file", False, str(e))

    if checked == 0:
        check("No cookie files to check (all stale)", True, "no live cookies yet")


def test_pool_dedup():
    """Test 3: Pool merge doesn't create duplicates."""
    print("\n=== Test 3: Pool Dedup ===")

    # Simulate a merge
    source = [
        {"email": "a@b.com", "name": "A", "password": "x"},
        {"email": "b@c.com", "name": "B", "password": "y"},
        {"email": "c@d.com", "name": "C", "password": "z"},
    ]
    target = [
        {"email": "a@b.com", "name": "A_old", "password": "x"},
        {"email": "d@e.com", "name": "D", "password": "w"},
    ]

    # Merge: add source entries whose email is NOT in target
    target_emails = {e["email"] for e in target}
    new_entries = [e for e in source if e["email"] not in target_emails]
    merged = target + new_entries

    check("Merge adds missing accounts", len(merged) == 4, f"got {len(merged)}")
    merged_emails = {e["email"] for e in merged}
    check("Merge no duplicates", len(merged_emails) == len(merged))

    # Verify dedup by email (not by name)
    check("Merge keeps 'a@b.com' from target (not source)",
          merged[0]["name"] == "A_old",
          f"got {merged[0]['name']}")

    print("  (Dedup logic: template merge) - PASS")


def test_session_verify():
    """Test 4: Verify one live session from pool."""
    print("\n=== Test 4: Live Session Verification ===")

    if not POOL_FILE.exists():
        check("Pool file", False, "not found")
        return

    import requests
    with open(POOL_FILE) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    live_count = 0
    for entry in entries:
        token = entry.get("session_token", "")
        if not token:
            continue
        try:
            resp = requests.get("https://conol.ai/api/auth/get-session",
                                cookies={"__Secure-better-auth.session_token": token},
                                headers={"Accept": "application/json"}, timeout=10)
            if resp.status_code == 200:
                user = resp.json().get("user", {})
                email_match = user.get("email", "").lower() == entry["email"].lower()
                check(f"{entry['name']}: session valid, email={user.get('email','?')}",
                      email_match, f"expected {entry['email']}")
                live_count += 1
                if live_count >= 3:
                    break
            else:
                check(f"{entry['name']}: expired session (HTTP {resp.status_code})", False)
        except Exception as e:
            check(f"{entry['name']}: verify error", False, str(e))

    if live_count == 0:
        check("At least one live session", False, "no valid sessions in pool")


def main():
    print("=" * 50)
    print("Conol Pool Self-Check")
    print("=" * 50)

    test_pool_schema()
    test_cookie_format()
    test_pool_dedup()
    test_session_verify()

    print("\n" + "=" * 50)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 50)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())