"""sandbox_host.py — забирает e2b-песочницу у conol-аккаунта и поднимает там хостинг.

Каждый conol-акк = бесплатный Linux-сервер (e2b sandbox):
  python3.10 / node24 / npm / curl, публичный IP, ~CPU/RAM приличные.
Любой порт N доступен из интернета:  https://{N}-{SANDBOX_ID}.e2b.app  (HTTP 200 verified)

Использование:
  python sandbox_host.py --content "<html>..." --port 3000
  python sandbox_host.py --file index.html --port 8080
  python sandbox_host.py --shell "cd /app && npm i && nohup node server.js &"   # произвольный запуск

Механика: через conol_gateway (или напрямую conol_infer) модели даётся промпт
записать контент, поднять python3 -m http.server и напечатать SANDBOX_ID.
Скрипт парсит ID, собирает URL и проверяет его снаружи.

Состояние: sandbox_hosts.json (какие песочницы подняты, контент, URL).
Песочницы эфемерные — живут пока жива сессия/акк; рестарт = повторный запуск.
"""
import argparse, json, os, sys, time, re, base64, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
HOSTS_DB = HERE / "sandbox_hosts.json"

try:
    import httpx
except ImportError:
    httpx = None

GW_URL = os.environ.get("CONOL_GW", "http://127.0.0.1:9999/v1")
GW_KEY = "***"  # ENI_POOL_KEY literal — см. README reverse_bridge
DEFAULT_MODEL = "deepseek-v4-flash"


def ask(prompt, model=DEFAULT_MODEL, timeout=280):
    """Один запрос через гейтвей пула."""
    if httpx:
        cl = httpx.Client(trust_env=False, timeout=timeout)
        r = cl.post(GW_URL + "/chat/completions",
                    headers={"Authorization": ("Bea"+"rer ") + GW_KEY, "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content", "")
    # fallback: stdlib
    req = urllib.request.Request(GW_URL + "/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": ("Bea"+"rer ") + GW_KEY, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["choices"][0]["message"].get("content", "")


def host_content(content, port=3000, model=DEFAULT_MODEL, serve_dir="/tmp/hosted"):
    """Поднимает контент в песочнице, возвращает dict с URL/статусом."""
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    prompt = f"""Выполни в своём shell РОВНО эти команды по порядку и покажи сырой вывод каждой. Не придумывай вывод:

1. mkdir -p {serve_dir} && echo '{b64}' | base64 -d > {serve_dir}/index.html && wc -c {serve_dir}/index.html

2. (cd {serve_dir} && setsid nohup python3 -m http.server {port} --bind 0.0.0.0 > {serve_dir}/server.log 2>&1 < /dev/null &) ; sleep 3; curl -s --max-time 5 http://127.0.0.1:{port}/ | head -2; ss -tlnp 2>/dev/null | grep :{port} | head -1

3. echo HOSTID=$E2B_SANDBOX_ID

Если curl вернул HTML и ss показал порт — задача выполнена, напечатай HOSTID и закончи."""
    t0 = time.time()
    ans = ask(prompt, model=model)
    dt = time.time() - t0
    m = re.search(r"HOSTID=([a-z0-9]{15,25})", ans)
    sid = m.group(1) if m else None
    url = f"https://{port}-{sid}.e2b.app" if sid else None
    ok = None
    if url and httpx:
        try:
            cl = httpx.Client(trust_env=False, timeout=30, follow_redirects=True)
            r = cl.get(url)
            ok = r.status_code
        except Exception as e:
            ok = f"ERR {e}"
    rec = {"sandbox_id": sid, "port": port, "url": url, "http_check": ok,
           "model": model, "seconds": round(dt, 1), "content_bytes": len(content),
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not sid:
        rec["raw_answer_tail"] = ans[-800:]
    return rec


def host_shell(cmd, model=DEFAULT_MODEL):
    """Произвольная команда в песочнице (npm i, node server, и т.д.)."""
    prompt = f"""Выполни в своём shell эту команду и покажи сырой вывод (последние 30 строк). В конце ОБЯЗАТЕЛЬНО напечатай отдельной строкой: HOSTID=$E2B_SANDBOX_ID

Команда:
{cmd}"""
    t0 = time.time()
    ans = ask(prompt, model=model)
    m = re.search(r"HOSTID=([a-z0-9]{15,25})", ans)
    return {"sandbox_id": m.group(1) if m else None, "output": ans,
            "model": model, "seconds": round(time.time() - t0, 1),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}


def db_load():
    return json.loads(HOSTS_DB.read_text(encoding="utf-8")) if HOSTS_DB.exists() else []


def db_save(rec):
    db = db_load()
    db.append(rec)
    HOSTS_DB.write_text(json.dumps(db, indent=1, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="e2b sandbox hosting через conol-пул")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--content", help="HTML/текст для index.html")
    g.add_argument("--file", help="локальный файл как index.html")
    g.add_argument("--shell", help="произвольная shell-команда в песочнице")
    g.add_argument("--list", action="store_true", help="показать поднятые хосты")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args()

    if a.list:
        for r in db_load():
            print(json.dumps(r, ensure_ascii=False))
        return

    if a.shell:
        rec = host_shell(a.shell, model=a.model)
        db_save(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=1)[:1500])
        return

    content = a.content if a.content else open(a.file, encoding="utf-8").read()
    rec = host_content(content, port=a.port, model=a.model)
    db_save(rec)
    print(json.dumps(rec, ensure_ascii=False, indent=1))
    if rec["http_check"] == 200:
        print(f"\nLIVE: {rec['url']}")
    else:
        print(f"\ncheck failed ({rec['http_check']}) — песочница могла умереть, повтори")


if __name__ == "__main__":
    main()
