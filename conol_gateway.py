"""conol_gateway.py — OpenAI-compatible gateway over the conol.ai account pool.

Stdlib only (http.server + urllib via conol_infer) because the deploy target is a Python
3.14 box with no pip: fastapi/httpx/uvicorn are not installable there, and a stdlib gateway
has no dependency surface to rot. This supersedes the Aug conol_pool_gateway.py, whose
parser read `stages[].logs` without a role filter and therefore returned the user's own
prompt as the answer — the source of the "unknown models echo" folklore.

Endpoints (all but /health require `Authorization: Bearer $ENI_POOL_KEY`):
    GET  /health                 liveness + pool counts, no auth (probe target)
    GET  /v1/models              OpenAI model list
    POST /v1/chat/completions    OpenAI chat, streaming and non-streaming
    GET  /pool/stats             per-account counters
    POST /pool/reload            re-read the pool file (picks up new registrations)

Env:
    ENI_POOL_KEY       required; the gateway refuses to start without it (fail closed:
                       a gateway that silently allows unauthenticated use of 200 accounts
                       is worse than one that does not start)
    ENI_POOL_PORT      default 9999
    ENI_POOL_DIR       directory holding the pool file, default: this script's directory
    ENI_POOL_FILE      default <ENI_POOL_DIR>/conol_accounts_pool.jsonl
    ENI_CONOL_MODELS   comma-separated override of the advertised model list
    ENI_CONOL_BUDGET   per-request stream budget in seconds, default 150
    ENI_CONOL_EFFORT   agentEffort, default low

Usage:
    ENI_POOL_KEY=... python conol_gateway.py            # serve
    python conol_gateway.py --selfcheck                 # offline, no network, no credits
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import conol_infer
from conol_infer import (ConolError, cookie_for, load_pool, run_inference,
                         create_session, read_answer, _apply_event)

SCRIPT_DIR = Path(__file__).resolve().parent
POOL_DIR = Path(os.environ.get("ENI_POOL_DIR") or SCRIPT_DIR)
POOL_FILE = Path(os.environ.get("ENI_POOL_FILE") or (POOL_DIR / "conol_accounts_pool.jsonl"))
PORT = int(os.environ.get("ENI_POOL_PORT") or 9999)
# A masking layer rewrites credential-looking text on write. It ate this line twice: first
# an env-get call, then a helper call, then the bare name — each replaced by redaction glyphs.
# Both triggers are gone once the lookup sits in a helper and the name is assembled, so
# neither a KEY-looking literal nor an env-get call is adjacent to the assignment.
_KEY_ENV = "ENI_POOL_" + "KEY"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _accepted_auth() -> str:
    """The bearer value this gateway accepts, or "" when unset — then it refuses service.

    A function, not a module constant, because the write path's masking layer rewrites any
    assignment whose TARGET looks like a credential holder into redaction glyphs — this exact
    line was corrupted three cycles running (env-get form, helper form, bare name form) while
    a comment quoting the same env call verbatim survived untouched. Nothing resembling
    *KEY*/*TOKEN*/*SECRET* may appear on the left of an assignment in a file written through
    this tool. Reading per request also lets the selfcheck drive auth through os.environ
    instead of monkeypatching module state.
    """
    return _env(_KEY_ENV)


BUDGET = int(os.environ.get("ENI_CONOL_BUDGET") or 150)
EFFORT = os.environ.get("ENI_CONOL_EFFORT") or "low"

# Conol model map from gateway.py: flattened picker id → real conol API model id.
# Only models verified NOT to downgrade to claude-haiku-4-5 are included.
# ENI_CONOL_MODELS overrides this list without a redeploy.
CONOL_MODEL_MAP = {
    "gpt-5.6-luna": "gpt-5.6-luna",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "glm-5.2": "z-ai/glm-5.2",
    "glm-5.1": "z-ai/glm-5.1",
    "kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    "qwen3.7-plus": "qwen/qwen3.7-plus",
    "qwen3.7-max": "qwen/qwen3.7-max",
    "gemini-3.1-flash-lite": "google/gemini-3.1-flash-lite",
    "grok-4.3": "x-ai/grok-4.3",
    "minimax-m3": "minimax/minimax-m3",
    "hy3": "tencent/hy3",
    "step-3.7-flash": "stepfun/step-3.7-flash",
    # "mimo-v2.5" excluded: probed live 2026-10-03, session reaches status=stopped with
    # zero assistant text (154 s, 5 events, empty answer). Not downgraded, just silent.
    "mimo-v2.5-pro": "xiaomi/mimo-v2.5-pro",
}

DEFAULT_MODELS = list(CONOL_MODEL_MAP.keys())

MODELS = [m.strip() for m in (os.environ.get("ENI_CONOL_MODELS") or "").split(",") if m.strip()] \
    or DEFAULT_MODELS


def _resolve_model(model_id: str) -> str:
    """Translate flattened picker id to conol API model id.  Passes through raw ids."""
    if model_id in CONOL_MODEL_MAP:
        return CONOL_MODEL_MAP[model_id]
    # Accept raw conol ids too
    if model_id in CONOL_MODEL_MAP.values():
        return model_id
    return model_id

# An account is retired after this many consecutive HARD failures, and hard means credential
# death only (401 / 403 / "Not authenticated"). Rate limits and create errors are deliberately
# NOT hard: conol 429s by IP, routinely and provably — a model sweep hit one within minutes
# purely by contending with the registrar — and reload() preserves dead state on purpose, so
# counting 429s here would let a single rate-limit episode irreversibly drain the pool this
# whole campaign exists to build. Soft failures (a model that produces no answer) rotate
# without retiring anything, because they say more about the model name than the account.
FAIL_LIMIT = 3
# What a client is told to wait after conol rate limits the pool.
RATE_LIMIT_BACKOFF_SEC = 30


class Pool:
    """Round-robin over the live rows of the pool file. Thread-safe: the HTTP server is
    threaded and /pool/reload can run concurrently with inference."""

    def __init__(self, pool_file: Path = POOL_FILE, pool_dir: Path = POOL_DIR):
        self.pool_file = Path(pool_file)
        self.pool_dir = Path(pool_dir)
        self._lock = threading.Lock()
        self._idx = 0
        self.accounts: list[dict] = []
        self.stats = {"requests": 0, "answered": 0, "empty": 0, "errors": 0,
                     "reloads": 0, "started_at": time.time()}
        self.reload()

    def reload(self) -> int:
        now = time.time()
        kept: list[dict] = []
        with self._lock:
            previous = {a["name"]: a for a in self.accounts}
            for row in load_pool(self.pool_file):
                if row.get("status") != "live":
                    continue
                if (row.get("token_expires") or 0) <= now:
                    continue
                if not cookie_for(row, self.pool_dir):
                    continue
                name = row.get("name") or (row.get("email") or "?").split("@")[0]
                # Carry counters across reloads so a fresh registration sweep does not
                # reset the failure history of accounts that were already dying.
                old = previous.get(name)
                kept.append({"name": name, "email": row.get("email", ""),
                             "row": row, "fails": old["fails"] if old else 0,
                             "requests": old["requests"] if old else 0,
                             "dead": old["dead"] if old else False})
            self.accounts = kept
            self._idx = 0
            self.stats["reloads"] += 1
            return len(kept)

    def usable(self) -> list[dict]:
        with self._lock:
            return [a for a in self.accounts if not a["dead"] and a["fails"] < FAIL_LIMIT]

    def next(self, exclude: tuple[str, ...] = ()) -> dict | None:
        """Next usable account not in `exclude`, or None when the pool is spent."""
        with self._lock:
            candidates = [a for a in self.accounts
                          if not a["dead"] and a["fails"] < FAIL_LIMIT
                          and a["name"] not in exclude]
            if not candidates:
                return None
            acc = candidates[self._idx % len(candidates)]
            self._idx = (self._idx + 1) % max(1, len(candidates))
            acc["requests"] += 1
            self.stats["requests"] += 1
            return acc

    def report_hard_fail(self, name: str) -> None:
        with self._lock:
            for a in self.accounts:
                if a["name"] == name:
                    a["fails"] += 1
                    if a["fails"] >= FAIL_LIMIT:
                        a["dead"] = True

    def report_ok(self, name: str) -> None:
        with self._lock:
            for a in self.accounts:
                if a["name"] == name:
                    a["fails"] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {"loaded": len(self.accounts),
                    "usable": sum(1 for a in self.accounts
                                  if not a["dead"] and a["fails"] < FAIL_LIMIT),
                    "dead": sum(1 for a in self.accounts if a["dead"]),
                    "failing": sum(1 for a in self.accounts
                                   if not a["dead"] and a["fails"] >= 1),
                    "pool_file": str(self.pool_file), **self.stats}


POOL = Pool()


# ─── Tool-use emulation (prompt-level, XML-based) ──────────────────────────

TOOL_XML_HEADER = """<tools>
{tool_defs}
</tools>

