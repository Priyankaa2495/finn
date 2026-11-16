"""Results card (Push): headline numbers from the primary filing, with explicit provenance.

Order of truth
  1. The results PDF attached to the filing -> "Primary Source (Filing)".
     - Every page is scanned (no first-N-pages cap: Kaynes put its table on p.7 of 25);
       the page that looks most like a SEBI results statement wins, consolidated preferred.
     - Unit guard: the statement's own unit line decides the scale. Lakhs (/100), millions
       (/10), thousands (/10,000) are converted to ₹ crore. No unit found -> we do not guess;
       the PDF is treated as unreadable.
     - EBITDA is shown only if the filing has an EBITDA row. It is never computed or estimated.
  2. screener.in's quarterly table, only if it already covers the filing's quarter.
  3. Otherwise: "Quarter not tabulated yet - verify with filing PDF". No numbers.
  When both 1 and 2 exist, prior-period columns in the filing are checked against screener;
  disagreements (restatements) are flagged on the card.
QoQ / YoY are computed in code from the filing's own comparative columns.

Also: management-change bios from the SEBI-mandated "brief profile" in the PDF annexure.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from ..research import metrics as M

log = logging.getLogger(__name__)

PRIMARY, SCREENER, NOT_TABULATED = ("Primary Source (Filing)", "Screener table",
                                    "Quarter not tabulated yet - verify with filing PDF")
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 80
PDF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/pdf,*/*",
}

# --------------------------------------------------------------------- units
_UNIT_WORD = r"(crores?|cr\b|lakhs?|lacs?|millions?|mn\b|thousands?)"
_CUR = r"(?:₹|rs\.?|inr|rupees)"
_UNIT_RES = (
    re.compile(_CUR + r"\s*(?:in\s+)?" + _UNIT_WORD, re.I),          # (Rs. in Lakhs), (₹ Million)
    re.compile(r"\bin\s+" + _CUR + r"\s*" + _UNIT_WORD, re.I),       # (Amount in INR crore)
)
_SCALE = {"crore": 1.0, "cr": 1.0, "lakh": 0.01, "lac": 0.01, "million": 0.1, "mn": 0.1, "thousand": 0.0001}
_UNIT_NAME = {1.0: "₹ crore", 0.01: "₹ lakh", 0.1: "₹ million", 0.0001: "₹ thousand"}


def detect_scale(text: str) -> tuple[float, str] | None:
    """Scale factor to ₹ crore and the unit as written. None if absent or contradictory."""
    seen: dict[float, str] = {}
    for rx in _UNIT_RES:
        for m in rx.finditer(text):
            word = m.group(1).lower().rstrip("s").rstrip(".")
            scale = next(v for k, v in _SCALE.items() if word.startswith(k))
            seen.setdefault(scale, m.group(0).strip())
    if len(seen) != 1:
        return None
    (scale, written), = seen.items()
    return scale, written


# ------------------------------------------------------------------- periods
_MONTHS = {m.lower(): i for m, i in M.MONTHS.items()}
_DATE_RES = (
    re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})"),
    re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})[,\s-]+(\d{2,4})", re.I),   # 30TH SEPTEMBER
    re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", re.I),
)


def _mk_date(a: str, b: str, c: str) -> date | None:
    try:
        if a.isalpha():                       # Month DD, YYYY
            mon, day, yr = _MONTHS.get(a[:3].lower()), int(b), int(c)
        elif b.isalpha():                     # DD Month YYYY
            day, mon, yr = int(a), _MONTHS.get(b[:3].lower()), int(c)
        else:                                 # DD.MM.YYYY
            day, mon, yr = int(a), int(b), int(c)
        if mon is None:
            return None
        yr = yr + 2000 if yr < 100 else yr
        return date(yr, mon, day)
    except ValueError:
        return None


_QE_PHRASE = re.compile(r"([QO]uarter|three months)(?:\s+and\s+(?:half[- ]year|six months|nine months|year))?"
                        r"\s+ended", re.I)   # 'OUARTER' is a real OCR misread seen in a Dixon filing


def quarter_end_in(text: str) -> date | None:
    for m in _QE_PHRASE.finditer(text):
        window = text[m.end(): m.end() + 80]
        for rx in _DATE_RES:
            d = rx.search(window)
            if d:
                dt = _mk_date(*d.groups())
                if dt and dt.month in (3, 6, 9, 12) and dt.day in (30, 31):
                    return dt
    return None


def quarter_before(filed: date) -> date:
    """Latest quarter end strictly before the filing date."""
    for y, m, d in ((filed.year, 12, 31), (filed.year, 9, 30), (filed.year, 6, 30),
                    (filed.year, 3, 31), (filed.year - 1, 12, 31)):
        if date(y, m, d) < filed:
            return date(y, m, d)
    raise AssertionError("unreachable")


def _shift(d: date, months: int) -> tuple[int, int]:
    idx = d.year * 12 + d.month - 1 + months
    return idx // 12, idx % 12 + 1


# ---------------------------------------------------------------------- rows
_ROWS = {   # metric -> (label pattern, labels to exclude)
    "revenue": (re.compile(r"revenue from operations", re.I), re.compile(r"other|segment", re.I)),
    "ebitda": (re.compile(r"\bebitda\b", re.I), re.compile(r"margin|%", re.I)),
    "pbt": (re.compile(r"profit\s*/?\s*\(?\s*(?:loss)?\s*\)?\s*before\s+(?:exceptional items? and\s+)?tax", re.I),
            re.compile(r"\bshare of\b", re.I)),
    "pat": (re.compile(r"(?:net\s+)?profit\s*/?\s*\(?\s*(?:loss)?\s*\)?\s*(?:for the (?:period|quarter|year)|after tax)",
                       re.I), re.compile(r"attributable|comprehensive|before|owners|non-controlling", re.I)),
}
_TOKEN = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_NIL = {"-", "–", "—"}
_ENUM = re.compile(r"^\s*(?:(?:[ivxIVX]{1,4}|\d{1,2}|[a-hA-H])[\).]?\s+)+(?=[A-Za-z])")
_PAREN_NOTE = re.compile(r"\([^)]*[A-Za-z][^)]*\)")
# Row-arithmetic notes: 'Profit before tax (3-4)', '( 7 + 8)'. Read as numbers they turned
# Kaynes' PBT of 1,270.17 mn into "3" (live test, Nov 2026).
_FORMULA = re.compile(r"\(\s*\d{1,2}\s*[-+]\s*\d{1,2}(?:\s*[-+]\s*\d{1,2})*\s*\)")
_BASIS = re.compile(r"\b(consolidated|standalone)\s+(?:\w+\s+){0,2}financial\s+results", re.I)


def _to_float(tok: str) -> float:
    neg = tok.startswith("(") and tok.endswith(")") or tok.startswith("-")
    v = float(tok.strip("()-").replace(",", ""))
    return -v if neg else v


def _numbers_after(s: str) -> list[float | None]:
    """Whole numeric tokens in column order. '-' (nil) keeps its column. Stops at the first
    non-number once numbers have started (scanned PDFs repeat the row as a glued OCR layer)."""
    out: list[float | None] = []
    for tok in s.split():
        if tok in _NIL and out:
            out.append(None)
        elif _TOKEN.fullmatch(tok) and tok.count("(") == tok.count(")"):
            out.append(_to_float(tok))
        elif out:
            break
    return out


def parse_rows(page: str) -> dict[str, list[float | None]]:
    lines = [_PAREN_NOTE.sub(" ", _FORMULA.sub(" ", _ENUM.sub("", ln))) for ln in page.splitlines()]
    out: dict[str, list[float | None]] = {}
    for metric, (label, exclude) in _ROWS.items():
        candidates = []
        for ln in lines:
            m = label.search(ln)
            if not m or exclude.search(ln[: m.end() + 30]):
                continue
            vals = _numbers_after(ln[m.end():])   # numbers must sit on the label's own line
            if len(vals) >= 3 and vals[0] is not None:
                candidates.append((("exceptional" in ln.lower()), vals))
        if candidates:
            candidates.sort(key=lambda c: c[0])      # prefer the plain 'Profit before tax' row
            out[metric] = candidates[0][1]
    return out


def _plausible(rows: dict[str, list[float | None]]) -> bool:
    """Reject column-scrambled extractions: profit can't exceed revenue in a results table."""
    rev = rows.get("revenue") or []
    for i in range(3):
        r = rev[i] if i < len(rev) else None
        if r is None or r <= 0:
            return False
        for metric in ("pbt", "pat", "ebitda"):
            v = (rows.get(metric) or [None] * 3)[i]
            if v is not None and abs(v) > 1.5 * r:
                return False
    return True


