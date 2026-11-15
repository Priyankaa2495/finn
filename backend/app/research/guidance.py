"""Narrative vs numbers: what management promised on the last four calls vs what the
reported quarters delivered.

Pipeline (cheapest step first):
  1. Download the last 4 concall transcripts (primary source), cache text forever.
  2. Pre-filter to forward-looking sentences about headline metrics with a regex.
     The model sees at most 40 candidate sentences, never the whole transcript.
  3. The fast model *selects and structures* candidates by ID. It never writes the
     quote itself; we attach our own sentence text by ID. So every quote is verbatim
     from the transcript by construction, and carries its page number.
  4. Targets are parsed and checked against reported quarters in plain Python
     (revenue growth, EBITDA margin). Only what can't be checked mechanically goes
     to the deep model, which receives the computed facts and nothing else.
  5. Guidance is tracked across calls: raised / maintained / cut ('guidance drift').
"""
import re
from datetime import datetime

from .. import llm
from ..config import LLM_ENABLED
from ..ingest import fundamentals

FWD = re.compile(r"\b(guid\w*|expect\w*|target\w*|aim\w*|outlook|we (will|should|would|are confident)|going forward|"
                 r"next (year|fiscal|quarter)|fy ?'?2\d|fy ?20\d\d|by (the end of )?fy|coming (year|quarters?)|"
                 r"double[- ]digit|sustain\w*|maintain\w*)\b", re.I)
METRIC = re.compile(r"\b(revenue|top ?line|sales|growth|margin|ebitda|capex|capital expenditure|volume|"
                    r"utili[sz]ation|order book|debt|bottom ?line|pat|profit)\b", re.I)
PAST = re.compile(r"\b(grew|was|were|reported|delivered|achieved|declined|rose|fell|posted|has been|have been|"
                  r"we had|had finished|finished the|continues to deliver|last (quarter|year)|declin(?:ed|ing))\b", re.I)
FWD_VERB = re.compile(r"\b(expect\w*|guid\w*|target\w*|will|should|aim\w*|going forward|confident)\b", re.I)
# Analysts paraphrase guidance back to management; that is not management giving it.
ASKING = re.compile(r"\byou(?: have| had|'ve)? (?:guided|said|mentioned|given|indicated)\b|\byour (?:guidance|target|ambition)\b|"
                    r"\b(?:could|can|would) you\b|\bjust (?:want|wanted|to understand)\b|\bclarification\b|"
                    r"\bif i recall\b|\bwhen you (?:are )?say|\bhow (?:are|do|should) (?:we|you)\b|\bmy question\b|"
                    r"\bwanted to (?:ask|understand)\b|^sir\b|\byou (?:have )?(?:taken|made)\b|\bif i may\b|"
                    r"\bwhen i look at\b|\bin your\b", re.I)
SEQUENTIAL = re.compile(r"quarter[- ]on[- ]quarter|sequential(ly)?|\bq-?o-?q\b|quarter \d (?:against|over|vs\.?) quarter \d", re.I)
ABSOLUTE = re.compile(r"(?:inr|rs\.?|₹)\s?[\d,]{3,}|[\d,]{3,}\s?(?:crores?|cr\b)", re.I)
MULTIPLE = re.compile(r"\b\d(?:\.\d)?\s?x\b|\b(?:double|triple)\b(?!-digit|\sdigit)", re.I)
SEGMENT = re.compile(r"\b(?:segments?|verticals?|divisions?|jv|joint venture|subsidiar(?:y|ies))\b|"
                     r"\b(?!our\b)[a-z]+(?: phone)? business\b", re.I)
_PCT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|per ?cent|percentage)", re.I)
_KINDS = [("pat_margin", r"(?:pat|net|profit) margin"),  # not operating margin; never graded against OPM
          ("ebitda_margin", r"(?<!pat )(?<!net )(?<!profit )margin|ebitda"), ("capex", r"capex|capital expenditure"),
          ("volume", r"volume"), ("utilisation", r"utili[sz]ation"),
          ("revenue_growth", r"revenue|top ?line|sales|(?<!volume )grow")]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _sentences(pages):
    out = []
    for pno, txt in enumerate(pages, 1):
        txt = re.sub(r"\s+", " ", txt or "")
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", txt):
            s = s.strip()
            if 40 <= len(s) <= 420:
                out.append((pno, s))
    return out


