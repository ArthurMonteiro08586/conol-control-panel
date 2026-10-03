#!/usr/bin/env python3
"""Apply production upgrade to gateway.py"""
from pathlib import Path

p = Path("C:/Users/User/Desktop/conol_autoreg/gateway.py")
content = p.read_text(encoding="utf-8")

# 1. Replace single API_KEY with multi-key support
old1 = 'API_KEY = os.environ.get("ENI_POOL_KEY", "test")'
new1 = '''# Multi-key support: comma-separated keys in ENI_POOL_KEY
_raw_keys = os.environ.get("ENI_POOL_KEY", "test")
API_KEYS: set[str] = {k.strip() for k in _raw_keys.split(",") if k.strip()}
API_KEY = next(iter(API_KEYS), "test")

# Rate limiting: per-key sliding window
_rate_limits: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = int(os.environ.get("ENI_POOL_RPM", "60"))

# Usage tracking
_usage: dict[str, dict] = {}
_concurrent = asyncio.Semaphore(int(os.environ.get("ENI_POOL_CONCURRENCY", "20")))
_request_log: deque = deque(maxlen=1000)'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    print("1. multi-key + rate limit + usage tracking")
else:
    print("1. SKIP")

# 2. Replace verify_key
old2 = '''async def verify_key(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if auth[7:] != API_KEY:
        raise HTTPException(401, "Invalid API key")'''

new2 = '''async def verify_key(request: Request) -> str:
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
    return key'''

if old2 in content:
    content = content.replace(old2, new2, 1)
    print("2. verify_key upgraded")
else:
    print("2. SKIP")

# 3. Update chat endpoint
old3 = '''@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    await verify_key(request)
    body = await request.json()'''

new3 = '''@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    api_key_used = await verify_key(request)
    body = await request.json()'''

if old3 in content:
    content = content.replace(old3, new3, 1)
    print("3. chat endpoint tracks key")
else:
    print("3. SKIP")

# 4. Add concurrent limit to non-streaming
old4 = '''    # Non-streaming
    try:
        selected_tools = _filter_tools_for_choice(tools, tool_choice)'''

new4 = '''    # Non-streaming
    try:
        async with _concurrent:
            selected_tools = _filter_tools_for_choice(tools, tool_choice)'''

if old4 in content:
    content = content.replace(old4, new4, 1)
    print("4. concurrent limit")
else:
    print("4. SKIP")

# 5. Track model usage
old5 = '''    if not messages:
        raise HTTPException(400, "messages is required")'''

new5 = '''    if not messages:
        raise HTTPException(400, "messages is required")

    _m = _usage.setdefault(api_key_used, {}).setdefault("models", {})
    _m[model] = _m.get(model, 0) + 1
    _request_log.append({"ts": time.time(), "key": api_key_used[:8], "model": model, "stream": stream})'''

if old5 in content:
    content = content.replace(old5, new5, 1)
    print("5. model usage tracking")
else:
    print("5. SKIP")

# 6. Add /v1/usage and /v1/keys endpoints before pool/stats
old6 = '''@app.get("/pool/stats")
async def pool_stats(request: Request):
    await verify_key(request)
    return JSONResponse(pool.stats())'''

new6 = '''@app.get("/v1/usage")
async def usage_stats(request: Request):
    """Usage statistics per API key."""
    await verify_key(request)
    return JSONResponse({
        "rate_limit": f"{RATE_LIMIT_MAX} req/{RATE_LIMIT_WINDOW}s",
        "concurrency": int(os.environ.get("ENI_POOL_CONCURRENCY", "20")),
        "keys": {
            k[:8] + "...": {
                "requests": v.get("requests", 0),
                "models": v.get("models", {}),
                "first_seen": v.get("first_seen", 0),
                "last_request": v.get("last_request", 0),
            }
            for k, v in _usage.items()
        },
        "recent_requests": list(_request_log)[-20:],
    })


@app.get("/v1/keys")
async def list_keys(request: Request):
    """List active API keys (masked)."""
    await verify_key(request)
    return JSONResponse({
        "total_keys": len(API_KEYS),
        "keys": [k[:8] + "..." for k in API_KEYS],
        "rate_limit_rpm": RATE_LIMIT_MAX,
    })


@app.get("/pool/stats")
async def pool_stats(request: Request):
    await verify_key(request)
    return JSONResponse(pool.stats())'''

if old6 in content:
    content = content.replace(old6, new6, 1)
    print("6. /v1/usage and /v1/keys endpoints")
else:
    print("6. SKIP")

# 7. Update version
content = content.replace('version="6.1"', 'version="7.0"', 1)
content = content.replace("v6.1", "v7.0", 1)
print("7. version -> 7.0")

p.write_text(content, encoding="utf-8")
print("Done")
