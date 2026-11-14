"""Deterministic financial analysis over screener's standardised tables.

Everything a calculator can do is done here, not by a model: growth rates, margins,
leverage, cash conversion. These numbers are then handed to the model as *facts*
when judgement is needed, which keeps the model from inventing figures."""


def _series(table, *names):
    if not table:
        return None, None
    for n in names:
        if n in table["rows"]:
            pairs = [(p, v) for p, v in zip(table["periods"], table["rows"][n]) if p.upper() != "TTM"]
            return [p for p, _ in pairs], [v for _, v in pairs]
    return None, None


def cagr(vals, years):
    vals = [v for v in vals if v is not None]
    if len(vals) <= years or vals[-1 - years] is None or vals[-1 - years] <= 0 or vals[-1] <= 0:
        return None
    return round(((vals[-1] / vals[-1 - years]) ** (1 / years) - 1) * 100, 1)


def is_lender(d):
    q = (d.get("quarters") or {}).get("rows", {})
    return "Financing Profit" in q or "Financing Margin %" in q


def snapshot(d):
    r = d.get("ratios", {})
    pl = d.get("profit_loss")
    periods, sales = _series(pl, "Sales", "Revenue")
    _, np_ = _series(pl, "Net Profit")
    _, opm = _series(pl, "OPM %", "Financing Margin %")
    ttm = None
    if pl and pl["periods"] and pl["periods"][-1].upper() == "TTM":
        rows = pl["rows"]
        ttm = {"revenue": (rows.get("Sales") or rows.get("Revenue") or [None])[-1],
               "pat": (rows.get("Net Profit") or [None])[-1]}
    return {
        "market_cap_cr": r.get("Market Cap"), "price": r.get("Current Price"), "pe": r.get("Stock P/E"),
        "roce": r.get("ROCE"), "roe": r.get("ROE"), "div_yield": r.get("Dividend Yield"),
        "book_value": r.get("Book Value"),
        "revenue_ttm": (ttm or {}).get("revenue") or (sales[-1] if sales else None),
        "pat_ttm": (ttm or {}).get("pat") or (np_[-1] if np_ else None),
        "opm_last_fy": opm[-1] if opm else None,
        "last_fy": periods[-1] if periods else None,
        "lender": is_lender(d),
    }


def trajectory(d):
    pl = d.get("profit_loss")
    periods, sales = _series(pl, "Sales", "Revenue")
    _, np_ = _series(pl, "Net Profit")
    _, opm = _series(pl, "OPM %", "Financing Margin %")
    if not periods:
        return None
    k = slice(-8, None)  # last 8 fiscal years is plenty on a phone
    out = {"periods": periods[k], "revenue": sales[k], "pat": np_[k] if np_ else None,
           "opm": opm[k] if opm else None,
           "revenue_cagr_3y": cagr(sales, 3), "revenue_cagr_5y": cagr(sales, 5),
           "pat_cagr_3y": cagr(np_ or [], 3), "pat_cagr_5y": cagr(np_ or [], 5), "flags": []}
    f = out["flags"]
    r3, r5 = out["revenue_cagr_3y"], out["revenue_cagr_5y"]
    if r3 is not None and r5 is not None:
        if r3 > r5 + 3:
            f.append(("+", f"Growth accelerating: revenue {r3}% CAGR over 3y vs {r5}% over 5y"))
        elif r3 < r5 - 3:
            f.append(("-", f"Growth slowing: revenue {r3}% CAGR over 3y vs {r5}% over 5y"))
    if opm and len([x for x in opm if x is not None]) >= 4:
        last, avg3 = opm[-1], sum(opm[-4:-1]) / 3
        if last is not None:
            if last > avg3 + 1.5:
                f.append(("+", f"Margins expanding: {last:.0f}% last year vs {avg3:.0f}% 3-yr average"))
            elif last < avg3 - 1.5:
                f.append(("-", f"Margins compressing: {last:.0f}% last year vs {avg3:.0f}% 3-yr average"))
    p3 = out["pat_cagr_3y"]
    if p3 is not None and r3 is not None:
        if p3 > r3 + 5:
            f.append(("+", f"Profit compounding faster than revenue ({p3}% vs {r3}% 3y CAGR)"))
        elif p3 < r3 - 5:
            f.append(("-", f"Profit lagging revenue ({p3}% vs {r3}% 3y CAGR)"))
    return out


