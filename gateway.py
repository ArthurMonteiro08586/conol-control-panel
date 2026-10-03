#!/usr/bin/env python3
"""
ENI :: Conol Pool Gateway v7.0 — Real Streaming + Emulated Tool Use
- Real-time SSE streaming: yields deltas AS conol.ai sends them
- Tool use: injects function definitions into system prompt, parses <function_call> XML
- /v1/models, /v1/chat/completions (stream + non-stream + tool use)
- /queue — email registration queue management
- Round-robin pool with failover
"""

import asyncio, json, os, re, time, uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, AsyncGenerator

import httpx
import jsonschema
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ─── Config ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "conol_accounts_pool.jsonl"
QUEUE_FILE = ROOT / "email_queue.jsonl"
STATE_FILE = ROOT / "conol_gateway_state.json"
CFG_FILE = ROOT / "config.json"

BASE_URL = "https://conol.ai"
PORT = int(os.environ.get("ENI_POOL_PORT", "9999"))
# Multi-key support: comma-separated keys in ENI_POOL_KEY env (bootstrap keys)
# Additional keys stored in SQLite keystore (survives restart)
import sqlite3

_raw_keys = os.environ.get("ENI_POOL_KEY", "test")
_bootstrap_keys: set[str] = {k.strip() for k in _raw_keys.split(",") if k.strip()}
API_KEY = next(iter(_bootstrap_keys), "test")  # backward compat for health display
ADMIN_KEY = os.environ.get("ENI_ADMIN_KEY", "")  # required for key management

DB_PATH = ROOT / "conol_keystore.db"

