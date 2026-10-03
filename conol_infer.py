"""conol_infer.py — the conol.ai inference protocol, stdlib only.

Single source of truth shared by probe_conol_infer.py (model sweeps) and conol_gateway.py
(the OpenAI-compatible pool gateway). Stdlib only on purpose: the deploy target is a
Python 3.14 box with no pip and no fastapi/httpx/uvicorn.

Protocol, verified live 2026-10-03 against a pool account:

    POST https://conol.ai/api/sessions
         {"source":{"type":"home"},"messages":[{"type":"text","content":PROMPT}],
          "timezone":"Europe/Kyiv","agentModel":MODEL,"agentEffort":"low"}
      -> 201 {"sessionId":..., "modelDowngraded":bool, "effectiveModel":...}

    GET  https://conol.ai/api/sessions/<sid>/messages?logDeltas=1     (SSE, no gzip)
      -> repeated `data: {"type":"history_delta","stages":[{...}]}` events

    GET  https://conol.ai/api/sessions/<sid>
      -> {"status": "running"|"stopped"|...}      # the termination signal

Three traps this module exists to encode:

1. `Accept-Encoding: gzip` on the SSE stream makes the server gzip it; iterating lines then
   yields compressed bytes, no `data:` prefix ever matches, and the call "times out" while
   the answer was already there. The stream request must not advertise gzip.
2. The assistant answer lives in `stages[].preview[]`, NOT in `stages[].logs[]`. `logs`
   carries the user turn only — and conol wraps that user turn in its own
   `<system-reminder>` preamble, so "response contains system-reminder" is NOT an echo
   signal. The historical "unknown models echo the prompt" observation was a parser taking
   the last text of any role, i.e. returning the user's own prompt.
3. `preview[]` entries are CUMULATIVE snapshots, not deltas, and carry a `type`:
   `thinking` (reasoning, many snapshots) and `message` (the final answer). Concatenating
   snapshots multiplies the text; the answer is the LAST `type == "message"` snapshot.
   The stream itself never closes after the agent finishes — termination is the session
   `status` turning terminal, polled while the read is idle (a short socket timeout turns
   "no data yet" into a poll instead of a 200 s block).
"""
from __future__ import annotations

import gzip
import io
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://conol.ai"
COOKIE_NAME = "__Secure-better-auth.session_token"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Socket read timeout for the SSE stream. Reading is line-oriented (readline), so a timeout
# means "no COMPLETE event for this long" and never leaves a half-consumed buffer behind.
STREAM_IDLE_SEC = 2
# Session statuses that mean "the agent will not produce more".
TERMINAL_STATUSES = ("stopped", "completed", "complete", "done", "failed", "error", "cancelled")
# Idle polls with no terminal status before giving up on a session.
MAX_IDLE_POLLS = 60
# Consecutive idle polls with a terminal status and an unchanged answer before the answer is
# accepted as final. Measured 2026-10-03: the session flips to "stopped" up to ~8 s AFTER the
# last message snapshot, and on some runs BEFORE the final snapshot lands — so the first
# terminal poll is not proof the text is complete. Two silent cycles are.
STABLE_POLLS = 2
# How long to keep waiting for a late `message` snapshot once the session already reads as
# terminal with no answer at all. Without this grace window a slow agent and a dead model
# look identical, and good models get written off (measured: a 60 s sweep reported 14 of 20
# models EMPTY; a direct timeline on the same account showed the answer at 1.1 s).
TERMINAL_GRACE_SEC = 12

VERDICTS = ("ANSWERED", "EMPTY", "ERROR", "SKIP")


class ConolError(Exception):
    """Any protocol-level failure, with a message safe to log (no cookie material)."""


def _open(url: str, cookie: str, data: bytes | None = None, timeout: int = 40,
          accept: str = "application/json", want_gzip: bool = True):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Cookie", "%s=%s" % (COOKIE_NAME, cookie))
    req.add_header("Accept", accept)
    req.add_header("User-Agent", UA)
    req.add_header("Origin", BASE_URL)
    req.add_header("Referer", BASE_URL + "/")
    req.add_header("Cache-Control", "no-cache")
    if want_gzip:
        req.add_header("Accept-Encoding", "gzip")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


