"""Demo layer, so the prototype always runs and the showcase workflows always fire.

Two kinds of non-live data, both labelled in the UI:
  * DEMO scenarios — a handful of synthetic filings (fictional people) injected into
    the live feed so management-change / results / material-event flows can be shown
    on any day. Always tagged DEMO.
  * Offline fixtures — only used when there is no live snapshot at all (e.g. a
    sandbox with no internet): synthetic prices and DEMOCHEM, a fictional company
    with a full set of financials and concall transcripts for the research view.
Real companies never get invented management quotes: transcripts exist only for the
fictional DEMOCHEM."""
import json
import math
import random
from datetime import datetime, timedelta

from . import db
from .ingest import fundamentals

ANCHOR = [datetime.now()]  # demo filings are placed relative to the latest live data, not wall-clock


def _ago(h):
    return (ANCHOR[0] - timedelta(hours=h)).isoformat(timespec="seconds")


SHOWCASE = [
    {"id": "demo-dixon-cfo", "symbol": "DIXON", "h": 2.2, "desc": "Change in Management",
     "text": "Dixon Technologies (India) Limited has informed the Exchange about appointment of Mr. Arvind Kulkarni "
             "as Chief Financial Officer and Key Managerial Personnel with effect from December 1, 2026. Brief profile: "
             "Mr. Kulkarni was Chief Financial Officer of Amber Enterprises India Limited from 2019 to 2026 and spent eleven "
             "years at Havells India Limited (2008-2019), latterly as Head of Finance. He is a Chartered Accountant and a "
             "commerce graduate of Shri Ram College of Commerce (2004).",
     "profile": {"name": "Arvind Kulkarni", "joining": True,
                 "experience": [{"org": "Havells India", "role": "Head of Finance", "from": 2008, "to": 2019},
                                {"org": "Amber Enterprises", "role": "Chief Financial Officer", "from": 2019, "to": 2026},
                                {"org": "Dixon Technologies", "role": "Chief Financial Officer", "from": 2026, "to": None}],
                 "education": [{"school": "SRCC", "degree": "B.Com", "from": 2001, "to": 2004},
                               {"school": "ICAI", "degree": "CA", "from": 2004, "to": 2007}]}},
    {"id": "demo-piind-ma", "symbol": "PIIND", "h": 5.5, "desc": "Acquisition",
     "text": "PI Industries Limited has informed the Exchange that its wholly owned subsidiary has signed a definitive "
             "agreement to acquire 100% of a European crop-protection custom-synthesis (CRAMS) company for an enterprise "
             "value of EUR 180 million, funded through internal accruals. Completion is expected in Q4 FY27."},
    {"id": "demo-polycab-results", "symbol": "POLYCAB", "h": 7.0, "desc": "Financial Result Updates",
     "text": "Polycab India Limited has informed the Exchange regarding Unaudited Financial Results (Standalone and "
             "Consolidated) for the quarter and half year ended September 30, 2026."},
    {"id": "demo-deepak-lit", "symbol": "DEEPAKNTR", "h": 11.0, "desc": "Action(s) taken or orders passed",
     "text": "Deepak Nitrite Limited has informed the Exchange that the Gujarat Pollution Control Board has issued a "
             "closure direction to one production block at its Dahej unit pending compliance with effluent norms. The "
             "block contributed about 6% of FY26 consolidated revenue. The Company is filing an appeal."},
    {"id": "demo-chola-md", "symbol": "CHOLAFIN", "h": 20.0, "desc": "Change in Management",
     "text": "Cholamandalam Investment and Finance Company Limited has informed the Exchange about appointment of "
             "Mr. Suresh Venkataraman as Managing Director, subject to RBI approval. He was Executive Director at Shriram "
             "Finance (2014-2026) and earlier Regional Head at Cholamandalam (2002-2014). He is an alumnus of IIM Bangalore (2002).",
     "profile": {"name": "Suresh Venkataraman", "joining": True,
                 "experience": [{"org": "Cholamandalam Investment and Finance", "role": "Regional Head", "from": 2002, "to": 2014},
                                {"org": "Shriram Finance", "role": "Executive Director", "from": 2014, "to": 2026}],
                 "education": [{"school": "IIM Bangalore", "degree": "PGP", "from": 2000, "to": 2002}]}},
]