def _init_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            rpm INTEGER DEFAULT 60
        );
        CREATE TABLE IF NOT EXISTS usage (
            key TEXT,
            ts REAL,
            model TEXT,
            stream INTEGER DEFAULT 0,
            tokens_est INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_usage_key ON usage(key);
    """)
    conn.commit()
    conn.close()

_init_db()

def _load_all_keys() -> set[str]:
    """Load all keys from SQLite + env bootstrap keys."""
    keys = set(_bootstrap_keys)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        for row in conn.execute("SELECT key FROM api_keys WHERE active=1"):
            keys.add(row[0])
        conn.close()
    except Exception:
        pass
    return keys

API_KEYS: set[str] = _load_all_keys()

def _add_key(key: str, name: str = "", rpm: int = 60) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("INSERT OR IGNORE INTO api_keys (key, name, created_at, active, rpm) VALUES (?,?,?,?,?)",
                     (key, name, time.time(), 1, rpm))
        conn.commit()
        API_KEYS.add(key)
        return True
    except Exception:
        return False
    finally:
        conn.close()

def _revoke_key(key: str) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("UPDATE api_keys SET active=0 WHERE key=?", (key,))
        conn.commit()
        API_KEYS.discard(key)
        return True
    except Exception:
        return False
    finally:
        conn.close()

def _list_keys() -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT key, name, created_at, active, rpm FROM api_keys ORDER BY created_at").fetchall()
    conn.close()
    return [{"key": r[0][:8] + "...", "name": r[1], "created": r[2], "active": bool(r[3]), "rpm": r[4]} for r in rows]

def _log_usage(key: str, model: str, stream: bool, tokens: int = 0):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("INSERT INTO usage (key, ts, model, stream, tokens_est) VALUES (?,?,?,?,?)",
                     (key, time.time(), model, int(stream), tokens))
        conn.commit()
        conn.close()
    except Exception:
        pass

# Rate limiting: per-key sliding window
_rate_limits: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = int(os.environ.get("ENI_POOL_RPM", "60"))

# Usage tracking (in-memory cache, persisted to SQLite)
_usage: dict[str, dict] = {}
_concurrent = asyncio.Semaphore(int(os.environ.get("ENI_POOL_CONCURRENCY", "20")))
_request_log: deque = deque(maxlen=1000)

# ─── Models ────────────────────────────────────────────────────────────────
# Flattened IDs (no slashes) so OMP picker shows them as hermes-conol/glm-5.2
# instead of hermes-conol/z-ai/glm-5.2 (double-slash broke the picker UI).
# CONOL_MODEL_MAP translates flattened ID → real conol API model ID.
# NOTE: 14 premium models (claude-opus/sonnet/fable, gpt-5.5/5.5-pro/5.6-sol/terra,
# gemini-3.5-flash, gemini-3.1-pro-preview, kimi-k3, fusion) were REMOVED:
# conol silently downgrades them to claude-haiku-4-5 (modelDowngraded=true,
# premiumEligible=false). Verified live 2026-07-31 via POST /api/sessions.
# Downgraded models ignore the XML tool protocol → no tool use + buffered text.
CONOL_MODEL_MAP = {
    # OpenAI (no prefix)
    "gpt-5.6-luna": "gpt-5.6-luna",
    # Anthropic (no prefix)
    "claude-haiku-4-5": "claude-haiku-4-5",
    # DeepSeek
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    # GLM
    "glm-5.2": "z-ai/glm-5.2",
    "glm-5.1": "z-ai/glm-5.1",
    # Kimi
    "kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    # Qwen
    "qwen3.7-plus": "qwen/qwen3.7-plus",
    "qwen3.7-max": "qwen/qwen3.7-max",
    # Google
    "gemini-3.1-flash-lite": "google/gemini-3.1-flash-lite",
    # Others
    "grok-4.3": "x-ai/grok-4.3",
    "minimax-m3": "minimax/minimax-m3",
    "hy3": "tencent/hy3",
    "step-3.7-flash": "stepfun/step-3.7-flash",
    "mimo-v2.5": "xiaomi/mimo-v2.5",
    "mimo-v2.5-pro": "xiaomi/mimo-v2.5-pro",
}

_TS = 1785341821
MODELS = [
    {"id": "gpt-5.6-luna", "object": "model", "created": _TS, "owned_by": "conol-pool", "input_modalities": ["text", "image"]},
    {"id": "claude-haiku-4-5", "object": "model", "created": _TS, "owned_by": "conol-pool", "input_modalities": ["text", "image"]},
    {"id": "deepseek-v4-pro", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "deepseek-v4-flash", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "glm-5.2", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "glm-5.1", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "kimi-k2.7-code", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "qwen3.7-plus", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "qwen3.7-max", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "gemini-3.1-flash-lite", "object": "model", "created": _TS, "owned_by": "conol-pool", "input_modalities": ["text", "image"]},
    {"id": "grok-4.3", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "minimax-m3", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "hy3", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "step-3.7-flash", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "mimo-v2.5", "object": "model", "created": _TS, "owned_by": "conol-pool"},
    {"id": "mimo-v2.5-pro", "object": "model", "created": _TS, "owned_by": "conol-pool"},
]


def _resolve_model(model_id: str) -> str:
    """Translate flattened OMP model ID → real conol API model ID.
    Accepts both flattened (glm-5.2) and original (z-ai/glm-5.2) forms."""
    if model_id in CONOL_MODEL_MAP:
        return CONOL_MODEL_MAP[model_id]
    # Accept original conol IDs too (backward compat)
    if model_id in CONOL_MODEL_MAP.values():
        return model_id
    return model_id

# ─── Account Pool ──────────────────────────────────────────────────────────

@dataclass
class Account:
    email: str
    name: str
    cookies_path: str
    credits: float = 0
    active: bool = True
    fail_count: int = 0
    downgrade_count: int = 0
    total_requests: int = 0
    last_used: float = 0


class PoolManager:
    def __init__(self):
        self.accounts: list[Account] = []
        self._idx = 0
        self._lock = asyncio.Lock()
        self._reload()

    def _reload(self):
        self.accounts.clear()
        if not ACCOUNTS_FILE.exists():
            print("[GW] WARNING: accounts file not found")
            return
        with ACCOUNTS_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    cp = Path(d["cookies_path"])
                    if cp.exists() and cp.stat().st_size > 10:
                        self.accounts.append(Account(
                            email=d["email"],
                            name=d.get("name", d["email"].split("@")[0]),
                            cookies_path=d["cookies_path"],
                            credits=d.get("credits", 0),
                        ))
                except Exception:
                    pass
        print(f"Pool loaded: {len(self.accounts)} accounts")

    async def get_next(self) -> Optional[Account]:
        async with self._lock:
            active = [a for a in self.accounts if a.active]
            if not active:
                return None
            for _ in range(len(active)):
                acc = active[self._idx % len(active)]
                self._idx = (self._idx + 1) % len(active)
                if acc.fail_count < 3:
                    acc.last_used = time.time()
                    acc.total_requests += 1
                    return acc
            for a in active:
                a.fail_count = 0
            acc = active[self._idx % len(active)]
            self._idx = (self._idx + 1) % len(active)
            acc.last_used = time.time()
            acc.total_requests += 1
            return acc

    def stats(self) -> dict:
        return {
            "total": len(self.accounts),
            "active": sum(1 for a in self.accounts if a.active),
            "total_requests": sum(a.total_requests for a in self.accounts),
            "accounts": [
                {"email": a.email, "name": a.name, "credits": a.credits,
                 "active": a.active, "fail_count": a.fail_count, "requests": a.total_requests}
                for a in self.accounts
            ],
        }

    async def check_credits(self):
        """Background task: check credits on ALL accounts, deactivate low, reactivate recovered."""
        while True:
            for acc in self.accounts:
                try:
                    cookies = _load_cookies(acc.cookies_path)
                    async with httpx.AsyncClient(cookies=cookies, timeout=10) as client:
                        r = await client.get(f"{BASE_URL}/api/billing/balance")
                        if r.status_code == 200:
                            bal = r.json().get("total", 0)
                            acc.credits = bal
                            if bal < 10 and acc.active:
                                acc.active = False
                                print(f"[GW] Deactivated {acc.name}: {bal:.0f} credits")
                            elif bal > 20 and not acc.active:
                                acc.active = True
                                acc.fail_count = 0
                                print(f"[GW] Reactivated {acc.name}: {bal:.0f} credits")
                except Exception:
                    pass
                await asyncio.sleep(2)  # stagger checks
            await asyncio.sleep(3600)  # check every hour


pool = PoolManager()

# ─── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="ENI Conol Pool Gateway", version="7.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def verify_key(request: Request) -> str:
    """Validate API key, enforce rate limit, return key for tracking."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    key = auth[7:]
    if key not in API_KEYS:
        raise HTTPException(401, "Invalid API key")

    # Rate limit
    now = time.time()
    reqs = _rate_limits.setdefault(key, [])
    reqs[:] = [t for t in reqs if now - t < RATE_LIMIT_WINDOW]
    if len(reqs) >= RATE_LIMIT_MAX:
        raise HTTPException(429, f"Rate limit: {RATE_LIMIT_MAX} req/{RATE_LIMIT_WINDOW}s")
    reqs.append(now)

    # Track usage
    u = _usage.setdefault(key, {"requests": 0, "tokens_est": 0, "models": {}, "first_seen": now, "last_request": now})
    u["requests"] += 1
    u["last_request"] = now
    return key


_cookie_cache: dict[str, dict] = {}

def _load_cookies(path: str) -> dict:
    if path in _cookie_cache:
        return _cookie_cache[path]
    with open(path) as f:
        raw = json.load(f)
    if isinstance(raw, list):
        cookies = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    elif isinstance(raw, dict):
        cookies = {k: v["value"] if isinstance(v, dict) and "value" in v else v
                for k, v in raw.items()}
    else:
        cookies = {}
    _cookie_cache[path] = cookies
    return cookies


# ─── Tool Use: XML-based emulation ─────────────────────────────────────────

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


def _filter_tools_for_choice(tools: list | None, tool_choice=None) -> list:
    """Apply OpenAI tool_choice semantics before exposing tools to conol."""
    if not tools or tool_choice == "none":
        return []
    if isinstance(tool_choice, dict):
        selected_name = (tool_choice.get("function") or {}).get("name")
        if selected_name:
            return [
                tool for tool in tools
                if tool.get("type") == "function"
                and (tool.get("function") or {}).get("name") == selected_name
            ]
    return tools


def _build_tool_system_prompt(tools: list, tool_choice=None) -> str:
    """Build the strict XML tool protocol from OpenAI function definitions."""
    selected_tools = _filter_tools_for_choice(tools, tool_choice)
    if not selected_tools:
        return ""

    tool_defs = []
    for tool in selected_tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function") or {}
        name = func.get("name")
        if not name:
            continue
        definition = {
            "name": name,
            "description": func.get("description", "No description"),
            "parameters": func.get("parameters", {}),
        }
        tool_defs.append(json.dumps(definition, ensure_ascii=False, separators=(",", ":")))

    if not tool_defs:
        return ""
    return TOOL_XML_HEADER.format(tool_defs="\n".join(tool_defs))


def _decode_json_object(text: str, start: int) -> tuple[dict | None, int]:
    """Decode one JSON object at start, accepting raw control characters in strings."""
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
            parsed_arguments = json.loads(arguments, strict=False)
        except json.JSONDecodeError:
            return None
        arguments = parsed_arguments
    if not isinstance(arguments, dict):
        return None
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _parse_tool_calls(text: str) -> tuple[list[dict], str]:
    """Extract every valid function-call object and remove its markup from text."""
    opening = "<function_call>"
    closers = ("</function_call>", "</function_function_call>")
    tool_calls = []
    clean_parts = []
    cursor = 0

    while True:
        tag_start = text.find(opening, cursor)
        if tag_start < 0:
            clean_parts.append(text[cursor:])
            break

        clean_parts.append(text[cursor:tag_start])
        json_start = tag_start + len(opening)
        while json_start < len(text) and text[json_start].isspace():
            json_start += 1
        data, json_end = _decode_json_object(text, json_start)
        tool_call = _make_tool_call(data) if data is not None else None
        if tool_call is None:
            clean_parts.append(opening)
            cursor = tag_start + len(opening)
            continue

        after_json = json_end
        while after_json < len(text) and text[after_json].isspace():
            after_json += 1
        matched_closer = next(
            (closer for closer in closers if text.startswith(closer, after_json)),
            None,
        )
        cursor = after_json + len(matched_closer) if matched_closer else json_end
        tool_calls.append(tool_call)

    return tool_calls, "".join(clean_parts).strip()


TOOL_REPAIR_ATTEMPTS = 3


def _validate_tool_calls(tool_calls: list[dict], tools: list | None) -> list[str]:
    """Validate parsed tool calls against the exposed function JSON schemas."""
    schemas = {}
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        func = tool.get("function") or {}
        name = func.get("name")
        if name:
            schemas[name] = func.get("parameters")
    errors = []
    for call in tool_calls:
        func = call.get("function") or {}
        name = func.get("name", "")
        if name not in schemas:
            errors.append(f"tool '{name}' is not exposed in this request")
            continue
        schema = schemas[name]
        if not isinstance(schema, dict) or not schema:
            continue
        raw_arguments = func.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments, strict=False) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as e:
            errors.append(f"{name}: arguments are not valid JSON ({e})")
            continue
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            schema_errors = list(validator_cls(schema).iter_errors(arguments))
        except Exception:
            continue  # unusable client schema: accept rather than loop on repair
        for error in schema_errors:
            path = ".".join(str(part) for part in error.absolute_path)
            where = f"{name}.{path}" if path else name
            errors.append(f"{where}: {error.message}")
    return errors


