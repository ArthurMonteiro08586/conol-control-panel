#!/usr/bin/env python3
"""
ENI :: Conol Dashboard Server v1.0
Web UI для управления пулом аккаунтов conol.ai
Порт: 9988
"""

import asyncio, json, os, sys, time, subprocess, threading
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ROOT = Path(__file__).resolve().parent
CFG_FILE = ROOT / "config.json"
ACCOUNTS_FILE = ROOT / "conol_accounts_pool.jsonl"
QUEUE_FILE = ROOT / "email_queue.jsonl"

with open(CFG_FILE) as f:
    CFG = json.load(f)
QUEST_STATE_FILE = ROOT / CFG["paths"].get("quest_state_file", "conol_quests_state.json")

GW_PORT = CFG["gateway"]["port"]
GW_HOST = CFG["gateway"]["host"]
GW_URL = f"http://{GW_HOST}:{GW_PORT}"
GW_KEY = CFG["gateway"]["api_key"]
DASH_PORT = int(os.environ.get("ENI_DASH_PORT", "9988"))

app = FastAPI(title="ENI Conol Dashboard")

# ─── Data loaders ──────────────────────────────────────────────────────────

def _load_accounts() -> list[dict]:
    accounts = []
    if not ACCOUNTS_FILE.exists():
        return accounts
    with ACCOUNTS_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                cp = Path(d.get("cookies_path", ""))
                exists = cp.exists() and cp.stat().st_size > 10
                accounts.append({
                    "email": d.get("email", "?"),
                    "name": d.get("name", d.get("email", "?").split("@")[0]),
                    "cookies_path": d.get("cookies_path", ""),
                    "cookies_ok": exists,
                    "credits": d.get("credits", 0),
                    "registered": d.get("registered_at", d.get("added_at", "")),
                })
            except Exception:
                pass
    return accounts


def _load_queue() -> list[dict]:
    queue = []
    if not QUEUE_FILE.exists():
        return queue
    with QUEUE_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                queue.append(json.loads(line))
            except Exception:
                pass
    return queue

