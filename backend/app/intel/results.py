"""Quarterly results: a first cut of Revenue / EBITDA / PBT / PAT, QoQ and YoY.

Source of truth is the standardised quarterly table (screener.in, built from the
exchange results filings) — deterministic arithmetic, no model in the loop, so the
numbers can't be hallucinated. If the filing is newer than the table (first hours
after a release), we fall back to having the fast LLM read the results PDF, and label
the numbers 'extracted — verify'.

Banks/NBFCs don't report EBITDA; we show 'Financing profit' (pre-provision operating
profit proxy) and say so."""
import re

from .. import config, llm
from ..ingest import fundamentals

ROW_MAP = {
    "Revenue": ["Sales", "Revenue", "Revenue from Operations"],
    "EBITDA": ["Operating Profit", "Financing Profit", "EBITDA"],
    "PBT": ["Profit before tax", "Profit Before Tax"],
    "PAT": ["Net Profit", "Profit after tax", "PAT"],
}


def _row(rows, names):
    for n in names:
        if n in rows:
            return n, rows[n]
    return None, None


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a - b) / abs(b) * 100, 1)


_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_PERIOD = re.compile(
    r"(?:quarter|period|half[- ]year|year|months)\s+ended\s+(?:on\s+)?"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?[\s,./-]+([a-z]{3})[a-z]*[\s,./-]+(\d{4})"   # 30 September 2026
    r"|([a-z]{3})[a-z]*\s+(\d{1,2}),?\s+(\d{4})"                               # September 30, 2026
    r"|(\d{1,2})[./-](\d{1,2})[./-](\d{4}))", re.I)                            # 30.09.2026


def filing_period(text: str):
    """'... financial results for the period ended September 30, 2026' -> 'Sep 2026'
    (screener's column label), or None if the filing doesn't say."""
    m = _PERIOD.search(text or "")
    if not m:
        return None
    g = m.groups()
    if g[1]:
        mon, yr = g[1].lower(), g[2]
    elif g[3]:
        mon, yr = g[3].lower(), g[5]
    else:
        n = int(g[7])
        mon, yr = (_MONTHS[n - 1] if 1 <= n <= 12 else ""), g[8]
    return f"{mon.title()} {yr}" if mon in _MONTHS else None


def _period_key(label: str):
    try:
        mon, yr = label.split()
        return int(yr) * 12 + _MONTHS.index(mon.lower()[:3])
    except (ValueError, AttributeError):
        return None


def for_filing(symbol: str, text: str, url: str = ""):
    """Headline numbers for the quarter THIS filing reports — never silently last quarter's.

    The standardised table usually lags a fresh filing by hours to days. When it does:
    with a model, read the filing PDF and label the numbers 'verify'; without one, say
    the numbers aren't tabulated yet rather than show the previous quarter as if current."""
    data = fundamentals.get(symbol)
    want = filing_period(text)
    periods = ((data or {}).get("quarters") or {}).get("periods") or []
    if not want or not periods or want in periods:
        return headline(symbol, data, period=want)
    if _period_key(want) is not None and _period_key(periods[-1]) is not None \
            and _period_key(want) < _period_key(periods[-1]):
        return headline(symbol, data)  # an old filing outside the table's range: show latest
    if config.LLM_ENABLED and url:
        extracted = _from_pdf_headline(url, want)
        if extracted:
            chk = cross_check(extracted, data)
            if chk and not chk["ok"]:  # the model misread the statement (wrong unit, column or entity)
                extracted = None
            elif chk:
                extracted["cross_check"] = chk["detail"]
        if extracted:
            return extracted
    return {"pending": True, "quarter": want, "latest": periods[-1], "metrics": [],
            "source": "screener.in quarterly table", "basis": (data or {}).get("basis", ""),
            "note": f"{want} numbers aren't in the standardised table yet (it runs to {periods[-1]}). "
                    f"Open the filing for the figures; this card fills in on the next refresh."}


