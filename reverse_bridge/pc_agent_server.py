# -*- coding: utf-8 -*-
"""PC Agent Server — исполнитель на ПК Влада, наружу через Cloudflare Tunnel.

Схема (reverse):
  [conol-агент в e2b-песочнице] --HTTPS--> [xxx.trycloudflare.com] --tunnel--> [этот сервер :18099]

e2b-песочница не видит домашний ПК (NAT), но видит публичный CF-туннель.
Агент шлёт POST с токеном, сервер исполняет на ПК и возвращает результат.

Endpoints (все POST, JSON, заголовок X-Agent-Token обязателен):
  /exec       {"command": "...", "workdir": "..."}  -> shell на ПК
  /read       {"path": "...", "offset":0, "limit":100000}
  /write      {"path": "...", "content": "..."}     (если allow.write)
  /list       {"path": "..."}
  /screenshot {}                                    -> base64 PNG экрана ПК
  /info       {}                                    -> hostname/user/os (доказательство что это ПК, не песочница)

Запуск:  python pc_agent_server.py            (токен авто-генерится в agent_token.txt)
         python pc_agent_server.py --port 18099 --allow-write
Туннель: cloudflared tunnel --url http://127.0.0.1:18099   (в отдельном окне)
"""
import argparse, base64, io, json, os, secrets, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "agent_token.txt")
STATE = {"allow_write": False, "workdir": os.path.expanduser("~"), "shell_timeout": 120}


def get_token():
    if os.path.exists(TOKEN_PATH):
        t = open(TOKEN_PATH, encoding="utf-8").read().strip()
        if t:
            return t
    t = secrets.token_urlsafe(32)
    open(TOKEN_PATH, "w", encoding="utf-8").write(t)
    return t


def safe_path(p):
    p = os.path.abspath(os.path.expanduser(p))
    root = os.path.abspath(os.path.expanduser(STATE["workdir"]))
    return p if (p == root or p.startswith(root + os.sep)) else None


def do_exec(a):
    wd = a.get("workdir") or STATE["workdir"]
    try:
        r = subprocess.run(a["command"], shell=True, cwd=wd, capture_output=True,
                           timeout=STATE["shell_timeout"], text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        return {"exit_code": r.returncode, "output": out[:30000],
                "hostname": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")}
    except subprocess.TimeoutExpired:
        return {"error": f"timeout {STATE['shell_timeout']}s"}
    except Exception as e:
        return {"error": str(e)[:500]}


def do_read(a):
    p = safe_path(a.get("path", ""))
    if not p:
        return {"error": "path outside sandbox"}
    try:
        off = int(a.get("offset") or 0); lim = int(a.get("limit") or 100000)
        data = open(p, encoding="utf-8", errors="replace").read()
        return {"content": data[off:off + lim], "size": len(data)}
    except Exception as e:
        return {"error": str(e)[:300]}


def do_write(a):
    if not STATE["allow_write"]:
        return {"error": "write disabled (start with --allow-write)"}
    p = safe_path(a.get("path", ""))
    if not p:
        return {"error": "path outside sandbox"}
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        open(p, "w", encoding="utf-8").write(a.get("content", ""))
        return {"written": len(a.get("content", "")), "path": p}
    except Exception as e:
        return {"error": str(e)[:300]}


def do_list(a):
    p = safe_path(a.get("path", ""))
    if not p:
        return {"error": "path outside sandbox"}
    try:
        entries = []
        for e in sorted(os.listdir(p))[:500]:
            fp = os.path.join(p, e)
            entries.append(("D " if os.path.isdir(fp) else "F ") + e)
        return {"entries": entries}
    except Exception as e:
        return {"error": str(e)[:300]}


def do_screenshot(_a):
    try:
        from PIL import ImageGrab
        buf = io.BytesIO()
        ImageGrab.grab().save(buf, format="PNG")
        return {"image_png_b64": base64.b64encode(buf.getvalue()).decode()}
    except Exception as e:
        return {"error": f"screenshot failed: {e}"}


def do_info(_a):
    import platform, getpass
    return {"hostname": platform.node(), "user": getpass.getuser(),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cwd": os.getcwd()}


HANDLERS = {"/exec": do_exec, "/read": do_read, "/write": do_write,
            "/list": do_list, "/screenshot": do_screenshot, "/info": do_info}


class H(BaseHTTPRequestHandler):
    token = None

    def log_message(self, *a):
        sys.stderr.write("[pc-agent] %s\n" % (a[0] % a[1:]))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "service": "pc-agent"})
        self._send(404, {"error": "use POST /exec /read /write /list /screenshot /info"})

    def do_POST(self):
        if self.headers.get("X-Agent-Token") != self.token:
            return self._send(401, {"error": "bad token"})
        fn = HANDLERS.get(self.path)
        if not fn:
            return self._send(404, {"error": f"unknown endpoint {self.path}"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            args = {}
        self._send(200, fn(args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-write", action="store_true")
    ap.add_argument("--workdir", default=os.path.expanduser("~"))
    ap.add_argument("--print-token", action="store_true")
    a = ap.parse_args()
    STATE["allow_write"] = a.allow_write
    STATE["workdir"] = a.workdir
    H.token = get_token()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"[pc-agent] listening http://{a.host}:{a.port} | write={'ON' if a.allow_write else 'OFF'} | sandbox={STATE['workdir']}")
    if a.print_token:
        print(f"[pc-agent] token: {H.token}")
    else:
        print(f"[pc-agent] token in {TOKEN_PATH}")
    print("[pc-agent] next: cloudflared tunnel --url http://127.0.0.1:%d" % a.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
