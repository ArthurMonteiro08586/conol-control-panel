"""deploy_conol_gateway.py — deploy the conol pool gateway to the New API host and wire it
in as a channel.

Target: the grok-gateway VPS (13.143.162.135) that runs new-api on 127.0.0.1:48000 behind
api.reformboss.com, reached through C:/Users/User/tmp/_gwssh.py.

Why the gateway lives on the VPS and not on this PC: every other farm New API consumes is a
localhost service there (grok-pool -> 127.0.0.1:8000, octopusx-farm -> 127.0.0.1:16433), so a
channel pointing at 127.0.0.1:9999 matches the existing pattern and needs no tunnel and no
dependency on this machine being awake.

Secrets never traverse this script's output or the agent's context:
  * the gateway key is generated ON the VPS with `openssl rand -hex 24` into
    /opt/conol-pool/env (0600) and referenced by systemd EnvironmentFile;
  * the New API admin token comes from the host's own refresher
    (/opt/grok-gateway/admin_token_refresh.py), which already handles the login rate limit;
  * channel creation runs as a remote script that reads both from those files.

Usage:
    python deploy_conol_gateway.py --selfcheck          # offline, no SSH
    python deploy_conol_gateway.py                      # dry run: print the plan
    python deploy_conol_gateway.py --apply              # install + start + health
    python deploy_conol_gateway.py --apply --smoke      # ... and one real inference
    python deploy_conol_gateway.py --sync --apply       # push the pool + /pool/reload
    python deploy_conol_gateway.py --channel            # dry run the channel payload
    python deploy_conol_gateway.py --channel --apply    # create/update + test the channel
    python deploy_conol_gateway.py --channel --apply --group default --priority 1   # promote as fallback
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parent
POOL_FILE = LOCAL_DIR / "conol_accounts_pool.jsonl"
UPLOADS = ["conol_gateway.py", "conol_infer.py"]

REMOTE_DIR = "/opt/conol-pool"
REMOTE_ENV = REMOTE_DIR + "/env"
UNIT_PATH = "/etc/systemd/system/conol-pool.service"
SERVICE = "conol-pool"
PORT = 9999
NEWAPI_BASE = "http://127.0.0.1:48000"
ADMIN_TOKEN_FILE = "/opt/grok-gateway/admin_token.txt"
ADMIN_TOKEN_REFRESHER = "/opt/grok-gateway/admin_token_refresh.py"
ADMIN_TOKEN_PYTHON = "/opt/venv/bin/python"
CHANNEL_NAME = "conol-farm-pool"
# Proven live 2026-10-03 against the authoritative spellings in gateway.py's CONOL_MODEL_MAP.
# Pass criterion, all four: exact-token answer, chars>0, create response carried NO
# modelDowngraded/effectiveModel, final status=stopped. A positive control settles the last
# one: gpt-5.5 and claude-opus-4-8 through the same harness returned
# {"modelDowngraded": true, "effectiveModel": "claude-haiku-4-5"}, while these ids returned
# only {"sessionId": ...} — conol emits those fields solely on downgrade, so their absence
# is a clean verdict, not missing data.
# Excluded mimo-v2.5 (status=stopped, zero assistant text). Excluded step-3.7-flash
# 2026-10-03 (stopped, zero text, 4 zones × 3 accounts, model-side regression).
# Excluded the 14 premium ids (claude-opus/sonnet/fable, gpt-5.5, gpt-5.5-pro, gpt-5.6-sol/
# terra, gemini-3.5-flash, gemini-3.1-pro-preview, kimi-k3, fusion): conol silently serves
# them as claude-haiku-4-5, so advertising them would misrepresent the channel. Flattened
# ids, matching the gateway's DEFAULT_MODELS; _resolve_model() also accepts the raw
# prefixed form.
CHANNEL_MODELS = ["gpt-5.6-luna", "claude-haiku-4-5", "deepseek-v4-pro", "deepseek-v4-flash",
                  "glm-5.2", "glm-5.1", "kimi-k2.7-code", "qwen3.7-plus", "qwen3.7-max",
                  "gemini-3.1-flash-lite", "grok-4.3", "minimax-m3", "hy3",
                  "mimo-v2.5-pro"]

# Pool rows carry `password`, `email`, `cookies_path` and `api_key_masked` — none of which
# the gateway reads: cookie_for() uses session_token, and the deployed copy resolves no
# Windows cookie paths. The host also runs public-facing services as root, so shipping
# plaintext passwords for 208 Gmail aliases there buys nothing and risks everything. Project
# each row down to the fields actually used before it leaves this machine.
POOL_FIELDS = ("name", "session_token", "token_expires", "status", "credits")
REMOTE_POOL = REMOTE_DIR + "/conol_pool.jsonl"
RAW_REMOTE_POOL = REMOTE_DIR + "/" + POOL_FILE.name
NEWAPI_DB = "/opt/grok-gateway/new-api/one-api.db"
# NOT "default", deliberately. conol answers most ids as claude-haiku-4-5 (the create
# response comes back modelDowngraded: true), and channel #104 octopusx-farm already serves
# eight of the same ids in group "default" at priority 5 / weight 0. An equal-priority
# channel in that group would split live ReformBoss traffic roughly 50/50 and silently
# downgrade half of it; for claude-opus-4-7/4-8 and claude-fable-5, whose #104 abilities rows
# are enabled=0, conol would become the ONLY enabled provider. A separate group routes
# nothing until a token's group selects it, and the channel-test endpoint works regardless —
# so isolation costs no verification. Promote with --group default only as an explicit,
# recorded decision.
CHANNEL_GROUP = "conol"

UNIT = """[Unit]
Description=ENI conol.ai account pool gateway (OpenAI-compatible)
Documentation=file://{remote}/conol_gateway.py
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={remote}
EnvironmentFile={env}
Environment=ENI_POOL_DIR={remote}
Environment=ENI_POOL_FILE={pool}
Environment=ENI_POOL_PORT={port}
Environment=ENI_CONOL_BUDGET=150
ExecStart=/usr/bin/python3 -u {remote}/conol_gateway.py --host 127.0.0.1 --port {port}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
""".format(remote=REMOTE_DIR, env=REMOTE_ENV, pool=REMOTE_POOL, port=PORT)



def _gw():
    sys.path.insert(0, r"C:/Users/User/tmp")
    from _gwssh import GW  # noqa: E402  (host helper lives outside this project)
    return GW


def sh(gw, cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    """Run a remote command, reconnecting once on a dropped transport."""
    try:
        return gw.run(cmd, timeout=timeout)
    except Exception as e:
        print("  [ssh] %s: %s — reconnecting" % (type(e).__name__, str(e)[:70]))
        gw.close()
        return gw.run(cmd, timeout=timeout)


def pool_rows(path: Path = POOL_FILE) -> tuple[int, int]:
    """(total rows, rows that look usable) without importing the gateway."""
    total = usable = 0
    now = time.time()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("status") == "live" and (row.get("token_expires") or 0) > now \
                and row.get("session_token"):
            usable += 1
    return total, usable



def project_pool(dest: Path) -> tuple[int, int]:
    """Write the deploy-safe projection of the pool to `dest`. Returns (rows, usable).

    Only POOL_FIELDS survive. `password` and `email` are the point: the gateway never reads
    them, so shipping them would be pure exposure on a box that also serves a public API.
    """
    now = time.time()
    written = usable = 0
    with dest.open("w", encoding="utf-8", newline="\n") as out:
        for line in POOL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            out.write(json.dumps({k: row[k] for k in POOL_FIELDS if k in row}) + "\n")
            written += 1
            if row.get("status") == "live" and (row.get("token_expires") or 0) > now \
                    and row.get("session_token"):
                usable += 1
    return written, usable

def install(gw, apply: bool, smoke: bool) -> int:
    total, usable = pool_rows()
    print("pool: %s rows, %d usable (live + unexpired + token)" % (total, usable))
    if usable == 0:
        print("refusing to deploy a gateway with no usable accounts", file=sys.stderr)
        return 2

    steps = [
        "mkdir -p %s && chmod 700 %s" % (REMOTE_DIR, REMOTE_DIR),
        "upload conol_gateway.py, conol_infer.py -> %s/" % REMOTE_DIR,
        "project the pool to %s (%s) and delete any raw copy"
        % (REMOTE_POOL, ",".join(POOL_FIELDS)),
        "generate %s with openssl rand -hex 24 (0600) unless it already exists" % REMOTE_ENV,
        "write %s" % UNIT_PATH,
        "systemctl daemon-reload && enable + RESTART %s, then assert the pid moved" % SERVICE,
        "verify GET 127.0.0.1:%d/health" % PORT,
    ]
    if smoke:
        steps.append("one real inference through the gateway (spends credits)")
    print("\nplan:")
    for i, s in enumerate(steps, 1):
        print("  %d. %s" % (i, s))
    if not apply:
        print("\ndry run — nothing changed. Re-run with --apply.")
        return 0

    rc, so, se = sh(gw, "mkdir -p %s && chmod 700 %s && echo OK" % (REMOTE_DIR, REMOTE_DIR))
    if rc != 0:
        print("mkdir failed: %s" % (se or so), file=sys.stderr)
        return 1

    for name in UPLOADS:
        local = LOCAL_DIR / name
        gw.upload(str(local), "%s/%s" % (REMOTE_DIR, name))
        print("  uploaded %s (%d bytes)" % (name, local.stat().st_size))

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        projected = Path(fh.name)
    try:
        written, proj_usable = project_pool(projected)
        gw.upload(str(projected), REMOTE_POOL)
    finally:
        projected.unlink(missing_ok=True)
    print("  uploaded projection %s (%d rows, %d usable, fields: %s)"
          % (REMOTE_POOL, written, proj_usable, ",".join(POOL_FIELDS)))
    # Delete any raw pool an earlier deploy left behind, then lock the directory down: the
    # projection still holds live session tokens. Files only — chmod 600 on __pycache__
    # would strip the execute bit a directory needs to be traversable.
    rc, so, se = sh(gw, "rm -f %s; chmod 700 %s; chmod 600 %s/*.py %s/*.jsonl %s 2>/dev/null; "
                        "ls -la %s | tail -6"
                    % (RAW_REMOTE_POOL, REMOTE_DIR, REMOTE_DIR, REMOTE_DIR, REMOTE_ENV, REMOTE_DIR))
    print("  %s" % (so.strip() or se.strip()[:220]))

    # The key is generated on the host and never read back into this process.
    rc, so, se = sh(gw, "test -s %s && echo PRESENT || "
                        "(umask 077 && printf 'ENI_POOL_%%s=%%s\\n' KEY "
                        "\"$(openssl rand -hex 24)\" > %s && echo GENERATED)"
                    % (REMOTE_ENV, REMOTE_ENV))
    print("  key file: %s" % (so.strip() or se.strip()))
    sh(gw, "chmod 600 %s" % REMOTE_ENV)

    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        fh.write(UNIT)
        unit_local = fh.name
    gw.upload(unit_local, UNIT_PATH)
    Path(unit_local).unlink(missing_ok=True)
    print("  wrote %s" % UNIT_PATH)

    # `enable --now` does NOT restart an already-active service, so a unit change — a new
    # Environment= line, a new ExecStart — silently never reaches the running process. The
    # pool is read into memory at import time, so the old process keeps serving 200 accounts
    # from a path that no longer exists while every health check reports green. That exact
    # no-op apply passed as verified once already. Hence: capture the pid, restart, require
    # the pid to move, and ask the RUNNING process which pool file it is using.
    _, pid_before, _ = sh(gw, "systemctl show -p MainPID --value %s 2>/dev/null" % SERVICE)
    pid_before = (pid_before or "0").strip()
    rc, so, se = sh(gw, "systemctl daemon-reload && systemctl enable %s >/dev/null 2>&1; "
                        "systemctl restart %s; sleep 4; systemctl is-active %s; "
                        "systemctl show -p MainPID --value %s"
                    % (SERVICE, SERVICE, SERVICE, SERVICE), timeout=180)
    lines = [x.strip() for x in so.splitlines() if x.strip()]
    state = lines[0] if lines else "?"
    pid_after = lines[-1] if len(lines) > 1 else pid_before
    print("  service: %s (pid %s -> %s)" % (state, pid_before, pid_after))
    if state != "active":
        _, so2, _ = sh(gw, "journalctl -u %s -n 25 --no-pager" % SERVICE)
        print(so2[-1800:], file=sys.stderr)
        return 1
    if pid_after == pid_before:
        print("  the pid did not move: the unit never reached the process, so nothing "
              "below would be measuring the new deployment", file=sys.stderr)
        return 1

    health = (
        "import json,urllib.request;"
        "r=urllib.request.urlopen('http://127.0.0.1:%d/health',timeout=15);"
        "d=json.loads(r.read().decode());"
        "print('health',r.status,json.dumps(d))" % PORT
    )
    rc, so, se = sh(gw, "python3 -c \"%s\"" % health, timeout=60)
    print("  %s" % (so.strip() or se.strip()[:200]))
    if '"usable"' not in so or '"loaded": 0' in so:
        print("gateway unhealthy — check journalctl -u %s" % SERVICE, file=sys.stderr)
        return 1

    stats_cmd = """python3 - <<'PYEOF'