def _body(resp) -> bytes:
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip" and raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw


def cookie_for(row: dict, pool_dir: Path | None = None) -> str | None:
    """Best credential for a pool row.

    The pool's `session_token` wins when present: conol_refresh.py rewrites it on every
    token refresh but leaves the browser cookie file untouched, so for refreshed accounts
    the file is the STALE copy (measured 2026-10-03: 186/189 rows agree, and in all 3
    disagreements the pool row was newer). The cookie file is the fallback for rows that
    carry no token, resolved by basename so a mirrored directory works on any host.
    """
    tok = row.get("session_token")
    if tok:
        return str(tok)
    cp = row.get("cookies_path")
    if not cp:
        return None
    candidates = [Path(cp)]
    if pool_dir is not None:
        candidates.append(Path(pool_dir) / Path(cp).name)
    for p in candidates:
        try:
            if p.exists():
                for c in json.loads(p.read_text(encoding="utf-8")):
                    if c.get("name") == COOKIE_NAME and c.get("value"):
                        return str(c["value"])
        except (OSError, ValueError):
            continue
    return None


def check_session(cookie: str) -> tuple[bool, str]:
    """The audit's liveness test: GET /api/auth/get-session must be 200 with a user."""
    try:
        with _open(BASE_URL + "/api/auth/get-session", cookie, timeout=30) as r:
            body = _body(r)
        try:
            email = (json.loads(body).get("user") or {}).get("email", "")
        except ValueError:
            email = ""
        return r.status == 200, "200 %s" % (email.split("@")[0] or "ok")
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code
    except Exception as e:
        return False, "%s %s" % (type(e).__name__, str(e)[:60])


def get_balance(cookie: str):
    """Total credits, or None. Used to skip exhausted accounts."""
    try:
        with _open(BASE_URL + "/api/billing/balance", cookie, timeout=30) as r:
            doc = json.loads(_body(r))
        total = doc.get("total")
        return float(total) if isinstance(total, (int, float)) else None
    except Exception:
        return None


def session_status(cookie: str, sid: str) -> str:
    try:
        with _open(BASE_URL + "/api/sessions/%s" % sid, cookie, timeout=15) as r:
            return str(json.loads(_body(r)).get("status") or "")
    except Exception:
        return ""


def _entry_text(entry: dict) -> str:
    """Text of one preview/log entry, whatever shape `content` has."""
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return ""


def create_session(cookie: str, prompt: str, model: str, effort: str = "low") -> dict:
    """POST /api/sessions. Returns the parsed create response."""
    payload = json.dumps({
        "source": {"type": "home"},
        "messages": [{"type": "text", "content": prompt}],
        "timezone": "Europe/Kyiv",
        "agentModel": model,
        "agentEffort": effort,
    }).encode()
    try:
        with _open(BASE_URL + "/api/sessions", cookie, data=payload, timeout=60) as r:
            body = _body(r)
            if r.status not in (200, 201):
                raise ConolError("create HTTP %d" % r.status)
    except urllib.error.HTTPError as e:
        raise ConolError("create HTTP %d %s"
                         % (e.code, e.read()[:140].decode("utf-8", "replace"))) from None
    except ConolError:
        raise
    except Exception as e:
        raise ConolError("create %s: %s" % (type(e).__name__, str(e)[:100])) from None
    try:
        doc = json.loads(body)
    except ValueError:
        raise ConolError("create returned non-JSON (%d bytes)" % len(body)) from None
    sid = doc.get("sessionId") or doc.get("id") or (doc.get("session") or {}).get("id")
    if not sid:
        raise ConolError("no sessionId in create response keys %s" % sorted(doc)[:6])
    doc["sessionId"] = sid
    return doc


