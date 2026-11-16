"""End-of-day price & volume via yfinance (NSE symbols get the '.NS' suffix).

yfinance is imported lazily so the rest of the app (and the test suite) runs
without it installed or without network access.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from typing import Iterable, Sequence

from .. import config, db

log = logging.getLogger(__name__)

_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}


def yahoo_symbol(symbol: str, exchange: str = "NSE") -> str:
    return f"{symbol.upper()}{_SUFFIX[exchange]}"


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


def rows_from_history(symbol: str, records: Iterable[Sequence], source: str = "yfinance") -> list[dict]:
    """records: (date_iso, open, high, low, close, volume). Rows without a close are dropped."""
    rows = []
    for date, o, h, l, c, v in records:
        close = _num(c)
        if close is None:
            continue
        vol = _num(v)
        rows.append({
            "symbol": symbol, "date": date, "open": _num(o), "high": _num(h),
            "low": _num(l), "close": close, "volume": int(vol) if vol is not None else None,
            "source": source,
        })
    return rows


def fetch_eod(symbols: Iterable[str], period: str = "3mo") -> list[dict]:
    """Fetch daily bars. 3 months covers the 20-day volume baseline with room to spare."""
    import yfinance as yf  # lazy: optional at runtime

    rows: list[dict] = []
    for sym in symbols:
        try:
            df = yf.Ticker(yahoo_symbol(sym)).history(period=period, interval="1d", auto_adjust=False)
        except Exception as exc:  # network / Yahoo hiccup must not kill the batch
            log.warning("prices: %s failed: %s", sym, exc)
            continue
        if df is None or df.empty:
            log.warning("prices: %s returned no data", sym)
            continue
        cols = df[["Open", "High", "Low", "Close", "Volume"]]
        records = ((idx.date().isoformat(), o, h, l, c, v) for idx, o, h, l, c, v in cols.itertuples())
        rows.extend(rows_from_history(sym, records))
    return rows


def refresh(conn: sqlite3.Connection, symbols: Iterable[str] | None = None,
            period: str = "3mo", force: bool = False) -> int:
    """Fetch symbols whose last fetch is older than the live TTL. Returns rows written."""
    todo = [s for s in (symbols or config.PORTFOLIO_SYMBOLS)
            if force or not db.is_fresh(conn, "prices", s)]
    if not todo:
        return 0
    rows = fetch_eod(todo, period=period)
    written = db.upsert_prices(conn, rows)
    for sym in {r["symbol"] for r in rows}:
        db.mark_fetched(conn, "prices", sym)
    return written