def _load_quests_state() -> list[dict]:
    """Load last quest farming results."""
    if not QUEST_STATE_FILE.exists():
        return []
    try:
        return json.loads(QUEST_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


async def _gw_get(path: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{GW_URL}{path}", headers={"Authorization": f"Bearer {GW_KEY}"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def _gw_post(path: str, body: dict = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{GW_URL}{path}", headers={"Authorization": f"Bearer {GW_KEY}"}, json=body or {})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def _gw_health() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{GW_URL}/health")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


# ─── API endpoints ─────────────────────────────────────────────────────────

@app.get("/api/state")
async def api_state():
    """Full dashboard state in one call."""
    health = await _gw_health()
    accounts = _load_accounts()
    queue = _load_queue()
    quests_state = _load_quests_state()

    gw_alive = health is not None
    active = sum(1 for a in accounts if a["cookies_ok"])
    dead = sum(1 for a in accounts if not a["cookies_ok"])

    queue_pending = sum(1 for e in queue if e.get("status") == "queued")
    queue_done = sum(1 for e in queue if e.get("status") == "done")
    queue_failed = sum(1 for e in queue if e.get("status") == "failed")

    return JSONResponse({
        "gateway": {
            "url": GW_URL,
            "alive": gw_alive,
            "version": health.get("version", "?") if health else "?",
            "active": health.get("active", 0) if health else 0,
            "total": health.get("total", 0) if health else 0,
            "total_requests": health.get("total_requests", 0) if health else 0,
            "api_key": GW_KEY,
            "port": GW_PORT,
        },
        "pool": {
            "total": len(accounts),
            "active": active,
            "dead": dead,
            "accounts": accounts,
        },
        "queue": {
            "total": len(queue),
            "pending": queue_pending,
            "done": queue_done,
            "failed": queue_failed,
            "items": queue,
        },
        "quests": {
            "total_accounts": len(quests_state),
            "total_credits": sum(q.get("credits_earned", 0) for q in quests_state),
            "total_done": sum(q.get("quests_done", 0) for q in quests_state),
            "items": quests_state[-20:],
        },
        "models": [
            "gpt-5.5", "gpt-5.5-pro", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
            "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-base",
            "kimi/kimi-k3", "qwen/qwen-3.7", "glm/glm-5.2",
            "claude-opus-4-8", "claude-fable-5", "claude-opus-4-7",
            "gemini-3-pro", "gemini-3-flash",
            "llama-4-maverick", "llama-4-scout", "mistral-large-3",
        ],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.post("/api/queue/add")
async def api_queue_add(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    target = int(body.get("target", 3))
    if not email:
        return JSONResponse({"ok": False, "error": "email required"}, status_code=400)

    queue = _load_queue()
    if any(e.get("email") == email for e in queue):
        return JSONResponse({"ok": True, "duplicate": True})

    entry = {
        "email": email,
        "password": body.get("password"),
        "added_at": time.time(),
        "added_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "queued",
        "accounts_registered": 0,
        "accounts_target": target,
    }
    queue.append(entry)
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        for e in queue:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True, "entry": entry})


@app.post("/api/queue/remove")
async def api_queue_remove(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    queue = _load_queue()
    removed = [e for e in queue if e.get("email") != email]
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        for e in removed:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True, "removed": email})


@app.post("/api/queue/clear")
async def api_queue_clear():
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        f.write("")
    return JSONResponse({"ok": True})


@app.post("/api/pool/reload")
async def api_pool_reload():
    res = await _gw_post("/pool/reload")
    return JSONResponse(res or {"ok": False, "error": "gateway not reachable"})


@app.post("/api/gateway/start")
async def api_gateway_start():
    """Start conol_gateway.py (the maintained stdlib gateway, v4) in background."""
    env = dict(os.environ)
    env.setdefault("ENI_POOL_KEY", CFG.get("gateway", {}).get("api_key", "test"))
    try:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", "conol_gateway.py"],
            cwd=str(ROOT),
            env=env,
            stdout=open(ROOT / "gateway.log", "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        await asyncio.sleep(2)
        health = await _gw_health()
        return JSONResponse({"ok": True, "health": health})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/reg/run")
async def api_reg_run(request: Request):
    """Run multi_reg.py in background."""
    body = await request.json()
    per_email = int(body.get("per_email", 3))
    max_parallel = int(body.get("max_parallel", 2))

    try:
        subprocess.Popen(
            ["python", "-u", "multi_reg.py", str(per_email), str(max_parallel)],
            cwd=str(ROOT),
            stdout=open(ROOT / "multi_reg.log", "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return JSONResponse({"ok": True, "message": f"Started: {per_email} accounts/email, {max_parallel} parallel"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/quests/run")
async def api_quests_run():
    """Run eni_conol.py quests in background."""
    try:
        subprocess.Popen(
            ["python", "-u", "eni_conol.py", "quests"],
            cwd=str(ROOT),
            stdout=open(ROOT / "quests.log", "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return JSONResponse({"ok": True, "message": "Quest farming started"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/reg/single")
async def api_reg_single(request: Request):
    """Run conol_register.py (the maintained registrar) in background.

    body: {count, provider: gmail|tonline|null, free_captcha: bool}
    free_captcha=true forces the Chrome CDP solver (no paid AntiCaptcha spend)."""
    body = await request.json()
    count = int(body.get("count", 3))
    provider = body.get("provider") or None
    free_captcha = bool(body.get("free_captcha", False))

    cmd = [sys.executable, "-X", "utf8", "-u", "conol_register.py", "--count", str(count)]
    if provider in ("gmail", "tonline"):
        cmd += ["--provider", provider]
    if free_captcha:
        cmd += ["--free-captcha"]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=open(ROOT / "reg.log", "a"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        label = provider or "auto"
        cap = "free Chrome CDP" if free_captcha else "chain (cdp→anticaptcha)"
        return JSONResponse({"ok": True, "message": f"Registering {count} accounts (email={label}, captcha={cap})"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/logs/{name}")
async def api_logs(name: str):
    """Read last N lines of a log file."""
    log_map = {
        "gateway": "gateway.log",
        "reg": "reg.log",
        "multi_reg": "multi_reg.log",
        "quests": "quests.log",
    }
    fname = log_map.get(name)
    if not fname:
        return JSONResponse({"ok": False, "error": "unknown log"}, status_code=400)
    fpath = ROOT / fname
    if not fpath.exists():
        return JSONResponse({"ok": True, "lines": [], "note": "no log yet"})
    try:
        lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        return JSONResponse({"ok": True, "lines": lines[-100:]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/tooluse/test")
async def api_tooluse_test(request: Request):
    """Test tool use via gateway: sends a chat completion with tools and returns the raw response."""
    body = await request.json()
    model = body.get("model", "gpt-5.6-luna")
    messages = body.get("messages", [{"role": "user", "content": "What's the weather in Tokyo?"}])
    tools = body.get("tools", [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            },
        },
    }])
    stream = body.get("stream", False)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{GW_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {GW_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "tools": tools, "stream": stream},
            )
            return JSONResponse({
                "ok": r.status_code == 200,
                "status": r.status_code,
                "body": r.json() if r.status_code == 200 else r.text[:500],
            })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─── HTML Dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTML_DASHBOARD


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ENI :: Conol Control Panel</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.header{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:20px 30px;border-bottom:2px solid #e94560;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:24px;color:#e94560;letter-spacing:1px}
.header .status{display:flex;gap:15px;align-items:center}
.pill{padding:6px 16px;border-radius:20px;font-size:13px;font-weight:600}
.pill.ok{background:#0f3460;color:#53d769;border:1px solid #53d76933}
.pill.bad{background:#1a0a0a;color:#e94560;border:1px solid #e9456033}
.pill.info{background:#0f3460;color:#4ea8de;border:1px solid #4ea8de33}
.container{max-width:1400px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:20px}
.card{background:#111118;border:1px solid #222;border-radius:12px;padding:20px}
.card h2{font-size:16px;color:#999;margin-bottom:14px;text-transform:uppercase;letter-spacing:1px}
.stat{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1a1a1a}
.stat:last-child{border:none}
.stat .label{color:#888;font-size:14px}
.stat .val{font-size:20px;font-weight:700;color:#e94560}
.stat .val.green{color:#53d769}
.stat .val.blue{color:#4ea8de}
.stat .val.orange{color:#f0a500}
.section{background:#111118;border:1px solid #222;border-radius:12px;padding:20px;margin-bottom:20px}
.section h2{font-size:18px;color:#e94560;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.btn{padding:8px 20px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-red{background:#e94560;color:#fff}.btn-red:hover{background:#c73e54}
.btn-blue{background:#0f3460;color:#4ea8de}.btn-blue:hover{background:#1a4a7a}
.btn-green{background:#0a3d2a;color:#53d769}.btn-green:hover{background:#0d4d34}
.btn-sm{padding:5px 12px;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px;background:#1a1a24;color:#999;font-weight:600;border-bottom:2px solid #333;position:sticky;top:0}
td{padding:8px 10px;border-bottom:1px solid #1a1a1a}
tr:hover{background:#15151f}
.badge{padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-ok{background:#0a3d2a;color:#53d769}
.badge-dead{background:#3d0a0a;color:#e94560}
.badge-queued{background:#0f3460;color:#4ea8de}
.badge-done{background:#0a3d2a;color:#53d769}
.badge-failed{background:#3d0a0a;color:#e94560}
.badge-processing{background:#3d2d0a;color:#f0a500}
.input{background:#1a1a24;border:1px solid #333;border-radius:8px;padding:8px 14px;color:#e0e0e0;font-size:14px;width:100%}
.input:focus{outline:none;border-color:#e94560}
.row{display:flex;gap:10px;margin-bottom:12px;align-items:center}
.row .input{flex:1}
.conn-box{background:#0d1117;border:1px solid #333;border-radius:8px;padding:16px;margin:12px 0}
.conn-box code{color:#53d769;font-size:14px;word-break:break-all}
.copy-btn{cursor:pointer;color:#4ea8de;font-size:12px;margin-left:8px}
.log-viewer{background:#0d0d12;border:1px solid #222;border-radius:8px;padding:12px;max-height:300px;overflow-y:auto;font-family:'Courier New',monospace;font-size:12px;color:#aaa}
.log-viewer div{padding:2px 0;border-bottom:1px solid #111}
.log-viewer div:last-child{border:none}
.scroll-table{max-height:400px;overflow-y:auto;border-radius:8px}
.actions-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.refresh-indicator{font-size:12px;color:#666}
.tab-bar{display:flex;gap:4px;margin-bottom:12px}
.tab{padding:6px 16px;border-radius:8px 8px 0 0;background:#1a1a24;cursor:pointer;font-size:13px;color:#666;border:1px solid #222;border-bottom:none}
.tab.active{background:#111118;color:#e94560}
.tab-content{display:none}.tab-content.active{display:block}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #333;border-top-color:#e94560;border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:768px){.grid{grid-template-columns:1fr}.header{flex-direction:column;gap:10px}}
</style>
</head>
<body>

<div class="header">
  <h1>🔥 ENI :: CONOL CONTROL PANEL</h1>
  <div class="status">
    <span id="gw-pill" class="pill bad">Gateway: OFFLINE</span>
    <span id="time-pill" class="pill info">--:--:--</span>
  </div>
</div>

<div class="container">

  <!-- Stats Grid -->
  <div class="grid">
    <div class="card">
      <h2>⚡ Gateway</h2>
      <div class="stat"><span class="label">Статус</span><span id="gw-status" class="val">—</span></div>
      <div class="stat"><span class="label">Версия</span><span id="gw-version" class="val blue">—</span></div>
      <div class="stat"><span class="label">Активных</span><span id="gw-active" class="val green">—</span></div>
      <div class="stat"><span class="label">Всего</span><span id="gw-total" class="val">—</span></div>
      <div class="stat"><span class="label">Запросов</span><span id="gw-reqs" class="val orange">—</span></div>
    </div>
    <div class="card">
      <h2>👥 Пул аккаунтов</h2>
      <div class="stat"><span class="label">Всего</span><span id="pool-total" class="val">—</span></div>
      <div class="stat"><span class="label">С cookies</span><span id="pool-active" class="val green">—</span></div>
      <div class="stat"><span class="label">Без cookies</span><span id="pool-dead" class="val orange">—</span></div>
      <div class="stat"><span class="label">Очередь email</span><span id="queue-total" class="val blue">—</span></div>
    </div>
    <div class="card">
      <h2>📊 Email очередь</h2>
      <div class="stat"><span class="label">Ожидают</span><span id="q-pending" class="val blue">—</span></div>
      <div class="stat"><span class="label">Готово</span><span id="q-done" class="val green">—</span></div>
      <div class="stat"><span class="label">Ошибка</span><span id="q-failed" class="val orange">—</span></div>
    </div>
  </div>

  <!-- Connection Info -->
  <div class="section">
    <h2>🔌 Подключение к API <span class="refresh-indicator">auto-refresh 5s</span></h2>
    <div class="conn-box">
      <div style="margin-bottom:8px;color:#888;font-size:13px;">OpenAI-совместимый endpoint:</div>
      <code id="api-url">http://127.0.0.1:9999/v1</code>
      <span class="copy-btn" onclick="copyText('http://127.0.0.1:9999/v1')">📋 копировать</span>
      <div style="margin-top:10px;margin-bottom:5px;color:#888;font-size:13px;">API ключ:</div>
      <code id="api-key">test</code>
      <span class="copy-btn" onclick="copyText(document.getElementById('api-key').textContent)">📋 копировать</span>
      <div style="margin-top:14px;color:#888;font-size:13px;line-height:1.6;">
        <b>OMP/Hermes config:</b><br>
        <code style="font-size:12px;">base_url: http://127.0.0.1:9999/v1<br>api_key: test<br>models: gpt-5.6-luna, deepseek/deepseek-v4-pro, claude-opus-4-8, gemini-3-pro, ...</code>
      </div>
    </div>
  </div>

  <!-- Actions -->
  <div class="section">
    <h2>🚀 Управление</h2>
    <div class="actions-bar">
      <button class="btn btn-green" onclick="gwStart()">▶ Запустить Gateway</button>
      <button class="btn btn-blue" onclick="poolReload()">🔄 Перезагрузить пул</button>
      <button class="btn btn-red" onclick="regSingle()">+ Регистрация (N акков)</button>
      <button class="btn btn-red" onclick="regMulti()">+ Массовая рег (из очереди)</button>
      <button class="btn btn-blue" onclick="questsRun()">🎯 Фарм квестов</button>
    </div>

    <!-- Quick reg form -->
    <div style="margin-top:12px;">
      <div class="row">
        <input class="input" id="reg-count" type="number" value="5" min="1" max="50" style="max-width:100px;">
        <span style="color:#888;font-size:13px;">кол-во акков для регистрации</span>
        <select class="input" id="reg-provider" style="max-width:160px;">
          <option value="">Email: auto</option>
          <option value="gmail">Email: gmail (+alias)</option>
          <option value="tonline">Email: t-online.de</option>
        </select>
        <label style="color:#888;font-size:13px;display:flex;align-items:center;gap:6px;">
          <input type="checkbox" id="reg-free-captcha"> фри-капча (Chrome CDP, без AntiCaptcha)
        </label>
        <button class="btn btn-red btn-sm" onclick="regSingleConfirm()">Регистрировать</button>
      </div>
    </div>

    <!-- Queue add form -->
    <div style="margin-top:16px;border-top:1px solid #222;padding-top:16px;">
      <h2 style="margin-bottom:12px;">📧 Добавить email в очередь</h2>
      <div class="row">
        <input class="input" id="queue-email" type="text" placeholder="email@gmail.com" style="flex:2;">
        <input class="input" id="queue-target" type="number" value="3" min="1" max="20" style="max-width:100px;flex:1;" title="аккаунтов на email">
        <button class="btn btn-blue btn-sm" onclick="queueAdd()">Добавить</button>
      </div>
    </div>
  </div>

  <!-- Tabs -->
  <div class="section">
    <div class="tab-bar">
      <div class="tab active" onclick="switchTab('accounts')">Аккаунты</div>
      <div class="tab" onclick="switchTab('queue')">Очередь</div>
      <div class="tab" onclick="switchTab('quests')">Квесты</div>
      <div class="tab" onclick="switchTab('models')">Модели</div>
      <div class="tab" onclick="switchTab('apitest')">API Test</div>
      <div class="tab" onclick="switchTab('logs')">Логи</div>
    </div>

    <!-- Accounts tab -->
    <div id="tab-accounts" class="tab-content active">
      <div class="scroll-table">
        <table>
          <thead><tr><th>#</th><th>Email</th><th>Имя</th><th>Cookies</th><th>Credits</th><th>Регистрация</th></tr></thead>
          <tbody id="acc-tbody"><tr><td colspan="6" style="text-align:center;color:#666;">Загрузка...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Queue tab -->
    <div id="tab-queue" class="tab-content">
      <div style="margin-bottom:10px;">
        <button class="btn btn-blue btn-sm" onclick="queueClear()">Очистить очередь</button>
      </div>
      <div class="scroll-table">
        <table>
          <thead><tr><th>Email</th><th>Статус</th><th>Акков</th><th>Цель</th><th>Добавлен</th><th>Действие</th></tr></thead>
          <tbody id="queue-tbody"><tr><td colspan="6" style="text-align:center;color:#666;">Загрузка...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Models tab -->
    <div id="tab-models" class="tab-content">
      <div id="models-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;"></div>
    </div>

    <!-- Quests tab -->
    <div id="tab-quests" class="tab-content">
      <div class="grid" style="margin-bottom:16px;">
        <div class="card">
          <h2>🎯 Квесты — итог</h2>
          <div class="stat"><span class="label">Аккаунтов</span><span id="qs-accounts" class="val">—</span></div>
          <div class="stat"><span class="label">Квестов выполнено</span><span id="qs-done" class="val green">—</span></div>
          <div class="stat"><span class="label">Кредитов заработано</span><span id="qs-credits" class="val orange">—</span></div>
        </div>
      </div>
      <div class="scroll-table">
        <table>
          <thead><tr><th>Email</th><th>Квестов</th><th>Credits</th><th>Детали</th></tr></thead>
          <tbody id="qs-tbody"><tr><td colspan="4" style="text-align:center;color:#666;">Нет данных</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- API Test tab -->
    <div id="tab-apitest" class="tab-content">
      <div class="card" style="margin-bottom:16px;">
        <h2>🔧 Tool Use Test</h2>
        <div class="row">
          <select class="input" id="tu-model" style="max-width:220px;">
            <option value="gpt-5.6-luna">gpt-5.6-luna</option>
            <option value="claude-opus-4-8">claude-opus-4-8</option>
            <option value="deepseek/deepseek-v4-pro">deepseek-v4-pro</option>
            <option value="gemini-3-pro">gemini-3-pro</option>
          </select>
          <button class="btn btn-green" onclick="toolUseTest()">▶ Тест tool_calls</button>
          <button class="btn btn-blue" onclick="toolUseStream()">🌊 Тест streaming</button>
        </div>
        <div style="margin-top:8px;color:#888;font-size:13px;">
          Тестирует OpenAI-compatible endpoint с tools (get_weather). Проверяет <code>finish_reason: tool_calls</code> и <code>delta.content</code> streaming.
        </div>
      </div>
      <div class="log-viewer" id="tu-output" style="max-height:400px;">Нажмите кнопку для теста...</div>
    </div>

    <!-- Logs tab -->
    <div id="tab-logs" class="tab-content">
      <div class="tab-bar">
        <div class="tab active" onclick="switchLog('gateway')">gateway</div>
        <div class="tab" onclick="switchLog('reg')">reg</div>
        <div class="tab" onclick="switchLog('multi_reg')">multi_reg</div>
        <div class="tab" onclick="switchLog('quests')">quests</div>
      </div>
      <div class="log-viewer" id="log-viewer">Выберите лог...</div>
    </div>
  </div>

</div>

<script>
let currentLog='gateway';
let refreshTimer;

async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body)opts.body=JSON.stringify(body);
  try{const r=await fetch(path,opts);return await r.json();}
  catch(e){return{ok:false,error:e.message};}
}

async function refresh(){
  const d=await api('/api/state');
  if(!d||!d.gateway)return;

  // Gateway
  const gw=d.gateway;
  const pill=document.getElementById('gw-pill');
  if(gw.alive){
    pill.className='pill ok';pill.textContent='Gateway: ONLINE';
  }else{
    pill.className='pill bad';pill.textContent='Gateway: OFFLINE';
  }
  document.getElementById('gw-status').textContent=gw.alive?'ONLINE':'OFFLINE';
  document.getElementById('gw-status').className='val '+(gw.alive?'green':'');
  document.getElementById('gw-version').textContent=gw.version;
  document.getElementById('gw-active').textContent=gw.active;
  document.getElementById('gw-total').textContent=gw.total;
  document.getElementById('gw-reqs').textContent=gw.total_requests||0;
  document.getElementById('api-key').textContent=gw.api_key||'test';

  // Pool
  document.getElementById('pool-total').textContent=d.pool.total;
  document.getElementById('pool-active').textContent=d.pool.active;
  document.getElementById('pool-dead').textContent=d.pool.dead;
  document.getElementById('queue-total').textContent=d.queue.total;

  // Queue
  document.getElementById('q-pending').textContent=d.queue.pending;
  document.getElementById('q-done').textContent=d.queue.done;
  document.getElementById('q-failed').textContent=d.queue.failed;

  // Time
  document.getElementById('time-pill').textContent=d.timestamp;

  // Accounts table
  const tb=document.getElementById('acc-tbody');
  if(d.pool.accounts.length===0){
    tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#666;">Нет аккаунтов</td></tr>';
  }else{
    tb.innerHTML=d.pool.accounts.map((a,i)=>`<tr>
      <td>${i+1}</td>
      <td>${a.email}</td>
      <td>${a.name}</td>
      <td><span class="badge ${a.cookies_ok?'badge-ok':'badge-dead'}">${a.cookies_ok?'OK':'DEAD'}</span></td>
      <td>${a.credits||0}</td>
      <td style="font-size:11px;color:#888;">${a.registered||'—'}</td>
    </tr>`).join('');
  }

  // Queue table
  const qb=document.getElementById('queue-tbody');
  if(d.queue.items.length===0){
    qb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#666;">Очередь пуста</td></tr>';
  }else{
    qb.innerHTML=d.queue.items.map(e=>`<tr>
      <td>${e.email}</td>
      <td><span class="badge badge-${e.status||'queued'}">${e.status||'queued'}</span></td>
      <td>${e.accounts_registered||0}</td>
      <td>${e.accounts_target||3}</td>
      <td style="font-size:11px;color:#888;">${e.added_human||'—'}</td>
      <td><button class="btn btn-red btn-sm" onclick="queueRemove('${e.email}')">🗑</button></td>
    </tr>`).join('');
  }

  // Models
  const ml=document.getElementById('models-list');
  ml.innerHTML=d.models.map(m=>`<div style="background:#1a1a24;padding:10px;border-radius:8px;border:1px solid #222;font-size:13px;color:#4ea8de;cursor:pointer;" onclick="copyText('${m}')">${m}</div>`).join('');

  // Quests
  if(d.quests){
    document.getElementById('qs-accounts').textContent=d.quests.total_accounts;
    document.getElementById('qs-done').textContent=d.quests.total_done;
    document.getElementById('qs-credits').textContent=d.quests.total_credits.toFixed(0);
    const qsb=document.getElementById('qs-tbody');
    if(!d.quests.items||d.quests.items.length===0){
      qsb.innerHTML='<tr><td colspan="4" style="text-align:center;color:#666;">Квесты ещё не фармились</td></tr>';
    }else{
      qsb.innerHTML=d.quests.items.map(q=>{
        const qd=q.quests||{};
        const details=Object.entries(qd).map(([k,v])=>{
          const st=typeof v==='string'?v:(v&&v.status||'ran');
          return `${k}:${st}`;
        }).join('; ');
        return `<tr>
          <td>${q.email||'?'}</td>
          <td>${q.quests_done||0}</td>
          <td>${(q.credits_earned||0).toFixed(0)}</td>
          <td style="font-size:11px;color:#888;">${details.substring(0,120)}</td>
        </tr>`;
      }).join('');
    }
  }
}

// Actions
async function gwStart(){
  const r=await api('/api/gateway/start','POST');
  if(r.ok){setTimeout(refresh,2000);}else{alert('Error: '+r.error);}
}
async function poolReload(){
  const r=await api('/api/pool/reload','POST');
  if(r.ok)refresh();else alert('Gateway not reachable: '+r.error);
}
async function regSingle(){
  const n=parseInt(document.getElementById('reg-count').value)||5;
  const provider=document.getElementById('reg-provider').value||null;
  const freeCaptcha=document.getElementById('reg-free-captcha').checked;
  const r=await api('/api/reg/single','POST',{count:n,provider:provider,free_captcha:freeCaptcha});
  alert(r.ok?r.message:'Error: '+r.error);
}
function regSingleConfirm(){regSingle();}
async function regMulti(){
  const r=await api('/api/reg/run','POST',{per_email:3,max_parallel:2});
  alert(r.ok?'Массовая регистрация запущена':'Error: '+r.error);
}
async function questsRun(){
  const r=await api('/api/quests/run','POST');
  alert(r.ok?'Фарм квестов запущен':'Error: '+r.error);
}
async function toolUseTest(){
  const model=document.getElementById('tu-model').value;
  const out=document.getElementById('tu-output');
  out.innerHTML='<div style="color:#f0a500;">⏳ Тестирую tool_calls через gateway...</div>';
  const r=await api('/api/tooluse/test','POST',{model,stream:false});
  if(r.ok&&r.body){
    const c=r.body.choices&&r.body.choices[0];
    const fr=c?c.finish_reason:'?';
    const tc=c&&c.message?c.message.tool_calls:null;
    out.innerHTML=`<div style="color:#53d769;">✅ HTTP ${r.status} | finish_reason: ${fr}</div>`+
      (tc?`<div style="color:#53d769;">tool_calls: ${JSON.stringify(tc,null,2)}</div>`:'')+
      `<div style="margin-top:8px;color:#888;">Full response:</div>`+
      `<div>${JSON.stringify(r.body,null,2).replace(/</g,'&lt;')}</div>`;
  }else{
    out.innerHTML=`<div style="color:#e94560;">❌ HTTP ${r.status||'?'}: ${r.error||r.body||'failed'}</div>`;
  }
}
async function toolUseStream(){
  const model=document.getElementById('tu-model').value;
  const out=document.getElementById('tu-output');
  out.innerHTML='<div style="color:#f0a500;">⏳ Тестирую streaming через gateway...</div>';
  const r=await api('/api/tooluse/test','POST',{model,stream:true});
  if(r.ok&&r.body){
    out.innerHTML=`<div style="color:#53d769;">✅ HTTP ${r.status}</div>`+
      `<div style="margin-top:8px;color:#888;">Streaming response (raw):</div>`+
      `<div>${JSON.stringify(r.body,null,2).replace(/</g,'&lt;')}</div>`;
  }else{
    out.innerHTML=`<div style="color:#e94560;">❌ HTTP ${r.status||'?'}: ${r.error||r.body||'failed'}</div>`;
  }
}
async function queueAdd(){
  const email=document.getElementById('queue-email').value.trim();
  const target=parseInt(document.getElementById('queue-target').value)||3;
  if(!email){alert('Введите email');return;}
  const r=await api('/api/queue/add','POST',{email,target});
  if(r.ok){document.getElementById('queue-email').value='';refresh();}else{alert('Error: '+r.error);}
}
async function queueRemove(email){
  await api('/api/queue/remove','POST',{email});
  refresh();
}
async function queueClear(){
  if(confirm('Очистить всю очередь?')){
    await api('/api/queue/clear','POST');
    refresh();
  }
}

// Tabs
function switchTab(name){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-bar > .tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  event.target.classList.add('active');
  if(name==='logs')switchLog(currentLog);
}

// Logs
async function switchLog(name){
  currentLog=name;
  document.querySelectorAll('#tab-logs .tab').forEach(t=>t.classList.remove('active'));
  event.target.classList.add('active');
  const r=await api('/api/logs/'+name);
  const lv=document.getElementById('log-viewer');
  if(r.ok&&r.lines){
    lv.innerHTML=r.lines.map(l=>`<div>${l.replace(/</g,'&lt;')}</div>`).join('');
    lv.scrollTop=lv.scrollHeight;
  }else{
    lv.innerHTML='<div style="color:#666;">Нет логов</div>';
  }
}

// Utils
function copyText(text){
  navigator.clipboard.writeText(text).then(()=>{});
}

// Init
refresh();
refreshTimer=setInterval(refresh,5000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"ENI Conol Dashboard — http://127.0.0.1:{DASH_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=DASH_PORT, log_level="info")
