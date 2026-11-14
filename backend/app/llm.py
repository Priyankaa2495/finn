"""LLM router with a content-addressed cache.

Three rules that keep cost near zero:
  1. Don't call a model if a rule can do it (classification is ~85% regex).
  2. Route by difficulty: 'fast' tier for bulk extraction, 'deep' tier only for
     judgement (promised-vs-delivered, bull/bear).
  3. Never pay twice: every call is cached by sha256(model + prompt). Filings and
     transcripts are immutable, so the cache hit-rate on re-runs is ~100%.

Every caller must handle `None` (no key configured / provider down) and fall back to
a deterministic path. The LLM is an upgrade, never a dependency."""
import hashlib
import json
import logging
import re
from datetime import datetime

import httpx

from . import config, db

log = logging.getLogger("finn.llm")
STATS = {"calls": 0, "cache_hits": 0, "failures": 0, "in_tokens_est": 0}


def _key(model, system, prompt):
    return hashlib.sha256(f"{model}\x00{system}\x00{prompt}".encode()).hexdigest()


def _gemini(model, system, prompt, want_json):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if want_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = httpx.post(url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _openai_compat(model, system, prompt, want_json):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "temperature": 0.1}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions", json=body, timeout=90,
                   headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def complete(prompt: str, tier="fast", system="", want_json=True):
    """Returns parsed JSON (want_json) or text; None if no model is available."""
    if not config.LLM_ENABLED:
        return None
    model = config.LLM_FAST_MODEL if tier == "fast" else config.LLM_DEEP_MODEL
    k = _key(model, system, prompt)
    with db.conn() as c:
        row = c.execute("SELECT response FROM llm_cache WHERE key=?", (k,)).fetchone()
    if row:
        STATS["cache_hits"] += 1
        return _parse(row["response"], want_json)
    STATS["calls"] += 1
    STATS["in_tokens_est"] += (len(prompt) + len(system)) // 4
    try:
        fn = _openai_compat if config.OPENAI_BASE_URL else _gemini
        out = fn(model, system, prompt, want_json)
    except Exception as e:  # network, quota, safety block...
        STATS["failures"] += 1
        log.warning("LLM call failed (%s): %s", model, e)
        return None
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?)",
                  (k, model, out, datetime.utcnow().isoformat()))
    return _parse(out, want_json)


def _parse(text, want_json):
    if not want_json:
        return text
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", text or "", re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None
