"""End-of-day threshold alerts. The investor defines "unusual"; code checks it.

- price: |close / previous close - 1| >= price_move_pct
- volume: today's volume >= volume_multiple x mean of the previous `lookback` sessions
          (today excluded from its own baseline). Needs `min_history` prior sessions.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class AlertConfig:
    price_move_pct: float = 5.0
    volume_multiple: float = 2.0
    lookback: int = 20
    min_history: int = 10

    def __post_init__(self):
        if self.price_move_pct <= 0 or self.volume_multiple <= 0:
            raise ValueError("thresholds must be positive")
        if not 1 <= self.min_history <= self.lookback:
            raise ValueError("need 1 <= min_history <= lookback")


@dataclass(frozen=True)
class Alert:
    symbol: str
    date: str
    kind: str          # price | volume
    value: float       # % move, or volume multiple
    threshold: float
    message: str


def detect(symbol: str, bars: Sequence[Mapping], cfg: AlertConfig = AlertConfig()) -> list[Alert]:
    """bars: ascending by date, each with 'date', 'close', 'volume'. Checks the latest bar."""
    if len(bars) < 2:
        return []
    today, prev = bars[-1], bars[-2]
    alerts: list[Alert] = []

    if prev["close"]:
        pct = (today["close"] / prev["close"] - 1.0) * 100.0
        if abs(pct) >= cfg.price_move_pct:
            alerts.append(Alert(symbol, today["date"], "price", pct, cfg.price_move_pct,
                                f"{symbol} {pct:+.1f}% on {today['date']} (your threshold ±{cfg.price_move_pct:g}%)"))

    history = [b["volume"] for b in bars[-1 - cfg.lookback:-1] if b.get("volume") is not None]
    vol = today.get("volume")
    if vol is not None and len(history) >= cfg.min_history:
        avg = sum(history) / len(history)
        if avg > 0:
            mult = vol / avg
            if mult >= cfg.volume_multiple:
                alerts.append(Alert(symbol, today["date"], "volume", mult, cfg.volume_multiple,
                                    f"{symbol} volume {mult:.1f}× its {len(history)}-day average on "
                                    f"{today['date']} (your threshold {cfg.volume_multiple:g}×)"))
    return alerts


def scan(conn: sqlite3.Connection, symbols: Iterable[str], cfg: AlertConfig = AlertConfig()) -> list[Alert]:
    out: list[Alert] = []
    for sym in symbols:
        rows = conn.execute(
            "SELECT date, close, volume FROM prices WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (sym, cfg.lookback + 1),
        ).fetchall()
        out.extend(detect(sym, [dict(r) for r in reversed(rows)], cfg))
    return out