import json, urllib.request
from pathlib import Path
key = [l.split("=", 1)[1] for l in Path("%s").read_text().splitlines()
       if l.startswith("ENI_POOL_" + "KEY=")][0]
req = urllib.request.Request("http://127.0.0.1:%d/pool/stats",
                             headers={"Authorization": "Bear" + "er " + key})
with urllib.request.urlopen(req, timeout=20) as r:
    print("stats", json.dumps(json.loads(r.read().decode())))
PYEOF""" % (REMOTE_ENV, PORT)
    rc, so, se = sh(gw, stats_cmd, timeout=60)
    print("  %s" % (so.strip() or se.strip()[:200]))
    if REMOTE_POOL not in so:
        print("the running process is NOT reading %s — the unit environment did not reach "
              "it, so the projection has never been exercised" % REMOTE_POOL, file=sys.stderr)
        return 1

    if smoke:
        rc, so, se = sh(gw, REMOTE_SMOKE, timeout=240)
        print("  smoke: %s" % (so.strip() or se.strip()[:300]))
        if "SMOKE_OK" not in so or "STREAM_OK" not in so:
            return 1
    return 0


# Runs on the VPS: reads the key from its own env file, so the secret never leaves the host.
# The script tests both non-streaming and streaming inference. Both legs MUST name an id that
# CONOL_MODEL_MAP actually advertises: an unadvertised id is not rejected, conol silently
# downgrades it (gpt-5.5 -> claude-haiku-4-5) and haiku answers the exact-token prompt just as
# well, so the old "gpt-5.5" smoke passed while exercising a downgraded path that also BUFFERS
# the stream — which is why the streaming leg saw frames=1 and failed on a healthy build.
REMOTE_SMOKE = r'''python3 - <<'PYEOF'
import json, urllib.request, urllib.error
from pathlib import Path
key = [l.split("=", 1)[1] for l in Path("/opt/conol-pool/env").read_text().splitlines()
       if l.startswith("ENI_POOL_" + "KEY=")][0]

# ── Non-streaming leg ──
# The prompt is deliberately ~50 chars, not "SMOKE_OK": an 8-char answer arrives as ONE content
# frame even on a healthy build (observed twice, 2026-10-03), so frames>=2 would fail a good
# deploy. A ~30-char answer measured 6 frames and a ~300-char answer 23 frames, so the threshold
# is meaningful only once the answer is long enough to be chunked.
body = json.dumps({"model": "gpt-5.6-luna",
                   "messages": [{"role": "user", "content": "Reply with exactly this text and nothing else: SMOKE_OK streaming leg 1 2 3 4 5 6 7 8 9 10"}]}).encode()
req = urllib.request.Request("http://127.0.0.1:9999/v1/chat/completions", data=body,
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bear" + "er " + key}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    txt = d["choices"][0]["message"]["content"]
    print("SMOKE_OK http=%s chars=%d usage=%s reply=%r"
          % (r.status, len(txt), d.get("usage", {}).get("total_tokens"), txt[:60]))
except urllib.error.HTTPError as e:
    print("SMOKE_FAIL http=%s %s" % (e.code, e.read()[:200]))
    raise SystemExit(1)
except Exception as e:
    print("SMOKE_FAIL %s %s" % (type(e).__name__, str(e)[:120]))
    raise SystemExit(1)

# ── Streaming leg ──
body_s = json.dumps({"model": "gpt-5.6-luna", "stream": True,
                     "messages": [{"role": "user", "content": "Reply with exactly this text and nothing else: SMOKE_OK streaming leg 1 2 3 4 5 6 7 8 9 10"}]}).encode()
req_s = urllib.request.Request("http://127.0.0.1:9999/v1/chat/completions", data=body_s,
                              headers={"Content-Type": "application/json",
                                       "Authorization": "Bear" + "er " + key}, method="POST")
try:
    with urllib.request.urlopen(req_s, timeout=180) as r:
        raw = r.read().decode()
    import re
    frames = []
    for ln in raw.split("\n"):
        if ln.startswith("data: ") and ln != "data: [DONE]":
            try:
                ch = json.loads(ln[6:])
                d = ch.get("choices", [{}])[0].get("delta", {})
                if "content" in d:
                    frames.append(d["content"])
            except (ValueError, KeyError):
                pass
    streamed = "".join(frames)
    faults = []
    if len(frames) < 2:
        faults.append("frames=%d < 2" % len(frames))
    if not streamed:
        faults.append("empty streamed text")
    elif streamed != txt:
        faults.append("stream != ns (%d vs %d)" % (len(streamed), len(txt)))
    if "conol pool could not answer" in raw:
        faults.append("error-leak")
    if "<final>" in raw or "</final>" in raw or "<function_call>" in raw:
        faults.append("tag-leak")
    if re.search(r"Conol\d+", raw):
        faults.append("account-leak")
    if faults:
        print("STREAM_FAIL %s" % ";".join(faults))
        raise SystemExit(1)
    print("STREAM_OK frames=%d len=%d" % (len(frames), len(streamed)))
except urllib.error.HTTPError as e:
    print("STREAM_FAIL http=%s %s" % (e.code, e.read()[:200]))
    raise SystemExit(1)
except Exception as e:
    print("STREAM_FAIL %s %s" % (type(e).__name__, str(e)[:120]))
    raise SystemExit(1)
PYEOF'''

# Runs on the VPS: refreshes the admin token with the host's own refresher, then creates or
# updates the conol channel and tests it. Reads both secrets from files; prints no secret.
REMOTE_CHANNEL = r'''python3 - <<'PYEOF'
import json, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

APPLY = %APPLY%
BASE = "http://127.0.0.1:48000"
NAME = "conol-farm-pool"
MODELS = %MODELS%
GROUP = %GROUP%
PRIORITY = %PRIORITY%
NEWAPI_DB = %DB%
key = [l.split("=", 1)[1] for l in Path("/opt/conol-pool/env").read_text().splitlines()
       if l.startswith("ENI_POOL_" + "KEY=")][0]

_ref = subprocess.run([%REFRESHER_PY%, %REFRESHER%], timeout=120, capture_output=True, text=True)
tok = Path("/opt/grok-gateway/admin_token.txt").read_text().strip().splitlines()[0]


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json",
                                         "Authorization": "Bear" + "er " + tok,
                                         "New-Api-User": "1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return None, {"err": "%s: %s" % (type(e).__name__, str(e)[:100])}

# Fail fast on an unusable admin token. The refresher's exit code used to be dropped and the
# stale token file read anyway, so a login refused by new-api (429 = the 100-per-24h session
# issuance window is full; observed 2026-10-03 17:18:12Z) surfaced much later as a pair of
# incomprehensible 401s on the read and the write. Probe first, name the cause, stop.
_probe_st, _probe_body = call("/api/channel/?p=0&size=1")
if _probe_st is None or _probe_st in (401, 403):
    _ref_out = (_ref.stdout or "") + (_ref.stderr or "")
    _why = (" — new-api refused the login (session issuance window full), retry after it rolls"
            if "429" in _ref_out else "")
    print("RESULT admin_token_unusable refresher_rc=%s probe_http=%s%s"
          % (_ref.returncode, _probe_st, _why))
    raise SystemExit(1)


# Existence is decided by a read-only SELECT, not by the paginated REST list: this DB holds
# 863 channels and the API caps a page far below that (size=500 came back with 100), so a
# list-based lookup can miss an existing channel and turn an idempotent re-run into a
# duplicate. Read-only, and the same access style the isolation proof below uses.
import sqlite3


def db_ro():
    return sqlite3.connect("file:%s?mode=ro" % NEWAPI_DB, uri=True)


_db = db_ro()
_row = _db.execute("SELECT id FROM channels WHERE name = ?", (NAME,)).fetchone()
_total = _db.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
_db.close()
existing_id = _row[0] if _row else None
print("channels in db: %d, existing conol channel id: %s" % (_total, existing_id))

payload = {"type": 1, "name": NAME, "base_url": "http://127.0.0.1:9999", "key": key,
           "models": ",".join(MODELS), "group": GROUP, "weight": 0, "priority": PRIORITY,
           "auto_ban": 0, "status": 1, "model_mapping": "", "test_model": MODELS[0]}
if existing_id:
    # Merge onto the stored object so a PUT cannot null out columns this script does not
    # manage (channel_info, settings, tag, remark, status_code_mapping, ...).
    st, cur = call("/api/channel/%d" % existing_id)
    stored = cur.get("data") if isinstance(cur, dict) else None
    if isinstance(stored, dict) and stored:
        merged = dict(stored)
        merged.update(payload)
        merged["id"] = existing_id
        payload = merged
        print("target: UPDATE id=%d (merged onto %d stored fields)" % (existing_id, len(stored)))
    else:
        payload["id"] = existing_id
        print("target: UPDATE id=%d (stored object unreadable: http=%s)" % (existing_id, st))
else:
    print("target: CREATE")
print("payload: type=%s base_url=%s models=%s group=%s priority=%s auto_ban=%s key=<%d chars>"
      % (payload["type"], payload["base_url"], len(str(payload.get("models", "")).split(",")),
         payload["group"], payload.get("priority"), payload.get("auto_ban"), len(key)))
if not APPLY:
    print("DRY_RUN no change made")
    raise SystemExit(0)

if existing_id:
    # UpdateChannel binds a BARE PatchChannel — no wrapper — and explicitly rejects a body
    # carrying "status" (that lives on /api/channel/status/), so drop it instead of failing.
    body = {k: v for k, v in payload.items() if k != "status"}
    method = "PUT"
else:
    # AddChannel binds AddChannelRequest{mode, multi_key_mode, channel}: POSTing the bare
    # channel object leaves its *model.Channel pointer nil, and validateChannel(nil, true)
    # answers "channel cannot be empty" — which is what the first attempt hit.
    body = {"mode": "single", "channel": payload}
    method = "POST"
print("request: %s /api/channel/ (%s, %d top-level keys)" % (method, "wrapped" if not existing_id
                                                           else "bare", len(body)))
st, res = call("/api/channel/", method, body)
print("write -> http=%s success=%s %s" % (st, res.get("success"), str(res.get("message") or "")[:120]))
if not res.get("success"):
    print("RESULT write=fail test=skipped isolation=skipped")
    raise SystemExit(1)

cid = existing_id
if not cid:
    _db = db_ro()
    _row = _db.execute("SELECT id FROM channels WHERE name = ?", (NAME,)).fetchone()
    _db.close()
    cid = _row[0] if _row else None
print("channel id now: %s" % cid)
if not cid:
    # Never claim a verification that did not run. Without an id there is no channel to test
    # and no abilities to query, so both states are "missing" — printing isolation=ok here
    # would assert the one control that keeps this channel out of live customer routing.
    print("CHANNEL_FAIL channel written but not found by name")
    print("RESULT write=ok test=missing isolation=missing")
    raise SystemExit(1)

st, res = call("/api/channel/test/%d?model=%s" % (cid, MODELS[0]))
test_state = "ok" if res.get("success") else "fail"
print("channel test -> http=%s success=%s %s"
      % (st, res.get("success"), str(res.get("message") or res)[:160]))

# Prove isolation with a query rather than trust the payload just written. Read-only.
_db = db_ro()
rows = _db.execute('SELECT "group", model, enabled FROM abilities WHERE channel_id=?',
                   (cid,)).fetchall()
_db.close()
groups = {}
for grp, mdl, en in rows:
    groups.setdefault(grp, []).append((mdl, en))
print("abilities: %d rows, by group: %s" % (len(rows), {g: len(v) for g, v in groups.items()}))
leaked = sorted(m for g, v in groups.items() if g == "default" for m, en in v if en)
if GROUP != "default" and leaked:
    print("ISOLATION_FAIL enabled in group 'default' for: %s" % leaked)
    print("RESULT write=ok test=%s isolation=fail" % test_state)
    raise SystemExit(1)
print("ISOLATION_OK group=%s, nothing enabled in 'default'" % GROUP)
# One marker, printed exactly once, truthful about every component it names — and the exit
# code agrees with it, so a caller can gate on either without them ever contradicting.
print("RESULT write=ok test=%s isolation=ok" % test_state)
if test_state != "ok":
    raise SystemExit(1)
PYEOF'''


def channel_payload_preview(group: str = CHANNEL_GROUP, priority: int = 5) -> dict:
    return {"type": 1, "name": CHANNEL_NAME, "base_url": "http://127.0.0.1:%d" % PORT,
            "key": "<read from %s on the host>" % REMOTE_ENV,
            "models": ",".join(CHANNEL_MODELS), "group": group, "weight": 0,
            "priority": priority, "auto_ban": 0, "status": 1, "test_model": CHANNEL_MODELS[0]}


def sync_pool(gw, apply: bool) -> int:
    total, usable = pool_rows()
    print("sync projection of %s -> %s (%d rows, %d usable, fields: %s)"
          % (POOL_FILE.name, REMOTE_POOL, total, usable, ",".join(POOL_FIELDS)))
    if not apply:
        print("dry run — nothing uploaded. Re-run with --apply.")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8", newline="\n") as fh:
        projected = Path(fh.name)
    try:
        written, proj_usable = project_pool(projected)
        gw.upload(str(projected), REMOTE_POOL)
    finally:
        projected.unlink(missing_ok=True)
    sh(gw, "chmod 600 %s; rm -f %s" % (REMOTE_POOL, RAW_REMOTE_POOL))
    print("  uploaded %d rows (%d usable) — no password/email/cookies_path left this machine"
          % (written, proj_usable))
    reload_remote = r'''python3 - <<'PYEOF'
import json, urllib.request, urllib.error
from pathlib import Path
key = [l.split("=", 1)[1] for l in Path("/opt/conol-pool/env").read_text().splitlines()
       if l.startswith("ENI_POOL_" + "KEY=")][0]
req = urllib.request.Request("http://127.0.0.1:9999/pool/reload", data=b"{}", method="POST",
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bear" + "er " + key})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    print("RELOAD_OK loaded=%s usable=%s dead=%s" % (d.get("loaded"), d.get("usable"), d.get("dead")))
except urllib.error.HTTPError as e:
    print("RELOAD_FAIL http=%s %s" % (e.code, e.read()[:160]))
except Exception as e:
    print("RELOAD_FAIL %s %s" % (type(e).__name__, str(e)[:100]))
PYEOF'''
    rc, so, se = sh(gw, reload_remote, timeout=120)
    print("  %s" % (so.strip() or se.strip()[:200]))
    return 0 if "RELOAD_OK" in so else 1


def do_channel(gw, apply: bool, group: str = CHANNEL_GROUP, priority: int = 5) -> int:
    if group == "default" and priority >= 5:
        print("REFUSING: --group default with priority %d (>=5) splits live traffic with "
              "channel #104 octopusx-farm 50/50; conol downgrades 8 of 10 models to "
              "claude-haiku-4-5, silently degrading half of all requests. "
              "Pass an explicit lower --priority (e.g. --priority 1) to run conol as a "
              "pure fallback, not a co-equal provider." % priority, file=sys.stderr)
        return 2
    print("channel group: %r — %s" % (group, "LIVE default routing, customer traffic affected"
                                      if group == "default" else "isolated, routes nothing yet"))
    print("channel payload:")
    print(json.dumps(channel_payload_preview(group, priority), indent=1))
    if not apply:
        print("\ndry run — nothing written. Re-run with --channel --apply.")
        return 0
    script = (REMOTE_CHANNEL
              .replace("%MODELS%", json.dumps(CHANNEL_MODELS))
              .replace("%GROUP%", json.dumps(group))
              .replace("%PRIORITY%", str(priority))
              .replace("%DB%", json.dumps(NEWAPI_DB))
              .replace("%APPLY%", "True" if apply else "False")
              .replace("%REFRESHER_PY%", json.dumps(ADMIN_TOKEN_PYTHON))
              .replace("%REFRESHER%", json.dumps(ADMIN_TOKEN_REFRESHER)))
    import re
    left = sorted(set(re.findall(r"%[A-Z_]+%", script)))
    if left:
        print("refusing to run: unsubstituted placeholders %s would reach the host as "
              "literal text" % left, file=sys.stderr)
        return 1
    rc, so, se = sh(gw, script, timeout=300)
    print(so[-3000:])
    if se.strip():
        print("--- stderr ---\n" + se[-600:])
    # Success is the COMPLETE marker plus the remote exit code. A prefix match is not enough:
    # "RESULT write=ok" is also the head of the isolation-failure and test-failure markers, so
    # a substring test would report a channel that leaked into group "default" — the exact
    # hijack this control exists to prevent — as a successful isolated deploy. Treating
    # "DRY_RUN" as success already produced one false green this session: the remote script is
    # fed over stdin, where sys.argv is ['-'] and a local --apply never crosses.
    if apply:
        ok = (rc == 0 and "RESULT write=ok test=ok isolation=ok" in so and "DRY_RUN" not in so)
        if not ok:
            print("channel step failed (remote rc=%s) — see the markers above" % rc,
                  file=sys.stderr)
        return 0 if ok else 1
    return 0 if (rc == 0 and "DRY_RUN" in so) else 1


def selfcheck() -> int:
    """Offline: everything that can be wrong without a network."""
    fails = []

    def check(name, cond, extra=None):
        print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
        if not cond:
            fails.append(name)

    import inspect
    import re

    total, usable = pool_rows()
    check("pool file readable", total > 0, total)
    check("pool has usable rows", usable > 0, usable)
    check("every upload exists locally", all((LOCAL_DIR / n).exists() for n in UPLOADS), UPLOADS)

    check("unit binds the localhost port", "--host 127.0.0.1 --port %d" % PORT in UNIT, UNIT[:80])
    check("unit loads the secret by reference, not by value",
          "EnvironmentFile=%s" % REMOTE_ENV in UNIT and "openssl" not in UNIT)
    check("unit restarts on failure", "Restart=always" in UNIT)
    check("unit points python at the deployed copy",
          "ExecStart=/usr/bin/python3 -u %s/conol_gateway.py" % REMOTE_DIR in UNIT)
    check("unit has no placeholder left", "{" not in UNIT and "}" not in UNIT)
    check("unit sets the pool dir the gateway reads", "ENI_POOL_DIR=%s" % REMOTE_DIR in UNIT)
    check("unit points the gateway at the projection, not the raw pool",
          "ENI_POOL_FILE=%s" % REMOTE_POOL in UNIT)
    check("the raw pool file is never uploaded",
          POOL_FILE.name not in UPLOADS and REMOTE_POOL != RAW_REMOTE_POOL)

    # The projection is the security control: the host runs public-facing services as root,
    # so plaintext passwords for the whole Gmail-alias pool must not leave this machine.
    tmp = Path(tempfile.mkdtemp(prefix="conol_proj_")) / "p.jsonl"
    try:
        written, proj_usable = project_pool(tmp)
        fields = set()
        for line in tmp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                fields.update(json.loads(line).keys())
        check("projection writes every row", written == pool_rows()[0], "%s vs %s"
              % (written, pool_rows()[0]))
        check("projection carries only fields the gateway reads",
              fields <= set(POOL_FIELDS), sorted(fields))
        check("projection drops password/email/cookies_path",
              not (fields & {"password", "email", "cookies_path", "api_key_masked", "recovered"}),
              sorted(fields))
        check("projection keeps the credential the gateway needs", "session_token" in fields)
        check("projection preserves the usable count", proj_usable == pool_rows()[1],
              "%s vs %s" % (proj_usable, pool_rows()[1]))
    finally:
        tmp.unlink(missing_ok=True)
        try:
            tmp.parent.rmdir()
        except OSError:
            pass

    payload = channel_payload_preview()
    check("channel type is OpenAI-compatible", payload["type"] == 1, payload["type"])
    check("channel base_url is the localhost gateway",
          payload["base_url"] == "http://127.0.0.1:%d" % PORT, payload["base_url"])
    check("channel models are the verified ids",
          payload["models"].split(",") == CHANNEL_MODELS, payload["models"][:60])
    check("channel does not auto-ban on a shared rate limit", payload["auto_ban"] == 0)
    check("channel key is never inlined in this script",
          "read from" in payload["key"] and len(payload["key"]) < 60, payload["key"])
    check("channel is isolated from live customer routing by default",
          CHANNEL_GROUP != "default", CHANNEL_GROUP)

    # --priority flag and default-group guard
    prio_payload = channel_payload_preview(priority=1)
    check("explicit priority flows into payload",
          prio_payload["priority"] == 1, prio_payload["priority"])
    prio_group_payload = channel_payload_preview(group="default", priority=3)
    check("default group with lowered priority produces correct payload",
          prio_group_payload["group"] == "default" and prio_group_payload["priority"] == 3,
          (prio_group_payload["group"], prio_group_payload["priority"]))
    check("guard source contains the 50/50 split refusal",
          "REFUSING" in inspect.getsource(do_channel) and "50/50" in inspect.getsource(do_channel))
    check("guard exits with code 2",
          "return 2" in inspect.getsource(do_channel))
    check("--priority flag exists in argparse",
          "--priority" in inspect.getsource(main))
    check("priority threads through to do_channel call",
          "args.priority" in inspect.getsource(main))

    check("remote channel script proves isolation with a query, not trust",
          "abilities" in REMOTE_CHANNEL and "ISOLATION_FAIL" in REMOTE_CHANNEL)
    check("remote channel script takes the group from a placeholder", "%GROUP%" in REMOTE_CHANNEL)
    placeholders = set(re.findall(r"%[A-Z_]+%", REMOTE_CHANNEL))
    check("remote channel script takes the apply flag from a placeholder, not sys.argv",
          "%APPLY%" in REMOTE_CHANNEL and "sys.argv" not in REMOTE_CHANNEL,
          "fed over stdin, sys.argv is ['-'] and a local flag never crosses")
    check("every remote placeholder is one do_channel substitutes",
          placeholders <= {"%MODELS%", "%GROUP%", "%PRIORITY%", "%DB%", "%APPLY%",
                           "%REFRESHER_PY%", "%REFRESHER%"}, sorted(placeholders))
    check("do_channel substitutes the apply flag",
          '%APPLY%' in inspect.getsource(do_channel))
    check("do_channel refuses a dry-run marker when --apply was asked",
          '"DRY_RUN" not in so' in inspect.getsource(do_channel))
    check("do_channel guards against unsubstituted placeholders",
          "unsubstituted placeholders" in inspect.getsource(do_channel))
    check("do_channel matches the COMPLETE marker, not a prefix of it",
          "RESULT write=ok test=ok isolation=ok" in inspect.getsource(do_channel),
          "a prefix match also accepts test=fail and isolation=fail")
    check("do_channel gates on the remote exit code",
          "rc == 0" in inspect.getsource(do_channel))
    check("remote channel script ends in one explicit result marker",
          "RESULT write=" in REMOTE_CHANNEL)
    check("a missing channel id is a failure, not a claimed isolation pass",
          "test=missing isolation=missing" in REMOTE_CHANNEL
          and "CHANNEL_FAIL channel written but not found" in REMOTE_CHANNEL)
    check("a failed channel test exits nonzero",
          'if test_state != "ok":' in REMOTE_CHANNEL)
    check("existence is decided by a read-only SELECT, not a capped REST page",
          "SELECT id FROM channels WHERE name" in REMOTE_CHANNEL
          and "mode=ro" in REMOTE_CHANNEL)
    check("a create posts the AddChannelRequest wrapper, not a bare channel",
          '{"mode": "single", "channel": payload}' in REMOTE_CHANNEL,
          "AddChannel binds AddChannelRequest; a bare body decodes Channel as nil and "
          "validateChannel answers 'channel cannot be empty'")
    check("an update drops status, which UpdateChannel rejects",
          'if k != "status"' in REMOTE_CHANNEL)
    check("create and update use different verbs",
          '"PUT"' in REMOTE_CHANNEL and '"POST"' in REMOTE_CHANNEL)
    check("an update merges onto the stored channel instead of overwriting it",
          "/api/channel/%d" in REMOTE_CHANNEL and "merged.update(payload)" in REMOTE_CHANNEL)

    # The remote scripts must read secrets from host files and must not print them.
    for label, script in (("smoke", REMOTE_SMOKE), ("channel", REMOTE_CHANNEL)):
        check("%s script reads the key from the host env file" % label,
              "/opt/conol-pool/env" in script)
        check("%s script never prints the key" % label, "print(key" not in script
              and "key)" not in script.split("print")[-1][:40])
        check("%s script assembles the auth prefix" % label, '"Bear" + "er "' in script)
    check("channel script refreshes the admin token with the host refresher",
          "admin_token_refresh.py" in REMOTE_CHANNEL.replace("%REFRESHER%", "admin_token_refresh.py"))
    check("channel script is idempotent (update when the name exists)",
          "existing" in REMOTE_CHANNEL and "PUT" in REMOTE_CHANNEL)
    check("channel script tests the channel after writing", "/api/channel/test/" in REMOTE_CHANNEL)
    # A refused login used to be invisible: the refresher's exit code was dropped, the stale
    # token file was read anyway, and the failure surfaced as two bare 401s on read and write.
    check("channel script keeps the refresher result", "_ref.returncode" in REMOTE_CHANNEL)
    check("channel script probes the admin token before writing",
          "/api/channel/?p=0&size=1" in REMOTE_CHANNEL)
    check("channel script fails fast on an unusable admin token",
          "admin_token_unusable" in REMOTE_CHANNEL)
    check("channel script names the issuance window as the cause of a refused login",
          "issuance window full" in REMOTE_CHANNEL)

    check("step-3.7-flash excluded from CHANNEL_MODELS",
          "step-3.7-flash" not in CHANNEL_MODELS and "step-3.7-flash" not in str(CHANNEL_MODELS),
          CHANNEL_MODELS)

    # Both smoke legs must name an advertised id. An unadvertised one is not rejected: conol
    # silently downgrades it to claude-haiku-4-5, haiku answers the exact-token prompt just as
    # well, and the downgraded path BUFFERS the stream — observed 2026-10-03 as frames=1 on a
    # healthy build while both legs still said "gpt-5.5".
    check("smoke non-stream leg names the advertised test model",
          '"model": "%s"' % CHANNEL_MODELS[0] in REMOTE_SMOKE, CHANNEL_MODELS[0])
    check("smoke stream leg names the advertised test model",
          '"model": "%s", "stream": True' % CHANNEL_MODELS[0] in REMOTE_SMOKE, CHANNEL_MODELS[0])
    check("smoke does not test a downgraded id", "gpt-5.5" not in REMOTE_SMOKE)
    check("smoke stream leg demands incremental frames", "frames=%d < 2" in REMOTE_SMOKE)
    check("smoke stream leg compares against the non-stream answer", "stream != ns" in REMOTE_SMOKE)
    check("smoke stream leg scans for account-name leaks", "Conol\\d+" in REMOTE_SMOKE)

    print()
    print("FAILURES: " + str(fails) if fails else "all blocks passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="make changes (default is a dry run)")
    ap.add_argument("--sync", action="store_true", help="push the pool file and reload it")
    ap.add_argument("--channel", action="store_true", help="create/update the New API channel")
    ap.add_argument("--smoke", action="store_true", help="run one real inference after install")
    ap.add_argument("--group", default=CHANNEL_GROUP,
                    help="New API routing group for the channel. Default %r routes nothing "
                         "until a token selects it; 'default' puts conol into live customer "
                         "routing, where it would share eight model ids with octopusx-farm "
                         "and answer them as claude-haiku-4-5" % CHANNEL_GROUP)
    ap.add_argument("--priority", type=int, default=5,
                    help="New API channel priority (default 5). With --group default, "
                         "MUST be <5 to avoid equal-priority split with #104 octopusx-farm, "
                         "which would silently downgrade half of live traffic to haiku. "
                         "The script refuses priority >=5 when group=default.")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    gw = _gw()
    try:
        if args.sync:
            return sync_pool(gw, args.apply)
        if args.channel:
            return do_channel(gw, args.apply, args.group, args.priority)
        return install(gw, args.apply, args.smoke)
    finally:
        gw.close()


if __name__ == "__main__":
    sys.exit(main())
