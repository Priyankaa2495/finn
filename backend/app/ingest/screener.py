"""Live fetch for tickers not in the snapshot: screener.in tables + concall transcript PDFs.

Primary-source rule: the tables are screener's tabulation of the company's own filings and
the transcripts are the BSE-hosted PDFs it links to. Nothing else (no news, no notes) is read.
Parsing is stdlib HTMLParser; PDF text via pypdf. Output is the bundle shape the
research engine consumes (identical to the committed snapshot files).
"""
from __future__ import annotations

import io
import logging
import re
import time
from datetime import date
from html.parser import HTMLParser

from .. import db

log = logging.getLogger(__name__)

SCREENER = "https://www.screener.in/company/{sym}/{variant}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
SECTIONS = ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios")
_MONTH_YEAR = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}$")

ROWS = {
    "sales": "revenue", "revenue": "revenue", "operating profit": "ebitda", "other income": "other_income",
    "interest": "interest", "depreciation": "depreciation", "profit before tax": "pbt", "net profit": "pat",
    "equity capital": "equity_capital", "reserves": "reserves", "borrowings": "borrowings",
    "cash from operating activity": "cfo",
    "debtor days": "debtor_days", "inventory days": "inventory_days", "days payable": "payable_days",
    "roce %": "roce",
}


class _ScreenerParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self.transcripts: list[tuple[str, str]] = []   # (month-year label, pdf url)
        self._section: str | None = None
        self._rows: list[list[str]] | None = None
        self._cell: list[str] | None = None
        self._href: str | None = None
        self._atext: list[str] = []
        self._last_date: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and a.get("id") in SECTIONS:
            self._section = a["id"]
        elif tag == "table" and self._section and self._section not in self.tables:
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._rows.append([])
        elif tag in ("td", "th") and self._rows is not None:
            self._cell = []
        elif tag == "a":
            self._href, self._atext = a.get("href"), []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._rows:
            self._rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._rows is not None:
            self.tables[self._section] = self._rows
            self._rows = None
        elif tag == "section":
            self._section = None
        elif tag == "a" and self._href is not None:
            if "".join(self._atext).strip().lower() == "transcript" and self._last_date:
                self.transcripts.append((self._last_date, self._href))
            self._href = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._href is not None:
            self._atext.append(data)
        s = data.strip()
        if _MONTH_YEAR.match(s):
            self._last_date = s


def _num(s: str) -> float | None:
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _table_to_periods(rows: list[list[str]]) -> dict[str, dict]:
    if not rows:
        return {}
    header = rows[0]
    periods = {i: h for i, h in enumerate(header) if _MONTH_YEAR.match(h)}
    out: dict[str, dict] = {h: {"period": h} for h in periods.values()}
    for row in rows[1:]:
        if not row:
            continue
        key = ROWS.get(row[0].replace("+", "").replace("\xa0", " ").strip().lower())
        if not key:
            continue
        for i, h in periods.items():
            if i < len(row):
                out[h][key] = _num(row[i])
    return out


def parse_company_page(html: str) -> dict:
    p = _ScreenerParser()
    p.feed(html)
    quarters = list(_table_to_periods(p.tables.get("quarters", [])).values())
    annual: dict[str, dict] = {}
    for sec in ("profit-loss", "balance-sheet", "cash-flow", "ratios"):
        for label, vals in _table_to_periods(p.tables.get(sec, [])).items():
            annual.setdefault(label, {"period": label}).update({k: v for k, v in vals.items() if k != "period"})
    for row in annual.values():
        cap, res = row.pop("equity_capital", None), row.pop("reserves", None)
        row["equity"] = None if cap is None or res is None else cap + res
    return {"quarters": quarters, "annual": list(annual.values()), "transcripts": p.transcripts}


def call_metadata(month_year: str) -> tuple[str, str]:
    """A call in Nov 2026 discusses the quarter ended Sep 2026 -> ('Q2 FY27', '2026-11-01')."""
    from ..research.metrics import MONTHS, fiscal_year, fy_label
    mon, yr = month_year.split()
    m, y = MONTHS[mon], int(yr)
    qm, qy = max([x for x in (3, 6, 9, 12) if x < m], default=12), y
    if m <= 3:
        qm, qy = 12, y - 1
    q = {6: 1, 9: 2, 12: 3, 3: 4}[qm]
    return f"Q{q} {fy_label(fiscal_year(qy, qm))}", date(y, m, 1).isoformat()


def pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]


def fetch_bundle(symbol: str, session=None, n_transcripts: int = 4, delay_s: float = 1.0) -> dict:
    if session is None:
        import requests
        session = requests.Session()
    session.headers.update(HEADERS)
    sym = symbol.upper()
    parsed, url = None, None
    for variant in ("consolidated/", ""):
        url = SCREENER.format(sym=sym, variant=variant)
        resp = session.get(url, timeout=20)
        if resp.status_code == 404:
            raise LookupError(f"{sym} not found on screener.in")
        resp.raise_for_status()
        parsed = parse_company_page(resp.text)
        if parsed["quarters"]:
            break
        time.sleep(delay_s)
    transcripts = []
    for label, pdf_url in parsed["transcripts"][:n_transcripts]:
        time.sleep(delay_s)
        try:
            r = session.get(pdf_url, timeout=30)
            r.raise_for_status()
            pages = pdf_pages(r.content)
        except Exception as exc:  # scanned/blocked PDFs are skipped, never fatal
            log.warning("transcript %s failed: %s", pdf_url, exc)
            continue
        if not any(p.strip() for p in pages):
            continue
        call, call_date = call_metadata(label)
        transcripts.append({"call": call, "call_date": call_date, "url": pdf_url, "pages": pages})
    return {"symbol": sym, "source": "live", "fetched_at": db.utcnow_iso(),
            "financials": {"unit": "INR_cr", "source_url": url,
                           "quarters": parsed["quarters"], "annual": parsed["annual"]},
            "transcripts": transcripts}
