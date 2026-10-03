"""chain_conol_scale.py — launch the next conol campaign once the current supervisor exits.

Why this exists: in conol_scale.py, `acquire_lock()` runs BEFORE `wait_until_idle(wait_pid)`,
so a second supervisor started with `--wait-pid <current>` does not wait — it exits 3 at once
(the lock heartbeat is fresh for the whole life of the running supervisor). The chained
campaign would then silently never start. This waiter polls from OUTSIDE the singleton until
the lock is free and the holder pid is dead, then launches the next run.

Usage:
    python chain_conol_scale.py --after-pid 30984 --target 277 --max-rounds 10
    python chain_conol_scale.py --selfcheck       # predicate checks only, launches nothing

Exit codes: 0 = child finished (its own code is logged), 2 = gave up (timeout/retries),
3 = child kept refusing the lock.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCALE = BASE_DIR / "conol_scale.py"
LOCK_FILE = BASE_DIR / "conol_scale.lock"
RUN_LOG = BASE_DIR / "conol_scale_run.log"
CHAIN_LOG = BASE_DIR / "conol_chain.log"

POLL_SEC = 20
# Must exceed conol_scale.LOCK_HEARTBEAT_SEC (240 = CHECK_INTERVAL_SEC * 4), whose beat
# thread writes every ~120 s. If this were tighter than the supervisor's own staleness
# rule, the waiter would call the lock free while acquire_lock() still refuses it, and the
# chained child would burn an exit-3 retry for nothing.
STALE_SEC = 300
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the child must outlive this waiter's console.
# This constant was deleted once by an unrelated edit that widened its replacement range, and
# every check still passed because none of them spawned anything — the failure surfaced only
# when the waiter tried to launch the real campaign, hours later, as a NameError in the log.
DETACHED = 0x00000008 | 0x00000200


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with CHAIN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Fail CLOSED (unknown -> alive): a broken tasklist must not trigger a launch that
    puts a second registrar on one pool file and one Gmail mailbox."""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return b"%d" % pid in out


def lock_state() -> tuple[int, float]:
    """(holder pid, heartbeat age seconds). Age is +inf when there is no usable lock."""
    if not LOCK_FILE.exists():
        return 0, float("inf")
    try:
        body = LOCK_FILE.read_text(encoding="utf-8").split("\n")
        holder = int((body[0] or "0").strip() or 0)
        beat = float((body[1] if len(body) > 1 else "0").strip() or 0)
    except (OSError, ValueError):
        return 0, float("inf")  # unreadable lock must not block the chain forever
    if beat <= 0:
        return holder, float("inf")
    return holder, time.time() - beat


def free_to_launch(after_pid: int) -> tuple[bool, str]:
    """The predicate this whole script exists for: no live predecessor, no fresh lock."""
    if pid_alive(after_pid):
        return False, "predecessor %d alive" % after_pid
    holder, age = lock_state()
    if holder and age < STALE_SEC:
        return False, "lock held by %d, heartbeat %.0fs old" % (holder, age)
    if holder:
        return True, "predecessor dead, lock from %d is stale (%.0fs)" % (holder, age)
    return True, "predecessor dead, no lock"


def _spawn(cmd: list, sink) -> int:
    """Start `cmd` detached with its output tee'd into `sink`, and return its exit code."""
    proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=sink,
                            stderr=subprocess.STDOUT, creationflags=DETACHED)
    log("child pid %d — waiting for it to finish" % proc.pid)
    return proc.wait()


def launch(target: int, max_rounds: int, batch: int) -> int:
    cmd = [sys.executable, "-X", "utf8", str(SCALE),
           "--target", str(target), "--max-rounds", str(max_rounds), "--batch", str(batch)]
    log("launching: " + " ".join(cmd))
    with RUN_LOG.open("a", encoding="utf-8") as sink:
        sink.write("\n=== chained campaign target=%d max-rounds=%d started %s ===\n"
                   % (target, max_rounds, time.strftime("%Y-%m-%d %H:%M:%S")))
        return _spawn(cmd, sink)


