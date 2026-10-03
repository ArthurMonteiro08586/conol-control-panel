"""probe_conol_infer.py — which conol models actually answer, and how fast.

Thin CLI over conol_infer.py (the protocol module shared with conol_gateway.py). The audit's
"live" verdict only proves `GET /api/auth/get-session` is 200; it says nothing about whether
an account can run a session and produce an answer, and conol silently downgrades some model
names (`modelDowngraded` / `effectiveModel` in the create response). Sweep before trusting a
model list in a gateway or a New API channel.

Usage:
    python probe_conol_infer.py --selfcheck
    python probe_conol_infer.py --models gpt-5.5 deepseek/deepseek-v4-pro --accounts 2
    python probe_conol_infer.py --sweep            # every candidate model, one account each
    python probe_conol_infer.py --sweep --json     # + machine-readable dump
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from conol_infer import (BASE_URL, check_session, cookie_for, live_accounts, load_pool,
                         run_inference)

BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "conol_accounts_pool.jsonl"

# Candidates worth sweeping. The Aug gateway advertised 18 model ids; the skill recorded 9
# as working, but that verdict came from a parser that returned the user's own prompt for
# every model (it read `logs` and ignored role), so the real working set is re-measured here
# rather than inherited.
# Authoritative spellings, from gateway.py's CONOL_MODEL_MAP (verified live 2026-07-31) and
# re-verified 2026-10-03 with a positive control. A misspelt id is NOT reported as an error
# by conol: POST /api/sessions answers 201 with a sessionId and the agent then produces zero
# assistant text, so the probe records EMPTY and the model looks unavailable. The previous
# list contained exactly those misspellings -- glm/glm-5.2 (correct: z-ai/glm-5.2),
# kimi/kimi-k3 (correct: moonshotai/kimi-k2.7-code), qwen/qwen-3.7 (correct: qwen/qwen3.7-plus
# and qwen/qwen3.7-max), plus deepseek/deepseek-v4-base, gemini-3-pro, gemini-3-flash,
# llama-4-maverick and mistral-large-3, none of which resolve. Do not re-add them.
#
# DOWNGRADED_CONTROLS exist to prove the harness can observe a downgrade at all: conol emits
# modelDowngraded/effectiveModel ONLY when it downgrades, so without a control a clean verdict
# is indistinguishable from a probe that never got a usable response.
CANDIDATE_MODELS = [
    "gpt-5.6-luna", "claude-haiku-4-5", "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash", "z-ai/glm-5.2", "z-ai/glm-5.1",
    "moonshotai/kimi-k2.7-code", "qwen/qwen3.7-plus", "qwen/qwen3.7-max",
    "google/gemini-3.1-flash-lite", "x-ai/grok-4.3", "minimax/minimax-m3",
    "tencent/hy3", "stepfun/step-3.7-flash", "xiaomi/mimo-v2.5-pro",
]
DOWNGRADED_CONTROLS = ["gpt-5.5", "claude-opus-4-8"]


def _ev(preview_entries, logs=None):
    return {"type": "history_delta",
            "stages": [{"id": "s1", "preview": preview_entries, "logs": logs or []}]}


def selfcheck() -> int:
    """Offline checks of the parsing rules that decide whether an answer is recognised.
    No network: _apply_event is pure, so the traps are testable without burning credits."""
    from conol_infer import _apply_event, _entry_text, cookie_for as cf

    fails = []

    def check(name, cond, extra=""):
        print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else " — " + str(extra)))
        if not cond:
            fails.append(name)

    def snap(role, typ, text):
        return {"role": role, "type": typ,
                "content": [{"type": "text", "text": text}], "timestamp": "t"}

    # Trap 3: preview entries are cumulative snapshots. Three growing snapshots of the same
    # answer must yield the answer ONCE, not the concatenation.
    out = {"answer": "", "thinking": ""}
    for t in ("The user", "The user is asking", "The user is asking me to reply. PROBE_OK"):
        _apply_event(out, _ev([snap("assistant", "thinking", t)]))
    check("cumulative thinking snapshots are not concatenated",
          out["thinking"] == "The user is asking me to reply. PROBE_OK", out["thinking"][:60])
    check("thinking does not leak into the answer", out["answer"] == "", out["answer"][:60])

    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([snap("assistant", "thinking", "reasoning here")]))
    _apply_event(out, _ev([snap("assistant", "message", "PROBE_OK")]))
    check("message snapshot becomes the answer", out["answer"] == "PROBE_OK", out["answer"])
    check("thinking stays in its own bucket", out["thinking"] == "reasoning here", out["thinking"])

    # Trap 2: the user turn carries conol's own <system-reminder> preamble and must never be
    # returned as the answer — that misread is what made every model look like it "echoed".
    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([], logs=[snap("user", "text",
                                         "<system-reminder>\nYou are talking to <user>X</user>."
                                         "</system-reminder>\n\nReply with exactly: PROBE_OK")]))
    check("user-turn preamble is not the answer", out["answer"] == "", out["answer"][:60])
    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([snap("assistant", "message",
                                "<system-reminder>note</system-reminder> real answer")]))
    check("system-reminder inside an ASSISTANT turn still counts",
          "real answer" in out["answer"], out["answer"][:60])

    # Type drift: an unseen non-reasoning type must still deliver the answer.
    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([snap("assistant", "text", "answer via unknown type")]))
    check("unknown entry type is treated as the answer, not dropped",
          out["answer"] == "answer via unknown type", out["answer"])
    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([snap("assistant", "REASONING", "upper-case reasoning")]))
    check("reasoning type match is case-insensitive",
          out["thinking"] == "upper-case reasoning" and out["answer"] == "", out)

    # Longest snapshot wins even if a later event carries a shorter (stale) one.
    out = {"answer": "", "thinking": ""}
    _apply_event(out, _ev([snap("assistant", "message", "the complete answer")]))
    _apply_event(out, _ev([snap("assistant", "message", "short")]))
    check("longest snapshot wins over a later shorter one",
          out["answer"] == "the complete answer", out["answer"])

    # Malformed events must not raise: the gateway calls this per event on live traffic.
    for bad in (None, {}, {"stages": None}, {"stages": [None]}, {"stages": [{"preview": [None]}]},
                _ev([{"role": "assistant", "content": 42}]), "not-a-dict"):
        try:
            _apply_event({"answer": "", "thinking": ""}, bad)
        except Exception as e:
            check("malformed event %r does not raise" % (bad,), False, "%s: %s" % (type(e).__name__, e))
            break
    else:
        check("malformed events never raise", True)

    check("_entry_text handles a plain string content",
          _entry_text({"content": "abc"}) == "abc")
    check("_entry_text handles a list of parts",
          _entry_text({"content": [{"text": "a"}, {"text": "b"}, "c"]}) == "abc")

    # Credential resolution: the pool token wins over a stale cookie file (measured 2026-10-03:
    # in all 3 disagreements the pool row was newer because conol_refresh.py rewrites only it).
    row = {"session_token": "POOLTOKEN", "cookies_path": str(BASE_DIR / "definitely-missing.json")}
    check("pool session_token wins over the cookie file", cf(row) == "POOLTOKEN", cf(row))
    check("row with no credential yields None", cf({"name": "x"}) is None)

    rows = load_pool(POOL_FILE)
    check("pool loads", len(rows) > 0, str(len(rows)))
    check("at least one live account is usable",
          len(live_accounts(POOL_FILE, 1)) == 1 if rows else False)
    check("BASE_URL is conol", BASE_URL.endswith("conol.ai"), BASE_URL)

    print()
    print("FAILURES: " + str(fails) if fails else "all blocks passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="probe conol.ai inference for pool accounts")
    ap.add_argument("--models", nargs="*", default=None, help="model ids to test")
    ap.add_argument("--sweep", action="store_true", help="test every candidate model")
    ap.add_argument("--accounts", type=int, default=2,
                    help="spread the model list over this many live accounts")
    ap.add_argument("--prompt", default="Reply with exactly: PROBE_OK")
    ap.add_argument("--budget", type=int, default=120, help="per-model stream budget, seconds")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="", help="write the raw results to this JSON path")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()

    # Default is a proven-honest id: gpt-5.5 downgrades to claude-haiku-4-5, and downgraded
    # models ignore the XML tool protocol and buffer their text, so probing one proves nothing.
    models = (CANDIDATE_MODELS + DOWNGRADED_CONTROLS) if args.sweep \
        else (args.models or ["gpt-5.6-luna"])
    accounts = live_accounts(POOL_FILE, max(args.accounts, 1))
    if not accounts:
        print("no live accounts with a usable credential in %s" % POOL_FILE, file=sys.stderr)
        return 2
    print("probing %d model(s) across %d account(s), budget %ds each\n"
          % (len(models), len(accounts), args.budget))
    print("%-28s %-9s %7s %6s %-22s %s"
          % ("MODEL", "VERDICT", "SEC", "CHARS", "EFFECTIVE", "DETAIL"))

    results = []
    for i, model in enumerate(models):
        acc = accounts[i % len(accounts)]
        cookie = cookie_for(acc)
        if not cookie:
            print("%-28s SKIP      -      - %-22s no credential" % (model, "-"))
            results.append({"model": model, "verdict": "SKIP", "detail": "no credential"})
            continue
        ok, who = check_session(cookie)
        if not ok:
            print("%-28s SKIP      -      - %-22s account not live (%s)" % (model, "-", who))
            results.append({"model": model, "verdict": "SKIP", "detail": who,
                            "account": acc.get("name")})
            continue
        res = run_inference(cookie, model, args.prompt, budget=args.budget)
        res["account"] = acc.get("name")
        results.append(res)
        print("%-28s %-9s %7.1f %6d %-22s %s"
              % (model, res["verdict"], res["seconds"], res["chars"],
                 str(res.get("effective_model"))[:22], res["detail"][:88]))

    answered = [r["model"] for r in results if r["verdict"] == "ANSWERED"]
    empt = [r["model"] for r in results if r["verdict"] == "EMPTY"]
    errs = [r["model"] for r in results if r["verdict"] == "ERROR"]
    lat = [r["seconds"] for r in results if r["verdict"] == "ANSWERED"]
    print("\nANSWERED %d/%d: %s" % (len(answered), len(results), answered))
    if empt:
        print("EMPTY   %d: %s" % (len(empt), empt))
    if errs:
        print("ERROR   %d: %s" % (len(errs), errs))
    if lat:
        print("latency answered: min %.1fs  median %.1fs  max %.1fs"
              % (min(lat), sorted(lat)[len(lat) // 2], max(lat)))
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print("wrote %s" % args.out)
    if args.json:
        print(json.dumps(results, ensure_ascii=False)[:1800])
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
