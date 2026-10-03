#!/usr/bin/env python3
"""Drive the conol.ai pool to a target size, then keep ExtremeRouter in sync.

Why this exists: conol_register.py caps at 200 attempts per run and only ~half of
the attempts historically produced a usable account, so reaching 200 accounts takes
several rounds. Each round must also be re-imported into ExtremeRouter, because ER
has no auto-refresh for conol-web and only knows what the importer wrote.

Usage:
    python conol_scale.py                 # target 200 LIVE accounts, max 6 rounds
    python conol_scale.py --target 200 --max-rounds 6
    python conol_scale.py --wait-pid 34068   # wait for a run already in flight

Exit codes (from main() return value, passed through sys.exit):
    0  = target reached (audited live >= target)
    1  = campaign did not reach target (audited live < target); restart meaningful
    2  = measurement untrustworthy (no report, unreadable, or too inconclusive to trust);
         success NOT claimed. Re-run once conol's rate limiter clears — a restart is the
         right action for a polluted audit, it re-measures in a quiet window
    3  = lock held by another supervisor
"""
import argparse
import json
import logging
import subprocess
import sys
import threading
import time
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"
PID_FILE = BASE_DIR / "conol_register_run.pid"
SUMMARY_FILE = BASE_DIR / "conol_scale_summary.json"
IMPORTER = Path(r"C:/Users/User/tmp/er-fork/ExtremeRouter/scripts/import-conol-pool.py")
ER_DB = Path(r"C:/Users/User/AppData/Roaming/extremerouter/db/data.sqlite")
LOCK_FILE = BASE_DIR / "conol_scale.lock"
# A round that finds the pool-writer slot taken (registrar exit 3) waits and retries
# this many times before the campaign gives up. Bounded so a wedged holder cannot spin
# the supervisor forever.
SLOT_RETRY_LIMIT = 3
# Ceiling for waits blocked ONLY by the slot oracle (pid and log-mtime oracles idle).
# 40 polls x CHECK_INTERVAL_SEC covers the longest legitimate slot-only hold — a token
# refresh over the whole pool — while registration rounds never wait slot-only because an
# active round keeps the run log fresh and the mtime oracle busy alongside the slot.
SLOT_WAIT_MAX_POLLS = 40
# An audit report that could not decide more than this fraction of its rows is too unsure
# to drive decisions (see oracle_live): undecided rows are not counted as live, so
# trusting the number understates reality and inflates the gap — launching a round that
# overshoots the goal, the mirror image of the short-stop.
ORACLE_MAX_INCONCLUSIVE_FRACTION = 0.25

def _as_count(value) -> int:
    """Coerce an audit-report field to an int count. The report writes `dead`,
    `no_token`, `inconclusive`, `details` as LISTS of row names but `rows`/`live` as
    ints — treating a list as a count raises `list > float` TypeError (which crashed a
    supervisor mid-campaign). len() for lists, int() otherwise, 0 for anything broken."""
    if isinstance(value, list):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
# Margin for the row-bounded stop (no trustworthy audit). live <= rows always, but rows
# may include structurally-dead rows (audit-dead, no_token) that never count as live, so
# stopping at exactly rows == target would land live < target and exit 1. Prefer the
# measured rows-vs-live deficit from the last trusted oracle; otherwise a documented
# fraction of target. Measured basis: 7 of 77 rows were structurally non-live in the
# 03:17 audit (~9%), rounded up to 10%. Only used on the no-oracle fallback path.
ROW_MARGIN_PCT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("conol_scale")

CHECK_INTERVAL_SEC = 60
# A live holder rewrites the lock from a daemon thread (_start_lock_heartbeat) every
# LOCK_HEARTBEAT_SEC/2 for its whole lifetime — including inside run_registration(),
# which blocks in subprocess.run for hours and would otherwise out-silence any
# heartbeat tied to the main loop. So silence for the full window means the holder is
# gone. This is the staleness signal because pid_alive() deliberately fails closed and
# a broken `tasklist` would otherwise make a dead supervisor's lock unbreakable forever.
LOCK_HEARTBEAT_SEC = CHECK_INTERVAL_SEC * 4