def balance_sheet(d):
    bs = d.get("balance_sheet")
    periods, borrow = _series(bs, "Borrowings", "Borrowing")
    _, eq = _series(bs, "Equity Capital")
    _, res = _series(bs, "Reserves")
    if not periods:
        return None
    de = [round(b / (e + r), 2) if None not in (b, e, r) and (e + r) else None
          for b, e, r in zip(borrow, eq, res)]
    rt = d.get("ratio_table")
    wc = {}
    for name in ("Debtor Days", "Inventory Days", "Days Payable", "Cash Conversion Cycle", "Working Capital Days"):
        p, v = _series(rt, name)
        if v:
            wc[name] = {"periods": p[-6:], "values": v[-6:]}
    out = {"periods": periods[-6:], "borrowings": borrow[-6:], "de": de[-6:], "working_capital": wc,
           "lender": is_lender(d), "flags": []}
    if out["lender"]:
        out["flags"].append(("·", "Lender: borrowing is the raw material, so D/E and working-capital days don't apply"))
        return out
    if de and de[-1] is not None:
        if de[-1] < 0.1:
            out["flags"].append(("+", f"Practically debt-free (D/E {de[-1]})"))
        elif de[-1] > 1:
            out["flags"].append(("-", f"Leveraged: D/E {de[-1]}"))
        if len(de) >= 3 and de[-3] is not None and de[-1] > de[-3] + 0.25:
            out["flags"].append(("-", f"Leverage rising: D/E {de[-3]} → {de[-1]} over 2 years"))
    ccc = wc.get("Cash Conversion Cycle", {}).get("values") or []
    ccc = [x for x in ccc if x is not None]
    if len(ccc) >= 3 and ccc[-1] > ccc[-3] + 15:
        out["flags"].append(("-", f"Working-capital cycle stretching: {ccc[-3]:.0f} → {ccc[-1]:.0f} days"))
    elif len(ccc) >= 3 and ccc[-1] < ccc[-3] - 15:
        out["flags"].append(("+", f"Working-capital cycle tightening: {ccc[-3]:.0f} → {ccc[-1]:.0f} days"))
    return out


def cash_quality(d):
    cf = d.get("cash_flow")
    periods, cfo = _series(cf, "Cash from Operating Activity")
    _, cfi = _series(cf, "Cash from Investing Activity")
    pl_periods, pat = _series(d.get("profit_loss"), "Net Profit")
    if not periods or not pat:
        return None
    pat_by_p = dict(zip(pl_periods, pat))
    pat_al = [pat_by_p.get(p) for p in periods]
    conv = [round(c / p * 100) if c is not None and p not in (None, 0) else None for c, p in zip(cfo, pat_al)]
    fcf = [round(c + i) if None not in (c, i) else None for c, i in zip(cfo, cfi or [None] * len(cfo))]
    n = min(5, len(periods))
    s_cfo = sum(x for x in cfo[-n:] if x is not None)
    s_pat = sum(x for x in pat_al[-n:] if x is not None)
    cum = round(s_cfo / s_pat * 100) if s_pat else None
    out = {"periods": periods[-6:], "cfo": cfo[-6:], "pat": pat_al[-6:], "conversion_pct": conv[-6:],
           "fcf_proxy": fcf[-6:], "cum_conversion_5y": cum, "lender": is_lender(d), "flags": [],
           "note": "FCF proxy = operating cash flow + investing cash flow (investing includes more than capex)."}
    if out["lender"]:
        out["flags"].append(("·", "Lender: operating cash flow swings with loan growth, so conversion isn't meaningful"))
    elif cum is not None:
        if cum >= 90:
            out["flags"].append(("+", f"Profits turn into cash: {cum}% of 5-yr PAT came in as operating cash"))
        elif cum < 70:
            out["flags"].append(("-", f"Weak cash conversion: only {cum}% of 5-yr PAT came in as operating cash"))
    return out


def quarterly(d):
    q = d.get("quarters")
    if not q:
        return None
    p, sales = _series(q, "Sales", "Revenue")
    _, opm = _series(q, "OPM %", "Financing Margin %")
    _, np_ = _series(q, "Net Profit")
    return {"periods": p, "revenue": sales, "opm": opm, "pat": np_}