ROUTINE = [  # only injected when there are no live filings at all
    ("HDFCBANK", 3, "Trading Window-XBRL", "HDFC Bank Limited has informed the Exchange about closure of trading window."),
    ("INFY", 4, "Copy of Newspaper Publication", "Infosys Limited has informed the Exchange about copy of newspaper publication."),
    ("ITC", 6, "Analysts/Institutional Investor Meet/Con. Call Updates", "ITC Limited has informed the Exchange about schedule of analyst / institutional investor meet."),
    ("LT", 8, "Certificate under SEBI (Depositories and Participants) Regulations, 2018", "Larsen & Toubro Limited has informed the Exchange about certificate under Reg. 74(5)."),
    ("TITAN", 9, "ESOP/ESOS/ESPS", "Titan Company Limited has informed the Exchange about allotment of equity shares under ESOS."),
    ("MARUTI", 13, "Postal Ballot", "Maruti Suzuki India Limited has informed the Exchange about notice of postal ballot."),
    ("SUNPHARMA", 15, "Board Meeting Intimation", "Sun Pharmaceutical Industries Limited has informed the Exchange that the board will meet to consider results."),
    ("BAJFINANCE", 16, "Updates", "Bajaj Finance Limited has informed the Exchange about allotment of non-convertible debentures on private placement basis."),
    ("ETERNAL", 18, "Credit Rating", "Eternal Limited has informed the Exchange that CRISIL has reaffirmed its long-term rating at AA with stable outlook."),
    ("TRENT", 10, "Press Release", "Trent Limited has informed the Exchange about launch of 14 new Zudio stores including first stores in two new states."),
    ("KAYNES", 12, "Insider Trading - SEBI (PIT)", "Kaynes Technology India Limited has informed the Exchange about disclosure of sale of shares by a promoter group entity."),
    ("BHARTIARTL", 21, "Analysts/Institutional Investor Meet/Con. Call Updates", "Bharti Airtel Limited has informed the Exchange about transcript of the earnings conference call."),
    ("RELIANCE", 22, "Updates", "Reliance Industries Limited has informed the Exchange about intimation regarding loss of share certificates."),
    ("HDFCBANK", 30, "Shareholders meeting", "HDFC Bank Limited has informed the Exchange about scrutinizer's report for the EGM."),
    ("INFY", 40, "Updates", "Infosys Limited has informed the Exchange about compliance certificate under Regulation 74(5)."),
]


def seed_filings(include_routine: bool, anchor: datetime | None = None):
    ANCHOR[0] = anchor or datetime.now()
    rows = []
    for s in SHOWCASE:
        extra = {"profile": s["profile"]} if s.get("profile") else {}
        rows.append((s["id"], s["symbol"], _ago(s["h"]), s["text"][:160], s["desc"], s["text"], "",
                     None, None, None, json.dumps(extra), 1))
    if include_routine:
        for i, (sym, h, desc, text) in enumerate(ROUTINE):
            rows.append((f"demo-r{i:02d}", sym, _ago(h), text[:160], desc, text, "", None, None, None, "{}", 1))
    with db.conn() as c:
        c.execute("DELETE FROM filings WHERE demo=1")
        c.executemany("INSERT OR REPLACE INTO filings (id, symbol, ts, subject, exchange_desc, text, url, category,"
                      " cls_method, summary, extra, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def synthetic_prices(symbols, days=90):
    """Seeded random walks with three injected 'unusual' days for the alert demo."""
    rng = random.Random(7)
    shocks = {"DIXON": (6.2, 3.1), "PIIND": (-4.8, 2.4), "KAYNES": (1.1, 2.3)}
    rows = []
    start = datetime.now().date() - timedelta(days=int(days * 1.45))
    dates = [start + timedelta(days=i) for i in range(int(days * 1.45)) if (start + timedelta(days=i)).weekday() < 5][-days:]
    for s in symbols:
        px, vol0 = rng.uniform(300, 4000), rng.uniform(4e5, 6e6)
        for i, d in enumerate(dates):
            ret, vm = rng.gauss(0.0004, 0.013), rng.uniform(0.7, 1.3)
            if i == len(dates) - 1 and s in shocks:
                ret, vm = shocks[s][0] / 100, shocks[s][1]
            px *= 1 + ret
            rows.append((s, d.isoformat(), round(px, 2), round(vol0 * vm)))
    with db.conn() as c:
        c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)", rows)
    db.kv_set("prices_synthetic", True)


# ---------------------------------------------------------------- DEMOCHEM -----
def _q_labels(n, last=(2026, 9)):
    y, m = last
    out = []
    for _ in range(n):
        out.append(datetime(y, m, 1).strftime("%b %Y"))
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    return out[::-1]


