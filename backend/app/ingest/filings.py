"""NSE corporate announcements.

NSE's JSON API rejects cookieless clients, so the session first loads the home
page and the announcements page to pick up cookies ("warm-up"), then calls the
API with a fixed delay between requests. On 401/403 it re-warms once and retries.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from .. import config, db
from ..intel.classify import classify

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
NSE_HOME = "https://www.nseindia.com"
NSE_WARMUP = f"{NSE_HOME}/companies-listing/corporate-filings-announcements"
NSE_API = f"{NSE_HOME}/api/corporate-announcements"
NSE_HEADERS = {
    # NSE blocks non-browser user agents. Generic UA; no user-identifying data.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_WARMUP,
}

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d%m%Y%H%M%S", "%d-%b-%Y")


def parse_nse_time(raw: str | None) -> str | None:
    """Return ISO-8601 with IST offset, or None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def filing_id(item: dict, symbol: str, filed_at: str, subject: str, attachment: str | None) -> str:
    if item.get("seq_id"):
        return f"NSE:{item['seq_id']}"
    digest = hashlib.sha256(f"{symbol}|{filed_at}|{subject}|{attachment or ''}".encode()).hexdigest()
    return f"NSEH:{digest[:20]}"


def normalise_nse_item(item: dict) -> dict | None:
    """Map one raw NSE announcement to our `filings` row shape (unclassified)."""
    symbol = (item.get("symbol") or "").strip().upper()
    subject = " ".join((item.get("desc") or "").split())
    filed_at = parse_nse_time(item.get("sort_date") or item.get("an_dt") or item.get("dt"))
    if not (symbol and subject and filed_at):
        return None
    attachment = (item.get("attchmntFile") or "").strip() or None
    return {
        "id": filing_id(item, symbol, filed_at, subject, attachment),
        "symbol": symbol,
        "exchange": "NSE",
        "filed_at": filed_at,
        "subject": subject,
        "body": " ".join((item.get("attchmntText") or "").split()),
        "attachment_url": attachment,
    }


class NSEClient:
    def __init__(self, session=None, delay_s: float = config.NSE_DELAY_S,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.session.headers.update(NSE_HEADERS)
        self.delay_s = delay_s
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None
        self._warmed = False

    def _polite(self) -> None:
        if self._last is not None:
            wait = self.delay_s - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def warm_up(self) -> None:
        for url in (NSE_HOME, NSE_WARMUP):
            self._polite()
            self.session.get(url, timeout=20)
        self._warmed = True

    def announcements(self, symbol: str, from_date: date, to_date: date) -> list[dict]:
        if not self._warmed:
            self.warm_up()
        params = {"index": "equities", "symbol": symbol.upper(),
                  "from_date": from_date.strftime("%d-%m-%Y"),
                  "to_date": to_date.strftime("%d-%m-%Y")}
        for attempt in (1, 2):
            self._polite()
            resp = self.session.get(NSE_API, params=params, timeout=20)
            if resp.status_code in (401, 403) and attempt == 1:
                self.warm_up()  # cookies expired
                continue
            resp.raise_for_status()
            break
        payload = resp.json()
        items = payload if isinstance(payload, list) else payload.get("data", [])
        return [row for row in map(normalise_nse_item, items) if row]


def refresh(conn: sqlite3.Connection, symbols: Iterable[str] | None = None, days: int = 7,
            client: NSEClient | None = None, force: bool = False,
            today: date | None = None) -> int:
    """Fetch, classify (rules only, zero model calls) and store announcements."""
    todo = [s for s in (symbols or config.PORTFOLIO_SYMBOLS)
            if force or not db.is_fresh(conn, "filings", s)]
    if not todo:
        return 0
    client = client or NSEClient()
    end = today or datetime.now(IST).date()
    start = end - timedelta(days=days)
    written = 0
    for sym in todo:
        try:
            rows = client.announcements(sym, start, end)
        except Exception as exc:  # one bad symbol must not kill the batch
            log.warning("filings: %s failed: %s", sym, exc)
            continue
        for row in rows:
            c = classify(row["subject"], row["body"])
            row.update(category=c.category, category_source=c.source, category_rule=c.rule)
        written += db.upsert_filings(conn, rows)
        db.mark_fetched(conn, "filings", sym)
    return written
