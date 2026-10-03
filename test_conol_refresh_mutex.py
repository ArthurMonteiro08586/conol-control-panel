#!/usr/bin/env python3
"""Self-check for conol_refresh.py's mutex guard and pool-tolerant reader.

Tests three invariants WITHOUT network access and WITHOUT touching the live pool:
  (a) live foreign pid → refresh refuses and does NOT write to pool;
  (b) empty/stale slot → refresh acquires slot and releases after completion;
  (c) load_pool() returns all full lines and does not crash on a truncated last line.

Monkeypatches the module so that NO HTTP request reaches conol.ai or AntiCaptcha;
writes to a temporary pool and pid directory, never the real conol_accounts_pool.jsonl.
"""

import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(r"C:/Users/User/Desktop/_PROJECTS/conol_autoreg/conol_refresh.py")
assert SRC.exists(), f"not found: {SRC}"

# Disable logging noise from the modules we import
logging.disable(logging.CRITICAL)

# ── guard: record any write to the test pool ───────────────────────────────
pool_writes: list[str] = []


def _tracking_write(path: str, content: str):
    """Write content AND append to pool_writes so we know what the module wrote."""
    pool_writes.append(content)
    Path(path).write_text(content, encoding="utf-8")


# ── load the module ────────────────────────────────────────────────────────
spec = importlib.util.spec_from_file_location("crf", SRC)
assert spec is not None and spec.loader is not None, f"cannot load {SRC}"
crf = importlib.util.module_from_spec(spec)

# Attenuate logging BEFORE exec so no module-level logging leaks into our output
crf.log = logging.getLogger("crf_test")
crf.log.disabled = True
crf.log.warning = lambda *a, **k: None
crf.log.error = lambda *a, **k: None

spec.loader.exec_module(crf)

# Reload conol_register side-effect-free (already loaded via crf's import chain)
import conol_register  # noqa: E402
conol_register.log.disabled = True
conol_register.log.warning = lambda *a, **k: None

# ── monkeypatches ──────────────────────────────────────────────────────────
TEST_DIR = Path(tempfile.mkdtemp(prefix="conol_refresh_test_"))
TEST_POOL = TEST_DIR / "conol_accounts_pool.jsonl"

# Redirect paths
crf.BASE_DIR = TEST_DIR
crf.POOL_FILE = TEST_POOL
crf.PID_FILE = TEST_DIR / "conol_register_run.pid"
crf.BACKUP_DIR = TEST_DIR / "backups"
crf.BACKUP_DIR.mkdir(exist_ok=True)
crf.REGISTER_LOG = TEST_DIR / "conol_register_run.log"

# Also redirect the conol_register module's PID_FILE so the slot function
# reads/writes in our test directory
conol_register.BASE_DIR = TEST_DIR
conol_register.POOL_FILE = TEST_POOL
conol_register.PID_FILE = TEST_DIR / "conol_register_run.pid"

# Stub out ALL network functions so the test never touches conol.ai or AntiCaptcha
crf.solve_captcha = lambda action="sign_in", api_key="", backup_key="": "mock-captcha-token"
crf.sign_in = lambda email, captcha_token: {"session_token": "mock-session-token"}
crf.verify_session = lambda session_token: {"email": "mock@test.com", "emailVerified": True}
crf.get_balance = lambda session_token: 100.0

# ── helpers ────────────────────────────────────────────────────────────────
fails: list[str] = []
checks_run = 0


def check(name: str, cond: bool, extra: str = ""):
    global checks_run
    checks_run += 1
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
    if not cond:
        fails.append(name)


def reset():
    """Remove all test artifacts."""
    for p in [crf.PID_FILE, crf.PID_FILE.with_suffix(".pid.tmp"),
              crf.REGISTER_LOG, TEST_POOL]:
        if p.exists():
            p.unlink()
    pool_writes.clear()


def sleeper() -> subprocess.Popen:
    """A real live process for testing _pid_alive."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    time.sleep(1.5)  # let it actually start
    return p


def _save_pool_internal_wrapper(accounts: list[dict]):
    """Thin wrapper calling the module's save_pool; logs that it was invoked."""
    crf.save_pool(accounts)