def _screener_html(name, about, industry, quarters, annual, bs, cf, ratios, top, concalls, ars):
    def table(sec, periods, rows):
        head = "".join(f"<th>{p}</th>" for p in periods)
        body = "".join(f"<tr><td class='text'>{k} <button>+</button></td>" + "".join(
            f"<td>{'' if v is None else f'{v:,.1f}' if isinstance(v, float) else v}</td>" for v in vals) + "</tr>"
                       for k, vals in rows.items())
        return f"<section id='{sec}'><table class='data-table'><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table></section>"

    lis = "".join(f"<li><span class='name'>{k}</span><span class='value'><span class='number'>{v}</span></span></li>" for k, v in top.items())
    cc = "".join(f"<li><div>{c['date']}</div><a href='{c['transcript']}'>Transcript</a><a href='{c.get('ppt','')}'>PPT</a></li>" for c in concalls)
    ar = "".join(f"<li><a href='{a['url']}'>{a['title']}</a></li>" for a in ars)
    ind = "".join(f"<a href='/market/IN07/{i}/'>{i}</a>" for i in industry)
    return (f"<html><body><h1>{name}</h1><div class='company-profile'><div class='about'><p>{about}</p></div></div>"
            f"<ul id='top-ratios'>{lis}</ul>"
            + table("quarters", *quarters) + table("profit-loss", *annual) + table("balance-sheet", *bs)
            + table("cash-flow", *cf) + table("ratios", *ratios)
            + f"<section id='peers'>{ind}</section>"
            + f"<section id='documents'><div class='annual-reports'><ul>{ar}</ul></div><div class='concalls'><ul>{cc}</ul></div></section>"
            + "</body></html>")