def _capture(cmd: list) -> str:
    """Run a probe and decode defensively.

    `tasklist` on this box emits bytes that are not valid UTF-8 (0xff from the
    console codepage), which made text=True raise UnicodeDecodeError inside the
    reader thread and left stdout as None. A probe that crashes must never be
    read as "nothing is running" — that inversion started a second registrar
    alongside a live one, i.e. two writers appending to the same pool file.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=40)
    except Exception as exc:  # noqa: BLE001
        log.warning("probe %s failed: %s", cmd[0], exc)
        return ""
    return (proc.stdout or b"").decode("utf-8", "replace")


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    out = _capture(["tasklist", "/FI", f"PID eq {pid}"])
    if not out:
        # Probe unavailable → assume ALIVE. Waiting is cheap; a duplicate
        # registrar corrupts the pool and burns captcha budget.
        log.warning("pid probe returned nothing for %s — assuming alive", pid)
        return True
    return str(pid) in out


def registrar_log_fresh(path: Path, window_sec: int = 240) -> bool:
    """Second oracle, independent of process tooling: a registrar writes its log
    at least once per account (~70-110 s), so a fresh mtime means one is running."""
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < window_sec
    except OSError:
        return True


def slot_holder() -> int:
    """Live foreign pid holding the pool-writer slot, else 0.

    Third idle oracle. conol_register.py and conol_refresh.py both claim
    conol_register_run.pid before touching the pool — the registrar appends to it,
    refresh rewrites the WHOLE file, so overlapping them silently loses rows. Without
    reading the slot here the supervisor starts a round straight into a refresh and
    only finds out when the child refuses with exit 3, which the round loop used to
    read as "zero accounts gained" and answer by ending the campaign.

    A claim is authoritative only while fresh (same SLOT_MAX_HOLD_SEC the registrar's
    acquire_pid_slot enforces): pid_alive fails closed on purpose and Windows recycles
    pids, so without the age term one stale file whose pid got reused would make the
    supervisor wait forever. Holders keep their claim fresh by rewriting the file while
    they work (the registrar once per account).
    """
    # Lazy on purpose: a module-level import of conol_register would run its
    # logging.basicConfig before this file's own and win the root-logger configuration.
    from conol_register import SLOT_MAX_HOLD_SEC
    try:
        holder = int(PID_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip())
    except (OSError, ValueError):
        return 0
    if not holder or holder == os.getpid():
        return 0
    try:
        age = time.time() - PID_FILE.stat().st_mtime
    except OSError:
        return 0  # cannot prove freshness: the child's own guard still refuses if truly held
    if age >= SLOT_MAX_HOLD_SEC:
        log.info("slot claim from pid %s is %.0fs old (limit %ds) — treating as abandoned",
                 holder, age, SLOT_MAX_HOLD_SEC)
        return 0
    if pid_alive(holder):
        return holder
    return 0


def wait_until_idle(pid: int) -> None:
    """Block until no other pool writer is running, by ANY of three oracles.

    The slot oracle has a ceiling: a holder that never releases would otherwise park the
    supervisor here forever, and SUMMARY_FILE would never be written — no artifact for
    the operator at all. After SLOT_WAIT_MAX_POLLS polls blocked ONLY by the slot (pid
    and log-mtime oracles idle), stop treating the slot as busy: the round's own
    acquire_pid_slot still refuses if the slot is genuinely held (exit 3 -> bounded
    retries -> a traced stop in the summary), so this cannot start a second writer, it
    only converts an infinite wait into a bounded, logged refusal.

    The ceiling NEVER applies while the pid or log-mtime oracles report busy: those are
    what sees the orphan registrar that does not hold the slot, and overriding them would
    start a real second writer.
    """
    run_log = BASE_DIR / "conol_register_run.log"
    holder = slot_holder()
    if holder:
        log.info("pool-writer slot held by pid %s (registrar or token refresh) — waiting", holder)
    log.info("waiting until the in-flight registrar is idle (pid=%s) ...", pid or "-")
    slot_only_polls = 0
    while pid_alive(pid) or registrar_log_fresh(run_log) or slot_holder():
        time.sleep(CHECK_INTERVAL_SEC)
        if pid_alive(pid) or registrar_log_fresh(run_log):
            slot_only_polls = 0
            continue
        if slot_holder():
            slot_only_polls += 1
            if slot_only_polls >= SLOT_WAIT_MAX_POLLS:
                log.warning("slot held for %d polls with no other registrar activity — "
                            "treating it as wedged; a round will be refused with exit 3 "
                            "and traced instead of waiting forever", slot_only_polls)
                return
    log.info("registrar idle — safe to start a round")


def acquire_lock() -> bool:
    """Singleton guard. Two supervisors means two registrars on one pool file and
    one Gmail mailbox — the exact failure this script's idle-wait exists to prevent,
    so it must also prevent a second copy of itself.

    Staleness is decided by HEARTBEAT, not by the pid probe: pid_alive() fails
    closed (a broken `tasklist` reads as "alive"), so a pid-only test would make a
    dead supervisor's lock unbreakable and the tool could never be restarted again.
    A live holder touches the lock every CHECK_INTERVAL_SEC, including while it sits
    in wait_until_idle, so silence for several intervals means it is gone.
    """
    if LOCK_FILE.exists():
        try:
            # First line only: the heartbeat thread writes "<pid>\n<epoch>\n", and
            # int() over the whole body raised ValueError -> holder 0, which killed the
            # re-entry branch and made the refusal log name "pid 0" instead of the real
            # owner — precisely when the message matters.
            holder = int(LOCK_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip() or 0)
        except ValueError:
            holder = 0
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            age = float("inf")
        if holder == os.getpid():
            return True
        if age < LOCK_HEARTBEAT_SEC:
            log.error("another supervisor holds %s (pid %s, heartbeat %.0fs ago) — refusing",
                      LOCK_FILE.name, holder, age)
            return False
        log.warning("lock from pid %s is stale (no heartbeat for %.0fs) — taking over", holder, age)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _start_lock_heartbeat(stop_event: "threading.Event") -> "threading.Thread":
    """Refresh the lock's heartbeat line for the whole life of this process.

    Runs on a daemon thread because run_registration() blocks inside subprocess.run
    for hours: a heartbeat that only fires in wait_until_idle's loop would go stale
    mid-round (240 s window vs a multi-hour round), acquire_lock() would then read the
    lock as abandoned, and a second supervisor would take over — two registrars on one
    pool file and one Gmail mailbox, the exact failure the lock exists to prevent.
    A daemon thread cannot outlive this process, so a stale heartbeat still proves the
    holder is gone.
    """
    pid = os.getpid()
    interval = max(30, LOCK_HEARTBEAT_SEC // 2)

    def beat() -> None:
        # Write before the first wait: acquire_lock() only stored a bare pid, so waiting
        # first would leave the lock without a heartbeat line for a whole interval.
        while True:
            try:
                LOCK_FILE.write_text(f"{pid}\n{time.time():.0f}\n", encoding="utf-8")
            except OSError:
                pass  # a missed beat is harmless; takeover needs the full window
            if stop_event.wait(interval):
                break

    thread = threading.Thread(target=beat, name="lock-heartbeat", daemon=True)
    thread.start()
    return thread


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").split("\n", 1)[0].strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except (OSError, ValueError) as exc:
        log.warning("could not release lock: %s", exc)


def pool_counts() -> dict:
    """Count pool rows and how many hold a non-expired token."""
    rows = []
    for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping unparsable pool line")
    now = time.time()
    live = sum(1 for r in rows if (r.get("token_expires") or 0) > now)
    return {"rows": len(rows), "live": live}


def oracle_live(since: float):
    """Audited live count, but only from a report written at/after `since`.

    The only live number worth stopping on. pool_counts()["live"] merely means a cookie
    file exists whose fabricated `expires` (now + 7 days at sign-in) has not lapsed, so
    it counts rows that never authenticate. The final gate already refuses to claim
    success on it; the loop's stop conditions must refuse too, or the campaign breaks on
    the heuristic (~193 of 200) and returns 1 with rounds left and ~18 accounts short.
    `since` is campaign start, so a report this invocation's sync just wrote qualifies
    and one left over from a previous run does not. Deliberately NOT folded into
    pool_counts(): that feeds the summary's `final_heuristic` next to `final_audited_live`,
    and overloading it would make the two agree, hiding measured-vs-guessed — the exact
    confusion that pushed the 429-as-dead artifact.
    """
    report = BASE_DIR / "conol_audit_live.json"
    try:
        if not (report.exists() and report.stat().st_mtime >= since):
            return None
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("oracle report unreadable (%s) — falling back to heuristic", exc)
        return None
    # Everything past the parse is wrapped: oracle_live sits on the campaign's critical
    # path and must be incapable of raising, whatever shape the report holds. A hostile
    # report (non-dict root, unexpected types) previously killed the supervisor mid-run
    # with `list > float` TypeError. Any anomaly degrades to the heuristic, never a crash.
    try:
        if not isinstance(data, dict):
            log.warning("oracle report root is not a JSON object — falling back")
            return None
        # A report that could not decide a large share of rows must not drive sizing:
        # `live` counts only decisive verdicts, undecided rows sit in `inconclusive`, so
        # a polluted audit understates reality and count = max(10, min(batch, gap*2))
        # would launch a round far past the real need. Reject it and fall back to the
        # heuristic, which under-provisions — the safe direction.
        rows_n = _as_count(data.get("rows"))
        inconclusive = _as_count(data.get("inconclusive"))
        if rows_n and inconclusive > rows_n * ORACLE_MAX_INCONCLUSIVE_FRACTION:
            log.warning("oracle report is %d/%d inconclusive (limit %.0f%%) — too unsure "
                        "to drive decisions, falling back to heuristic",
                        inconclusive, rows_n, ORACLE_MAX_INCONCLUSIVE_FRACTION * 100)
            return None
        live = data.get("live")
        return live if isinstance(live, int) else None
    except Exception as exc:  # noqa: BLE001 — degrade to heuristic, never crash the loop
        log.warning("oracle report malformed (%s) — falling back to heuristic", exc)
        return None


def row_stop_target(target: int, last_oracle_deficit):
    """Rows at which to stop when no trustworthy audit exists. Returns (rows, reason).

    Bounds both failure modes in the fallback state: live <= rows always, so rows below
    target+deficit proves live < target (safe to continue), and reaching it means live may
    be >= target (stop and let the final gate measure). deficit is the last measured
    (rows - live) from a trusted oracle, else a documented fraction of target.
    """
    if last_oracle_deficit is not None:
        return target + last_oracle_deficit, f"measured deficit {last_oracle_deficit}"
    # Integer ceiling (no float): int(target * 0.1) can truncate to one row less when
    # the float product lands just below the integer.
    margin = -(-target * ROW_MARGIN_PCT // 100)
    return target + margin, f"documented margin {margin} ({ROW_MARGIN_PCT}% of target)"


def run_registration(count: int) -> int:
    """One capped registration round. Returns the process exit code.

    Re-checks idleness before EVERY round, not just at startup: two registrars
    appending to one pool file interleave and silently lose rows.
    """
    wait_until_idle(0)
    log.info("registration round: --count %d", count)
    # Append the child's output to the SAME log the idleness oracle watches.
    # Without this a supervisor round inherited the supervisor's handle and wrote
    # to conol_scale.log, so registrar_log_fresh() could not see it: a second
    # supervisor would pass the guard and start a duplicate registrar, and the
    # documented `tail -f conol_register_run.log` would show nothing.
    with open(BASE_DIR / "conol_register_run.log", "a", encoding="utf-8", errors="replace") as sink:
        # Force UTF-8 in the child. With stdout redirected to a file the child's
        # encoding comes from the locale (cp1251/cp866 on this box) instead of a tty,
        # and conol_register.py logs ✅/❌ — so every supervisor round would die with
        # UnicodeEncodeError on its first success line, hours into an unattended run.
        # The manually launched run only survived because git-bash set it up differently.
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-u",
                                  str(BASE_DIR / "conol_register.py"), "--count", str(count)],
                                 cwd=str(BASE_DIR), stdout=sink, stderr=subprocess.STDOUT,
                                 env=child_env)
        # The slot belongs to the CHILD alone: conol_register.py claims
        # conol_register_run.pid at startup and refuses to run while a live foreign pid
        # holds it. Publishing the pid here instead clobbered another writer's claim
        # before the child could observe it — the child then read its OWN pid, took the
        # `other == me` branch, skipped the refusal, and appended to the pool while
        # conol_refresh.py sat mid read-modify-write of the whole file. Rows lost, and
        # the selfcheck stayed green because it tested acquire_pid_slot in isolation.
        # wait_until_idle() now watches the slot as its third oracle, so a round does
        # not even start while another writer holds it.
        rc = child.wait()
    return rc


def run_audit(env: dict) -> bool:
    """Run the throttled audit and prove it actually wrote a NEW report.

    The audit's exit code cannot be used as the success signal: it returns 1 when zero
    accounts are live, so "nothing is live" and "it crashed" are indistinguishable.
    A crashed or timed-out run leaves the PREVIOUS report on disk — the write happens
    only after every row is collected — and that file can easily be younger than the
    importer's staleness limit. That is exactly how pre-fix reports, which recorded
    HTTP 429 as `dead` with a hardcoded `http: 200`, would get trusted again.
    Freshness is therefore measured against this call's start time, not the file's age.
    """
    audit_json = BASE_DIR / "conol_audit_live.json"
    rows = pool_counts().get("rows", 0)
    # Serial and throttled at ~1.2 s per row, plus backoff headroom for rate-limited
    # rows; without a timeout a hung conol edge case stalls the whole supervisor loop,
    # which is the blocking-subprocess pattern one level down.
    timeout = max(300, rows * 10 + 300)
    started = time.time()
    try:
        subprocess.run([sys.executable, "-X", "utf8", "-u",
                        str(BASE_DIR / "conol_audit_live.py"), "--workers", "1"],
                       cwd=str(BASE_DIR), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("audit exceeded %ds — no report was written by this run", timeout)
    except OSError as exc:
        log.warning("audit could not run: %s", exc)
    try:
        fresh = audit_json.exists() and audit_json.stat().st_mtime >= started
    except OSError:
        fresh = False
    if not fresh:
        log.warning("audit produced no new report — the stale file on disk is NOT reused")
    return fresh


def sync_extremerouter() -> None:
    """Re-import the pool so ER sees new accounts and refreshed tokens.

    Runs the throttled audit first and passes --audit only when that audit really
    produced a new report. Without it the importer recomputes isActive from the
    fabricated cookie `expires` (save_cookies writes now + 7 days), so every round
    would overwrite the oracle's verdicts: rows proven dead get resurrected, and rows
    ER parked itself via markAccountUnavailable get reactivated. That is how the 429
    incident's correction would have been silently undone on the very next round.
    """
    if not IMPORTER.exists():
        log.warning("importer missing at %s — skipping ER sync", IMPORTER)
        return
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    # No round is in flight here, so the IP budget is ours alone: keep the audit serial
    # and throttled, or conol answers 429 and every verdict comes back inconclusive.
    fresh = run_audit(env)
    cmd = [sys.executable, "-X", "utf8", str(IMPORTER), "--pool", str(POOL_FILE)]
    if fresh:
        cmd += ["--audit", str(BASE_DIR / "conol_audit_live.json")]
    else:
        log.warning("importing without --audit: the importer keeps the stored isActive "
                    "for rows it cannot verify instead of guessing from cookie expiry")
    proc = subprocess.run(cmd, cwd=str(IMPORTER.parent.parent), env=env)
    log.info("ER sync exit=%d", proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scale the conol pool and keep ER in sync")
    parser.add_argument("--target", type=int, default=200,
                        help="target LIVE accounts (audited). When no trustworthy audit "
                             "exists the loop stops at rows >= target + margin instead, "
                             "conservative because live <= rows")
    parser.add_argument("--max-rounds", type=int, default=6, help="hard stop on registration rounds")
    parser.add_argument("--batch", type=int, default=40,
                        help="attempts per registration round. Default 40: each round is "
                             "re-measured by the audit afterwards, so an undersized round "
                             "costs one extra cycle while a badly-sized large round burns "
                             "Gmail aliases and captcha budget that cannot be recovered. "
                             "max-rounds 6 x batch 40 = 240 attempts (~130 accounts at the "
                             "measured 45% yield); if an audit reveals a large gap, raise "
                             "--max-rounds rather than --batch")
    parser.add_argument("--wait-pid", type=int, default=0, help="wait for this pid before starting")
    args = parser.parse_args()

    wait_pid = args.wait_pid
    if not wait_pid and PID_FILE.exists():
        try:
            wait_pid = int(PID_FILE.read_text().strip())
        except ValueError:
            wait_pid = 0
    if not acquire_lock():
        return 3
    # An exception would leave a stale lock file, but acquire_lock() probes the
    # holder's pid and takes over when it is gone, so that is self-healing.
    stop_beat = threading.Event()
    beat_thread = _start_lock_heartbeat(stop_beat)
    wait_until_idle(wait_pid)

    # Every live number below prefers the audited oracle and falls back to the
    # cookie-expiry heuristic only while no fresh report exists. `basis` records which
    # measurement drove each decision so the summary can tell a measured count from a
    # guessed one — conflating them is how the 429-as-dead artifact got pushed.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    campaign_start = time.time()
    # Establish an oracle baseline BEFORE round 1: otherwise the first iteration has no
    # report and every decision there silently falls back to the inflated heuristic —
    # the exact overestimate this fix exists to stop trusting. No round is in flight yet,
    # which is the quiet window the audit needs to avoid 429s.
    if oracle_live(campaign_start) is None:
        log.info("no fresh audit at campaign start — running a baseline audit")
        run_audit(env)
    history = []
    stop_reason = "max_rounds_exhausted"
    # Deficit (rows - live) from the most recent trusted oracle; sizes the row-bounded
    # stop when no trustworthy audit exists. Updated on every valid oracle reading.
    last_oracle_deficit = None
    for round_no in range(1, args.max_rounds + 1):
        before = pool_counts()
        before_oracle = oracle_live(campaign_start)
        before_live = before_oracle if before_oracle is not None else before["live"]
        basis = "oracle" if before_oracle is not None else "heuristic"
        log.info("round %d/%d — pool rows %d, live %d (%s), target %d",
                 round_no, args.max_rounds, before["rows"], before_live, basis, args.target)
        # Early break decisions. With a trustworthy oracle, break when audited live
        # reaches target. With NO oracle the heuristic overestimates (fabricated
        # expires), so never break on it; instead stop once ROWS reach the margined
        # target, which is conservative because live <= rows always.
        if before_oracle is not None:
            last_oracle_deficit = before["rows"] - before_oracle
            if before_oracle >= args.target:
                log.info("target already met (%d live, audited)", before_oracle)
                stop_reason = "target_reached_audited"
                break
        else:
            stop_rows, margin_reason = row_stop_target(args.target, last_oracle_deficit)
            if before["rows"] >= stop_rows:
                log.info("no trustworthy audit — stopping on rows (%d >= %d, %s); the "
                         "final gate will measure", before["rows"], stop_rows, margin_reason)
                stop_reason = "row_bounded_no_oracle"
                break

        # Size the round off a TRUSTED live number. With no oracle, use ROWS (measurement-
        # free, only grow on successful registration) sized against the margined stop, so
        # the gap does not hit zero several rows early and force every later round onto the
        # count=10 floor. The heuristic overestimates live and would inflate the gap.
        if before_oracle is not None:
            gap = args.target - before_oracle
        else:
            stop_rows, _ = row_stop_target(args.target, last_oracle_deficit)
            gap = max(0, stop_rows - before["rows"])
        count = max(10, min(args.batch, gap * 2))
        code = run_registration(count)
        # Exit 3 = the child refused because another pool writer held the slot. Not a
        # failed round and not a budget problem: letting it fall through to the
        # `gained == 0` breaker below would END the campaign with --max-rounds unused
        # and a log line blaming the budget, so the first token refresh that overlapped
        # a round would leave the operator with a stopped supervisor days later.
        # wait_until_idle() blocks on the slot, so this waits rather than spins.
        waits = 0
        while code == 3 and waits < SLOT_RETRY_LIMIT:
            waits += 1
            log.warning("round %d did not start: another pool writer holds the slot "
                        "(exit 3), wait %d/%d — waiting for it to free",
                        round_no, waits, SLOT_RETRY_LIMIT)
            wait_until_idle(0)
            code = run_registration(count)
        if code == 3:
            # Record the refusal so the summary shows WHY fewer rounds ran: a round that
            # never started is not gained==0 (a registration failure), and without this
            # row the campaign would exit with max-rounds unused and leave no trace.
            history.append({"round": round_no, "exit": 3, "gained": None,
                            "rows_gained": None, "basis": "refused",
                            "slot_waits": waits, "rows": before["rows"],
                            "live": before_live})
            log.error("slot still held after %d waits — stopping instead of fighting "
                      "the other writer", SLOT_RETRY_LIMIT)
            stop_reason = "slot_refused"
            break

        sync_extremerouter()

        # sync_extremerouter just ran the audit, so the oracle now covers every row
        # including this round's. Measure the outcome on it, falling back to the
        # heuristic only if the audit produced no report.
        after = pool_counts()
        after_oracle = oracle_live(campaign_start)
        if after_oracle is not None:
            last_oracle_deficit = after["rows"] - after_oracle
        after_live = after_oracle if after_oracle is not None else after["live"]
        basis = "oracle" if after_oracle is not None else "heuristic"
        gained = after_live - before_live
        rows_gained = after["rows"] - before["rows"]
        log.info("round %d done: exit=%d, +%d rows / %+d live (pool %d, live %d, %s)",
                 round_no, code, rows_gained, gained, after["rows"], after_live, basis)
        history.append({"round": round_no, "exit": code, "gained": gained,
                        "rows_gained": rows_gained, "basis": basis,
                        "rows": after["rows"], "live": after_live})

        # Breaker on ROWS APPENDED, not on the live delta: a partially rate-limited
        # audit records those rows as `rate_limited`, not `live`, so the oracle live
        # count can be flat or lower than the previous round's even though the round
        # registered accounts. Firing the breaker on a live-delta of zero would end the
        # campaign with "zero accounts" the moment an audit catches 429s — the same
        # trap that parked 15 working accounts earlier. Rows are immune to both the
        # fabricated expires and the audit verdicts.
        if rows_gained <= 0:
            log.error("round appended zero rows — stopping instead of burning budget")
            stop_reason = "round_appended_nothing"
            break
        # Target break ONLY on a measured oracle, for the same reason as the top-of-loop
        # break: a heuristic-only break would stop on a guess.
        if after_oracle is not None and after_oracle >= args.target:
            log.info("target reached: %d live accounts (audited)", after_oracle)
            stop_reason = "target_reached_audited"
            break
        # No trustworthy audit: stop once ROWS reach the margined target. live <= rows
        # always, so rows below it prove the goal is unmet (continue) and reaching it
        # means live may be >= target (stop, let the final gate measure). The margin
        # covers structurally-dead rows that never count as live. This caps a
        # persistently polluted audit from running every round at count 10 and
        # overshooting an already-met target, and it cannot short-stop.
        if after_oracle is None:
            stop_rows, margin_reason = row_stop_target(args.target, last_oracle_deficit)
            if after["rows"] >= stop_rows:
                log.info("no trustworthy audit — stopping on rows (%d >= %d, %s); the "
                         "final gate will measure", after["rows"], stop_rows, margin_reason)
                stop_reason = "row_bounded_no_oracle"
                break

    final = pool_counts()
    # Gate on the ORACLE, not on pool_counts(): its "live" only means a cookie file was
    # written after a sign-in (expires is fabricated as now + 7 days), so it would print
    # "200 live, exit 0" for rows that never authenticate. Run the audit HERE instead of
    # trusting sync_extremerouter's copy: on the "target already met" break path sync
    # never ran in this invocation, and whatever sits on disk could be anybody's report —
    # including a pre-fix one that recorded HTTP 429 as dead. No round is in flight now,
    # which is the quiet window the audit needs to avoid 429s.
    audited, audit_status = None, "unavailable"
    gate_started = time.time()
    if run_audit(env):
        # oracle_live applies the same unusable-report guard as the loop (unreadable,
        # missing live field, or too inconclusive to trust): a 429-polluted final audit
        # must read as "measurement broken" (exit 2), not as a low live count (exit 1).
        audited = oracle_live(gate_started)
        if audited is None:
            # Name the actual cause with numbers; the summary is the operator's only
            # artifact and "unusable" without a reason forces a guess.
            try:
                data = json.loads((BASE_DIR / "conol_audit_live.json").read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    audit_status = "report root is not a JSON object (%s)" % type(data).__name__
                elif not isinstance(data.get("live"), int):
                    audit_status = "report has no integer 'live' field"
                else:
                    audit_status = "too inconclusive: %d/%d rows undecided" % (
                        _as_count(data.get("inconclusive")), _as_count(data.get("rows")))
            except (OSError, ValueError) as exc:
                audit_status = "report unreadable: %s" % exc
            except Exception as exc:
                # Last resort: this branch runs AFTER the round loop and BEFORE the
                # summary is written, so any escaping exception loses the campaign's
                # only artifact. Same contract oracle_live already honours.
                audit_status = "report malformed: %s: %s" % (type(exc).__name__, exc)
        else:
            audit_status = "ok"
    summary = {"target": args.target, "rounds": history,
               "final_heuristic": final, "final_audited_live": audited,
               "audit_status": audit_status, "stop_reason": stop_reason,
               "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    stop_beat.set()
    # Join before unlinking: if the thread is inside write_text when the lock is
    # removed, its write lands after the unlink and a lock file holding a dead pid
    # survives until the staleness window expires — 240 s of refused starts.
    beat_thread.join(timeout=max(5, LOCK_HEARTBEAT_SEC // 2 + 5))
    release_lock()
    log.info("FINAL: pool rows %d | heuristic live %d | AUDITED live %s (%s) | stop_reason %s -> %s",
             final["rows"], final["live"], audited, audit_status, stop_reason, SUMMARY_FILE)
    if audited is None:
        # Exit 2, distinct from 1 = "target not met". The campaign may well have
        # succeeded; what failed is the measurement. Conflating them makes the exit
        # code useless for telling a failed campaign from a broken oracle.
        log.error("exit 2: oracle unavailable (%s) — success NOT claimed; heuristic said "
                  "%d live", audit_status, final["live"])
        return 2
    return 0 if audited >= args.target else 1


if __name__ == "__main__":
    sys.exit(main())
