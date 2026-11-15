"""Fundamentals from screener.in: standardised multi-year tables + links to the primary
documents (annual reports, concall transcripts) hosted on BSE / company sites.

Works for ANY listed symbol — nothing here is specific to the demo set. Parsed result
is cached in SQLite for a day; documents are immutable so their text is cached forever."""
import io
import json
import logging
import re
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from .. import db
from ..clock import utcnow
from ..config import HTTP_UA

log = logging.getLogger("finn.fundamentals")
BASE = "https://www.screener.in"


def _num(s):
    s = (s or "").replace(",", "").replace("%", "").replace("₹", "").replace("Cr.", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _table(section):
    """Screener's data tables: header row of periods, then label + values per row."""
    if section is None:
        return None
    t = section.select_one("table.data-table") or section.find("table")
    if t is None:
        return None
    periods = [th.get_text(strip=True) for th in t.select("thead th")][1:]
    rows = {}
    for tr in t.select("tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = re.sub(r"\s*\+\s*$", "", tds[0].get_text(" ", strip=True)).strip()
        rows[label] = [_num(td.get_text(strip=True)) for td in tds[1:]]
    return {"periods": periods, "rows": rows}


def parse(html: str, symbol: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {"symbol": symbol, "name": (soup.select_one("h1") or soup.new_tag("x")).get_text(strip=True)}
    about = soup.select_one(".company-profile .about") or soup.select_one("div.about")
    out["about"] = about.get_text(" ", strip=True) if about else ""
    out["ratios"] = {}
    for li in soup.select("#top-ratios li"):
        n, v = li.select_one(".name"), li.select_one(".value")
        if n and v:
            out["ratios"][n.get_text(strip=True)] = _num(" ".join(x.get_text() for x in v.select(".number")) or v.get_text())
    for key in ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"):
        out[key.replace("-", "_") if key != "ratios" else "ratio_table"] = _table(soup.select_one(f"section#{key}"))
    # sector / industry breadcrumb in the peers section
    out["industry"] = [a.get_text(strip=True) for a in soup.select("#peers a[href^='/market/']")]
    # primary documents
    docs = {"annual_reports": [], "concalls": []}
    for a in soup.select("#documents .annual-reports a, .documents .annual-reports a"):
        docs["annual_reports"].append({"title": a.get_text(" ", strip=True), "url": a.get("href")})
    for li in soup.select("#documents .concalls li, .documents .concalls li"):
        date_el = li.find(["div", "span"])
        entry = {"date": date_el.get_text(strip=True) if date_el else ""}
        for a in li.find_all("a"):
            kind = a.get_text(strip=True).lower()
            if kind.startswith("transcript"):
                entry["transcript"] = a.get("href")
            elif kind.startswith("ppt"):
                entry["ppt"] = a.get("href")
        if entry.get("transcript") or entry.get("ppt"):
            docs["concalls"].append(entry)
    out["documents"] = docs
    out["fetched_at"] = utcnow().isoformat()
    return out


def fetch(symbol: str, max_age_hours=20, force=False) -> dict | None:
    symbol = symbol.upper().strip()
    if not force:
        cached = get(symbol)
        if cached and datetime.fromisoformat(cached["fetched_at"]) > utcnow() - timedelta(hours=max_age_hours):
            return cached
    headers = {"User-Agent": HTTP_UA}
    data = None
    for path in (f"/company/{symbol}/consolidated/", f"/company/{symbol}/"):
        try:
            r = httpx.get(BASE + path, headers=headers, timeout=20, follow_redirects=True)
            if r.status_code != 200:
                continue
            d = parse(r.text, symbol)
            # some companies have no consolidated numbers; fall through to standalone
            if d.get("quarters") and any(v for vals in d["quarters"]["rows"].values() for v in vals if v):
                data = d
                data["basis"] = "consolidated" if "consolidated" in path else "standalone"
                break
        except Exception as e:
            log.warning("screener fetch failed %s%s: %s", symbol, path, e)
    if data:
        store(symbol, data)
        return data
    return get(symbol)  # stale is better than nothing


def store(symbol, data):
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?)",
                  (symbol, data.get("fetched_at", utcnow().isoformat()), json.dumps(data)))


def get(symbol):
    with db.conn() as c:
        row = c.execute("SELECT data FROM fundamentals WHERE symbol=?", (symbol.upper(),)).fetchone()
    return json.loads(row["data"]) if row else None


def document_text(url: str, max_pages=40) -> list[str] | None:
    """Download a PDF (transcript / AR / results) and return text per page.
    Cached in the kv table by URL — primary documents don't change once filed."""
    if not url:
        return None
    key = "doc:" + url
    cached = db.kv_get(key)
    if isinstance(cached, list):  # older cache shape: page list, total unknown
        cached = {"pages": cached, "total": None}
    if cached and (len(cached["pages"]) >= max_pages or cached["total"] == len(cached["pages"])):
        return cached["pages"][:max_pages]
    # Not cached, or cached shorter than this caller needs: a 6-page read must not starve
    # a later 30-page read of the same filing.
    try:
        r = httpx.get(url, headers={"User-Agent": HTTP_UA, "Referer": "https://www.bseindia.com/"},
                      timeout=45, follow_redirects=True)
        r.raise_for_status()
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
        total = len(reader.pages)
    except Exception as e:
        log.warning("document fetch failed %s: %s", url, e)
        return cached["pages"] if cached else None
    db.kv_set(key, {"pages": pages, "total": total})
    return pages