def demochem():
    """A fictional specialty-chemicals company whose management keeps cutting guidance."""
    qp = _q_labels(13)
    qs = [410, 425, 440, 470, 455, 480, 500, 540, 505, 520, 545, 590, 540, 560][-13:]
    qopm = [19, 19.5, 20, 20.5, 19, 20, 20, 21, 17, 16.5, 17, 18, 16, 15.5][-13:]
    q_rows = {"Sales": [float(x) for x in qs], "Expenses": [round(s * (1 - o / 100), 1) for s, o in zip(qs, qopm)],
              "Operating Profit": [round(s * o / 100, 1) for s, o in zip(qs, qopm)], "OPM %": qopm,
              "Other Income": [6.0] * 13, "Interest": [round(4 + i * 0.6, 1) for i in range(13)],
              "Depreciation": [round(18 + i * 0.8, 1) for i in range(13)]}
    q_rows["Profit before tax"] = [round(op + 6 - it - dp, 1) for op, it, dp in zip(q_rows["Operating Profit"], q_rows["Interest"], q_rows["Depreciation"])]
    q_rows["Tax %"] = [25] * 13
    q_rows["Net Profit"] = [round(p * 0.75, 1) for p in q_rows["Profit before tax"]]

    ap = [f"Mar {y}" for y in range(2019, 2027)] + ["TTM"]
    sales = [980, 1050, 1180, 1420, 1610, 1745, 1975, 2160, 2235]
    opm = [18, 19, 21, 22, 20, 19, 20, 17.1, 16.6]
    op = [round(s * o / 100, 1) for s, o in zip(sales, opm)]
    npf = [round(x * 0.62, 1) for x in op]
    a_rows = {"Sales": [float(s) for s in sales], "Operating Profit": op, "OPM %": opm, "Net Profit": npf,
              "EPS in Rs": [round(n / 5, 1) for n in npf], "Dividend Payout %": [20] * 9}

    bp = ap[:-1]
    borrow = [120, 110, 100, 140, 260, 380, 520, 640]
    reserves = [600, 700, 830, 1000, 1180, 1360, 1580, 1800]
    bs_rows = {"Equity Capital": [50.0] * 8, "Reserves": [float(r) for r in reserves], "Borrowings": [float(b) for b in borrow],
               "Total Liabilities": [float(50 + r + b + 300) for r, b in zip(reserves, borrow)]}
    conv = [1.02, 0.97, 0.92, 0.86, 0.74, 0.66, 0.6, 0.55]
    cfo = [round(n * c, 1) for n, c in zip(npf[:-1], conv)]
    cf_rows = {"Cash from Operating Activity": cfo, "Cash from Investing Activity": [-90, -95, -120, -210, -330, -360, -400, -420],
               "Cash from Financing Activity": [-20, -40, -30, 30, 110, 100, 120, 110]}
    dd, inv, dp = [70, 72, 74, 78, 84, 88, 92, 98], [80, 82, 85, 90, 96, 102, 108, 115], [60, 60, 58, 58, 57, 56, 55, 55]
    r_rows = {"Debtor Days": dd, "Inventory Days": inv, "Days Payable": dp,
              "Cash Conversion Cycle": [a + b - c for a, b, c in zip(dd, inv, dp)], "ROCE %": [24, 25, 27, 28, 23, 20, 19, 15]}
    calls = [{"date": d, "transcript": f"fixture://demochem/{d.replace(' ', '-').lower()}",
              "ppt": f"fixture://demochem/ppt-{d.replace(' ', '-').lower()}"}
             for d in ["Nov 2026", "Aug 2026", "May 2026", "Feb 2026"]]
    html = _screener_html(
        "Demo Specialty Chemicals Ltd",
        "FICTIONAL COMPANY FOR OFFLINE DEMO. Manufactures fluorinated intermediates and agrochemical actives "
        "for global innovators, with a contract-manufacturing (CRAMS) arm and a bulk fluorochemicals business.",
        ["Chemicals", "Specialty Chemicals"],
        (qp, q_rows), (ap, a_rows), (bp, bs_rows), (bp, cf_rows), (bp, r_rows),
        {"Market Cap": "18,400", "Current Price": "3,680", "Stock P/E": "42.5", "Book Value": "370",
         "Dividend Yield": "0.4", "ROCE": "15.2", "ROE": "13.1"}, calls,
        [{"title": "Financial Year 2026 from bse", "url": "fixture://demochem/ar-2026"}])
    d = fundamentals.parse(html, "DEMOCHEM")
    d["basis"], d["synthetic"] = "consolidated", True
    fundamentals.store("DEMOCHEM", d)

    tx = {
        "feb-2026": ["Good evening and welcome to the Q3 FY26 earnings call of Demo Specialty Chemicals.",
                     "Revenue for the quarter grew 9% year on year to Rs 545 crore.",
                     "For FY26 we will close with revenue growth of around 10%, as guided last quarter.",
                     "Analyst: What is driving the margin decline in fluorochemicals and when does it bottom out?",
                     "Looking ahead, for FY27 we expect revenue growth of 18 to 20% as the new fluorination block ramps up.",
                     "We are confident EBITDA margin should recover to 20% in FY27 as pricing normalises."],
        "may-2026": ["Welcome to the Q4 FY26 earnings call.", "FY26 revenue grew 9.4% to Rs 2,160 crore.",
                     "We reiterate our FY27 guidance of 18 to 20% revenue growth with EBITDA margins of 19 to 20%.",
                     "The Rs 350 crore fluorination block capex will be commissioned by June 2026.",
                     "Analyst: Is the 18 to 20% growth target dependent on the new block?"],
        "aug-2026": ["Welcome to the Q1 FY27 earnings call.",
                     "Given delays in customer validation, we now expect FY27 revenue growth of 14 to 16%.",
                     "We expect EBITDA margin of 18 to 19% for FY27, with the second half stronger.",
                     "The fluorination block commissioning has moved to Q3 FY27 and capex remains at Rs 350 crore."],
        "nov-2026": ["Welcome to the Q2 FY27 earnings call.",
                     "Revenue for the half grew 7% year on year; Chinese pricing remains aggressive.",
                     "For FY27 we now expect revenue growth of around 12% and EBITDA margin of 17 to 18%.",
                     "Analyst: This is the third cut this year; what gives you confidence in the second half?",
                     "We expect working capital to normalise going forward as inventory of the old block is liquidated."],
    }
    for k, lines in tx.items():
        pages = [" ".join(lines[:3]), " ".join(lines[3:])]
        db.kv_set(f"doc:fixture://demochem/{k}", pages)
    ppt = ["Business mix FY26: Fluorinated intermediates 46% of revenue (+14% YoY); CRAMS 31% (+6% YoY); "
           "Bulk fluorochemicals 23% (-4% YoY). Segment contribution shown on page 2."]
    for c in calls:
        db.kv_set(f"doc:{c['ppt']}", ppt)


def sample_results_fundamentals(symbol):
    """Offline only: plausible quarterly table so the results card renders. Flagged synthetic."""
    if fundamentals.get(symbol):
        return
    rng = random.Random(symbol)
    qp = _q_labels(9)
    base = rng.uniform(4000, 6000)
    sales = [round(base * (1.035 ** i) * (1 + rng.uniform(-0.03, 0.03)), 1) for i in range(9)]
    opm = [round(rng.uniform(12, 14.5), 1) for _ in range(9)]
    op = [round(s * o / 100, 1) for s, o in zip(sales, opm)]
    pbt = [round(o * rng.uniform(0.84, 0.92), 1) for o in op]
    d = {"symbol": symbol, "name": symbol, "about": "", "ratios": {}, "industry": [],
         "quarters": {"periods": qp, "rows": {"Sales": sales, "Operating Profit": op, "OPM %": opm,
                                              "Profit before tax": pbt, "Net Profit": [round(p * rng.uniform(0.73, 0.77), 1) for p in pbt]}},
         "documents": {}, "fetched_at": "1970-01-01T00:00:00", "basis": "consolidated", "synthetic": True}
    fundamentals.store(symbol, d)
