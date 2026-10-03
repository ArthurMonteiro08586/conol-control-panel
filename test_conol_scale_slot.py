"""Self-check for conol_scale.py's pool-writer slot oracle and exit-3 handling.

Run: python C:/Users/User/Desktop/_PROJECTS/conol_autoreg/test_conol_scale_slot.py

Isolated from the live campaign: PID_FILE is redirected to a temp path and
registrar_log_fresh is stubbed, because the real run log is being written by a
registrar right now and would make every wait_until_idle() call block for real.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import json
import threading
import time
from pathlib import Path

SRC = Path(r"C:/Users/User/Desktop/_PROJECTS/conol_autoreg/conol_scale.py")
spec = importlib.util.spec_from_file_location("cs", SRC)
assert spec is not None and spec.loader is not None, f"cannot load {SRC}"
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

TMP = Path(tempfile.mkdtemp(prefix="conol_slot_test_"))
cs.PID_FILE = TMP / "conol_register_run.pid"
cs.CHECK_INTERVAL_SEC = 0.2
# Without this the real orphan registrar's fresh log makes every wait block for hours.
cs.registrar_log_fresh = lambda *a, **k: False
# oracle_live() resolves BASE_DIR/"conol_audit_live.json" internally. Point BASE_DIR at
# the temp dir so it NEVER reads the real production report (~4 h old) — otherwise the
# checks pass/fail on live campaign state instead of on the code under test, the same
# coupling that made earlier selfchecks lie.
cs.BASE_DIR = TMP

fails = []


def check(name, cond, extra=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
    if not cond:
        fails.append(name)


def sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    time.sleep(1.5)
    return p


try:
    # 1. no slot file -> nobody holds it
    check("absent slot file -> 0", cs.slot_holder() == 0)

    # 2. a pid left by taskkill /F must read as stale, else the campaign deadlocks
    cs.PID_FILE.write_text("999999\n", encoding="utf-8")
    check("dead pid -> 0 (stale, not a blocker)", cs.slot_holder() == 0)

    # 3. our own pid must not block us
    cs.PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    check("own pid -> 0", cs.slot_holder() == 0)

    # 4. garbage must not raise
    cs.PID_FILE.write_text("not-a-pid\n", encoding="utf-8")
    check("unparsable slot file -> 0, no exception", cs.slot_holder() == 0)

    # 5. a LIVE foreign writer is reported — this is the whole point of the oracle
    live = sleeper()
    cs.PID_FILE.write_text(f"{live.pid}\n", encoding="utf-8")
    check("live foreign pid -> reported", cs.slot_holder() == live.pid, cs.slot_holder())

    # 6. the heartbeat two-line format must still parse the pid off line one
    cs.PID_FILE.write_text(f"{live.pid}\n{int(time.time())}\n", encoding="utf-8")
    check("two-line slot file -> reported", cs.slot_holder() == live.pid)

    # 7. wait_until_idle must BLOCK on a held slot and return once the holder dies.
    #    Before this oracle existed it polled only pid_alive(0) and the log mtime, so a
    #    token refresh mid read-modify-write was invisible and the round started anyway.
    done = threading.Event()

    def waiter():
        cs.wait_until_idle(0)
        done.set()

    th = threading.Thread(target=waiter, daemon=True)
    t0 = time.time()
    th.start()
    time.sleep(1.2)
    check("wait_until_idle blocks while a foreign writer holds the slot", not done.is_set())
    live.kill()
    live.wait()
    th.join(timeout=20)
    check("wait_until_idle returns once the holder dies", done.is_set())
    check(f"wait stayed bounded ({time.time() - t0:.1f}s)", time.time() - t0 < 25)

    # 8. run_registration must no longer publish the child's pid: that write clobbered
    #    a foreign claim before the child could observe it.
    src = SRC.read_text(encoding="utf-8")
    body = src.split("def run_registration", 1)[1].split("\ndef ", 1)[0]
    check("run_registration does not write PID_FILE", "PID_FILE.write_text" not in body)
    check("run_registration does not unlink PID_FILE", "PID_FILE.unlink" not in body)

    # 9. exit 3 must be retried, not fed to the gained==0 breaker
    main_body = src.split("def main(", 1)[1]
    # Anchor on CODE, not prose. The comment above the retry block itself mentions
    # "gained == 0", so a bare substring search matched that comment first and this
    # check failed while the code was correct. A test that measures its own commentary
    # is worse than no test — and it failed loudly, which is the only reason it is
    # worth keeping.
    i3 = main_body.find("while code == 3 and waits < SLOT_RETRY_LIMIT")
    ig = main_body.find("if rows_gained <= 0:")
    check("exit 3 is handled in main()", i3 != -1)
    check("exit-3 handling precedes the rows-appended breaker", 0 <= i3 < ig, f"i3={i3} ig={ig}")
    check("retry count is bounded by SLOT_RETRY_LIMIT", "SLOT_RETRY_LIMIT" in main_body)
    check("SLOT_RETRY_LIMIT is a positive int",
          isinstance(cs.SLOT_RETRY_LIMIT, int) and cs.SLOT_RETRY_LIMIT > 0)

    # 10. oracle_live() — the function that now carries the whole goal fix.
    rep = TMP / "conol_audit_live.json"
    def _write_report(live, rows, inconclusive=0):
        rep.write_text(json.dumps({"live": live, "rows": rows, "inconclusive": inconclusive}),
                       encoding="utf-8")
    now = time.time()
    # fresh valid report -> the int
    _write_report(150, 160)
    rep.touch()
    check("fresh valid report -> live count", cs.oracle_live(0) == 150, cs.oracle_live(0))
    # report older than `since` -> None
    check("report older than since -> None", cs.oracle_live(now + 9999) is None)
    # absent report -> None
    rep.unlink()
    check("absent report -> None", cs.oracle_live(0) is None)
    # too-inconclusive report -> None (live would understate reality and inflate gap)
    _write_report(90, 100, inconclusive=40)   # 40% > 25% limit
    rep.touch()
    check("too-inconclusive report -> None", cs.oracle_live(0) is None)
    # inconclusive within the limit -> the int
    _write_report(90, 100, inconclusive=10)   # 10% <= 25%
    rep.touch()
    check("inconclusive within limit -> live count", cs.oracle_live(0) == 90)
    # non-int live -> None
    _write_report("garbage", 100)
    rep.touch()
    check("non-int live -> None", cs.oracle_live(0) is None)
    # Real report shape: `inconclusive` is a LIST of row names, not an int. The
    # supervisor crashed on `list > float` before _as_count existed, so these cases
    # exercise the production shape, not a convenience int.
    _write_report(90, 100, inconclusive=["r%d" % i for i in range(40)])  # 40 names -> 40%
    rep.touch()
    check("list inconclusive over limit -> None", cs.oracle_live(0) is None)
    _write_report(90, 100, inconclusive=["r0", "r1"])  # 2 names -> 2%
    rep.touch()
    check("list inconclusive within limit -> live count", cs.oracle_live(0) == 90)
    # Hostile report: oracle_live sits on the campaign's critical path and must never
    # raise, whatever shape the report holds. The supervisor died on `list > float`
    # before this; a non-dict root and wrong-typed fields must also degrade to None.
    rep.write_text(json.dumps({"inconclusive": ["x"], "rows": "167", "live": None}),
                   encoding="utf-8")
    rep.touch()
    check("hostile report -> None, no exception", cs.oracle_live(0) is None)
    rep.write_text(json.dumps([1, 2, 3]), encoding="utf-8")  # non-dict root
    rep.touch()
    check("non-dict report root -> None, no exception", cs.oracle_live(0) is None)
    rep.unlink()

    # row_stop_target decides when the campaign stops in the no-oracle fallback, so its
    # arithmetic is on the stop path. The margin must be an integer CEILING of
    # target * ROW_MARGIN_PCT / 100: int(target * 0.1) truncates one row low for some
    # targets because 0.1 is not exact in binary floating point.
    rows, reason = cs.row_stop_target(200, 8)
    check("measured deficit -> target + deficit", rows == 208, rows)
    check("deficit reason names the number", reason.startswith("measured deficit 8"), reason)
    rows, reason = cs.row_stop_target(200, None)
    check("no-oracle margin -> target + 10%", rows == 220, rows)
    check("margin reason states the margin", reason.startswith("documented margin 20"), reason)
    # 205 * 0.10 = 20.5 -> ceil 21; int() would give 20. This is the truncation case.
    check("margin ceilings, not truncates (t=205)", cs.row_stop_target(205, None)[0] == 226,
          cs.row_stop_target(205, None)[0])
    check("margin == ceil for every t in 1..499",
          all(cs.row_stop_target(t, None)[0] - t == -(-t * cs.ROW_MARGIN_PCT // 100)
              for t in range(1, 500)))
    check("deficit path ignores the margin entirely", cs.row_stop_target(50, 3)[0] == 53)
finally:
    if cs.PID_FILE.exists():
        cs.PID_FILE.unlink()
    try:
        TMP.rmdir()
    except OSError:
        pass

print()
print("FAILURES: " + str(fails) if fails else "all blocks passed")
sys.exit(1 if fails else 0)