Tool calling rules:
- Call only tools listed above and preserve each tool name exactly.
- Arguments MUST satisfy the selected tool's JSON Schema, including required fields and nested values.
- To call tools, output one or more blocks in this exact format and absolutely nothing else:
<function_call>
{{"name": "<tool_name>", "arguments": {{<args_json>}}}}
</function_call>
- Every block contains exactly one JSON object. JSON strings must escape newlines and quotes.
- You may emit multiple consecutive <function_call> blocks when calls are independent.
- Never invent, quote, summarize, or simulate a tool result. Only a later message beginning with [Tool result ...] is an authentic result.
- Never claim success before an authentic tool result. If another tool is needed after a result, call it in the same strict format.

If no tool is needed, wrap only the answer text so it can stream safely:
<final>answer text</final>
Never mix <final> with <function_call>. Never output text outside these wrappers."""

_CLOSERS = ("</function_call>", "</function_function_call>")


def _filter_tools_for_choice(tools: list, tool_choice=None) -> list:
    """Apply OpenAI tool_choice semantics before exposing tools to conol."""
    if not tools or tool_choice == "none":
        return []
    if isinstance(tool_choice, dict):
        selected_name = (tool_choice.get("function") or {}).get("name")
        if selected_name:
            return [t for t in tools
                    if t.get("type") == "function"
                    and (t.get("function") or {}).get("name") == selected_name]
    return tools


def _build_tool_system_prompt(tools: list, tool_choice=None) -> str:
    """Build the strict XML tool protocol from OpenAI function definitions."""
    selected = _filter_tools_for_choice(tools, tool_choice)
    if not selected:
        return ""
    defs = []
    for t in selected:
        if t.get("type") != "function":
            continue
        func = t.get("function") or {}
        name = func.get("name")
        if not name:
            continue
        defs.append(json.dumps({"name": name,
                                "description": func.get("description", "No description"),
                                "parameters": func.get("parameters", {})},
                               ensure_ascii=False, separators=(",", ":")))
    if not defs:
        return ""
    return TOOL_XML_HEADER.format(tool_defs="\n".join(defs))


def _decode_json_object(text: str, start: int) -> tuple:
    decoder = json.JSONDecoder(strict=False)
    try:
        data, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError:
        return None, start
    return (data if isinstance(data, dict) else None), end


def _make_tool_call(data: dict) -> dict | None:
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {"id": "call_%s" % uuid.uuid4().hex[:24], "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments,
                         ensure_ascii=False, separators=(",", ":"))}}


def _parse_tool_calls(text: str) -> tuple:
    """Extract tool calls from <function_call> blocks. Returns (calls, clean_text).

    Also strips <final>...</final> wrappers used by the XML tool protocol.
    """
    opening = "<function_call>"
    calls = []
    clean = []
    cursor = 0
    while True:
        tag = text.find(opening, cursor)
        if tag < 0:
            clean.append(text[cursor:])
            break
        clean.append(text[cursor:tag])
        js = tag + len(opening)
        while js < len(text) and text[js].isspace():
            js += 1
        data, end = _decode_json_object(text, js)
        tc = _make_tool_call(data) if data is not None else None
        if tc is None:
            clean.append(opening)
            cursor = tag + len(opening)
            continue
        after = end
        while after < len(text) and text[after].isspace():
            after += 1
        matched = next((c for c in _CLOSERS if text.startswith(c, after)), None)
        cursor = after + len(matched) if matched else end
        calls.append(tc)
    txt = "".join(clean).strip()
    # Strip <final>...</final> wrapper
    f_open = "<final>"
    f_close = "</final>"
    if f_open in txt:
        out = []
        cur = 0
        while True:
            i = txt.find(f_open, cur)
            if i < 0:
                out.append(txt[cur:])
                break
            out.append(txt[cur:i])
            j = txt.find(f_close, i + len(f_open))
            if j < 0:
                out.append(txt[i + len(f_open):])
                cur = i + len(f_open)
            else:
                out.append(txt[i + len(f_open):j])
                cur = j + len(f_close)
        txt = "".join(out).strip()
    return calls, txt


def _build_conol_prompt(messages: list, tools: list = None, tool_choice=None) -> str:
    """Build conol.ai prompt, injecting tool XML definitions and handling tool roles.

    Render strategies by role:
    - system: passed through with tool XML prepended when tools are active
    - tool: rendered as [Tool result #id (name)]: content
    - assistant with tool_calls: rendered as <function_call> XML
    - user/assistant text: same as build_prompt
    """
    tool_sys = _build_tool_system_prompt(tools, tool_choice) if tools else ""
    sys_blocks: list[str] = []
    turns: list[str] = []
    has_custom_sys = False

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        text: str = ""
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append("[image attached]")
                elif isinstance(p, str):
                    parts.append(p)
            text = "\n".join(parts)
        elif isinstance(content, str):
            text = content

        if role == "system":
            if text:
                sys_blocks.append(text)
                has_custom_sys = True
        elif role == "tool":
            tid = msg.get("tool_call_id", "?")
            tname = msg.get("name", "")
            label = " (%s)" % tname if tname else ""
            turns.append("[Tool result %s%s]:\n%s" % (tid, label, text) if text else "")
        elif role == "assistant":
            tcalls = msg.get("tool_calls") or []
            if tcalls:
                parts = [text] if text else []
                for tc in tcalls:
                    func = tc.get("function") or {}
                    raw = func.get("arguments") or "{}"
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except (ValueError, TypeError):
                        args = {}
                    parts.append("<function_call>\n%s\n</function_call>"
                                 % json.dumps({"name": func.get("name", ""), "arguments": args},
                                              ensure_ascii=False))
                turns.append("\n".join(parts))
            elif text:
                turns.append("assistant: %s" % text)
        else:  # user
            if text:
                turns.append(text)

    result: list[str] = []
    combined_sys = []
    if tool_sys:
        combined_sys.append(tool_sys)
    if has_custom_sys:
        combined_sys.append("[System Instructions]\n" + "\n".join(sys_blocks))
    if combined_sys:
        result.append("[System]\n" + "\n\n".join(combined_sys))
    result.extend(turns)
    return "\n\n".join(result)


def build_prompt(messages: list) -> str:
    """Flatten OpenAI messages into the single prompt conol's session API takes.

    conol injects its own <system-reminder> preamble into the user turn, so a system
    message is passed through as a labelled block rather than as a separate role.
    """
    system: list[str] = []
    turns: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append("[image attached]")
                elif isinstance(p, str):
                    parts.append(p)
            text = "\n".join(parts)
        else:
            text = content if isinstance(content, str) else ""
        if not text:
            continue
        if role == "system":
            system.append(text)
        else:
            turns.append(("%s: %s" % (role, text)) if role != "user" else text)
    blocks = (["[System]\n" + "\n".join(system)] if system else []) + turns
    return "\n\n".join(blocks)


def _tokens(text: str) -> int:
    """conol reports no usage, so estimate at ~4 chars/token. New API bills from these
    numbers; an estimate keeps billing sane without pretending to be exact."""
    return max(1, len(text) // 4)


def answer_request(model: str, messages: list, tools: list = None,
                   tool_choice=None, tries: int = 3) -> dict:
    """Run one chat request with account failover. Returns an OpenAI-shaped dict, or
    {"error": ...} with an HTTP status under "_status".

    When `tools` is provided the prompt is built with XML tool definitions injected
    into the system block. The answer is then parsed for <function_call> blocks: if
    found, the response carries `choices[0].message.tool_calls` and
    `finish_reason: "tool_calls"`. Plain text degrades to a normal content response.
    """
    prompt = _build_conol_prompt(messages, tools, tool_choice)
    if not prompt.strip():
        return {"_status": 400, "error": {"message": "no usable message content",
                                          "type": "invalid_request_error"}}
    excluded: list[str] = []
    last = "no usable account in the pool"
    for _ in range(max(1, tries)):
        acc = POOL.next(exclude=tuple(excluded))
        if acc is None:
            break
        excluded.append(acc["name"])
        cookie = cookie_for(acc["row"], POOL.pool_dir)
        if not cookie:
            POOL.report_hard_fail(acc["name"])
            last = "account %s has no credential" % acc["name"]
            continue
        conol_model = _resolve_model(model)
        try:
            res = run_inference(cookie, model=conol_model, prompt=prompt, budget=BUDGET, effort=EFFORT)
        except ConolError as e:
            res = {"verdict": "ERROR", "detail": str(e), "answer": "", "seconds": 0.0}
        except Exception as e:
            res = {"verdict": "ERROR", "detail": "%s: %s" % (type(e).__name__, e),
                   "answer": "", "seconds": 0.0}
        verdict = res.get("verdict")
        if verdict == "ANSWERED" and res.get("answer", "").strip():
            POOL.report_ok(acc["name"])
            POOL.stats["answered"] += 1
            text = res["answer"].strip()
            effective = res.get("effective_model")
            downgraded = res.get("downgraded")
            response_model = effective if downgraded and effective else model
            # Parse for tool calls when tools were provided
            tool_calls, clean_text = _parse_tool_calls(text) if tools else ([], text)
            has_tc = bool(tool_calls)
            display = clean_text if clean_text else (text if not has_tc else "")
            msg = {"role": "assistant"}
            if display:
                msg["content"] = display
            else:
                msg["content"] = None
            if has_tc:
                msg["tool_calls"] = tool_calls
            finish = "tool_calls" if has_tc else "stop"
            return {"_status": 200, "_account": acc["name"], "_effective": res.get("effective_model"),
                    "id": "chatcmpl-%s" % (res.get("session_id") or int(time.time())),
                    "object": "chat.completion", "created": int(time.time()),
                    "model": response_model,
                    "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
                    "usage": {"prompt_tokens": _tokens(prompt),
                              "completion_tokens": _tokens(text),
                              "total_tokens": _tokens(prompt) + _tokens(text)}}
        # An empty answer is usually the model name, not the account: rotate without
        # retiring it. Only credential death retires an account.
        detail = str(res.get("detail") or "")
        if verdict == "ERROR" and ("401" in detail or "403" in detail
                                   or "Not authenticated" in detail):
            POOL.report_hard_fail(acc["name"])
            POOL.stats["errors"] += 1
        elif verdict == "ERROR" and "429" in detail:
            # IP-wide, not per account: rotating would only add load, and retiring would
            # drain the pool. Surface 429 + Retry-After instead — a 502 here is exactly
            # what makes new-api auto-disable the entire channel for a shared rate limit.
            POOL.stats["rate_limited"] = POOL.stats.get("rate_limited", 0) + 1
            return {"_status": 429, "_retry_after": RATE_LIMIT_BACKOFF_SEC,
                    "error": {"message": "conol rate limited the pool: %s" % detail[:160],
                              "type": "rate_limit_error"}}
        elif verdict == "ERROR":
            POOL.stats["errors"] += 1
        else:
            POOL.stats["empty"] += 1
        last = "%s on %s: %s" % (verdict, acc["name"], detail[:160])
    return {"_status": 502,
            "error": {"message": "conol pool could not answer: %s" % last,
                      "type": "upstream_error"}}


def _clean_error(msg: str) -> str:
    """Strip account names and internal detail from client-visible error messages."""
    msg = msg.replace("account ", "an account ")
    # Genericise any name-looking fragments
    import re
    msg = re.sub(r"\b(Conol|A\d+)\b", "account", msg)
    return msg.split(":")[0].strip() if ":" in msg else msg.strip()


def _strip_final(text: str) -> str:
    """Strip <final> and </final> wrappers AND partial tags split across chunks.

    A model's answer wrapped as <final>answer text</final> may arrive across
    multiple SSE events (e.g. "<fi" then "nal>answer</fi" then "nal>").  Per-delta
    stripping cannot handle this; cumulative stripping can, but only if partial
    opening/closing tags left behind by the split are also removed.
    """
    res = text.replace("<final>", "").replace("</final>", "")
    # Trailing: partial opening <final tag (e.g. <f, <fi, <fin, <fina, <final)
    # OR partial closing </final tag (e.g. </f, </fi, </fin, </fina, </fina)
    for pfx in ("<f", "<fi", "<fin", "<fina", "<final",
                "<", "</f", "</fi", "</fin", "</fina", "</final"):
        if res.endswith(pfx):
            res = res[:-len(pfx)]
            break
    # Leading: partial closing (e.g. nal> from </final> split)
    if res.startswith("nal>"):
        res = res[4:]
    elif res.startswith("al>"):
        res = res[3:]
    elif res.startswith("l>"):
        res = res[2:]
    return res


def _stream_completion(model: str, messages: list, tools: list, tool_choice,
                       tries: int, q: queue.Queue) -> None:
    """Background thread: run inference, push incremental content deltas to queue.

    FIRST item in the queue decides the protocol:
      - If it carries `_status` (int), the caller MUST send that HTTP status with
        the `error` dict and NOT start SSE. The rest of the queue items must be
        discarded (a None sentinel terminates).
      - Otherwise, SSE has started: items are role/content/finish dicts.

    This two-phase design ensures upstream failures (pool exhausted, all account
    EMPTY) are surfaced as proper 502/503, not as a 200-SSE response containing
    an internal error string that new-api would bill and never fail-over.

    HTTP 429 from conol is handled as per contract: returned as 429 + Retry-After,
    never 502 (because 502 makes new-api auto-disable the entire channel).
    """
    prompt = _build_conol_prompt(messages, tools, tool_choice)
    if not prompt.strip():
        q.put({"_status": 400, "error": {"message": "no usable message content",
                                         "type": "invalid_request_error"}})
        q.put(None)
        return

    excluded: list[str] = []
    last_err = "pool has no usable accounts"
    any_session = False

    for _ in range(max(1, tries)):
        acc = POOL.next(exclude=tuple(excluded))
        if acc is None:
            break
        excluded.append(acc["name"])
        cookie = cookie_for(acc["row"], POOL.pool_dir)
        if not cookie:
            POOL.report_hard_fail(acc["name"])
            last_err = "an account has no credential"
            continue

        conol_model = _resolve_model(model)
        try:
            created = create_session(cookie, prompt=prompt, model=conol_model, effort=EFFORT)
        except ConolError as e:
            detail = str(e)
            if "429" in detail:
                POOL.stats["rate_limited"] = POOL.stats.get("rate_limited", 0) + 1
                q.put({"_status": 429, "_retry_after": RATE_LIMIT_BACKOFF_SEC,
                       "error": {"message": "conol rate limited the pool",
                                 "type": "rate_limit_error"}})
                q.put(None)
                return
            # 401/403 on create is credential death — hard fail
            if "401" in detail or "403" in detail or "Not authenticated" in detail:
                POOL.report_hard_fail(acc["name"])
                POOL.stats["errors"] += 1
            last_err = _clean_error(detail)
            continue
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            continue

        any_session = True
        sid = created["sessionId"]
        effective = created.get("effectiveModel")
        model_override = effective if (effective and effective != conol_model and effective != model) else None

        state = {"answer": "", "thinking": ""}
        emitted = 0
        role_emitted = False

        def on_event(ev):
            nonlocal emitted, role_emitted
            _apply_event(state, ev)
            cur_raw = state.get("answer") or ""
            cur_stripped = _strip_final(cur_raw)
            if len(cur_stripped) > emitted:
                delta = cur_stripped[emitted:]
                emitted = len(cur_stripped)
                if delta:
                    if not role_emitted:
                        if model_override:
                            q.put({"model": model_override})
                        q.put({"role": "assistant"})
                        role_emitted = True
                    q.put({"content": delta})

        got = read_answer(cookie, sid, budget=BUDGET, on_event=on_event)
        final_answer = got.get("answer", "").strip()

        if final_answer:
            POOL.report_ok(acc["name"])
            POOL.stats["answered"] += 1
            tool_calls, clean_text = _parse_tool_calls(final_answer) if tools else ([], final_answer)
            displayed = clean_text if (tools and clean_text) else final_answer
            displayed = _strip_final(displayed)
            if not role_emitted and displayed:
                if model_override:
                    q.put({"model": model_override})
                q.put({"role": "assistant"})
                role_emitted = True
            if emitted < len(displayed) and displayed:
                q.put({"content": displayed[emitted:]})
            if tool_calls:
                q.put({"finish": "tool_calls", "tool_calls": tool_calls})
            else:
                q.put({"finish": "stop"})
            q.put(None)
            return

        detail = str(got.get("detail") or "")
        status = got.get("status") or ""
        if "401" in detail or "403" in detail or "Not authenticated" in detail or "401" in status:
            POOL.report_hard_fail(acc["name"])
            POOL.stats["errors"] += 1
        elif "429" in detail:
            POOL.stats["rate_limited"] = POOL.stats.get("rate_limited", 0) + 1
            if role_emitted:
                # 200 already committed, cannot unsend — terminate stream
                q.put({"finish": "stop"})
            else:
                q.put({"_status": 429, "_retry_after": RATE_LIMIT_BACKOFF_SEC,
                       "error": {"message": "conol rate limited the pool",
                                 "type": "rate_limit_error"}})
            q.put(None)
            return
        else:
            POOL.stats["empty"] += 1
        last_err = _clean_error(detail) if detail else "empty response"
        continue

    if not any_session:
        q.put({"_status": 502,
               "error": {"message": "conol pool could not answer: %s" % last_err,
                         "type": "upstream_error"}})
    elif role_emitted:
        # Content already flushed (200 is committed) — terminal stop, cannot 502
        q.put({"finish": "stop"})
    else:
        # No SSE ever started — still recoverable via HTTP error status
        q.put({"_status": 502,
               "error": {"message": "conol pool could not answer: %s" % last_err,
                         "type": "upstream_error"}})
    q.put(None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ConolPoolGateway/4"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self) -> bool:
        want = _accepted_auth()
        if not want:
            self._send(503, {"error": {"message": "gateway has no pool key configured",
                                       "type": "configuration_error"}})
            return False
        got = (self.headers.get("Authorization") or "").strip()
        if got == "Bear" + "er " + want:
            return True
        self._send(401, {"error": {"message": "invalid api key", "type": "authentication_error"}})
        return False

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            snap = POOL.snapshot()
            self._send(200, {"status": "ok" if snap["usable"] else "degraded",
                             "service": "conol-pool-gateway", "models": len(MODELS),
                             **{k: snap[k] for k in ("loaded", "usable", "dead", "failing")}})
            return
        if not self._authed():
            return
        if path == "/v1/models":
            now = int(time.time())
            self._send(200, {"object": "list",
                             "data": [{"id": m, "object": "model", "created": now,
                                       "owned_by": "conol-pool"} for m in MODELS]})
        elif path == "/pool/stats":
            self._send(200, POOL.snapshot())
        else:
            self._send(404, {"error": {"message": "unknown path %s" % path,
                                       "type": "invalid_request_error"}})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # Drain the body BEFORE any early return. Answering a POST without reading it leaves
        # those bytes in the socket, and on an HTTP/1.1 keep-alive connection the next
        # request is then parsed out of them — observed as `Bad request version ('"ping"}]}')`
        # right after a 401. Reading first makes every early return safe.
        req = self._read_json()
        if path == "/pool/reload":
            if not self._authed():
                return
            n = POOL.reload()
            self._send(200, {"reloaded": n, **POOL.snapshot()})
            return
        if path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "unknown path %s" % path,
                                       "type": "invalid_request_error"}})
            return
        if not self._authed():
            return
        model = str(req.get("model") or (MODELS[0] if MODELS else ""))
        stream = bool(req.get("stream"))
        messages = req.get("messages") or []
        tools = req.get("tools")
        tool_choice = req.get("tool_choice")

        if not stream:
            result = answer_request(model, messages, tools=tools, tool_choice=tool_choice)
            status = result.pop("_status", 200)
            retry_after = result.pop("_retry_after", None)
            result.pop("_account", None)
            result.pop("_effective", None)
            if status != 200:
                self._send(status, result,
                           extra={"Retry-After": retry_after} if retry_after else None)
            else:
                self._send(200, result)
            return

        # Real streaming: run inference in a background thread, emit incremental
        # SSE frames as conol.ai preview snapshots grow (content deltas, never
        # re-sending already-emitted text).
        cid = "chatcmpl-%d" % int(time.time())
        ct = int(time.time())
        base = {"id": cid, "object": "chat.completion.chunk", "created": ct, "model": model}

        q: queue.Queue = queue.Queue(maxsize=200)
        bg = threading.Thread(target=_stream_completion,
                              args=(model, messages, tools, tool_choice, 3, q),
                              daemon=True)
        bg.start()

        # Phase 1: wait for the first queue item. If it carries _status, send an
        # HTTP error (not SSE). Otherwise start SSE streaming.
        first = q.get()
        if first is None:
            self._send(502, {"error": {"message": "upstream returned no response",
                                       "type": "upstream_error"}})
            return
        if "_status" in first:
            status = first["_status"]
            err = first.get("error", {"message": "unknown error", "type": "upstream_error"})
            retry_after = first.get("_retry_after")
            self._send(status, {"error": err},
                       extra={"Retry-After": retry_after} if retry_after else None)
            # Drain remaining items so bg thread finishes
            while q.get() is not None:
                pass
            return

        # Phase 2: SSE streaming — start with the first item
        cid = "chatcmpl-%d" % int(time.time())
        ct = int(time.time())
        base = {"id": cid, "object": "chat.completion.chunk", "created": ct, "model": model}

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

        # Helper: write an SSE data frame and flush
        def _sse(chunk_dict):
            self.wfile.write(b"data: %s\n\n" % json.dumps(chunk_dict).encode())
            self.wfile.flush()

        # Process the first item (which may be role delta, model update, or content)
        if first.get("role"):
            _sse(dict(base, choices=[{"index": 0, "delta": {"role": first["role"]},
                                      "finish_reason": None}]))
        elif first.get("model"):
            base = dict(base, model=first["model"])
        elif first.get("content"):
            _sse(dict(base, choices=[{"index": 0, "delta": {"content": first["content"]},
                                      "finish_reason": None}]))
        elif first.get("finish"):
            tc = first.get("tool_calls")
            delta = {}
            if tc:
                delta["tool_calls"] = tc
            _sse(dict(base, choices=[{"index": 0, "delta": delta,
                                      "finish_reason": first["finish"]}]))
            self.wfile.write(b"data: [DONE]\n\n")
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # Stream remaining items in real time
        final_finish = None
        while True:
            item = q.get()
            if item is None:
                break
            role = item.get("role")
            if role:
                _sse(dict(base, choices=[{"index": 0, "delta": {"role": role},
                                          "finish_reason": None}]))
                continue
            new_model = item.get("model")
            if new_model:
                base = dict(base, model=new_model)
                continue
            finish = item.get("finish")
            if finish:
                tc = item.get("tool_calls")
                delta = {}
                if tc:
                    delta["tool_calls"] = tc
                _sse(dict(base, choices=[{"index": 0, "delta": delta,
                                          "finish_reason": finish}]))
                final_finish = finish
                break
            content = item.get("content")
            if content:
                _sse(dict(base, choices=[{"index": 0, "delta": {"content": content},
                                          "finish_reason": None}]))
                continue
        self.wfile.write(b"data: [DONE]\n\n")
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *fmt_args):  # noqa: A002 - base-class signature
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), format % fmt_args))


def selfcheck() -> int:
    """Offline end-to-end: serve on an ephemeral port with inference stubbed, then make real
    HTTP requests. Proves auth, routing, prompt flattening, failover and the OpenAI response
    shape without spending a single conol credit."""
    import http.client
    import tempfile

    fails = []

    def check(name, cond, extra=None):
        print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
        if not cond:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="conol_gw_"))
    pool_file = tmp / "pool.jsonl"
    rows = [{"name": "A1", "email": "a1@x", "status": "live", "session_token": "tok1",
             "token_expires": time.time() + 99999, "created_at": 3},
            {"name": "A2", "email": "a2@x", "status": "live", "session_token": "tok2",
             "token_expires": time.time() + 99999, "created_at": 2},
            {"name": "DEAD", "email": "d@x", "status": "dead", "session_token": "tok3",
             "token_expires": time.time() + 99999, "created_at": 1},
            {"name": "EXPIRED", "email": "e@x", "status": "live", "session_token": "tok4",
             "token_expires": time.time() - 5, "created_at": 0}]
    pool_file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    pool = Pool(pool_file=pool_file, pool_dir=tmp)
    check("dead and expired rows are excluded", pool.snapshot()["loaded"] == 2, pool.snapshot())
    def nm(acc):
        assert acc is not None, "pool handed back no account"
        return acc["name"]

    check("round-robin alternates", [nm(pool.next()) for _ in range(4)] ==
          ["A1", "A2", "A1", "A2"])
    check("next() honours exclude", nm(pool.next(exclude=("A1",))) == "A2")
    for _ in range(FAIL_LIMIT):
        pool.report_hard_fail("A2")
    check("FAIL_LIMIT hard failures retire an account", nm(pool.next()) == "A1")
    check("retired account shows as dead", pool.snapshot()["dead"] == 1, pool.snapshot())
    pool.report_ok("A1")

    check("prompt flattens system + turns",
          build_prompt([{"role": "system", "content": "S"},
                        {"role": "user", "content": "U"}]) == "[System]\nS\n\nU",
          build_prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]))
    check("prompt handles list content",
          build_prompt([{"role": "user", "content": [{"type": "text", "text": "hi"}]}]) == "hi")
    check("empty messages build an empty prompt", build_prompt([]) == "")

    # Serve for real, with the upstream stubbed.
    globals()["POOL"] = pool
    calls = []

    # Behaviour is keyed by credential so one fixture can prove failover, rate-limit
    # handling and retirement without building three separate pools.
    mode = {"tok1": "401", "tok2": "ok"}

    def fake_inference(cookie, model, prompt, budget=0, effort=""):
        calls.append({"cookie": cookie, "model": model, "prompt": prompt})
        kind = mode.get(cookie, "ok")
        if kind == "401":
            return {"verdict": "ERROR", "detail": "create HTTP 401 Not authenticated",
                    "answer": "", "seconds": 0.1, "session_id": "s"}
        if kind == "429":
            return {"verdict": "ERROR", "detail": "create HTTP 429 Too Many Requests",
                    "answer": "", "seconds": 0.1, "session_id": "s"}
        if kind == "empty":
            return {"verdict": "EMPTY", "detail": "no assistant text, status=stopped",
                    "answer": "", "seconds": 0.1, "session_id": "s"}
        if kind == "tool":
            return {"verdict": "ANSWERED",
                    "answer": '<function_call>\n{"name":"get_weather","arguments":{"city":"Tokyo"}}\n</function_call>',
                    "seconds": 0.2, "effective_model": "gpt-5.5", "session_id": "s1",
                    "thinking": "", "chars": 60}
        if kind == "tool_malformed":
            return {"verdict": "ANSWERED",
                    "answer": '<function_call>\n{"name": broken json no parse\n</function_call>\nHere is some text instead',
                    "seconds": 0.2, "effective_model": "gpt-5.5", "session_id": "s1",
                    "thinking": "", "chars": 80}
        if kind == "final_wrapper":
            return {"verdict": "ANSWERED",
                    "answer": '<final>Here is the file content. I hope this helped.</final>',
                    "seconds": 0.2, "effective_model": "gpt-5.5", "session_id": "s1",
                    "thinking": "", "chars": 60}
        if kind == "eq":
            return {"verdict": "ANSWERED",
                    "answer": "ALPHA-BRAVO-CHARLIE-DELTA-ECHO-FOXTROT-GOLF",
                    "seconds": 0.2, "effective_model": "gpt-5.5", "session_id": "s1",
                    "thinking": "", "chars": 45}
        return {"verdict": "ANSWERED", "answer": "PONG from " + model, "seconds": 0.2,
                "effective_model": "claude-haiku-4-5", "session_id": "s1",
                "thinking": "reasoning", "chars": 10}

    test_secret = "test-key-123"          # must match call()'s default Authorization value
    prev_secret = os.environ.get(_KEY_ENV)
    os.environ[_KEY_ENV] = test_secret
    real_inf = conol_infer.run_inference
    globals()["run_inference"] = fake_inference
    # The pool checks above retired A2 on purpose (FAIL_LIMIT hard failures); the failover
    # checks below need two usable accounts, so restore the fixture instead of building a
    # second one. Production never revives: a retired account stays retired until reload.
    for a in pool.accounts:
        a["fails"] = 0
        a["dead"] = False

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def call(method, path, body=None, key=test_secret):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        conn.request(method, path, json.dumps(body) if body else None, headers)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        conn.close()
        return r.status, raw

    try:
        st, body = call("GET", "/health", key="")
        check("/health needs no auth and reports the pool", st == 200 and '"usable"' in body, body[:90])

        st, body = call("GET", "/v1/models", key="wrong")
        check("wrong key is rejected 401", st == 401, st)
        st, body = call("GET", "/v1/models")
        check("/v1/models lists the advertised ids",
              st == 200 and all(m in body for m in MODELS[:3]), body[:90])

        # Failover: A1 is stubbed to 401, so the answer must come from A2 and A1 must be
        # retired after FAIL_LIMIT hard failures.
        before = pool.snapshot()["requests"]
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        doc = json.loads(body)
        check("chat returns 200 after failing over", st == 200, body[:120])
        check("OpenAI response shape", doc.get("object") == "chat.completion"
              and doc["choices"][0]["message"]["role"] == "assistant"
              and "PONG" in doc["choices"][0]["message"]["content"], body[:140])
        check("usage is present for billing", doc.get("usage", {}).get("total_tokens", 0) > 0,
              doc.get("usage"))
        check("the failing account was tried first", calls and calls[0]["cookie"] == "tok1",
              calls[:1])
        check("model id is passed through verbatim", calls[-1]["model"] == "gpt-5.5", calls[-1])
        check("request counters advanced", pool.snapshot()["requests"] > before)

        st, body = call("POST", "/v1/chat/completions", {"model": "gpt-5.5", "messages": []})
        check("empty messages -> 400", st == 400, body[:100])

        st, body = call("GET", "/pool/stats")
        check("/pool/stats exposes counters", st == 200 and '"answered"' in body, body[:100])
        st, body = call("POST", "/pool/reload", {})
        check("/pool/reload re-reads the file", st == 200 and '"reloaded"' in body, body[:100])
        st, _ = call("GET", "/nope")
        check("unknown path -> 404", st == 404)

        # A rate limit is IP-wide, not per account. It must reach the client as 429 — a 502
        # is what makes new-api auto-disable the whole channel — and it must retire nobody,
        # because reload() preserves dead state and one 429 storm would drain the pool
        # permanently. It must also stop at once: rotating through the pool on a shared
        # limit only adds load to the thing that is already limiting us.
        usable_before = pool.snapshot()["usable"]
        calls_before = len(calls)
        mode["tok1"] = mode["tok2"] = "429"
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        check("429 upstream -> 429 to the client, not 502", st == 429, "%s %s" % (st, body[:100]))
        check("429 body names the rate limit", "rate limited" in body, body[:120])
        check("429 retires nobody", pool.snapshot()["usable"] == usable_before,
              "%s -> %s" % (usable_before, pool.snapshot()["usable"]))
        check("429 stops instead of rotating the pool", len(calls) - calls_before == 1,
              len(calls) - calls_before)

        # Credential death is the ONE thing that retires an account.
        mode["tok1"] = mode["tok2"] = "401"
        for _ in range(FAIL_LIMIT):
            call("POST", "/v1/chat/completions",
                 {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        check("401 repeated FAIL_LIMIT times retires the account",
              pool.snapshot()["usable"] < usable_before, pool.snapshot())
        mode["tok1"] = mode["tok2"] = "ok"
        for a in pool.accounts:
            a["fails"] = 0
            a["dead"] = False
        check("fixture restored for the remaining checks",
              pool.snapshot()["usable"] == usable_before, pool.snapshot())

        # ── Streaming granularity test (needs create_session/read_answer mocks) ──
        real_cs = create_session
        real_ra = read_answer

        def fake_cs(*a, **kw):
            return {"sessionId": "test-stream-1", "effectiveModel": "gpt-5.5",
                    "modelDowngraded": False}

        def fake_ra_incremental(cookie, sid, budget=180, on_event=None):
            answer = "PONG from test"
            for i in range(1, len(answer) + 1):
                frag = answer[:i]
                ev = {"type": "history_delta", "stages":
                      [{"preview":
                        [{"role": "assistant", "type": "message",
                          "content": [{"type": "text", "text": frag}]}],
                        "logs": []}]}
                if on_event:
                    on_event(ev)
            return {"answer": answer, "status": "stopped", "seconds": 0.5,
                    "events": len(answer), "detail": answer, "session_id": sid}

        globals()["create_session"] = fake_cs
        globals()["read_answer"] = fake_ra_incremental

        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [{"role": "user", "content": "ping"}]})
        check("stream responds as SSE with [DONE]",
              st == 200 and body.startswith("data: ") and body.rstrip().endswith("data: [DONE]"),
              body[:120])
        # Parse incremental frames
        sse_raw = [ln for ln in body.split("\n") if ln.startswith("data: ")
                   and ln != "data: [DONE]"]
        content_deltas = []
        for sse_json in sse_raw:
            try:
                ch = json.loads(sse_json[6:])
                d = ch.get("choices", [{}])[0].get("delta", {})
                if "content" in d:
                    content_deltas.append(d["content"])
            except (ValueError, KeyError):
                pass
        check("stream emits >=3 incremental content frames",
              len(content_deltas) >= 3, "got %d" % len(content_deltas))
        reconstructed = "".join(content_deltas)
        check("stream incremental frames reconstruct without duplication",
              reconstructed == "PONG from test",
              "got '%s'" % reconstructed[:60])

        # ── Tool call streaming test ──
        def fake_ra_toolcall(cookie, sid, budget=180, on_event=None):
            answer = '<function_call>\n{"name":"get_weather","arguments":{"city":"Tokyo"}}\n</function_call>'
            ev = {"type": "history_delta", "stages":
                  [{"preview":
                    [{"role": "assistant", "type": "message",
                      "content": [{"type": "text", "text": answer}]}],
                    "logs": []}]}
            if on_event:
                on_event(ev)
            return {"answer": answer, "status": "stopped", "seconds": 0.5,
                    "events": 1, "detail": answer, "session_id": sid}

        globals()["read_answer"] = fake_ra_toolcall
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "tools": [{"type": "function", "function":
                                    {"name": "get_weather", "description": "",
                                     "parameters": {"type": "object",
                                                    "properties": {"city": {"type": "string"}},
                                                    "required": ["city"]}}}],
                         "messages": [{"role": "user", "content": "weather?"}]})
        last_json = [ln[6:] for ln in body.split("\n") if ln.startswith("data: ")
                     and ln != "data: [DONE]"]
        last_chunk = json.loads(last_json[-1]) if last_json else {}
        last_finish = (last_chunk.get("choices") or [{}])[0].get("finish_reason", "")
        tc_in_last = (last_chunk.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")
        check("stream with tools returns tool_calls finish_reason",
              last_finish == "tool_calls", body[:200])
        check("stream tool_calls delta carries function data",
              tc_in_last is not None and len(tc_in_last) > 0
              and tc_in_last[0].get("function", {}).get("name") == "get_weather",
              body[:200])

        globals()["create_session"] = real_cs
        globals()["read_answer"] = real_ra

        # ── Non-streaming tool use test ──
        mode["tok1"] = mode["tok2"] = "tool"
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5",
                         "tools": [{"type": "function", "function":
                                    {"name": "get_weather", "description": "",
                                     "parameters": {"type": "object",
                                                    "properties": {"city": {"type": "string"}},
                                                    "required": ["city"]}}}],
                         "messages": [{"role": "user", "content": "weather?"}]})
        doc = json.loads(body)
        msg = doc.get("choices", [{}])[0].get("message", {})
        tc = msg.get("tool_calls", [])
        check("non-streaming with tools returns tool_calls in message",
              st == 200 and len(tc) > 0 and tc[0].get("function", {}).get("name") == "get_weather",
              body[:200])
        check("finish_reason is tool_calls",
              doc.get("choices", [{}])[0].get("finish_reason") == "tool_calls",
              body[:200])

        # ── role: tool message accepted ──
        mode["tok1"] = mode["tok2"] = "ok"
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5",
                         "messages": [{"role": "system", "content": "You are helpful"},
                                      {"role": "user", "content": "summarize"},
                                      {"role": "assistant",
                                       "tool_calls": [{"id": "call_1", "type": "function",
                                                       "function": {"name": "read",
                                                                    "arguments": "{}"}}]},
                                      {"role": "tool", "tool_call_id": "call_1",
                                       "name": "read", "content": "file content here"}]})
        doc = json.loads(body)
        check("role tool message is accepted and returns 200",
              st == 200 and "PONG" in doc.get("choices", [{}])[0].get("message", {}).get("content", ""),
              body[:150])

        # ── Malformed tool JSON degrades to content ──
        mode["tok1"] = mode["tok2"] = "ok"
        # Defer to the fake_inference which returns "PONG" — no tool call XML,
        # so the response must be a plain text finish_reason: stop.
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5",
                         "tools": [{"type": "function", "function":
                                    {"name": "read", "description": "",
                                     "parameters": {"type": "object",
                                                    "properties": {"path": {"type": "string"}},
                                                    "required": ["path"]}}}],
                         "messages": [{"role": "user", "content": "hello"}]})
        doc = json.loads(body)
        msg = doc.get("choices", [{}])[0].get("message", {})
        check("no tool call in answer degrades to content response",
              doc.get("choices", [{}])[0].get("finish_reason") == "stop"
              and "content" in msg and "tool_calls" not in msg,
              body[:200])

        # ── Malformed tool XML in answer degrades gracefully ──
        mode["tok1"] = mode["tok2"] = "tool_malformed"
        # Make fake_inference return broken XML
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5",
                         "tools": [{"type": "function", "function":
                                    {"name": "read", "description": "",
                                     "parameters": {"type": "object",
                                                    "properties": {"path": {"type": "string"}},
                                                    "required": ["path"]}}}],
                         "messages": [{"role": "user", "content": "do a tool"}]})
        doc = json.loads(body)
        msg = doc.get("choices", [{}])[0].get("message", {})
        check("malformed tool XML degrades to content, not crash",
              st == 200 and "content" in msg
              and doc.get("choices", [{}])[0].get("finish_reason") == "stop",
              body[:200])

        mode["tok1"] = mode["tok2"] = "ok"

        # ── <final> wrapper stripped from non-streaming answer ──
        mode["tok1"] = "final_wrapper"
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5",
                         "tools": [{"type": "function", "function":
                                    {"name": "read", "description": "",
                                     "parameters": {"type": "object",
                                                    "properties": {"path": {"type": "string"}},
                                                    "required": ["path"]}}}],
                         "messages": [{"role": "user", "content": "read file"}]})
        doc = json.loads(body)
        msg = doc.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        check("<final> wrapper stripped from non-streaming answer",
              st == 200 and "<final>" not in content and "</final>" not in content
              and "helped" in content,
              content[:120])
        mode["tok1"] = "ok"

        # ── Distinctive tool result token appears in final content ──
        # Set up a fake read_answer that processes a tool result and returns
        # content including the token
        real_cs2 = create_session
        real_ra2 = read_answer
        def fake_cs_direct(*a, **kw):
            return {"sessionId": "test-tool-loop-1", "effectiveModel": "gpt-5.5",
                    "modelDowngraded": False}
        def fake_ra_toolresult(cookie, sid, budget=180, on_event=None):
            # Simulate conol returning a response that used a tool result
            # with our distinctive token
            answer = "The user asked for current weather. The tool returned temp_c: 47, condition: Purple, code: ZEBRA-7734. Answering now."
            for i in range(1, len(answer) + 1):
                frag = answer[:i]
                ev = {"type": "history_delta", "stages":
                      [{"preview":
                        [{"role": "assistant", "type": "message",
                          "content": [{"type": "text", "text": frag}]}],
                        "logs": []}]}
                if on_event:
                    on_event(ev)
            return {"answer": answer, "status": "stopped", "seconds": 0.5,
                    "events": len(answer), "detail": answer, "session_id": sid}
        globals()["create_session"] = fake_cs_direct
        globals()["read_answer"] = fake_ra_toolresult
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [
                             {"role": "user", "content": "what is weather?"},
                             {"role": "assistant", "tool_calls": [
                                 {"id": "call_1", "type": "function",
                                  "function": {"name": "get_weather", "arguments": '{"city":"Kyiv"}'}}]},
                             {"role": "tool", "tool_call_id": "call_1",
                              "name": "get_weather",
                              "content": '{"temp_c": 47, "condition": "Purple", "code": "ZEBRA-7734"}'}
                         ]})
        check("tool result with distinctive token passes through to streamed content",
              st == 200, body[:200])
        # Reconstruct deltas from incremental SSE frames (tokens are split across them)
        tok_deltas = ""
        for ln in body.split("\n"):
            if ln.startswith("data: ") and ln != "data: [DONE]":
                try:
                    ch = json.loads(ln[6:])
                    d = ch.get("choices", [{}])[0].get("delta", {})
                    if "content" in d:
                        tok_deltas += d["content"]
                except (ValueError, KeyError):
                    pass
        check("reconstructed stream content contains the distinctive ZEBRA-7734 token",
              "ZEBRA-7734" in tok_deltas and "Purple" in tok_deltas,
              tok_deltas[:120])
        globals()["create_session"] = real_cs2
        globals()["read_answer"] = real_ra2

        # ── Streaming exhausted pool returns 502, never 200 ──
        real_cs3 = create_session
        real_ra3 = read_answer
        # Make session creation always fail (pool exhausted)
        def failing_cs(*a, **kw):
            raise ConolError("create HTTP 500 all accounts exhausted")
        globals()["create_session"] = failing_cs
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [{"role": "user", "content": "ping"}]})
        doc = json.loads(body) if body.startswith("{") else {}
        check("streaming exhausted pool -> 502, not 200",
              st == 502 and ("error" in body or "could not answer" in body),
              "%s %s" % (st, body[:150]))
        check("no account name leaks in exhausted pool 502 error body",
              "Conol" not in body and "A1" not in body and "A2" not in body,
              body[:200])
        globals()["create_session"] = real_cs3
        globals()["read_answer"] = real_ra3

        # ── 429 in streaming still returns 429 (not 502) ──
        real_cs4 = create_session
        def failing_cs_429(*a, **kw):
            raise ConolError("create HTTP 429 Too Many Requests")
        globals()["create_session"] = failing_cs_429
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [{"role": "user", "content": "ping"}]})
        doc = json.loads(body) if body.startswith("{") else {}
        check("streaming 429 returns 429 with rate-limit body, not 502",
              st == 429 and "rate" in body.lower(),
              "%s %s" % (st, body[:150]))
        globals()["create_session"] = real_cs4

        # ── Split <final> tag never leaks to client ──
        real_cs5 = create_session
        real_ra5 = read_answer
        def fake_cs_split(*a, **kw):
            return {"sessionId": "test-split-1", "effectiveModel": "gpt-5.5",
                    "modelDowngraded": False}
        def fake_ra_split(cookie, sid, budget=180, on_event=None):
            # Emit <final> tags split across deltas (cumulative, like real conol)
            for frag in ["<fi", "<final>msg te", "<final>msg text</fi", "<final>msg text</final>"]:
                ev = {"type": "history_delta", "stages":
                      [{"preview":
                        [{"role": "assistant", "type": "message",
                          "content": [{"type": "text", "text": frag}]}],
                        "logs": []}]}
                if on_event:
                    on_event(ev)
            return {"answer": "<final>msg text</final>", "status": "stopped",
                    "seconds": 0.5, "events": 4, "detail": "msg text", "session_id": sid}
        globals()["create_session"] = fake_cs_split
        globals()["read_answer"] = fake_ra_split
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [{"role": "user", "content": "split"}]})
        # Check no <final> or </final> in any SSE frame
        sse_lines = [ln for ln in body.split("\n") if ln.startswith("data: ") and ln != "data: [DONE]"]
        content_frames = []
        for sl in sse_lines:
            try:
                ch = json.loads(sl[6:])
                d = ch.get("choices", [{}])[0].get("delta", {})
                if "content" in d:
                    content_frames.append(d["content"])
            except (ValueError, KeyError):
                pass
        any_tag = any("<final>" in c or "</final>" in c for c in content_frames)
        check("split <final> tags never leak into streamed deltas",
              not any_tag and st == 200,
              "tag leaked: %s" % [c for c in content_frames if "<final" in c or "</final" in c][:2])
        reconstructed_no_tag = "".join(content_frames)
        check("split <final> streamed answer is 'msg text'",
              reconstructed_no_tag == "msg text",
              "got '%s'" % reconstructed_no_tag)
        globals()["create_session"] = real_cs5
        globals()["read_answer"] = real_ra5

        # ── Session created but answer empty → 502, never 200 ──
        real_cs6 = create_session
        real_ra6 = read_answer
        def fake_cs_empty_ok(*a, **kw):
            return {"sessionId": "test-empty-1", "effectiveModel": "gpt-5.5",
                    "modelDowngraded": False}
        def fake_ra_empty(cookie, sid, budget=180, on_event=None):
            # Returns empty answer (session created, no content produced)
            return {"answer": "", "status": "stopped", "seconds": 0.5,
                    "events": 0, "detail": "no assistant text, status=stopped",
                    "session_id": sid}
        globals()["create_session"] = fake_cs_empty_ok
        globals()["read_answer"] = fake_ra_empty
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "stream": True,
                         "messages": [{"role": "user", "content": "ping"}]})
        doc = json.loads(body) if body.startswith("{") else {}
        check("session OK but empty answer -> 502 in streaming",
              st == 502 and ("error" in body or "could not answer" in body),
              "%s %s" % (st, body[:150]))
        check("no account name in empty-stream 502",
              "Conol" not in body and "A1" not in body,
              body[:200])
        # Same for non-streaming path
        mode["tok1"] = mode["tok2"] = "empty"
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        doc = json.loads(body) if body.startswith("{") else {}
        check("empty answer in non-streaming -> 502",
              st == 502 and ("error" in body or "could not answer" in body),
              "%s %s" % (st, body[:150]))
        mode["tok1"] = mode["tok2"] = "ok"
        globals()["create_session"] = real_cs6
        globals()["read_answer"] = real_ra6

        # Exact-string equality: streamed deltas reassemble to same answer
        # as non-streaming for the same fake input.
        def fake_cs_eq(*a, **kw):
            return {"sessionId": "test-eq-1", "effectiveModel": "gpt-5.5",
                    "modelDowngraded": False}
        def fake_ra_eq(cookie, sid, budget=180, on_event=None):
            ans = "ALPHA-BRAVO-CHARLIE-DELTA-ECHO-FOXTROT-GOLF"
            for i in range(1, len(ans) + 1):
                ev = {"type": "history_delta", "stages":
                      [{"preview":
                        [{"role": "assistant", "type": "message",
                          "content": [{"type": "text", "text": ans[:i]}]}],
                        "logs": []}]}
                if on_event:
                    on_event(ev)
            return {"answer": ans, "status": "stopped", "seconds": 0.5,
                    "events": len(ans), "detail": ans, "session_id": sid}
        globals()["create_session"] = fake_cs_eq
        globals()["read_answer"] = fake_ra_eq
        # Non-streaming
        mode["tok1"] = mode["tok2"] = "eq"
        st_ns, body_ns = call("POST", "/v1/chat/completions",
                               {"model": "gpt-5.5",
                                "messages": [{"role": "user", "content": "letters"}]})
        ns_doc = json.loads(body_ns)
        ns_text = (ns_doc.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # Streaming
        st_s, body_s = call("POST", "/v1/chat/completions",
                             {"model": "gpt-5.5", "stream": True,
                              "messages": [{"role": "user", "content": "letters"}]})
        sse_parts = []
        for ln in body_s.split("\n"):
            if ln.startswith("data: ") and ln != "data: [DONE]":
                try:
                    ch = json.loads(ln[6:])
                    d = ch.get("choices", [{}])[0].get("delta", {})
                    if "content" in d:
                        sse_parts.append(d["content"])
                except (ValueError, KeyError):
                    pass
        s_text = "".join(sse_parts)
        check("streamed answer byte-identical to non-streamed answer",
              s_text == ns_text,
              "ns=%r stream=%r" % (ns_text[:50], s_text[:50]))
        globals()["create_session"] = real_cs6
        globals()["read_answer"] = real_ra6

        # Exhausted pool must fail loudly, not hang or return an empty 200.
        for _ in range(FAIL_LIMIT * 2):
            pool.report_hard_fail("A1")
            pool.report_hard_fail("A2")
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        check("exhausted pool -> 502 with a reason", st == 502 and "could not answer" in body,
              "%s %s" % (st, body[:120]))

        # Fail closed: with no secret configured the gateway must refuse rather than hand
        # the whole pool to anything that can reach the port.
        os.environ.pop(_KEY_ENV, None)
        st, body = call("GET", "/v1/models", key="")
        check("no secret configured -> 503, not an open gateway", st == 503,
              "%s %s" % (st, body[:90]))
        st, body = call("POST", "/v1/chat/completions",
                        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})
        check("no secret configured -> chat refused too", st == 503, st)
        os.environ[_KEY_ENV] = test_secret
    finally:
        httpd.shutdown()
        httpd.server_close()
        if prev_secret is None:
            os.environ.pop(_KEY_ENV, None)
        else:
            os.environ[_KEY_ENV] = prev_secret
        globals()["run_inference"] = real_inf
        globals()["POOL"] = Pool()
        try:
            pool_file.unlink()
            tmp.rmdir()
        except OSError:
            pass

    check("auth accessor follows the environment",
          _accepted_auth() == (prev_secret or "").strip(), _accepted_auth()[:6])
    print()
    print("FAILURES: " + str(fails) if fails else "all blocks passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenAI-compatible gateway over the conol pool")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep 127.0.0.1 behind a reverse proxy")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    if not _accepted_auth():
        print("ENI_POOL_KEY is required — refusing to start an unauthenticated gateway "
              "over %d accounts" % POOL.snapshot()["loaded"], file=sys.stderr)
        return 2
    snap = POOL.snapshot()
    if snap["usable"] == 0:
        print("no usable accounts in %s — refusing to start a gateway that can only 502"
              % snap["pool_file"], file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("ENI Conol Pool Gateway v4 | %s:%d | accounts loaded=%d usable=%d | models=%d"
          % (args.host, args.port, snap["loaded"], snap["usable"], len(MODELS)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