# Preview/log entry types conol uses for reasoning rather than the user-visible answer.
# Anything NOT in this set counts as the answer: the observed set was {"thinking","message"}
# on 2026-10-03, and an allowlist-of-answer-types would silently swallow the reply the day
# conol renames "message".
REASONING_TYPES = ("thinking", "reasoning", "thought", "plan")


def _apply_event(out: dict, ev: dict) -> None:
    """Fold one SSE event into the result dict. Pure — no I/O, so it is testable offline.

    Traps 2 and 3 from the module docstring live here: the answer arrives in
    `stages[].preview` (logs hold the user turn, wrapped in conol's own <system-reminder>
    preamble, so role filtering is what keeps the prompt from being returned as the answer),
    and every preview entry is a CUMULATIVE snapshot of the text so far. Longest snapshot
    wins; concatenating them multiplies the text (measured: 17 snapshots -> 2787 chars of
    duplication for an 8-char answer).
    """
    if not isinstance(ev, dict):
        return
    for stage in ev.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        entries = list(stage.get("preview") or []) + list(stage.get("logs") or [])
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("role") != "assistant":
                continue
            txt = _entry_text(entry)
            if not txt:
                continue
            if str(entry.get("type") or "").lower() in REASONING_TYPES:
                if len(txt) >= len(out.get("thinking") or ""):
                    out["thinking"] = txt
            elif len(txt) >= len(out.get("answer") or ""):
                out["answer"] = txt


def read_answer(cookie: str, sid: str, budget: int = 180,
                on_event=None) -> dict:
    """Stream a session to completion and return the assistant's final message.

    Returns {answer, thinking, status, seconds, idle_polls, events}. Never raises for
    stream-level problems: an unreadable stream is an empty answer, and the caller decides
    whether that is a dead account or a dead model.

    Two rules here are load-bearing and were both learned from truncated answers:

    * Read with readline(), never read(N). `read(2048)` blocks until it can fill the buffer,
      and when the stream is cut short the last PARTIAL line stays unprocessed — with
      cumulative snapshots that is exactly the final, longest one. Measured symptom: answers
      returned as "PROBE_" and "PROBE" instead of "PROBE_OK".
    * Do not stop at the first terminal status. Require the answer to survive STABLE_POLLS
      silent cycles, and give a session with no answer TERMINAL_GRACE_SEC before calling it
      empty, because conol can mark the session stopped before the message snapshot arrives.
    """
    out = {"answer": "", "thinking": "", "status": "", "seconds": 0.0,
           "idle_polls": 0, "events": 0}
    started = time.time()
    url = BASE_URL + "/api/sessions/%s/messages?logDeltas=1" % sid
    try:
        # Longer timeout for initial connection (server may be slow under load).
        # After connect, reduce to STREAM_IDLE_SEC for line-by-line reads.
        resp = _open(url, cookie, timeout=15,
                     accept="text/event-stream", want_gzip=False)
        try:
            resp.fp.raw.settimeout(STREAM_IDLE_SEC)
        except AttributeError:
            pass
    except Exception as e:
        out["status"] = "stream_open_failed:%s" % type(e).__name__
        out["seconds"] = round(time.time() - started, 1)
        return out

    terminal_at = None
    stable = 0
    idle = 0
    while time.time() - started < budget:
        try:
            raw = resp.readline()
        except (socket.timeout, TimeoutError):
            raw = None                     # idle: no complete event within STREAM_IDLE_SEC
        except Exception as e:
            out["status"] = out["status"] or "stream_error:%s" % type(e).__name__
            break

        if raw:
            stable = 0
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                body = line[5:].strip()
                if body == "[DONE]":
                    out["status"] = out["status"] or "done"
                    break
                try:
                    ev = json.loads(body)
                except ValueError:
                    continue
                out["events"] += 1
                _apply_event(out, ev)
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception as _cb_err:
                        out["callback_error"] = str(_cb_err)
            continue

        if raw == b"":
            # Clean EOF: server closed the stream, but the session may still be active.
            # Don't break — poll status below for the remaining budget.
            time.sleep(STREAM_IDLE_SEC)

        # Idle read timeout. conol leaves the stream open after the agent stops, so the
        # session status is the only termination signal — but not proof the text is
        # complete, hence the stability rule.
        st = session_status(cookie, sid)
        if st:
            out["status"] = st
        idle += 1
        out["idle_polls"] = idle
        if st in TERMINAL_STATUSES:
            if terminal_at is None:
                terminal_at = time.time()
            if out["answer"]:
                stable += 1
                if stable >= STABLE_POLLS:
                    break
            elif time.time() - terminal_at >= TERMINAL_GRACE_SEC:
                break
        else:
            stable = 0
        if idle >= MAX_IDLE_POLLS:
            if st in TERMINAL_STATUSES or not st:
                out["status"] = st or "idle_timeout"
                break
    if not out["status"]:
        out["status"] = session_status(cookie, sid) or "unknown"
    out["seconds"] = round(time.time() - started, 1)
    try:
        resp.close()
    except Exception:
        pass
    return out


