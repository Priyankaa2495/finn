"""Optional LLM client: two tiers, any OpenAI-compatible endpoint, content-addressed cache.

`LLMClient(tier, system, prompt) -> str | None`. Returns None when no API key is set or every
attempt fails. Every caller MUST have a deterministic path for None (zero-key operation).

Deep tier resilience: the deep model gets a hard 25 s read timeout (FINN_LLM_DEEP_TIMEOUT_S).
On timeout/error/empty reply the same prompt is retried once on the fast tier, and the event
is counted (`deep_fallbacks`, persisted in the `counters` table and exposed via meta()).
Callers' validators are tier-agnostic, so a fast-tier answer is held to the same rules.

Never pass the investor's network data to this module (Hard Rule 2).
"""
from __future__ import annotations

import logging
import sqlite3
import time

from . import config, db

log = logging.getLogger(__name__)

FAST, DEEP = "fast", "deep"
CONNECT_TIMEOUT_S = 5.0
# Rate-limit hardening: up to 2 retries with exponential backoff (1 s, 2 s; Retry-After honoured,
# capped) on 429 / 5xx / connection errors. Read timeouts are NOT retried: a hung deep call
# should fall back to the fast tier after 25 s, not hold the user for 75 s.
MAX_RETRIES = 2
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 8.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class LLMClient:
    def __init__(self, cfg: config.LLMConfig | None = None, conn: sqlite3.Connection | None = None,
                 session=None, sleep=time.sleep):
        self.cfg = cfg or config.llm_config()
        self.conn = conn
        self._session = session
        self._sleep = sleep
        self.retries = 0         # backoff retries taken (429 / 5xx / connection)
        self.calls = 0           # paid network calls that succeeded
        self.cache_hits = 0      # calls avoided
        self.failures = 0        # attempts that errored or timed out
        self.deep_fallbacks = 0  # deep attempts that were answered by the fast tier (or not at all)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _count(self, name: str) -> None:
        if self.conn is not None:
            with db.LOCK:
                db.incr(self.conn, name)

    def _attempt(self, model: str, system: str, prompt: str, read_timeout: float) -> str | None:
        cache_text = f"{system}\n\n{prompt}"
        if self.conn is not None:
            with db.LOCK:
                hit = db.cache_get(self.conn, model, cache_text)
            if hit is not None:
                self.cache_hits += 1
                return hit
        import requests
        if self._session is None:
            self._session = requests.Session()
        text, usage = None, {}
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.post(
                    self.cfg.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                    json={"model": model, "temperature": 0,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": prompt}]},
                    timeout=(CONNECT_TIMEOUT_S, read_timeout),
                )
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    self._backoff(attempt, model, f"HTTP {resp.status_code}",
                                  getattr(resp, "headers", {}).get("retry-after"))
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                if not text or not text.strip():
                    raise ValueError("empty completion")
                usage = data.get("usage") or {}
                break
            except requests.exceptions.Timeout as exc:          # incl. ConnectTimeout: no retry
                return self._fail(model, exc)
            except requests.exceptions.ConnectionError as exc:
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, model, type(exc).__name__, None)
                    continue
                return self._fail(model, exc)
            except Exception as exc:  # model is an upgrade, never a dependency
                return self._fail(model, exc)
        self.calls += 1
        self._count("llm_calls")
        if self.conn is not None:
            with db.LOCK:
                db.cache_put(self.conn, model, cache_text, text,
                             usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return text

    def _backoff(self, attempt: int, model: str, why: str, retry_after) -> None:
        delay = BACKOFF_BASE_S * (2 ** attempt)
        try:
            if retry_after is not None:
                delay = max(delay, float(retry_after))
        except (TypeError, ValueError):
            pass                                  # HTTP-date form: keep the exponential delay
        delay = min(delay, BACKOFF_CAP_S)
        self.retries += 1
        self._count("llm_retries")
        log.info("llm %s %s; retry %d in %.1fs", model, why, attempt + 1, delay)
        self._sleep(delay)

    def _fail(self, model: str, exc: Exception) -> None:
        log.warning("llm %s failed: %s: %s", model, type(exc).__name__, exc)
        self.failures += 1
        self._count("llm_failures")
        return None

    def __call__(self, tier: str, system: str, prompt: str) -> str | None:
        if not self.enabled:
            return None
        if tier != DEEP:
            return self._attempt(self.cfg.fast_model, system, prompt, self.cfg.timeout_s)
        out = self._attempt(self.cfg.deep_model, system, prompt, self.cfg.deep_timeout_s)
        if out is not None:
            return out
        self.deep_fallbacks += 1
        self._count("deep_fallbacks")
        log.warning("deep tier unavailable; falling back to %s", self.cfg.fast_model)
        return self._attempt(self.cfg.fast_model, system, prompt, self.cfg.timeout_s)

    def meta(self) -> dict:
        """For /api/meta: config + this process's counters + persisted totals."""
        return {"enabled": self.enabled, "fast_model": self.cfg.fast_model, "deep_model": self.cfg.deep_model,
                "deep_timeout_s": self.cfg.deep_timeout_s,
                "session": {"calls": self.calls, "cache_hits": self.cache_hits, "retries": self.retries,
                            "failures": self.failures, "deep_fallbacks": self.deep_fallbacks},
                "totals": self._totals()}

    def _totals(self) -> dict:
        if self.conn is None:
            return {}
        with db.LOCK:
            return db.counters(self.conn)