def _num(v):
    """Model output arrives as 1234.5, '1,234.50' or '(12.3)' (a negative in Indian statements)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("₹", "")
        neg = s.startswith("(") and s.endswith(")")
        try:
            x = float(s.strip("()"))
        except ValueError:
            return None
        return -x if neg else x
    return None


def _in_source(x, source_numbers):
    return x is not None and any(abs(abs(x) - n) < 0.006 for n in source_numbers)


def _from_pdf_headline(url, want):
    got = from_pdf(url)
    if not got:
        return None
    out, source = got
    if not isinstance(out, dict) or not isinstance(out.get("revenue"), list):
        return None
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", source)]
    unit = str(out.get("unit") or "").lower()
    # the card shows ₹ crore; filings print lakh, million or crore
    # unknown unit -> assume crore; cross_check() then rejects the card if the scale is wrong
    to_cr = next((f for pat, f in ((r"crore|\bcr\b", 1.0), (r"million|\bmn\b", 0.1), (r"lakh|\blac", 0.01),
                                   (r"thousand|\b000s?\b", 1e-4)) if re.search(pat, unit)), 1.0)
    metrics = []
    for label, key in (("Revenue", "revenue"), ("EBITDA", "ebitda"), ("PBT", "pbt"), ("PAT", "pat")):
        v = out.get(key)
        if not isinstance(v, list) or len(v) < 3:
            continue
        cur, prev, ya = (_num(x) for x in v[:3])
        if not _in_source(cur, nums):
            continue  # a figure that isn't printed in the statement is never shown
        prev = prev if _in_source(prev, nums) else None
        ya = ya if _in_source(ya, nums) else None
        metrics.append({"label": label, "source_row": key, "value": round(cur * to_cr, 2),
                        "prev_value": round(prev * to_cr, 2) if prev is not None else None,
                        "qoq": _pct(cur, prev), "yoy": _pct(cur, ya)})
    if not metrics:
        return None
    return {"quarter": want, "metrics": metrics, "basis": f"₹ cr (filing reports {out.get('unit') or 'unit not stated'})",
            "extracted": True,
            "source": "results PDF, model-extracted — verify against the filing", "synthetic": False,
            "verdict": _verdict(metrics)}


def cross_check(extracted, table):
    """Second source for model-read numbers: the previous quarter the model read from the
    PDF should match the standardised table's latest quarter (Kaynes, Nov 2026: 945.9 vs 946 cr)."""
    q = (table or {}).get("quarters") or {}
    rows, periods = q.get("rows") or {}, q.get("periods") or []
    when = periods[-1] if periods else "latest"
    agree, differ, rev_ok = [], [], None
    for m in extracted["metrics"]:
        _, tab = _row(rows, ROW_MAP.get(m["label"], []))
        if m.get("prev_value") is None or not tab or tab[-1] in (None, 0):
            continue
        same = abs(m["prev_value"] - tab[-1]) / abs(tab[-1]) * 100 <= 2.0
        (agree if same else differ).append(f"{m['label']} {m['prev_value']:,.0f} vs {tab[-1]:,.0f} cr")
        m["table_check"] = "matches" if same else "differs"
        if m["label"] == "Revenue":
            rev_ok = same
    if rev_ok is None:
        return None
    parts = []
    if agree:
        parts.append(f"{when} figures read from the PDF match the standardised table: " + ", ".join(agree) + ".")
    if differ:
        parts.append("Differ from the table, possibly a definition difference such as exceptional items: "
                     + ", ".join(differ) + ".")
    # Revenue is the anchor: if even that disagrees, the model misread the unit, column or entity
    return {"ok": rev_ok, "detail": " ".join(parts)}


