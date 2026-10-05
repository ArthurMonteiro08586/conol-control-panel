"""conol_quest_farm.py — full quest farm: all live pool accounts, easy quests (4 x 300cr).
Resumable: state in conol_quest_farm_state.json (gitignored). Sequential with cooldowns.
Old accounts with completed quests are skipped instantly.

Usage: python -X utf8 conol_quest_farm.py
"""
import json, sys, time, urllib.request, urllib.error
from pathlib import Path

repo = Path(r"C:\Users\User\Desktop\_PROJECTS\conol_autoreg")
STATE = Path(__file__).resolve().parent / "conol_quest_farm_state.json"
rows = [json.loads(l) for l in (repo / "conol_accounts_pool.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
live = [r for r in rows if r.get("status") == "live" and r.get("cookies_path") and Path(r["cookies_path"]).exists()]

EASY = {
    "note_written_by_agent": "Please create a new note titled 'ENI Log' with this content: 'Quest marker - note created by ENI automation.'",
    "memory_written": "Save this fact to your persistent memory: 'ENI automation: quest farming system initialized successfully.' Use the memory tool.",
    "note_edited_by_agent": "Edit the note titled 'ENI Log' - append this line: 'Updated: quest edit complete.'",
    "timer_scheduled": "Schedule a timer/reminder for 5 minutes from now with the message 'ENI quest timer check'. Use your scheduling tool.",
}

state = json.loads(STATE.read_text()) if STATE.exists() else {}

def save():
    STATE.write_text(json.dumps(state, indent=0))

def hdrs(cp):
    cookies = json.loads(Path(cp).read_text(encoding="utf-8"))
    return {"Cookie": "; ".join(f'{c["name"]}={c["value"]}' for c in cookies),
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 Chrome/140.0 Safari/537.36"}

def api(H, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request("https://conol.ai" + path, data=data, method=method, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return -1, str(e)[:120]

total_earned = state.get("_total_earned", 0)
t_start = time.time()
print(f"farm start: {len(live)} accounts, already done: {len(state)} states, earned so far: {total_earned}cr", flush=True)

for idx, acc in enumerate(live):
    email = acc["email"]
    if state.get(email, {}).get("done"):
        continue
    H = hdrs(acc["cookies_path"])
    st, body = api(H, "GET", "/api/quests", timeout=20)
    if st != 200:
        state[email] = {"done": False, "err": f"quests HTTP {st}"}
        save(); time.sleep(2); continue
    qs = {q["id"]: q.get("completed") for q in json.loads(body).get("quests", [])}
    pending = [q for q in EASY if not qs.get(q)]
    if not pending:
        state[email] = {"done": True, "earned": 0, "note": "all easy complete"}
        save(); time.sleep(1); continue

    earned = 0
    for qid in pending:
        st, body = api(H, "POST", "/api/sessions", {
            "source": {"type": "home"},
            "messages": [{"type": "text", "content": EASY[qid]}],
            "timezone": "Europe/Kyiv", "agentModel": "gpt-5.6-luna", "agentEffort": "low",
        }, timeout=40)
        if st != 201:
            time.sleep(10); continue
        sid = json.loads(body).get("sessionId")
        # poll until quest flips (max 100s)
        deadline = time.time() + 100
        flipped = False
        while time.time() < deadline:
            time.sleep(10)
            s2, b2 = api(H, "GET", "/api/quests", timeout=15)
            if s2 == 200:
                qs2 = {q["id"]: q.get("completed") for q in json.loads(b2).get("quests", [])}
                if qs2.get(qid):
                    flipped = True; break
        if flipped:
            earned += 300
        time.sleep(2)
    state[email] = {"done": True, "earned": earned, "quests": pending,
                    "ts": int(time.time())}
    total_earned += earned
    state["_total_earned"] = total_earned
    save()
    el = int(time.time() - t_start)
    print(f"[{idx+1}/{len(live)}] {email[-28:]}: +{earned}cr (total {total_earned}cr, {el}s elapsed)", flush=True)
    time.sleep(4)

print(f"\nFARM COMPLETE: earned {total_earned} credits total", flush=True)
