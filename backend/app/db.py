"""SQLite storage: schema, upserts, the content-addressed LLM cache and live-fetch TTL log.

Timestamps:
- `fetched_at` / `created_at` are UTC ISO-8601.
- `filings.filed_at` is ISO-8601 in IST (+05:30) for every row, so string
  comparison is a valid time-window filter.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,              -- YYYY-MM-DD (exchange trading day)
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL NOT NULL,
    volume      INTEGER,
    source      TEXT NOT NULL DEFAULT 'yfinance',
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS filings (
    id              TEXT PRIMARY KEY,       -- 'NSE:<seq_id>' or content hash
    symbol          TEXT NOT NULL,
    exchange        TEXT NOT NULL DEFAULT 'NSE',
    filed_at        TEXT NOT NULL,          -- ISO-8601, IST offset
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    attachment_url  TEXT,
    category        TEXT,
    category_source TEXT,                   -- subject | subject+body | body | model | none
    category_rule   TEXT,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_filings_time        ON filings(filed_at);
CREATE INDEX IF NOT EXISTS ix_filings_symbol_time ON filings(symbol, filed_at);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol       TEXT NOT NULL,
    period       TEXT NOT NULL,             -- e.g. 'Sep 2026' or 'FY2026'
    period_type  TEXT NOT NULL CHECK (period_type IN ('Q', 'FY')),
    metric       TEXT NOT NULL,             -- revenue | ebitda | pbt | pat | ...
    value        REAL,
    unit         TEXT NOT NULL DEFAULT 'INR_cr',
    source_url   TEXT,
    source_page  INTEGER,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, period, period_type, metric)
);

-- Content-addressed: key = sha256(model || 0x1F || prompt). The prompt itself is
-- NOT stored, only the hash, so the cache never becomes a second copy of inputs.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    created_at  TEXT NOT NULL
);

-- Eagerly parsed attachments (results cards, management bios), keyed by filing.
CREATE TABLE IF NOT EXISTS enrichment (
    filing_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- results_card | bio
    payload     TEXT NOT NULL,              -- JSON
    created_at  TEXT NOT NULL,
    PRIMARY KEY (filing_id, kind)
);

-- Operational counters surfaced by /api/meta (e.g. deep_fallbacks, llm_calls, llm_failures).
CREATE TABLE IF NOT EXISTS counters (
    name        TEXT PRIMARY KEY,
    value       INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- Freshness log for live on-demand fetches (Option C, 20h TTL).
CREATE TABLE IF NOT EXISTS fetch_log (
    source      TEXT NOT NULL,              -- prices | filings | fundamentals
    key         TEXT NOT NULL,              -- usually the symbol
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


# One process, one connection, many threads (FastAPI threadpool + asyncio.to_thread for
# research). sqlite3 objects are not safe for concurrent use, so every caller that may run
# off the event-loop thread takes this lock. Single uvicorn worker by decision.
LOCK = threading.RLock()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = str(path or config.DB_PATH)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ----------------------------------------------------------------------- upserts
def upsert_prices(conn: sqlite3.Connection, rows: Iterable[Mapping]) -> int:
    now = utcnow_iso()
    data = [
        (r["symbol"], r["date"], r.get("open"), r.get("high"), r.get("low"),
         r["close"], r.get("volume"), r.get("source", "yfinance"), now)
        for r in rows
    ]
    conn.executemany(
        """INSERT INTO prices (symbol, date, open, high, low, close, volume, source, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume,
             source=excluded.source, fetched_at=excluded.fetched_at""",
        data,
    )
    conn.commit()
    return len(data)


def upsert_filings(conn: sqlite3.Connection, rows: Iterable[Mapping]) -> int:
    """Filings are immutable; only the classification columns may be refreshed."""
    now = utcnow_iso()
    data = [
        (r["id"], r["symbol"], r.get("exchange", "NSE"), r["filed_at"], r["subject"],
         r.get("body", ""), r.get("attachment_url"), r.get("category"),
         r.get("category_source"), r.get("category_rule"), now)
        for r in rows
    ]
    conn.executemany(
        """INSERT INTO filings (id, symbol, exchange, filed_at, subject, body, attachment_url,
                                category, category_source, category_rule, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             category=excluded.category,
             category_source=excluded.category_source,
             category_rule=excluded.category_rule""",
        data,
    )
    conn.commit()
    return len(data)


# --------------------------------------------------------------------- LLM cache
def cache_key(model: str, prompt: str) -> str:
    # 0x1F separator so ("ab", "c") and ("a", "bc") never collide.
    return hashlib.sha256(f"{model}\x1f{prompt}".encode("utf-8")).hexdigest()


def cache_get(conn: sqlite3.Connection, model: str, prompt: str) -> str | None:
    row = conn.execute(
        "SELECT response FROM llm_cache WHERE key = ?", (cache_key(model, prompt),)
    ).fetchone()
    return row["response"] if row else None


def cache_put(conn: sqlite3.Connection, model: str, prompt: str, response: str,
              tokens_in: int | None = None, tokens_out: int | None = None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO llm_cache (key, model, response, tokens_in, tokens_out, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cache_key(model, prompt), model, response, tokens_in, tokens_out, utcnow_iso()),
    )
    conn.commit()


# -------------------------------------------------------------------- enrichment
def put_enrichment(conn: sqlite3.Connection, filing_id: str, kind: str, payload: dict) -> None:
    conn.execute(
        """INSERT INTO enrichment (filing_id, kind, payload, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(filing_id, kind) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at""",
        (filing_id, kind, json.dumps(payload), utcnow_iso()),
    )
    conn.commit()


def get_enrichment(conn: sqlite3.Connection, filing_ids: list[str]) -> dict[str, dict[str, dict]]:
    if not filing_ids:
        return {}
    marks = ",".join("?" * len(filing_ids))
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute(f"SELECT filing_id, kind, payload FROM enrichment WHERE filing_id IN ({marks})",
                          filing_ids):
        out.setdefault(r["filing_id"], {})[r["kind"]] = json.loads(r["payload"])
    return out


# ---------------------------------------------------------------------- counters
def incr(conn: sqlite3.Connection, name: str, by: int = 1) -> None:
    conn.execute(
        """INSERT INTO counters (name, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET value = value + excluded.value, updated_at = excluded.updated_at""",
        (name, by, utcnow_iso()),
    )
    conn.commit()


def counters(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM counters")}


# ------------------------------------------------------------- live-fetch TTL log
def is_fresh(conn: sqlite3.Connection, source: str, key: str,
             ttl_hours: float | None = None, now: datetime | None = None) -> bool:
    ttl = config.LIVE_TTL_HOURS if ttl_hours is None else ttl_hours
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE source = ? AND key = ?", (source, key)
    ).fetchone()
    if not row:
        return False
    fetched = datetime.fromisoformat(row["fetched_at"])
    return (now or utcnow()) - fetched < timedelta(hours=ttl)


def mark_fetched(conn: sqlite3.Connection, source: str, key: str,
                 when: datetime | None = None) -> None:
    ts = (when or utcnow()).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO fetch_log (source, key, fetched_at) VALUES (?, ?, ?)
           ON CONFLICT(source, key) DO UPDATE SET fetched_at = excluded.fetched_at""",
        (source, key, ts),
    )
    conn.commit()