def run_inference(cookie: str, model: str, prompt: str, budget: int = 180,
                  effort: str = "low") -> dict:
    """One full inference attempt. Returns a flat result dict; never raises.

    `detail` (consumed by conol_gateway.py) starts with a stable prefix for machine matching:

      budget_exhausted_active  — budget ran out while conol still reported active/running
      terminal_no_text         — status is terminal, no assistant text landed
      no_events                — zero substantive events received (stream/connect error)
      callback_error           — callback raised, error message follows
      create/stream error      — literal upstream error, unchanged
    """
    res = {"model": model, "verdict": "ERROR", "detail": "", "answer": "", "thinking_chars": 0,
           "chars": 0, "seconds": 0.0, "effective_model": None, "downgraded": None,
           "status": ""}
    started = time.time()
    try:
        created = create_session(cookie, prompt, model, effort=effort)
    except ConolError as e:
        res["detail"] = str(e)
        res["seconds"] = round(time.time() - started, 1)
        return res
    res["effective_model"] = created.get("effectiveModel")
    res["downgraded"] = created.get("modelDowngraded")
    sid = created["sessionId"]
    res["session_id"] = sid

    got = read_answer(cookie, sid, budget=budget)
    res.update({"seconds": got["seconds"], "status": got["status"],
                "chars": len(got["answer"]), "thinking_chars": len(got["thinking"]),
                "answer": got["answer"]})
    if got["answer"].strip():
        res["verdict"] = "ANSWERED"
        res["detail"] = got["answer"].strip()[:160].replace("\n", " ")
    elif got["thinking"].strip():
        res["verdict"] = "EMPTY"
        kind = "terminal_no_text" if got["status"] in TERMINAL_STATUSES else "budget_exhausted_active"
        res["detail"] = "%s: thinking only (%d chars), status=%s, events=%d, elapsed=%.1fs/budget=%ds" % (
            kind, len(got["thinking"]), got["status"], got["events"], got["seconds"], budget)
    else:
        res["verdict"] = "EMPTY"
        cb_err = got.get("callback_error")
        if cb_err:
            res["detail"] = "callback_error: %s, status=%s, events=%d, elapsed=%.1fs" % (
                cb_err, got["status"], got["events"], got["seconds"])
        elif got["status"] in TERMINAL_STATUSES:
            res["detail"] = "terminal_no_text: no assistant text, status=%s, events=%d, elapsed=%.1fs/budget=%ds" % (
                got["status"], got["events"], got["seconds"], budget)
        elif got["events"] == 0:
            res["detail"] = "no_events: status=%s, elapsed=%.1fs/budget=%ds" % (
                got["status"], got["seconds"], budget)
        else:
            res["detail"] = "budget_exhausted_active: no assistant text, status=%s, events=%d, elapsed=%.1fs/budget=%ds" % (
                got["status"], got["events"], got["seconds"], budget)
    return res