def headline(symbol: str, data: dict | None = None, period: str | None = None):
    data = data or fundamentals.get(symbol)
    if not data or not data.get("quarters"):
        return None
    q = data["quarters"]
    periods, rows = q["periods"], q["rows"]
    if len(periods) < 2:
        return None
    i = periods.index(period) if period in periods and periods.index(period) >= 1 else len(periods) - 1
    out = {"quarter": periods[i], "prev": periods[i - 1],
           "year_ago": periods[i - 4] if i >= 4 else None, "metrics": [],
           "basis": data.get("basis", ""), "source": "screener.in quarterly table",
           "synthetic": data.get("synthetic", False)}
    for label, names in ROW_MAP.items():
        name, vals = _row(rows, names)
        if not vals:
            continue
        cur = vals[i]
        m = {"label": label, "source_row": name, "value": cur,
             "qoq": _pct(cur, vals[i - 1]),
             "yoy": _pct(cur, vals[i - 4]) if i >= 4 else None}
        if label == "EBITDA" and name == "Financing Profit":
            m["label"] = "Fin. profit"
            m["note"] = "Lenders don't report EBITDA; pre-provision profit shown"
        out["metrics"].append(m)
    rev = next((m["value"] for m in out["metrics"] if m["label"] == "Revenue"), None)
    ebitda = next((m for m in out["metrics"] if m["label"] == "EBITDA"), None)
    if rev and ebitda and ebitda["value"] is not None:
        out["ebitda_margin"] = round(ebitda["value"] / rev * 100, 1)
    out["verdict"] = _verdict(out["metrics"])
    return out


def _verdict(metrics):
    """One honest line, built from the numbers rather than a model."""
    d = {m["label"]: m for m in metrics}
    r, p = d.get("Revenue"), d.get("PAT")
    if not r or not p or r.get("yoy") is None or p.get("yoy") is None:
        return ""
    if r["yoy"] > 0 and p["yoy"] > r["yoy"]:
        return f"Profit outgrew revenue YoY ({p['yoy']:+.0f}% vs {r['yoy']:+.0f}%) — operating leverage showing."
    if r["yoy"] > 0 and p["yoy"] < 0:
        return f"Revenue up {r['yoy']:.0f}% YoY but PAT down {abs(p['yoy']):.0f}% — margins under pressure."
    if r["yoy"] < 0:
        return f"Revenue shrank {abs(r['yoy']):.0f}% YoY."
    return f"Revenue {r['yoy']:+.0f}% YoY, PAT {p['yoy']:+.0f}% YoY."


_STATEMENT = ("revenue from operations", "profit before tax", "total income", "profit for the")


def statement_pages(pages, keep=2):
    """A results filing opens with a cover letter and the auditor's report; the P&L table
    is several pages in (page 7 of 25 for Kaynes, Nov 2026). Send the model only the pages
    that look like the statement, consolidated first — chunk selection before the call."""
    scored = []
    for i, t in enumerate(pages):
        low = (t or "").lower()
        s = sum(k in low for k in _STATEMENT)
        if s >= 2:
            scored.append((s + (1 if "consolidated" in low else 0), -i, t))
    scored.sort(reverse=True)
    best = sorted(scored[:keep], key=lambda x: -x[1])  # back in page order
    return [t[:7000] for _, _, t in best]


def from_pdf(url: str):
    """Fallback when the standardised table hasn't caught up with the filing yet."""
    pages = fundamentals.document_text(url, max_pages=30)
    if not pages:
        return None
    text = "\n".join(statement_pages(pages))
    if not text:
        return None
    out = llm.complete(
        "From this Indian listed-company results filing, extract the CONSOLIDATED figures "
        "(or standalone if no consolidated) for the latest quarter, previous quarter and same "
        "quarter last year. Return JSON: {\"unit\": \"...\", \"periods\": [cur, prev, year_ago], "
        "\"revenue\": [..3], \"ebitda\": [..3 or null], \"pbt\": [..3], \"pat\": [..3]}. "
        "Use numbers exactly as printed, as JSON numbers. Null if absent; do not compute a figure "
        "that isn't printed (EBITDA usually isn't).\n\n" + llm.untrusted(text, 14000),
        tier="fast")
    return (out, text) if out else None
