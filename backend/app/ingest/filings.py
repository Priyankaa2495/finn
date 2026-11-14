"""Corporate announcements from NSE's public JSON endpoint.

NSE needs browser-like headers and a cookie from the homepage first; it also blocks
many cloud IP ranges. So ingestion runs from a scheduled job (or a laptop) and writes
to the DB; the web app only ever reads the DB. Swapping in BSE's API or a paid feed
only changes this file."""
import hashlib
import logging
import time
from datetime import datetime, timedelta

import httpx

from .. import db
from ..config import HTTP_UA

log = logging.getLogger("finn.filings")
NSE = "https://www.nseindia.com"


def _client():
    c = httpx.Client(headers={
        "User-Agent": HTTP_UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{NSE}/companies-listing/corporate-filings-announcements",
    }, timeout=20, follow_redirects=True)
    c.get(NSE)  # sets the nsit / nseappid cookies
    return c


def _parse_ts(item):
    for k, fmt in (("sort_date", "%Y-%m-%d %H:%M:%S"), ("an_dt", "%d-%b-%Y %H:%M:%S")):
        try:
            return datetime.strptime(item[k], fmt).isoformat()
        except Exception:
            pass
    return datetime.utcnow().isoformat()


def fetch(symbols, days=7):
    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)
    c = _client()
    rows = []
    for s in symbols:
        try:
            r = c.get(f"{NSE}/api/corporate-announcements", params={
                "index": "equities", "symbol": s,
                "from_date": from_d.strftime("%d-%m-%Y"), "to_date": to_d.strftime("%d-%m-%Y")})
            r.raise_for_status()
            items = r.json()
            items = items.get("data", items) if isinstance(items, dict) else items
        except Exception as e:
            log.warning("NSE fetch failed for %s: %s", s, e)
            continue
        for it in items:
            fid = str(it.get("seq_id") or hashlib.md5(
                f"{s}{it.get('an_dt')}{it.get('attchmntFile')}".encode()).hexdigest())
            text = (it.get("attchmntText") or "").strip()
            rows.append((fid, s, _parse_ts(it), text[:160] or it.get("desc", ""),
                         it.get("desc", ""), text, it.get("attchmntFile", "")))
        time.sleep(0.4)  # be polite; NSE rate-limits aggressively
    with db.conn() as conn:
        # INSERT OR IGNORE keeps any classification/summary already computed
        conn.executemany("INSERT OR IGNORE INTO filings (id, symbol, ts, subject, exchange_desc, text, url)"
                         " VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def in_window(symbols, since_iso):
    q = ",".join("?" * len(symbols))
    with db.conn() as c:
        rows = c.execute(f"SELECT * FROM filings WHERE symbol IN ({q}) AND ts >= ? ORDER BY ts DESC",
                         (*symbols, since_iso)).fetchall()
    return [dict(r) for r in rows]