def page_basis(text: str) -> str | None:
    m = _BASIS.search(text)
    return m.group(1).title() if m else None


def _page_score(text: str) -> int:
    t = text.lower()
    s = 3 * ("financial results" in t and "statement" in t) + 2 * ("revenue from operations" in t)
    s += 2 * bool(_ROWS["pbt"][0].search(t)) + bool(_QE_PHRASE.search(t))
    return s + 2 * (page_basis(text) == "Consolidated")   # title may sit anywhere in extracted text


# ---------------------------------------------------------------------- card
@dataclass
class ParsedResults:
    page: int                     # 1-based
    basis: str                    # Consolidated | Standalone
    quarter_end: date
    scale: float
    unit_written: str
    values: dict[str, list[float]]   # crore: [current, previous quarter, year-ago quarter, ...]


@dataclass
class ResultsCard:
    symbol: str
    quarter: str
    period_end: str
    source: str
    rows: dict[str, dict] = field(default_factory=dict)
    basis: str | None = None
    page: int | None = None
    unit_note: str | None = None
    flags: list[str] = field(default_factory=list)
    pdf_url: str | None = None


def parse_results_pages(pages: list[str]) -> ParsedResults | str:
    """Returns ParsedResults, or a reason string explaining why the PDF can't be trusted."""
    if not pages:
        return "no pages"
    scored = sorted(((p_score, i) for i, p in enumerate(pages) if (p_score := _page_score(p)) >= 5),
                    key=lambda x: (-x[0], x[1]))
    if not scored:
        return "no results statement found in PDF"
    for _, i in scored:
        page = pages[i]
        rows = parse_rows(page)
        if not {"revenue", "pat"} <= rows.keys() or not _plausible(rows):
            continue
        unit = detect_scale(page) or detect_scale("\n".join(pages[max(0, i - 1): i + 1]))
        if unit is None:
            return f"unit not stated (or ambiguous) on page {i + 1}; not guessing a scale"
        qe = quarter_end_in(page) or quarter_end_in("\n".join(pages))
        if qe is None:
            return "quarter-end date not found"
        scale, written = unit
        return ParsedResults(i + 1, page_basis(page) or "Unstated", qe, scale, written,
                             {k: [None if v is None else round(v * scale, 2) for v in vs] for k, vs in rows.items()})
    return "results statement found but table layout not row-readable"