def candidates(pages, limit=40):
    """Forward-looking statements about headline metrics, ranked by how 'guidance-y'."""
    scored = []
    for pno, s in _sentences(pages):
        if not (FWD.search(s) and METRIC.search(s)):
            continue
        if s.rstrip().endswith("?") or re.match(r"^(analyst|participant|moderator|question)\b", s, re.I) \
                or ASKING.search(s) or not re.search(r"[.!]\W*$", s):
            continue  # analysts asking about a target is not management giving one; fragments cut at a page break
        if PAST.search(s) and not FWD_VERB.search(s):
            continue  # 'revenue grew 9%' is a result, not a promise
        sc = len(FWD.findall(s)) + len(METRIC.findall(s)) + (2 if re.search(r"\d+\s*(%|per ?cent|bps)", s, re.I) else 0)
        scored.append((sc, pno, s))
    scored.sort(key=lambda x: -x[0])
    return [{"id": i, "page": p, "text": s} for i, (_, p, s) in enumerate(scored[:limit])]


def metric_kind(text):
    """The metric the target number belongs to: the keyword nearest the first percentage.
    ('80-90% capacity utilisation ... with a double-digit margin' is a utilisation figure,
    not a margin target; 'volume growth of 8-9%' is not revenue growth.)"""
    t = text.lower()
    pct = _PCT.search(t)
    if not pct:
        for kind, pat in _KINDS:
            if re.search(pat, t):
                return kind
        return "capex" if re.search(r"\binvest", t) else "other"
    best, dist = "other", None
    for kind, pat in _KINDS:
        for m in re.finditer(pat, t):
            d = min(abs(m.start() - pct.start()), abs(m.end() - pct.end()))
            if dist is None or d < dist:
                best, dist = kind, d
    return best


def _numbers(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")]


def grounded(value, text):
    """A model-supplied number counts only if the quoted text contains it."""
    return value is None or any(abs(value - n) < 1e-6 for n in _numbers(text))


def parse_target(text):
    t = text.lower().replace("per cent", "%").replace("percent", "%")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%)?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1)), float(m.group(1))
    if "double digit" in t or "double-digit" in t:
        return 10.0, None
    return None, None


def call_fy(call_date: str):
    """'Nov 2025' -> the call discusses the quarter ending Sep 2025, fiscal year FY26."""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{4})", call_date or "")
    if not m:
        return None
    mon, yr = MONTHS.get(m.group(1).lower(), 1), int(m.group(2))
    q_mon, q_yr = (mon - 2, yr) if mon > 2 else (mon + 10, yr - 1)  # results lag ~1-2 months
    return (q_yr + 1 if q_mon >= 4 else q_yr) % 100


def horizon(text, call_date):
    t = text.lower()
    m = re.search(r"fy\s?'?(20)?(\d{2})\b", t)
    if m:
        return int(m.group(2))
    base = call_fy(call_date)
    if base is None:
        return None
    if re.search(r"next (year|fiscal)", t):
        return base + 1
    return base


def _fy_of(period):  # 'Sep 2025' -> 26
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{4})", period)
    mon, yr = MONTHS.get(m.group(1).lower(), 1), int(m.group(2))
    return (yr + 1 if mon >= 4 else yr) % 100