def _tool_repair_message(validation_errors: list[str]) -> dict:
    """Build a conol correction turn instructing the model to re-emit valid calls."""
    details = "\n".join(f"- {error}" for error in validation_errors)
    return {
        "type": "text",
        "content": (
            "[Tool validation error] The previous <function_call> was rejected:\n"
            f"{details}\n"
            "Respond again with corrected <function_call> markup whose arguments "
            "satisfy the tool's JSON schema exactly. Do not apologize or add prose."
        ),
    }


class _ToolCallStreamFilter:
    """Stream explicit final text while withholding all ambiguous/tool output."""

    _tool_open = "<function_call>"
    _final_open = "<final>"
    _final_close = "</final>"

    def __init__(self):
        self._mode = None
        self._buffer = ""
        self._final_pending = ""

    @staticmethod
    def _partial_suffix_length(text: str, marker: str) -> int:
        max_length = min(len(text), len(marker) - 1)
        for length in range(max_length, 0, -1):
            if text.endswith(marker[:length]):
                return length
        return 0

    def _feed_final(self, text: str) -> str:
        self._final_pending += text
        close_at = self._final_pending.find(self._final_close)
        if close_at >= 0:
            safe = self._final_pending[:close_at]
            self._final_pending = ""
            self._mode = "done"
            return safe

        held_length = self._partial_suffix_length(self._final_pending, self._final_close)
        safe_end = len(self._final_pending) - held_length
        safe = self._final_pending[:safe_end]
        self._final_pending = self._final_pending[safe_end:]
        return safe

    def feed(self, delta: str) -> str:
        if self._mode in ("tool", "done"):
            if self._mode == "tool":
                self._buffer += delta
            return ""
        if self._mode == "final":
            return self._feed_final(delta)

        self._buffer += delta
        tool_at = self._buffer.find(self._tool_open)
        final_at = self._buffer.find(self._final_open)

        if tool_at >= 0 and (final_at < 0 or tool_at < final_at):
            self._mode = "tool"
            return ""
        if final_at >= 0:
            final_text = self._buffer[final_at + len(self._final_open):]
            self._buffer = ""
            self._mode = "final"
            return self._feed_final(final_text)
        return ""

    def finish(self) -> tuple[str, list[dict]]:
        if self._mode == "tool":
            calls, _ = _parse_tool_calls(self._buffer)
            return ("", calls) if calls else ("", [])
        if self._mode == "final":
            tail = self._final_pending
            held_length = self._partial_suffix_length(tail, self._final_close)
            if held_length:
                tail = tail[:-held_length]
            self._final_pending = ""
            return tail, []
        if self._mode == "done":
            return "", []

        calls, _ = _parse_tool_calls(self._buffer)
        if calls:
            return "", calls
        if re.search(r"(?im)^\s*(?:[-*]\s*)?\[?\s*tool\s+result\b", self._buffer):
            return "", []
        return self._buffer, []


