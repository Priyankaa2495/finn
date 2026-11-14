"""End-of-day price & volume via yfinance (NSE tickers get the '.NS' suffix).

EOD is all the brief asks for, so this runs once a day after close (~16:00 IST)
from a scheduled job, not on user requests. Idempotent upserts by (symbol, date)."""
import logging

from .. import db

log = logging.getLogger("finn.prices")


def fetch(symbols, period="6mo"):
    import yfinance as yf

    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                     auto_adjust=False, progress=False, threads=True)
    rows = []
    for s, t in zip(symbols, tickers):
        try:
            sub = df[t] if len(tickers) > 1 else df
            sub = sub.dropna(subset=["Close"])
            for d, r in sub.iterrows():
                rows.append((s, d.strftime("%Y-%m-%d"), float(r["Close"]), float(r["Volume"])))
        except Exception as e:
            log.warning("no price data for %s: %s", s, e)
    with db.conn() as c:
        c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)", rows)
    return len(rows)


def series(symbol, n=60):
    with db.conn() as c:
        rows = c.execute("SELECT date, close, volume FROM prices WHERE symbol=? "
                         "ORDER BY date DESC LIMIT ?", (symbol, n)).fetchall()
    return [dict(r) for r in reversed(rows)]