def check(item, q):
    """Deterministic promised-vs-delivered for the two metrics a calculator can judge."""
    kind, (lo, hi), fy = item["metric"], item["target"], item["fy"]
    quote = item.get("quote", "")
    if kind == "revenue_growth" and SEQUENTIAL.search(quote):
        return {"status": "judgement", "detail": "Sequential (QoQ) target; not comparable with annual growth"}
    # Found on live DIXON with the model on: these were graded mechanically and came out wrong.
    if kind == "revenue_growth" and lo is not None and (lo > 100 or ABSOLUTE.search(quote)):
        return {"status": "judgement", "detail": "An absolute revenue target (₹), not a growth rate"}
    if MULTIPLE.search(quote):
        return {"status": "judgement", "detail": "A multiple-of target (2x, 3x); not checked mechanically"}
    if SEGMENT.search(quote):
        return {"status": "judgement", "detail": "A segment-level target; the reported numbers are company-wide"}
    if not q or fy is None or lo is None or kind not in ("revenue_growth", "ebitda_margin"):
        return {"status": "judgement", "detail": "Not mechanically checkable"}
    periods = q["periods"]
    idx = [i for i, p in enumerate(periods) if _fy_of(p) == fy]
    if not idx:
        return {"status": "too_early", "detail": f"No FY{fy} quarters reported yet"}
    n = len(idx)
    if kind == "revenue_growth":
        cur = [q["revenue"][i] for i in idx]
        prev = [q["revenue"][i - 4] if i >= 4 else None for i in idx]
        if None in cur or None in prev or not sum(prev):
            return {"status": "judgement", "detail": "Not enough quarterly history"}
        actual = round((sum(cur) / sum(prev) - 1) * 100, 1)
        what = f"Revenue {actual:+}% YoY over {n} reported quarter{'s' if n > 1 else ''} of FY{fy}"
    else:
        vals = [q["opm"][i] for i in idx if q["opm"] and q["opm"][i] is not None]
        if not vals:
            return {"status": "judgement", "detail": "No margin data"}
        actual = round(sum(vals) / len(vals), 1)
        what = f"Operating margin averaged {actual}% over {n} reported quarter{'s' if n > 1 else ''} of FY{fy}"
    done = n == 4
    # 'close' band: within 15% of a growth target, or within 1pp of a margin target
    close_floor = lo * 0.85 if kind == "revenue_growth" else lo - 1.0
    if actual >= lo - 0.25:
        st = "delivered" if done else "on_track"
    elif actual >= close_floor:
        st = "close" if done else "slightly_behind"
    else:
        st = "missed" if done else "behind"
    return {"status": st, "actual": actual, "detail": what}


def _heuristic_pick(cands, call):
    items = []
    for c in cands:
        k = metric_kind(c["text"])
        lo, hi = parse_target(c["text"])
        if k in ("other", "utilisation") or (lo is None and k != "capex"):
            continue
        items.append({"metric": k, "statement": c["text"][:200], "quote": c["text"], "page": c["page"],
                      "target": [lo, hi], "fy": horizon(c["text"], call["date"]), "method": "heuristic"})
        if len(items) >= 5:
            break
    return items


def extract_call(call):
    pages = fundamentals.document_text(call.get("transcript"))
    if not pages:
        return None
    cands = candidates(pages)
    if not cands:
        return []
    if LLM_ENABLED:
        listing = "\n".join(f"[{c['id']}] (p{c['page']}) {c['text']}" for c in cands)
        out = llm.complete(
            f"Concall date: {call['date']}. Below are numbered sentences from the transcript.\n"
            "Pick the ones where management gives EXPLICIT forward guidance on a headline number "
            "(revenue growth, EBITDA margin, capex, volumes, order book). Ignore analyst questions, "
            "history and vague optimism. Return JSON {\"items\": [{\"id\": int, \"metric\": "
            "\"revenue_growth|ebitda_margin|capex|volume|other\", \"statement\": \"<=15-word paraphrase\", "
            "\"target_low\": number|null, \"target_high\": number|null, \"fiscal_year\": 2-digit int|null}]}.\n\n"
            + listing, tier="fast",
            system="You extract management guidance from earnings-call transcripts. Precision over recall.")
        if out and isinstance(out.get("items"), list):
            by_id = {c["id"]: c for c in cands}
            items = []
            for it in out["items"]:
                c = by_id.get(it.get("id"))
                if not c:  # model referenced a sentence that doesn't exist -> dropped
                    continue
                lo, hi = it.get("target_low"), it.get("target_high")
                if not (grounded(lo, c["text"]) and grounded(hi, c["text"])):
                    lo = hi = None  # the model's number isn't in the quote: don't grade management on it
                if lo is None:
                    lo, hi = parse_target(c["text"])
                items.append({"metric": it.get("metric") or metric_kind(c["text"]),
                              "statement": it.get("statement") or c["text"][:160],
                              "quote": c["text"], "page": c["page"], "target": [lo, hi],
                              "fy": it.get("fiscal_year") or horizon(c["text"], call["date"]),
                              "method": "llm:fast"})
            return items
    return _heuristic_pick(cands, call)