def _row(cur, prev, ly) -> dict:
    return {"value": cur, "qoq": M.pct_change(cur, prev), "yoy": M.pct_change(cur, ly),
            "prev_quarter": prev, "year_ago": ly}


def _check_against_screener(p: ParsedResults, fin: M.Financials | None) -> list[str]:
    if fin is None or p.basis != "Consolidated":
        return []
    flags = []
    pq, ly = _shift(p.quarter_end, -3), _shift(p.quarter_end, -12)
    for col, (y, mth) in ((1, pq), (2, ly)):
        sq = fin.quarter(y, mth)
        if sq is None:
            continue
        for metric in ("revenue", "pbt", "pat"):
            filed, tab = (p.values.get(metric) or [None] * 3)[col], sq.get(metric)
            if filed is None or tab is None:
                continue
            if abs(filed - tab) > max(1.0, 0.01 * abs(tab)):   # screener rounds to whole crore
                flags.append(f"{metric.upper() if metric != 'revenue' else 'Revenue'} for {sq.label}: "
                             f"filing {M.fmt_cr(filed)} vs screener {M.fmt_cr(tab)} "
                             f"(restated, or defined differently)")
    return flags


def build_card(symbol: str, filed_on: date, pages: list[str] | None = None,
               fin: M.Financials | None = None, pdf_url: str | None = None) -> ResultsCard:
    parsed = parse_results_pages(pages) if pages else "no PDF"
    if isinstance(parsed, ParsedResults):
        qe = parsed.quarter_end
        y, m = qe.year, qe.month
        card = ResultsCard(symbol, f"Q{ {6: 1, 9: 2, 12: 3, 3: 4}[m]} {M.fy_label(M.fiscal_year(y, m))}",
                           qe.isoformat(), PRIMARY, basis=parsed.basis, page=parsed.page, pdf_url=pdf_url)
        for metric in ("revenue", "ebitda", "pbt", "pat"):
            vals = parsed.values.get(metric)
            if vals:                       # EBITDA absent in filing -> row absent on card
                card.rows[metric] = _row(*vals[:3])
        card.unit_note = ("Filing reports in ₹ crore" if parsed.scale == 1.0 else
                          f"Filing reports in {_UNIT_NAME[parsed.scale]} ('{parsed.unit_written}'); converted to ₹ crore")
        if "ebitda" not in card.rows:
            card.flags.append("EBITDA not disclosed in filing; not shown")
        card.flags += _check_against_screener(parsed, fin)
        return card

    qe = quarter_before(filed_on)
    y, m = qe.year, qe.month
    quarter = f"Q{ {6: 1, 9: 2, 12: 3, 3: 4}[m]} {M.fy_label(M.fiscal_year(y, m))}"
    sq = fin.quarter(y, m) if fin else None
    if sq is None:
        return ResultsCard(symbol, quarter, qe.isoformat(), NOT_TABULATED, pdf_url=pdf_url,
                           flags=[f"PDF not usable: {parsed}"])
    card = ResultsCard(symbol, quarter, qe.isoformat(), SCREENER, basis="Consolidated (screener)",
                       pdf_url=pdf_url, flags=[f"PDF not usable: {parsed}"])
    pq, ly = fin.quarter(*_shift(qe, -3)), fin.quarter(*_shift(qe, -12))
    for metric in ("revenue", "ebitda", "pbt", "pat"):
        cur = sq.get(metric)
        if cur is not None:
            card.rows[metric] = _row(cur, pq.get(metric) if pq else None, ly.get(metric) if ly else None)
    if "ebitda" in card.rows:
        card.flags.append("EBITDA is screener's 'Operating Profit' line")
    return card


