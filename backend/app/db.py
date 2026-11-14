"""SQLite storage. One file, zero ops — right for a single-investor prototype.

Market data (prices, filings, fundamentals) and the investor's network are kept in
separate stores on purpose: market data is public and shareable across users, the
network is private to one investor (see network/store.py)."""
import json
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT, date TEXT, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS filings (
    id TEXT PRIMARY KEY,
    symbol TEXT, ts TEXT, subject TEXT, exchange_desc TEXT, text TEXT, url TEXT,
    category TEXT, cls_method TEXT, summary TEXT,
    extra TEXT,               -- JSON: extracted profile, results numbers, etc.
    demo INTEGER DEFAULT 0    -- 1 = synthetic showcase scenario, always labelled in the UI
);
CREATE INDEX IF NOT EXISTS ix_filings_ts ON filings(ts);
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT PRIMARY KEY, fetched_at TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY, model TEXT, response TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
"""


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def kv_get(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