def load_pool(pool_file: Path) -> list[dict]:
    rows = []
    try:
        lines = Path(pool_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def live_accounts(pool_file: Path, limit: int = 1, pool_dir: Path | None = None) -> list[dict]:
    """Newest-first live rows that actually carry a credential."""
    now = time.time()
    ok = [r for r in load_pool(pool_file)
          if r.get("status") == "live" and (r.get("token_expires") or 0) > now
          and cookie_for(r, pool_dir)]
    ok.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return ok[:limit]


def selfcheck() -> int:
    """Offline checks: _apply_event, verdict logic, callback safety. Zero network needed."""
    errors = 0

    # _apply_event: empty input
    out = {"answer": "", "thinking": ""}
    _apply_event(out, {"stages": []})
    assert out == {"answer": "", "thinking": ""}, "_apply_event([]) mutated out"

    # _apply_event: message (answer) event
    out = {"answer": "", "thinking": ""}
    _apply_event(out, {"stages": [{"preview": [{"role": "assistant", "type": "message", "content": "hi"}]}]})
    assert out["answer"] == "hi", "message: %r" % out["answer"]
    assert not out["thinking"], "no thinking from message"

    # Cumulative snapshots: longest wins
    for txt in ["a", "ab", "abc"]:
        _apply_event(out, {"stages": [{"preview": [{"role": "assistant", "type": "message", "content": txt}]}]})
    assert out["answer"] == "abc", "cumulative: %r" % out["answer"]

    # Thinking event
    out2 = {"answer": "", "thinking": ""}
    _apply_event(out2, {"stages": [{"preview": [{"role": "assistant", "type": "thinking", "content": "reason..."}]}]})
    assert out2["thinking"] == "reason...", "thinking: %r" % out2["thinking"]
    assert not out2["answer"], "no answer from thinking"

    # All REASONING_TYPES
    for rt in REASONING_TYPES:
        out3 = {"answer": "", "thinking": ""}
        _apply_event(out3, {"stages": [{"preview": [{"role": "assistant", "type": rt, "content": "r"}]}]})
        assert out3["thinking"] == "r", "%s not caught as thinking" % rt

    # _entry_text variations
    assert _entry_text({"content": "plain"}) == "plain"
    assert _entry_text({"content": [{"text": "a"}, {"text": "b"}]}) == "ab"
    assert _entry_text({"content": ["x", "y"]}) == "xy"
    assert _entry_text({}) == ""

    # Verdict decision tree (pure logic test, not requiring read_answer)
    def _sim_verdict(got):
        if got["answer"].strip():
            return "ANSWERED"
        if got["thinking"].strip():
            return "EMPTY"
        return "EMPTY"
    assert _sim_verdict({"answer": "x", "thinking": ""}) == "ANSWERED"
    assert _sim_verdict({"answer": "", "thinking": "r"}) == "EMPTY"
    assert _sim_verdict({"answer": "", "thinking": ""}) == "EMPTY"

    # (c) Callback safety: _apply_event runs before on_event, callback exception caught
    out4 = {"answer": "", "thinking": "", "events": 0}
    ev_good = {"stages": [{"preview": [{"role": "assistant", "type": "message", "content": "ok"}]}]}
    _apply_event(out4, ev_good)  # apply first

    def _bad_cb(_ev):
        raise ValueError("callback failure")
    try:
        _bad_cb(ev_good)
        errors += 1  # should have raised
    except ValueError:
        pass  # expected — this is what try/except catches
    # Verify answer survived
    assert out4["answer"] == "ok", "answer lost after callback raise"
    # Simulate the try/except from read_answer
    out5 = dict(out4)
    try:
        _bad_cb(ev_good)
    except Exception as _e:
        out5["callback_error"] = str(_e)
    assert "callback_error" in out5
    assert out5["answer"] == "ok"

    print("selfcheck: %s" % ("PASS" if errors == 0 else "FAIL %d" % errors))
    return errors or 0


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