def selfcheck() -> int:
    """Exercise the predicate against synthetic states. Launches nothing, touches no real
    lock: LOCK_FILE is redirected to a temp path for the duration."""
    import tempfile

    fails = []
    real_lock = globals()["LOCK_FILE"]
    tmp = Path(tempfile.mkdtemp(prefix="conol_chain_"))
    globals()["LOCK_FILE"] = tmp / "conol_scale.lock"

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
        if not cond:
            fails.append(name)

    try:
        # 1. no lock file at all, dead predecessor -> free
        ok, why = free_to_launch(999999999)
        check("absent lock + dead pid -> free", ok, why)

        # 2. fresh heartbeat from a dead pid -> still blocked (another supervisor may be
        #    mid-shutdown; launching now risks two registrars)
        globals()["LOCK_FILE"].write_text("999999999\n%f\n" % time.time(), encoding="utf-8")
        ok, why = free_to_launch(999999999)
        check("fresh heartbeat blocks even if that pid is dead", not ok, why)

        # 3. stale heartbeat -> free, and the reason says stale
        globals()["LOCK_FILE"].write_text("999999999\n%f\n" % (time.time() - STALE_SEC - 5),
                                          encoding="utf-8")
        ok, why = free_to_launch(999999999)
        check("stale heartbeat -> free", ok, why)
        check("stale reason mentions staleness", "stale" in why, why)

        # 4. corrupt lock body must not wedge the chain
        globals()["LOCK_FILE"].write_text("not-a-pid\ngarbage\n", encoding="utf-8")
        ok, why = free_to_launch(999999999)
        check("corrupt lock -> free (fails open on garbage, not wedged)", ok, why)

        # 5. live predecessor blocks regardless of lock state
        globals()["LOCK_FILE"].unlink()
        me = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6)"])
        time.sleep(1.0)
        ok, why = free_to_launch(me.pid)
        check("live predecessor -> blocked", not ok, why)
        me.terminate()

        # 6. pid_alive fails closed when it cannot tell
        check("pid_alive(0) is False", pid_alive(0) is False)
        check("pid_alive of this interpreter is True", pid_alive(__import__("os").getpid()) is True)

        # 7. The launch path itself. Everything above is a predicate over files and pids;
        # this actually spawns a detached child, which is the only way to catch a NameError
        # inside launch()/_spawn(). A deleted DETACHED constant passed all eight checks above
        # and then killed a real campaign handoff hours later, silently, in the chain log.
        sink_path = tmp / "spawn_sink.log"
        with sink_path.open("a", encoding="utf-8") as sink:
            code = _spawn([sys.executable, "-c",
                           "import sys; print('SINK_PROOF'); sys.exit(7)"], sink)
        check("_spawn returns the child's exit code", code == 7, code)
        check("_spawn tees the child's output into the sink",
              "SINK_PROOF" in sink_path.read_text(encoding="utf-8"),
              sink_path.read_text(encoding="utf-8")[:60])
        check("DETACHED carries both creation flags", DETACHED == 0x8 | 0x200, DETACHED)
        sink_path.unlink(missing_ok=True)
    finally:
        globals()["LOCK_FILE"] = real_lock
        try:
            (tmp / "conol_scale.lock").unlink()
        except OSError:
            pass
        try:
            tmp.rmdir()
        except OSError:
            pass

    print()
    print("FAILURES: " + str(fails) if fails else "all blocks passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="launch the next conol campaign when the current supervisor exits")
    ap.add_argument("--after-pid", type=int, default=0,
                    help="wait for this supervisor pid to die (0 = read the lock holder)")
    ap.add_argument("--target", type=int, default=277,
                    help="target LIVE accounts for the chained campaign")
    ap.add_argument("--max-rounds", type=int, default=10)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--timeout-hours", type=float, default=24.0,
                    help="give up if the predecessor has not exited by then")
    ap.add_argument("--retries", type=int, default=5,
                    help="relaunch attempts after a child exits 3 (lock refused)")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    after = args.after_pid
    if not after:
        after = lock_state()[0]
    if not after:
        log("no predecessor pid given and the lock names none — launching immediately")

    deadline = time.time() + args.timeout_hours * 3600
    while time.time() < deadline:
        ok, why = free_to_launch(after)
        if ok:
            log("free to launch: " + why)
            break
        log("waiting: " + why)
        time.sleep(POLL_SEC)
    else:
        log("TIMEOUT after %.1f h — predecessor never exited; not launching" % args.timeout_hours)
        return 2

    for attempt in range(1, args.retries + 1):
        rc = launch(args.target, args.max_rounds, args.batch)
        log("child exited %d (attempt %d/%d)" % (rc, attempt, args.retries))
        if rc != 3:
            return 0 if rc == 0 else rc
        # A racing chained copy can hold the lock; back off and retry rather than give up.
        wait = 60 * attempt
        log("lock refused (exit 3) — backing off %ds before retry" % wait)
        time.sleep(wait)
    log("gave up after %d lock refusals" % args.retries)
    return 3


if __name__ == "__main__":
    sys.exit(main())