# ----------------------------------------------------------------- PDF fetch
def pdf_pages(data: bytes, max_pages: int = MAX_PAGES, layout: bool = False) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    kw = {"extraction_mode": "layout"} if layout else {}
    return [(reader.pages[i].extract_text(**kw) or "") for i in range(min(len(reader.pages), max_pages))]


def card_from_pdf(symbol: str, filed_on: date, data: bytes | None, fin: M.Financials | None = None,
                  pdf_url: str | None = None) -> ResultsCard:
    """Plain extraction first; if that can't produce a trustworthy table (column-block or
    scanned layouts, e.g. Dixon), retry with layout-preserving extraction before falling back."""
    if not data:
        return build_card(symbol, filed_on, None, fin, pdf_url)
    card = build_card(symbol, filed_on, pdf_pages(data), fin, pdf_url)
    if card.source == PRIMARY:
        return card
    retry = build_card(symbol, filed_on, pdf_pages(data, layout=True), fin, pdf_url)
    if retry.source == PRIMARY:
        retry.flags.append("parsed with layout-preserving extraction")
        return retry
    return card


def fetch_pdf(url: str, session=None) -> bytes:
    """BSE/NSE-hosted attachment bytes. Raises on HTTP errors or oversize files."""
    if session is None:
        import requests
        session = requests.Session()
    headers = dict(PDF_HEADERS)
    if "bseindia.com" in url:
        headers["Referer"] = "https://www.bseindia.com/"
    elif "nseindia.com" in url:
        headers["Referer"] = "https://www.nseindia.com/"
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if len(resp.content) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds size cap")
    return resp.content


def fetch_pdf_pages(url: str, session=None, max_pages: int = MAX_PAGES) -> list[str]:
    """BSE/NSE-hosted attachment -> page texts. Raises on HTTP errors or oversize files."""
    if session is None:
        import requests
        session = requests.Session()
    headers = dict(PDF_HEADERS)
    if "bseindia.com" in url:
        headers["Referer"] = "https://www.bseindia.com/"
    elif "nseindia.com" in url:
        headers["Referer"] = "https://www.nseindia.com/"
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if len(resp.content) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds size cap")
    return pdf_pages(resp.content, max_pages)