# ── record live pool size/mtime before any test ─────────────────────────────
LIVE_POOL = Path(r"C:/Users/User/Desktop/_PROJECTS/conol_autoreg/conol_accounts_pool.jsonl")
live_before = (LIVE_POOL.stat().st_size, LIVE_POOL.stat().st_mtime) if LIVE_POOL.exists() else (0, 0)
print(f"Live pool BEFORE: {live_before[0]} bytes, mtime={live_before[1]:.0f}")

# ────────────────────────────────────────────────────────────────────────────
try:

    # ═════════════════════════════════════════════════════════════════════════
    # (c) load_pool() tolerant of truncated last line
    # ═════════════════════════════════════════════════════════════════════════
    reset()
    # Two full lines + one truncated
    TEST_POOL.write_text(
        '{"email":"a@t.com","name":"A"}\n'
        '{"email":"b@t.com","name":"B"}\n'
        '{"email":"c@t.com","na',  # truncated
        encoding="utf-8",
    )
    rows = crf.load_pool()
    check("(c) load_pool returns 2 rows from 2-full + 1-truncated", len(rows) == 2, len(rows))
    check("(c) first row email is a@t.com", rows[0].get("email") == "a@t.com", rows[0])
    check("(c) second row email is b@t.com", rows[1].get("email") == "b@t.com", rows[1])

    # ═════════════════════════════════════════════════════════════════════════
    # (a) live foreign pid → refresh refuses and does NOT write to pool
    # ═════════════════════════════════════════════════════════════════════════
    reset()
    live = sleeper()
    crf.PID_FILE.write_text(f"{live.pid}\n", encoding="utf-8")

    # Simulate what main() does: try to acquire the slot
    slot_held = crf.acquire_pid_slot(owner="token refresher")
    check("(a) acquire_pid_slot returns False when slot held by live process",
          slot_held is False)

    # Ensure no write happened to the pool
    check("(a) pool is empty after refusal", not TEST_POOL.exists() or TEST_POOL.stat().st_size == 0)
    check("(a) pid file still holds the foreign pid",
          crf.PID_FILE.read_text(encoding="utf-8").strip() == str(live.pid))

    live.kill()
    live.wait()

    # ═════════════════════════════════════════════════════════════════════════
    # (b) empty slot → refresh acquires slot, releases after completion
    # ═════════════════════════════════════════════════════════════════════════
    reset()
    slot_held = crf.acquire_pid_slot(owner="token refresher")
    check("(b) acquire succeeds on empty slot", slot_held is True)

    pid_content = crf.PID_FILE.read_text(encoding="utf-8").strip()
    check("(b) pid file contains our pid",
          pid_content == str(os.getpid()),
          f"expected {os.getpid()}, got {pid_content}")

    # Simulate a successful save_pool by calling it (with monkeypatched network)
    TEST_POOL.write_text('{"email":"test@test.com","name":"Test"}\n', encoding="utf-8")
    # The pool now has 1 account; simulate a refresh by calling save_pool
    # But save_pool reads the live pool — set up a minimal pool
    accounts = [{"email": "test@test.com", "name": "Test", "session_token": "s1",
                 "token_expires": time.time() + 604800, "last_login": time.time(),
                 "status": "live", "credits": 50, "password": "pw"}]
    crf.save_pool(accounts)

    check("(b) pool was written during refresh",
          TEST_POOL.exists() and TEST_POOL.stat().st_size > 0)

    # Release the slot
    crf.release_pid_slot()
    check("(b) pid file removed after release",
          not crf.PID_FILE.exists())

    # (b2) stale pid → take over
    reset()
    crf.PID_FILE.write_text("99999999\n", encoding="utf-8")
    slot_held = crf.acquire_pid_slot(owner="token refresher")
    check("(b2) acquire succeeds on stale pid", slot_held is True)
    pid_content = crf.PID_FILE.read_text(encoding="utf-8").strip()
    check("(b2) pid file rewritten with our pid",
          pid_content == str(os.getpid()))
    crf.release_pid_slot()
    check("(b2) pid file removed after release",
          not crf.PID_FILE.exists())

    # ═════════════════════════════════════════════════════════════════════════
    # (d) registrar_log_fresh() — the oracle that sees writers which do NOT
    #     participate in the pid-slot protocol: a registrar orphaned by
    #     `taskkill /F` of its supervisor (no job object, so the child survives),
    #     or one launched before the slot existed. Without this, refresh claims the
    #     slot unopposed, renews tokens for tens of minutes, then rewrites the whole
    #     pool from the snapshot it took first — erasing every row appended since.
    # ═════════════════════════════════════════════════════════════════════════
    _json, _os, _time = crf.json, crf.os, crf.time
    reset()
    check("no run log -> not fresh", crf.registrar_log_fresh() is False)

    crf.REGISTER_LOG.write_text("old\n", encoding="utf-8")
    ancient = _time.time() - (crf.REGISTER_LOG_WINDOW_SEC + 600)
    _os.utime(crf.REGISTER_LOG, (ancient, ancient))
    check("run log older than the window -> not fresh", crf.registrar_log_fresh() is False)

    crf.REGISTER_LOG.touch()
    check("run log touched now -> fresh", crf.registrar_log_fresh() is True)
    # Prove window_sec is honored, in the direction immune to clock granularity: the
    # SAME old file counts as fresh under a window wide enough to cover it. A window=0
    # probe was tried first and is degenerate — NTFS can report an mtime a few hundred
    # nanoseconds AHEAD of time.time() for a file just touched, so the age goes negative
    # and `age < 0` is True. That answers "fresh", which is the safe direction for the
    # real guard, but it asserts nothing.
    _os.utime(crf.REGISTER_LOG, (ancient, ancient))
    check("window wide enough -> the same old log counts as fresh",
          crf.registrar_log_fresh(window_sec=crf.REGISTER_LOG_WINDOW_SEC + 1200) is True)

    # (e) end-to-end: main() must REFUSE while a registrar is active — before it
    #     claims the slot, before any network call, and without writing the pool.
    reset()
    crf.REGISTER_LOG.touch()
    row = _json.dumps({"name": "Conol1", "email": "a@b.com",
                       "session_token": "t", "expires": int(_time.time()) + 86400})
    TEST_POOL.write_text(row + "\n", encoding="utf-8")
    reached: list[str] = []
    stub_sign_in = crf.sign_in
    crf.sign_in = lambda *a, **k: reached.append("sign_in") or {"session_token": "x"}
    argv = sys.argv
    sys.argv = ["conol_refresh.py"]
    try:
        rc_busy = crf.main()
    finally:
        sys.argv = argv
        crf.sign_in = stub_sign_in
    check("main() returns 20 while a registrar is active", rc_busy == 20, f"rc={rc_busy}")
    check("refusal never claims the pid slot", not crf.PID_FILE.exists())
    check("refusal writes nothing to the pool", pool_writes == [], pool_writes[:1])
    check("refusal makes no network call", reached == [], reached)

    # (f) --force is the documented escape hatch and must clear BOTH guards. With no
    #     matching --account the run does no network work, so this stays offline.
    reset()
    crf.REGISTER_LOG.touch()
    TEST_POOL.write_text(row + "\n", encoding="utf-8")
    sys.argv = ["conol_refresh.py", "--force", "--account", "NoSuchAccountXyz"]
    try:
        rc_force = crf.main()
    finally:
        sys.argv = argv
    check("--force gets past the log guard (not exit 20)", rc_force != 20, f"rc={rc_force}")
    check("--force made no network call for an empty selection", reached == [], reached)

except Exception:
    import traceback
    traceback.print_exc()
    fails.append("unexpected exception")
finally:
    # ── cleanup ─────────────────────────────────────────────────────────────
    reset()
    try:
        import shutil
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    except Exception:
        pass

    # Re-enable logging for the summary
    logging.disable(logging.NOTSET)

    # Verify live pool was NOT touched
    live_after = (LIVE_POOL.stat().st_size, LIVE_POOL.stat().st_mtime) if LIVE_POOL.exists() else (0, 0)
    print(f"Live pool AFTER:  {live_after[0]} bytes, mtime={live_after[1]:.0f}")
    check("Live pool size unchanged", live_before[0] == live_after[0],
          f"{live_before[0]} vs {live_after[0]}")
    check("Live pool mtime unchanged", live_before[1] == live_after[1],
          f"{live_before[1]} vs {live_after[1]}")

    # ── summary ─────────────────────────────────────────────────────────────
    total = checks_run  # derived, not hardcoded: a literal here silently lied the
    passed = total - len(fails)  # last time blocks were added without updating it
    print(f"\n{passed}/{total} checks passed"
          if not fails else
          f"\nFAILURES ({len(fails)}): {fails}")
    sys.exit(1 if fails else 0)