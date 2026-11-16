"""Deterministic financial arithmetic (Hard Rule 3). All values are ₹ crore as tabulated.

Input: screener-style tables (quarters + annual), periods labelled 'Sep 2026' / 'Mar 2026'.
Indian fiscal year ends in March: 'Sep 2026' is Q2 FY27; 'Mar 2026' is FY26.
Every derived number returns None rather than guessing when an input is missing.
The output 'facts pack' is the only numeric material the deep model ever sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_Q_OF_MONTH = {6: 1, 9: 2, 12: 3, 3: 4}


# ----------------------------------------------------------------- periods
def fiscal_year(year: int, month: int) -> int:
    return year + 1 if month >= 4 else year


def fy_label(fy: int) -> str:
    return f"FY{fy % 100:02d}"


@dataclass
class Period:
    label: str                       # 'Sep 2026'
    year: int
    month: int
    values: dict[str, float | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, row: dict) -> "Period":
        label = row["period"]
        mon, yr = label.split()
        values = {k: (None if v is None else float(v)) for k, v in row.items() if k != "period"}
        return cls(label, int(yr), MONTHS[mon[:3].title()], values)

    @property
    def fy(self) -> int:
        return fiscal_year(self.year, self.month)

    @property
    def qlabel(self) -> str:
        return f"Q{_Q_OF_MONTH.get(self.month, '?')} {fy_label(self.fy)}"

    def get(self, key: str) -> float | None:
        return self.values.get(key)


@dataclass
class Financials:
    symbol: str
    unit: str
    source_url: str | None
    quarters: list[Period]           # ascending
    annual: list[Period]             # ascending, fiscal year ends only

    @classmethod
    def from_dict(cls, symbol: str, d: dict) -> "Financials":
        if d.get("unit", "INR_cr") != "INR_cr":
            raise ValueError(f"unsupported unit {d.get('unit')!r}; tables must be in ₹ crore")
        key = lambda p: (p.year, p.month)
        annual = [Period.parse(r) for r in d.get("annual", [])]
        if annual:
            # Screener appends an interim balance-sheet column (e.g. 'Sep 2026'); that is not a
            # fiscal year. Keep only the company's fiscal-year-end month (the most common one).
            months = [p.month for p in annual]
            fy_end = max(set(months), key=lambda m: (months.count(m), m == 3))
            annual = [p for p in annual if p.month == fy_end]
        return cls(symbol, "INR_cr", d.get("source_url"),
                   sorted((Period.parse(r) for r in d.get("quarters", [])), key=key),
                   sorted(annual, key=key))

    def annual_for(self, fy: int) -> Period | None:
        return next((p for p in self.annual if p.fy == fy), None)

    def quarters_in(self, fy: int) -> list[Period]:
        return [q for q in self.quarters if q.fy == fy]

    def quarter(self, year: int, month: int) -> Period | None:
        return next((q for q in self.quarters if (q.year, q.month) == (year, month)), None)


# -------------------------------------------------------------- arithmetic
def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None  # growth off a zero/negative base is not meaningful
    return (new / old - 1) * 100


def ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def margin(num: float | None, den: float | None) -> float | None:
    r = ratio(num, den)
    return None if r is None else r * 100


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return ((last / first) ** (1 / years) - 1) * 100


def ssum(values) -> float | None:
    values = list(values)
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def _sub(a, b):
    return None if a is None or b is None else a - b


def _add(a, b):
    return None if a is None or b is None else a + b


# ---------------------------------------------------------------- views
HEADLINE = ("revenue", "ebitda", "pbt", "pat")


def quarter_snapshot(fin: Financials) -> dict | None:
    if not fin.quarters:
        return None
    cur = fin.quarters[-1]
    prev = fin.quarters[-2] if len(fin.quarters) > 1 else None
    ly = fin.quarter(cur.year - 1, cur.month)
    rows = {}
    for m in HEADLINE:
        v = cur.get(m)
        rows[m] = {"value": v,
                   "qoq": pct_change(v, prev.get(m)) if prev else None,
                   "yoy": pct_change(v, ly.get(m)) if ly else None}
    em = margin(cur.get("ebitda"), cur.get("revenue"))
    em_ly = margin(ly.get("ebitda"), ly.get("revenue")) if ly else None
    return {"period": cur.label, "quarter": cur.qlabel, "prev": prev.label if prev else None,
            "year_ago": ly.label if ly else None, "rows": rows,
            "ebitda_margin": em, "ebitda_margin_yoy_pp": _sub(em, em_ly),
            "pat_margin": margin(cur.get("pat"), cur.get("revenue"))}


def annual_rows(fin: Financials) -> list[dict]:
    out = []
    for i, p in enumerate(fin.annual):
        prior = fin.annual[i - 1] if i > 0 and fin.annual[i - 1].fy == p.fy - 1 else None
        rev, pat, cfo, capex = p.get("revenue"), p.get("pat"), p.get("cfo"), p.get("capex")
        equity = p.get("equity")
        if equity is None:
            equity = _add(p.get("equity_capital"), p.get("reserves"))
        debt = p.get("borrowings")
        ebit = _add(p.get("pbt"), p.get("interest"))

        def days(field_days, component):
            given = p.get(field_days)
            if given is not None:
                return given
            r = ratio(p.get(component), rev)
            return None if r is None else r * 365

        dd, idays, pdays = days("debtor_days", "receivables"), days("inventory_days", "inventory"), \
            days("payable_days", "payables")
        out.append({
            "fy": p.fy, "label": fy_label(p.fy), "revenue": rev, "ebitda": p.get("ebitda"), "pat": pat,
            "revenue_growth": pct_change(rev, prior.get("revenue")) if prior else None,
            "pat_growth": pct_change(pat, prior.get("pat")) if prior else None,
            "ebitda_margin": margin(p.get("ebitda"), rev), "pat_margin": margin(pat, rev),
            "cfo": cfo, "capex": capex, "fcf": _sub(cfo, capex), "cfo_to_pat": ratio(cfo, pat),
            "equity": equity, "borrowings": debt, "debt_to_equity": ratio(debt, equity),
            "net_debt": _sub(debt, p.get("cash")),
            "debtor_days": dd, "inventory_days": idays, "payable_days": pdays,
            "cash_conversion_cycle": None if None in (dd, idays, pdays) else dd + idays - pdays,
            "roce": p.get("roce") if p.get("roce") is not None else margin(ebit, _add(equity, debt)),
            "interest_coverage": ratio(ebit, p.get("interest")),
        })
    return out


def trajectory(rows: list[dict], span: int = 3) -> dict:
    if len(rows) < 2:
        return {}
    last = rows[-1]
    first = rows[max(0, len(rows) - 1 - span)]
    n = last["fy"] - first["fy"]
    return {"from": first["label"], "to": last["label"], "years": n,
            "revenue_cagr": cagr(first["revenue"], last["revenue"], n),
            "pat_cagr": cagr(first["pat"], last["pat"], n),
            "ebitda_margin_from": first["ebitda_margin"], "ebitda_margin_to": last["ebitda_margin"]}


def fy_actuals(fin: Financials, fy: int) -> dict | None:
    """Full-year actuals if the FY is reported; otherwise year-to-date vs the same quarters a year ago."""
    cur, prev = fin.annual_for(fy), fin.annual_for(fy - 1)
    if cur:
        rev, ebitda, pat = cur.get("revenue"), cur.get("ebitda"), cur.get("pat")
        p_rev, p_pat = (prev.get("revenue"), prev.get("pat")) if prev else (None, None)
        return {"kind": "full", "basis": f"{fy_label(fy)} full year", "revenue": rev, "ebitda": ebitda,
                "pat": pat, "revenue_growth": pct_change(rev, p_rev), "pat_growth": pct_change(pat, p_pat),
                "ebitda_margin": margin(ebitda, rev), "capex": cur.get("capex")}
    qs = fin.quarters_in(fy)
    if not qs:
        return None
    prior = [fin.quarter(q.year - 1, q.month) for q in qs]
    rev = ssum(q.get("revenue") for q in qs)
    ebitda = ssum(q.get("ebitda") for q in qs)
    pat = ssum(q.get("pat") for q in qs)
    have_prior = all(prior)
    p_rev = ssum(q.get("revenue") for q in prior) if have_prior else None
    p_pat = ssum(q.get("pat") for q in prior) if have_prior else None
    n = len(qs)
    basis = {1: "Q1", 2: "H1", 3: "9M"}.get(n, f"{n}Q") + f" {fy_label(fy)}"
    return {"kind": "ytd", "basis": f"{basis} ({n} quarter{'s' if n > 1 else ''})", "revenue": rev,
            "ebitda": ebitda, "pat": pat, "revenue_growth": pct_change(rev, p_rev),
            "pat_growth": pct_change(pat, p_pat), "ebitda_margin": margin(ebitda, rev), "capex": None}


# ------------------------------------------------------------- formatting
def fmt_cr(x: float) -> str:
    return f"{'−' if x < 0 else ''}₹{abs(x):,.0f} Cr"


def fmt_pct(x: float, signed: bool = False) -> str:
    return f"{x:+.1f}%" if signed else f"{x:.1f}%"


# ------------------------------------------------------------- facts pack
@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    polarity: int      # +1 supports bull, -1 supports bear, 0 neutral
    weight: float
    kind: str


class FactBook:
    def __init__(self):
        self.facts: list[Fact] = []

    def add(self, text: str, polarity: int = 0, weight: float = 1.0, kind: str = "financial") -> Fact:
        f = Fact(f"F{len(self.facts) + 1}", text, polarity, weight, kind)
        self.facts.append(f)
        return f

    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}


def _pol(x: float | None, good: float, bad: float, higher_is_better: bool = True) -> int:
    if x is None:
        return 0
    if higher_is_better:
        return 1 if x >= good else -1 if x < bad else 0
    return 1 if x <= good else -1 if x > bad else 0


def financial_facts(fin: Financials, book: FactBook) -> None:
    q = quarter_snapshot(fin)
    if q:
        r = q["rows"]["revenue"]
        if r["value"] is not None and r["yoy"] is not None:
            qoq = f", {fmt_pct(r['qoq'], True)} QoQ" if r["qoq"] is not None else ""
            book.add(f"{q['quarter']} revenue {fmt_cr(r['value'])} ({fmt_pct(r['yoy'], True)} YoY{qoq})",
                     _pol(r["yoy"], 10, 3))
        if q["ebitda_margin"] is not None and q["ebitda_margin_yoy_pp"] is not None:
            d = q["ebitda_margin_yoy_pp"]
            book.add(f"{q['quarter']} EBITDA margin {fmt_pct(q['ebitda_margin'])} vs "
                     f"{fmt_pct(q['ebitda_margin'] - d)} a year ago", _pol(d, 1, -1))
        p = q["rows"]["pat"]
        if p["value"] is not None and p["yoy"] is not None:
            book.add(f"{q['quarter']} PAT {fmt_cr(p['value'])} ({fmt_pct(p['yoy'], True)} YoY)", _pol(p["yoy"], 10, 0))

    rows = annual_rows(fin)
    if not rows:
        return
    last, first = rows[-1], rows[0]
    if last["revenue"] is not None and last["revenue_growth"] is not None:
        book.add(f"{last['label']} revenue {fmt_cr(last['revenue'])} ({fmt_pct(last['revenue_growth'], True)} YoY)",
                 _pol(last["revenue_growth"], 10, 3))
    if last["pat"] is not None and last["pat_growth"] is not None:
        book.add(f"{last['label']} PAT {fmt_cr(last['pat'])} ({fmt_pct(last['pat_growth'], True)} YoY)",
                 _pol(last["pat_growth"], 10, 0))
    t = trajectory(rows)
    if t.get("revenue_cagr") is not None:
        book.add(f"Revenue CAGR {t['from']}–{t['to']}: {fmt_pct(t['revenue_cagr'])}", _pol(t["revenue_cagr"], 12, 6))
    if t.get("pat_cagr") is not None:
        book.add(f"PAT CAGR {t['from']}–{t['to']}: {fmt_pct(t['pat_cagr'])}", _pol(t["pat_cagr"], 12, 6))
    if first["ebitda_margin"] is not None and last["ebitda_margin"] is not None and first is not last:
        d = last["ebitda_margin"] - first["ebitda_margin"]
        book.add(f"EBITDA margin {fmt_pct(first['ebitda_margin'])} ({first['label']}) → "
                 f"{fmt_pct(last['ebitda_margin'])} ({last['label']})", _pol(d, 1, -1))
    if last["cfo_to_pat"] is not None:
        prior = f" vs {first['cfo_to_pat']:.2f}x in {first['label']}" if first["cfo_to_pat"] is not None and first is not last else ""
        book.add(f"Cash conversion (CFO/PAT) {last['cfo_to_pat']:.2f}x in {last['label']}{prior}",
                 _pol(last["cfo_to_pat"], 1.0, 0.8), kind="cash")
    fcfs = [(r["label"], r["fcf"]) for r in rows[-3:] if r["fcf"] is not None]
    if fcfs:
        neg = sum(1 for _, v in fcfs if v < 0)
        book.add(f"Free cash flow (CFO − capex) negative in {neg} of last {len(fcfs)} years; "
                 f"{fcfs[-1][0]} FCF {fmt_cr(fcfs[-1][1])}", -1 if neg >= 2 else 1 if neg == 0 else 0, kind="cash")
    if last["debt_to_equity"] is not None:
        f_de = first["debt_to_equity"]
        prior = f" ({first['label']}: {f_de:.2f}x)" if f_de is not None and first is not last else ""
        rising = f_de is not None and last["debt_to_equity"] - f_de >= 0.1
        book.add(f"Debt/Equity {last['debt_to_equity']:.2f}x in {last['label']}{prior}",
                 -1 if rising or last["debt_to_equity"] > 1 else 1 if last["debt_to_equity"] < 0.3 else 0,
                 kind="balance_sheet")
    if last["debtor_days"] is not None and first["debtor_days"] is not None and first is not last:
        d = last["debtor_days"] - first["debtor_days"]
        book.add(f"Debtor days {first['debtor_days']:.0f} ({first['label']}) → {last['debtor_days']:.0f} ({last['label']})",
                 -1 if d >= 10 else 1 if d <= -10 else 0, kind="balance_sheet")
    if last["roce"] is not None:
        book.add(f"ROCE {fmt_pct(last['roce'])} in {last['label']}", _pol(last["roce"], 18, 12))
    if last["interest_coverage"] is not None:
        book.add(f"Interest coverage (EBIT/interest) {last['interest_coverage']:.1f}x in {last['label']}",
                 _pol(last["interest_coverage"], 6, 3), kind="balance_sheet")
