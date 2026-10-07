# -*- coding: utf-8 -*-
"""Conol Agent Bridge — computer-use агент поверх conol-серверов.

Архитектура (reverse): агент-мозг крутится на conol.ai (через gateway :9999
или напрямую через cookie-пул), а исполнитель — этот воркер на твоём ПК.
ПК сам ходит наружу (outbound), открытых портов нет.

Воркер:
  1. шлёт запрос модели на conol с tool-схемами (shell/file/screenshot)
  2. модель отвечает tool_calls
  3. воркер исполняет ЛОКАЛЬНО, результат обратно в модель
  4. цикл до финального текста

Запуск:
  python agent_bridge.py "задача агента"            # one-shot
  python agent_bridge.py --repl                     # интерактив

Config: config.json (gitignored). Пример: config.example.json
  gateway_url: http://127.0.0.1:9999/v1  (conol_gateway.py)
  gateway_key: ключ гейтвея (test-key-123 по умолчанию)
  model:       имя модели из /v1/models
  allow:       {"shell": true, "write": false, "screenshot": true}
  workdir:     корень для файловых операций (sandbox)
"""
import argparse, base64, io, json, os, subprocess, sys, time

try:
    import httpx
except ImportError:
    sys.exit("pip install httpx")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CFG = {
    "gateway_url": "http://127.0.0.1:9999/v1",
    "gateway_key": "test-key-123",
    "model": None,  # None = первая из /v1/models
    "allow": {"shell": True, "write": False, "screenshot": True},
    "workdir": os.path.expanduser("~"),
    "max_turns": 20,
    "shell_timeout": 120,
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        cfg.update(json.load(open(CFG_PATH, encoding="utf-8")))
    return cfg


TOOLS = [
    {"type": "function", "function": {
        "name": "shell_exec",
        "description": "Выполнить shell-команду на ПК пользователя (Windows, git-bash доступен). Возвращает stdout+stderr+exit_code.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "workdir": {"type": "string", "description": "опц. рабочая директория"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "file_read",
        "description": "Прочитать текстовый файл с ПК. Возвращает содержимое (до 100KB).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "file_write",
        "description": "Записать файл на ПК (перезапись).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "file_list",
        "description": "Список файлов в директории ПК.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "screenshot",
        "description": "Скриншот экрана ПК (base64 PNG). Модель получает картинку.",
        "parameters": {"type": "object", "properties": {}}}},
]


def safe_path(cfg, p):
    p = os.path.abspath(os.path.expanduser(p))
    root = os.path.abspath(os.path.expanduser(cfg["workdir"]))
    if not (p == root or p.startswith(root + os.sep)):
        return None
    return p


def tool_call(cfg, name, args):
    allow = cfg["allow"]
    if name == "shell_exec":
        if not allow.get("shell"):
            return {"error": "shell disabled in config"}
        wd = args.get("workdir") or cfg["workdir"]
        try:
            r = subprocess.run(args["command"], shell=True, cwd=wd,
                               capture_output=True, timeout=cfg["shell_timeout"],
                               text=True, encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            return {"exit_code": r.returncode, "output": out[:20000]}
        except subprocess.TimeoutExpired:
            return {"error": f"timeout {cfg['shell_timeout']}s"}
        except Exception as e:
            return {"error": str(e)[:500]}
    if name in ("file_read", "file_write", "file_list"):
        if name == "file_write" and not allow.get("write"):
            return {"error": "write disabled in config"}
        p = safe_path(cfg, args["path"])
        if p is None:
            return {"error": "path outside sandbox workdir"}
        try:
            if name == "file_read":
                off = int(args.get("offset") or 0)
                lim = int(args.get("limit") or 100000)
                data = open(p, encoding="utf-8", errors="replace").read()
                return {"content": data[off:off + lim], "size": len(data)}
            if name == "file_write":
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                open(p, "w", encoding="utf-8").write(args["content"])
                return {"written": len(args["content"]), "path": p}
            entries = []
            for e in sorted(os.listdir(p))[:500]:
                fp = os.path.join(p, e)
                entries.append(("D " if os.path.isdir(fp) else "F ") + e)
            return {"entries": entries}
        except Exception as e:
            return {"error": str(e)[:500]}
    if name == "screenshot":
        if not allow.get("screenshot"):
            return {"error": "screenshot disabled in config"}
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return {"image_png_b64": base64.b64encode(buf.getvalue()).decode()}
        except Exception as e:
            return {"error": f"screenshot failed: {e}"}
    return {"error": f"unknown tool {name}"}


def pick_model(cfg, client):
    if cfg["model"]:
        return cfg["model"]
    r = client.get(cfg["gateway_url"].rstrip("/") + "/models",
                   headers={"Authorization": "Bearer " + cfg["gateway_key"]}, timeout=30)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    if not ids:
        sys.exit("gateway returned no models")
    return ids[0]


def run_task(cfg, task, history=None, verbose=True):
    client = httpx.Client(trust_env=False)
    model = pick_model(cfg, client)
    if verbose:
        print(f"[bridge] model={model} gateway={cfg['gateway_url']}", flush=True)
    messages = history or [{"role": "system", "content":
        "Ты — агент с доступом к ПК пользователя через инструменты. "
        "Windows 11, git-bash. Делай задачу пошагово, проверяй результаты, "
        "не выдумывай. Скриншоты — только когда нужен визуальный контекст."}]
    messages.append({"role": "user", "content": task})
    for turn in range(cfg["max_turns"]):
        r = client.post(cfg["gateway_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + cfg["gateway_key"],
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages, "tools": TOOLS,
                              "temperature": 0.2},
                        timeout=300)
        if r.status_code != 200:
            print(f"[bridge] HTTP {r.status_code}: {r.text[:300]}", flush=True)
            return None
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            final = msg.get("content") or ""
            if verbose:
                print(f"[bridge] FINAL (turn {turn+1}):\n{final}", flush=True)
            messages.append({"role": "assistant", "content": final})
            return {"final": final, "messages": messages}
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if verbose:
                print(f"[bridge] tool {name} {json.dumps(args, ensure_ascii=False)[:200]}", flush=True)
            result = tool_call(cfg, name, args)
            if "image_png_b64" in result:
                # мультимодальная отдача если модель держит картинки
                content = [{"type": "text", "text": "screenshot:"},
                           {"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + result["image_png_b64"]}}]
                result = content
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": result if isinstance(result, list)
                             else json.dumps(result, ensure_ascii=False)[:20000]})
    print("[bridge] max_turns reached", flush=True)
    return {"final": None, "messages": messages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?")
    ap.add_argument("--repl", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="локальный тест инструментов без модели")
    a = ap.parse_args()
    cfg = load_cfg()
    if a.selftest:
        print(json.dumps(tool_call(cfg, "shell_exec", {"command": "echo BRIDGE_OK && hostname"}), ensure_ascii=False))
        print(json.dumps(tool_call(cfg, "file_list", {"path": cfg["workdir"]}), ensure_ascii=False)[:300])
        r = tool_call(cfg, "screenshot", {})
        print("screenshot bytes:", len(r.get("image_png_b64", "")) if isinstance(r, dict) else "?", r if "error" in str(r) else "")
        return
    if a.repl:
        hist = None
        while True:
            try:
                t = input("task> ").strip()
            except EOFError:
                break
            if not t:
                continue
            res = run_task(cfg, t, history=hist)
            if res:
                hist = res["messages"]
        return
    if not a.task:
        ap.error("task required (или --repl / --selftest)")
    run_task(cfg, a.task)


if __name__ == "__main__":
    main()
