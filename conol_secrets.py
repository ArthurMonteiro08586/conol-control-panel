"""
conol_secrets.py - single source of truth for credentials.

Secrets live in config.json (gitignored) or environment variables.
Env vars win over config.json. NEVER hardcode secrets in code files.

Env mapping: CONOL_GMAIL_ADDRESS, CONOL_GMAIL_APP_PASSWORD, CONOL_PASSWORD,
CONOL_SITE_KEY, CONOL_BASE_URL, CONOL_ANTICAPTCHA_KEYS (comma-separated).
"""
import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        p = _ROOT / "config.json"
        _CFG = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _CFG


def get(section: str, key: str, default=None):
    env = os.environ.get("CONOL_" + section.upper() + "_" + key.upper())
    if env:
        return env
    return _cfg().get(section, {}).get(key, default)


GMAIL = get("gmail", "address", "")
GMAIL_APP = get("gmail", "app_password", "")
PASSWORD = get("conol", "password", "")
SITE_KEY = get("conol", "site_key", "")
BASE_URL = get("conol", "base_url", "https://conol.ai")

_keys_env = os.environ.get("CONOL_ANTICAPTCHA_KEYS")
if _keys_env:
    ANTICAPTCHA_KEYS = [k.strip() for k in _keys_env.split(",") if k.strip()]
else:
    ANTICAPTCHA_KEYS = list(get("captcha", "anticaptcha_keys", []) or [])

ANTICAPTCHA_KEY = ANTICAPTCHA_KEYS[0] if ANTICAPTCHA_KEYS else ""
ANTICAPTCHA_BACKUP = ANTICAPTCHA_KEYS[1] if len(ANTICAPTCHA_KEYS) > 1 else ""