# ---------------------------------------------------------------------- bios
_PROFILE_START = re.compile(r"brief\s+profile(?:\s*\(in case of appointments?\))?\s*:?", re.I)
_PROFILE_STOP = re.compile(r"disclosure of relationships?|relationships? between directors"
                           r"|information as required|names? of (the )?listed entities", re.I)
_NAME = re.compile(r"(?i:appointment|resignation|cessation|re-appointment) of\s+((?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?\s+"
                   r"[A-Z][\w.]*(?:\s+[A-Z][\w.]*){0,4})")
_ROLE = re.compile(r"\bas\s+(?:an?\s+|the\s+)?((?:(?:Additional|Independent|Non-Executive|Whole[- ]time|Executive)\s+)*"
                   r"(?:Director|Managing Director|Chief [A-Z]\w+ Officer|CFO|CEO|Company Secretary))")
_EMPLOYER = re.compile(r"\b(?:at|with|from|joined)\s+((?:[A-Z][\w&.'-]*\s+){0,5}"
                       r"(?:Ltd\.?|Limited|Bank|Industries|Technologies|Services|Company|Corporation|Group|LLP))")
ONE_LINER_CHARS = 200


# Real annexures (Nov 2026 refresh) put placeholders in the table cell ("As provided in
# Annexure II", "Not Applicable") and the real profile elsewhere, or a heading like
# "Brief profile of Mr. X, as required under Regulation 30...". Clean, then validate.
_LEAD_JUNK = re.compile(r"^\s*(?:\(in case of appointments?\)|of\s+(?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?[^,:.]{0,60}[,:]"
                        r"(?:\s*as required[^.:]*[.:])?|[:\-–]|\d+\.)\s*", re.I)
_PLACEHOLDER = re.compile(r"^(not applicable|n\.?\s?a\.?\b|nil\b|as (?:provided|given|mentioned)|(?:please )?refer"
                          r"|annexure|attached|enclosed)", re.I)
_BIO_CUE = re.compile(r"\b(experience|worked|served|serves|joined|holds|graduat\w*|degree|career|years|alumn\w*"
                      r"|qualified|chartered|engineer|MBA|has been|was the|is a|is an)\b", re.I)


def _clean_profile(raw: str) -> str | None:
    text = " ".join(raw.split())
    for _ in range(4):
        new = _LEAD_JUNK.sub("", text, count=1)
        if new == text:
            break
        text = new
    if len(text) < 60 or _PLACEHOLDER.match(text) or not _BIO_CUE.search(text[:400]):
        return None
    return text[:900]


def _profile_from(texts: list[str]) -> tuple[str | None, int | None]:
    for i, p in enumerate(texts):
        for m in _PROFILE_START.finditer(p):
            rest = p[m.end():] + "\n" + (texts[i + 1] if i + 1 < len(texts) else "")
            stop = _PROFILE_STOP.search(rest)
            prof = _clean_profile(rest[: stop.start() if stop else 1200])
            if prof:
                return prof, i + 1
    return None, None


def extract_bio(body: str, pages: list[str] | None = None) -> dict | None:
    """SEBI LODR Annexure: 'Brief profile (in case of appointment)'. Uses the PDF when the exchange
    text is a one-liner. Returns name/role/profile/employer hints, or None if nothing found.
    Placeholder cells are skipped; every 'Brief profile' occurrence is tried in order."""
    name = _NAME.search(body or "")
    role = _ROLE.search(body or "")
    profile, page_no = None, None
    if body and _PROFILE_START.search(body):
        profile, _ = _profile_from([body])
    if profile is None and pages:
        profile, page_no = _profile_from(pages)
        whole = "\n".join(pages)
        name = name or _NAME.search(whole)
        role = role or _ROLE.search(whole)
    if not (profile or name):
        return None
    employers = list(dict.fromkeys(" ".join(e.split()) for e in _EMPLOYER.findall(profile or "")))
    return {"name": " ".join(name.group(1).split()) if name else None,
            "role": " ".join(role.group(1).split()) if role else None,
            "profile": profile, "page": page_no, "employers": employers}