def _to_conol_content(content) -> str:
    """Convert OpenAI content (string or multi-part) to conol.ai string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part["text"])
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {})
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    parts.append(f"[Image: {url}]")
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _build_conol_messages(messages: list, tools: list = None, tool_choice=None) -> list[dict]:
    """Build conol.ai messages while preserving OpenAI tool-call turns."""
    cm = []

    tool_sys = _build_tool_system_prompt(tools, tool_choice)

    for msg in messages:
        role = msg.get("role", "user")
        content = _to_conol_content(msg.get("content") or "")

        if role == "system":
            combined = content
            if tool_sys:
                combined = f"{tool_sys}\n\n[System Instructions]\n{content}"
            cm.append({"type": "text", "content": f"[System]\n{combined}"})
            continue

        if role == "tool":
            tool_id = msg.get("tool_call_id", "unknown")
            tool_name = msg.get("name")
            label = f"#{tool_id} ({tool_name})" if tool_name else f"#{tool_id}"
            cm.append({"type": "text", "content": f"[Tool result {label}]:\n{content}"})
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            parts = []
            if content:
                parts.append(content)
            for tool_call in tool_calls:
                func = tool_call.get("function") or {}
                raw_arguments = func.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments, strict=False) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError:
                    arguments = {}
                parts.append(
                    "<function_call>\n"
                    + json.dumps({"name": func.get("name", ""), "arguments": arguments}, ensure_ascii=False)
                    + "\n</function_call>"
                )
            if parts:
                cm.append({"type": "text", "content": "\n".join(parts)})
            continue

        cm.append({"type": "text", "content": content})

    # If no system message was found, prepend tool sys
    if tool_sys and not any(
        m["content"].startswith("[System]") and TOOL_XML_HEADER[:20] in m["content"]
        for m in cm
    ):
        cm.insert(0, {"type": "text", "content": f"[System]\n{tool_sys}"})

    return cm


# ─── Core: Real-time SSE Stream from conol.ai ──────────────────────────────

async def _conol_stream_text(
    cookies_path: str, messages: list, model: str, timeout: int = 300,
    effort: str = "low", source_type: str = "home",
) -> AsyncGenerator[str, None]:
    """
    Stream text deltas from conol.ai in REAL TIME.
    Conol sends incremental previews (full text so far) via stage.preview;
    we diff against the last yielded text and emit only the new suffix.
    Final stage.logs contains the complete message.
    """
    cookies = _load_cookies(cookies_path)

    async with httpx.AsyncClient(cookies=cookies, timeout=httpx.Timeout(timeout)) as client:
        r = await client.post(f"{BASE_URL}/api/sessions", json={
            "source": {"type": source_type},
            "messages": messages,
            "timezone": "Europe/Kyiv",
            "agentModel": model,
            "agentEffort": effort,
        })
        if r.status_code != 201:
            yield f"\n[Error: HTTP {r.status_code}]"
            return

        sid = r.json()["sessionId"]
        deadline = time.time() + timeout
        last_message_text = ""
        final_emitted = False

        async with client.stream("GET",
            f"{BASE_URL}/api/sessions/{sid}/messages?logDeltas=1"
        ) as stream:
            async for line in stream.aiter_lines():
                if time.time() > deadline:
                    break
                if not line or not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue

                if ev.get("type") in ("done", "error"):
                    break

                if ev.get("type") != "history_delta":
                    continue

                for stage in ev.get("stages", []):
                    # Incremental preview: each event has the full text so far.
                    # Diff against last yielded and emit only the new suffix.
                    for pv in (stage.get("preview") or []):
                        if pv.get("role") != "assistant":
                            continue
                        if pv.get("type") not in ("message", None):
                            continue
                        for part in (pv.get("content") or []):
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "text":
                                full = part.get("text", "")
                                if full and full.startswith(last_message_text):
                                    new_suffix = full[len(last_message_text):]
                                    if new_suffix:
                                        last_message_text = full
                                        yield new_suffix

                    # Final logs: emit any remaining text not captured by preview.
                    if not final_emitted:
                        for log in (stage.get("logs") or []):
                            if log.get("role") != "assistant":
                                continue
                            if log.get("type") not in ("message", None):
                                continue
                            for part in (log.get("content") or []):
                                if not isinstance(part, dict):
                                    continue
                                if part.get("type") == "text":
                                    full = part.get("text", "")
                                    if full and full.startswith(last_message_text):
                                        new_suffix = full[len(last_message_text):]
                                        if new_suffix:
                                            last_message_text = full
                                            yield new_suffix
                        final_emitted = True

async def _conol_collect_text(
    cookies_path: str, messages: list, model: str, timeout: int = 300,
    effort: str = "low", source_type: str = "home",
) -> str:
    """Collect full text (non-streaming)."""
    parts = []
    async for chunk in _conol_stream_text(cookies_path, messages, model, timeout, effort, source_type):
        parts.append(chunk)
    return "".join(parts)


# ─── Tokenizer for Streaming ───────────────────────────────────────────────

def _split_token(text: str, max_len: int = 0) -> list[str]:
    """Pass text through as single chunk — conol already sends incremental deltas."""
    if not text:
        return [""]
    return [text]


# ─── Streaming Generator (REAL-TIME) ───────────────────────────────────────

async def _sse_stream_realtime(
    acc: Account, messages: list, model: str,
    chat_id: str, tools: list = None, tool_choice: str = None,
    user_effort: str = None,
) -> AsyncGenerator[str, None]:
    """Stream safe text immediately and convert withheld XML into OpenAI tool deltas."""
    try:
        selected_tools = _filter_tools_for_choice(tools, tool_choice)
        has_tools = bool(selected_tools)

        role_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

        async def _emit_content(text: str):
            """Tokenize and yield a content delta chunk."""
            nonlocal chunk_idx
            for sub in _split_token(text):
                if not sub:
                    continue
                chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": sub},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                chunk_idx += 1

        for _retry in range(5):
            chunk_idx = 0
            conol_msgs = _build_conol_messages(messages, tools, tool_choice)
            attempts_left = TOOL_REPAIR_ATTEMPTS

            while True:
                stream_filter = _ToolCallStreamFilter() if has_tools else None
                # OMP tools use the XML emulation protocol on source.type "home".
                # source.type "code" activates conol's E2B sandbox with native tools
                # (Bash, Read, …) whose names/schemas do NOT match OMP tools — merging
                # them would cause double execution and name mismatches. So code-mode
                # is only used for plain requests without OMP tools.
                src_type = "home"
                stream_start = time.time()
                async for text_delta in _conol_stream_text(
                    acc.cookies_path,
                    conol_msgs,
                    model,
                    timeout=600 if (user_effort or "max") == "max" else 300,
                    effort=user_effort or "max",
                    source_type=src_type,
                ):
                    safe_text = stream_filter.feed(text_delta) if stream_filter else text_delta
                    if safe_text:
                        async for chunk in _emit_content(safe_text):
                            yield chunk
                    elif has_tools and time.time() - stream_start > 5:
                        # SSE heartbeat to keep connection alive during buffering
                        yield ": ping\n\n"

                tail_text, xml_tool_calls = "", []
                if stream_filter:
                    tail_text, xml_tool_calls = stream_filter.finish()

                tool_calls = xml_tool_calls

                attempts_left -= 1
                validation_errors = _validate_tool_calls(tool_calls, selected_tools) if tool_calls else []
                if validation_errors and attempts_left > 0:
                    conol_msgs = conol_msgs + [_tool_repair_message(validation_errors)]
                    continue
                if validation_errors:
                    tool_calls = [
                        tc for tc in tool_calls
                        if not _validate_tool_calls([tc], selected_tools)
                    ]
                    if not tool_calls and not tail_text:
                        tail_text = "\n[Tool validation failed]\n" + "\n".join(validation_errors)
                break

            # Account rotation on downgrade (tools requested but text returned)
            # Only rotate if no content has been emitted yet (chunk_idx == 0)
            # Cap rotation at 15s total to avoid client timeout
            if has_tools and not tool_calls and _retry < 4 and chunk_idx == 0 and time.time() - stream_start < 15:
                acc.fail_count += 1
                next_acc = await pool.get_next()
                if next_acc and next_acc is not acc:
                    acc = next_acc
                    continue
            break

        if tail_text:
            async for chunk in _emit_content(tail_text):
                yield chunk
        if tool_calls:
            # Emit tool_calls as OpenAI-style incremental streaming deltas:
            # chunk 1: {index, id, type, function:{name, arguments:""}}
            # chunk 2+: {index, function:{arguments: "<partial>"}}
            for tc_idx, tc in enumerate(tool_calls):
                fn = tc.get("function", {})
                args_str = fn.get("arguments", "")
                # Header chunk: id + type + name + empty arguments
                header_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": tc_idx,
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": ""},
                        }]},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(header_chunk, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.01)
                # Arguments chunks: stream arguments string in pieces
                for i in range(0, len(args_str), 20):
                    arg_piece = args_str[i:i+20]
                    arg_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"tool_calls": [{
                                "index": tc_idx,
                                "function": {"arguments": arg_piece},
                            }]},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(arg_chunk, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.01)
            finish = "tool_calls"
        else:
            finish = "stop"

        # Final chunk
        final = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        err = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": f"\n[Stream error: {e}]"},
                "finish_reason": "error",
            }],
        }
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


# ─── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "eni-conol-pool-gateway",
        "version": "7.0",
        "port": PORT,
        "api_key_prefix": API_KEY[:8] + "...",
        **pool.stats(),
    }


@app.get("/v1/models")
async def list_models(request: Request):
    await verify_key(request)
    return JSONResponse({"object": "list", "data": MODELS})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    api_key_used = await verify_key(request)
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "gpt-5.6-luna")
    conol_model = _resolve_model(model)
    stream = body.get("stream", False)
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    # Parse effort: OMP sends reasoning_effort, OpenAI sends reasoning.effort
    _raw_effort = body.get("reasoning_effort")
    if not _raw_effort and isinstance(body.get("reasoning"), dict):
        _raw_effort = body["reasoning"].get("effort")
    _EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "max": "max", "xhigh": "max"}
    user_effort = _EFFORT_MAP.get(str(_raw_effort or "").lower())

    if not messages:
        raise HTTPException(400, "messages is required")

    _m = _usage.setdefault(api_key_used, {}).setdefault("models", {})
    _m[model] = _m.get(model, 0) + 1
    _request_log.append({"ts": time.time(), "key": api_key_used[:8], "model": model, "stream": stream})

    acc = await pool.get_next()
    if not acc:
        raise HTTPException(503, "No active accounts in pool")

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:32]}"

    if stream:
        async def _stream_with_logging():
            async for chunk in _sse_stream_realtime(acc, messages, conol_model, chat_id, tools, tool_choice, user_effort):
                yield chunk
            _log_usage(api_key_used, model, True)
        return StreamingResponse(
            _stream_with_logging(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    try:
        async with _concurrent:
            selected_tools = _filter_tools_for_choice(tools, tool_choice)
        conol_msgs = _build_conol_messages(messages, tools, tool_choice)
        for _retry in range(5):
            attempts_left = TOOL_REPAIR_ATTEMPTS
            while True:
                # OMP tools use XML emulation on source.type "home" only.
                # code-mode native tools (Bash/Read/…) have mismatched names/schemas
                # and would double-execute — never merge them with OMP tool_calls.
                full_text = await _conol_collect_text(
                    acc.cookies_path,
                    conol_msgs,
                    conol_model,
                    timeout=600 if (user_effort or "max") == "max" else 300,
                    effort=user_effort or "max",
                    source_type="home",
                )
                if selected_tools:
                    output_filter = _ToolCallStreamFilter()
                    output_filter.feed(full_text)
                    clean_text, xml_tool_calls = output_filter.finish()
                else:
                    xml_tool_calls, clean_text = [], full_text

                tool_calls = xml_tool_calls

                attempts_left -= 1
                validation_errors = _validate_tool_calls(tool_calls, selected_tools) if tool_calls else []
                if validation_errors and attempts_left > 0:
                    conol_msgs = conol_msgs + [_tool_repair_message(validation_errors)]
                    continue
                if validation_errors:
                    tool_calls = [
                        tc for tc in tool_calls
                        if not _validate_tool_calls([tc], selected_tools)
                    ]
                    if not tool_calls and not clean_text:
                        clean_text = "[Tool validation failed]\n" + "\n".join(validation_errors)
                break

            # Retry on next account if tools were requested but downgraded model returned text
            if selected_tools and not tool_calls and _retry < 4:
                acc.fail_count += 1
                next_acc = await pool.get_next()
                if next_acc and next_acc is not acc:
                    acc = next_acc
                    continue
            break

        message = {"role": "assistant"}
        finish = "stop"

        if tool_calls:
            message["tool_calls"] = tool_calls
            message["content"] = None
            finish = "tool_calls"
        else:
            message["content"] = clean_text or "(empty)"

        _log_usage(api_key_used, model, False)
        return JSONResponse({
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish,
                "logprobs": None,
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "system_fingerprint": f"fp_conol_{acc.name}",
        })
    except Exception as e:
        acc.fail_count += 1
        if acc.fail_count >= 5:
            acc.active = False
        raise HTTPException(502, f"Conol backend error: {e}")


# ─── Queue endpoints ───────────────────────────────────────────────────────

def _load_queue() -> list[dict]:
    queue = []
    if QUEUE_FILE.exists():
        with QUEUE_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    queue.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return queue


def _save_queue(queue: list[dict]):
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        for entry in queue:
            f.write(json.dumps(entry) + "\n")


@app.get("/queue")
async def queue_list(request: Request):
    await verify_key(request)
    queue = _load_queue()
    return JSONResponse({
        "queue": queue,
        "total": len(queue),
        "pending": sum(1 for e in queue if e.get("status") == "queued"),
        "processing": sum(1 for e in queue if e.get("status") == "processing"),
        "done": sum(1 for e in queue if e.get("status") == "done"),
        "failed": sum(1 for e in queue if e.get("status") == "failed"),
    })


@app.post("/queue/add")
async def queue_add(request: Request):
    await verify_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    email = body.get("email", "").strip()
    if not email:
        raise HTTPException(400, "'email' is required")

    queue = _load_queue()
    if any(e.get("email") == email for e in queue):
        return JSONResponse({"ok": True, "duplicate": True, "message": "Email already in queue"})

    entry = {
        "email": email,
        "password": body.get("password") or None,
        "added_at": time.time(),
        "added_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "status": "queued",
        "accounts_registered": 0,
        "accounts_target": body.get("target", 3),
    }
    queue.append(entry)
    _save_queue(queue)
    return JSONResponse({"ok": True, "entry": entry})


@app.post("/queue/remove")
async def queue_remove(request: Request):
    await verify_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    email = body.get("email", "").strip()
    if not email:
        raise HTTPException(400, "'email' is required")

    queue = _load_queue()
    removed = [e for e in queue if e.get("email") != email]
    if len(removed) == len(queue):
        raise HTTPException(404, "Email not found in queue")

    _save_queue(removed)
    return JSONResponse({"ok": True, "removed": email})


@app.post("/queue/clear")
async def queue_clear(request: Request):
    await verify_key(request)
    _save_queue([])
    return JSONResponse({"ok": True})


# ─── Pool management ───────────────────────────────────────────────────────

@app.get("/v1/usage")
async def usage_stats(request: Request):
    """Usage statistics from SQLite + in-memory cache."""
    await verify_key(request)
    # Get persistent stats from SQLite
    try:
        conn = sqlite3.connect(str(DB_PATH))
        total_reqs = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        by_model = conn.execute("SELECT model, COUNT(*) FROM usage GROUP BY model ORDER BY COUNT(*) DESC").fetchall()
        by_key = conn.execute("SELECT key, COUNT(*) FROM usage GROUP BY key ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
        conn.close()
    except Exception:
        total_reqs, by_model, by_key = 0, [], []
    return JSONResponse({
        "rate_limit": f"{RATE_LIMIT_MAX} req/{RATE_LIMIT_WINDOW}s",
        "concurrency": int(os.environ.get("ENI_POOL_CONCURRENCY", "20")),
        "total_requests_persisted": total_reqs,
        "by_model": {r[0]: r[1] for r in by_model},
        "by_key": {r[0][:8] + "...": r[1] for r in by_key},
        "in_memory": {
            k[:8] + "...": {"requests": v.get("requests", 0), "models": v.get("models", {})}
            for k, v in _usage.items()
        },
        "recent_requests": list(_request_log)[-20:],
    })


@app.get("/v1/keys")
async def list_keys(request: Request):
    """List all API keys (masked) from SQLite + env."""
    await verify_key(request)
    return JSONResponse({
        "total_keys": len(API_KEYS),
        "env_bootstrap_keys": [k[:8] + "..." for k in _bootstrap_keys],
        "db_keys": _list_keys(),
        "rate_limit_rpm": RATE_LIMIT_MAX,
    })

async def _verify_admin(request: Request):
    """Verify admin key. Fail-closed: empty ADMIN_KEY = no admin access."""
    if not ADMIN_KEY:
        raise HTTPException(503, "Admin key not configured (set ENI_ADMIN_KEY env var)")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if auth[7:] != ADMIN_KEY:
        raise HTTPException(403, "Admin access required")


@app.post("/v1/keys/create")
async def create_key(request: Request):
    """Create a new API key. Admin only. Body: {name, rpm}"""
    await _verify_admin(request)
    body = await request.json()
    name = body.get("name", "")
    rpm = int(body.get("rpm", 60))
    new_key = f"cnl-{uuid.uuid4().hex[:24]}"
    if _add_key(new_key, name, rpm):
        return JSONResponse({"ok": True, "key": new_key, "name": name, "rpm": rpm})
    raise HTTPException(409, "Key creation failed")


@app.post("/v1/keys/revoke")
async def revoke_key(request: Request):
    """Revoke an API key. Admin only. Body: {key}"""
    await _verify_admin(request)
    body = await request.json()
    key = body.get("key", "")
    if key in _bootstrap_keys:
        raise HTTPException(400, "Cannot revoke bootstrap key (set via env var)")
    if _revoke_key(key):
        return JSONResponse({"ok": True, "revoked": key[:8] + "..."})
    raise HTTPException(404, "Key not found")


@app.get("/pool/stats")
async def pool_stats(request: Request):
    await verify_key(request)
    return JSONResponse(pool.stats())


@app.post("/pool/reload")
async def pool_reload(request: Request):
    await verify_key(request)
    pool._reload()
    return JSONResponse({"ok": True, **pool.stats()})


# ─── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"ENI Conol Pool Gateway v6.1")
    print(f"  Port:      {PORT}")
    print(f"  API Key:   {API_KEY}")
    print(f"  Accounts:  {pool.stats()['active']}/{pool.stats()['total']} active")
    print(f"  Queue:     {QUEUE_FILE}")
    print(f"  Features:  real streaming + XML tool use emulation")
    STATE_FILE.write_text(json.dumps({
        "port": PORT, "api_key": API_KEY,
        "started_at": time.time(),
        "started_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        **pool.stats(),
    }, indent=2))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