def analyse(d, max_calls=4):
    calls = [c for c in (d.get("documents") or {}).get("concalls", []) if c.get("transcript")][:max_calls]
    q = {"periods": [], "revenue": [], "opm": []}
    qt = d.get("quarters")
    if qt:
        rows = qt["rows"]
        q = {"periods": qt["periods"], "revenue": rows.get("Sales") or rows.get("Revenue"),
             "opm": rows.get("OPM %") or rows.get("Financing Margin %")}
    items, sources = [], []
    for call in calls:
        got = extract_call(call)
        if got is None:
            continue
        sources.append({"type": "Concall transcript", "date": call["date"], "url": call["transcript"]})
        for it in got:
            it["call"] = call["date"]
            it["check"] = check(it, q)
            items.append(it)

    # Guidance drift: same metric + same fiscal year across calls (oldest first)
    drift = {}
    for it in sorted(items, key=lambda x: _call_sort(x["call"])):
        if it["target"][0] is None or it["fy"] is None:
            continue
        seq = drift.setdefault(f"{it['metric']}|FY{it['fy']}", [])
        if seq and seq[-1]["call"] == it["call"]:  # one data point per call
            continue
        seq.append({"call": it["call"], "target": it["target"]})
    drift_out = []
    for k, seq in drift.items():
        if len(seq) < 2:
            continue
        a, b = seq[0]["target"][0], seq[-1]["target"][0]
        trend = "raised" if b > a else "cut" if b < a else "maintained"
        m, fy = k.split("|")
        drift_out.append({"metric": m, "fy": fy, "trend": trend, "history": seq})

    summary = None
    if LLM_ENABLED and items:
        facts = "\n".join(f"- [{i}] {it['call']} call, p{it['page']}: \"{it['quote']}\" -> "
                          f"check: {it['check'].get('detail')} ({it['check']['status']})"
                          for i, it in enumerate(items))
        qtab = ", ".join(f"{p}: rev {r}, OPM {o}%" for p, r, o in
                         zip(q["periods"][-8:], (q["revenue"] or [])[-8:], (q["opm"] or [])[-8:]))
        summary = llm.complete(
            "You are checking management credibility using ONLY the facts below (guidance quotes from "
            "their own concall transcripts, and reported quarterly numbers). No outside knowledge.\n"
            f"Reported quarters (₹ cr): {qtab}\n\nGuidance items:\n{facts}\n\n"
            "Return JSON {\"verdict\": \"one sentence on whether management's narrative matches the numbers\", "
            "\"judgements\": [{\"id\": int, \"status\": \"delivered|on_track|behind|missed|too_early|unclear\", "
            "\"reason\": \"<=20 words citing the numbers\"}]} — judgements only for items whose check is 'judgement'.",
            tier="deep")
        if summary:
            for j in summary.get("judgements", []) or []:
                i = j.get("id")
                if isinstance(i, int) and 0 <= i < len(items) and items[i]["check"]["status"] == "judgement":
                    items[i]["check"] = {"status": j.get("status", "unclear"), "detail": j.get("reason", ""),
                                         "method": "llm:deep"}
    return {"items": items, "drift": drift_out, "sources": sources,
            "verdict": (summary or {}).get("verdict") or _rule_verdict(items),
            "calls_found": len(calls)}


def _call_sort(c):
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{4})", c or "")
    return (int(m.group(2)), MONTHS.get(m.group(1).lower(), 0)) if m else (0, 0)


def _rule_verdict(items):
    checked = [i for i in items if i["check"]["status"] not in ("judgement", "too_early")]
    if not checked:
        return None
    good = sum(i["check"]["status"] in ("delivered", "on_track") for i in checked)
    v = f"{good} of {len(checked)} checkable guidance points were delivered or are on track against reported numbers."
    if all(i.get("method") == "heuristic" for i in checked):
        v = "Rough read (keyword extraction, no model): " + v[0].lower() + v[1:]
    return v
