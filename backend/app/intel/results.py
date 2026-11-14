"""Quarterly results: a first cut of Revenue / EBITDA / PBT / PAT, QoQ and YoY.

Source of truth is the standardised quarterly table (screener.in, built from the
exchange results filings) — deterministic arithmetic, no model in the loop, so the
numbers can't be hallucinated. If the filing is newer than the table (first hours
after a release), we fall back to having the fast LLM read the results PDF, and label
the numbers 'extracted — verify'.

Banks/NBFCs don't report EBITDA; we show 'Financing profit' (pre-provision operating
profit proxy) and say so."""
from .. import llm
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


def headline(symbol: str, data: dict | None = None):
    data = data or fundamentals.get(symbol)
    if not data or not data.get("quarters"):
        return None
    q = data["quarters"]
    periods, rows = q["periods"], q["rows"]
    if len(periods) < 2:
        return None
    i = len(periods) - 1
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


def from_pdf(url: str):
    """Fallback when the standardised table hasn't caught up with the filing yet."""
    pages = fundamentals.document_text(url, max_pages=6)
    if not pages:
        return None
    text = "\n".join(pages)[:12000]
    return llm.complete(
        "From this Indian listed-company results filing, extract the CONSOLIDATED figures "
        "(or standalone if no consolidated) for the latest quarter, previous quarter and same "
        "quarter last year. Return JSON: {\"unit\": \"...\", \"periods\": [cur, prev, year_ago], "
        "\"revenue\": [..3], \"ebitda\": [..3 or null], \"pbt\": [..3], \"pat\": [..3]}. "
        "Use numbers exactly as printed. Null if absent.\n\n" + text,
        tier="fast")
