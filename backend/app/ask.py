"""One input box for everything. Intent routing is rules-first:

  "DIXON" / "research navin fluorine"         -> research
  "who should I call about Polycab"           -> who_to_call
  "who knows the new Chola MD" / a person     -> path / diligence
  "experts in specialty chemicals"            -> expertise
  "blind spots" / "coverage"                  -> coverage

Rules handle the phrasings an investor actually types; the fast model is only asked
when the rules can't find an entity, and it may only answer with a known intent."""
import json
import re

from rapidfuzz import fuzz, process

from . import llm
from .config import DATA_DIR
from .network import graph
from .network.resolve import org_index
from .network.store import load as load_network

PORTFOLIO = json.loads((DATA_DIR / "portfolio.json").read_text())


def _find_org(q):
    idx = org_index()
    for tok in re.findall(r"\b[A-Z][A-Z0-9&]{2,}\b", q):  # typed tickers: DIXON, M&M
        if tok in idx.by_ticker:
            return idx.by_ticker[tok], tok
    words = re.sub(r"[^a-z0-9& ]", " ", q.lower())
    best = None
    for key, oid in idx.alias.items():
        if len(key) >= 3 and re.search(rf"\b{re.escape(key)}\b", words):
            if not best or len(key) > len(best[0]):
                best = (key, oid)
    if best:
        o = idx.get(best[1])
        return best[1], o.get("ticker")
    return None, None


def _find_person(q):
    net = load_network()
    pool = [(p["name"], p) for p in net["people"] + net.get("second_degree", [])]
    hit = process.extractOne(q, [n for n, _ in pool], scorer=fuzz.partial_ratio)
    if hit and hit[1] >= 88:
        return next(p for n, p in pool if n == hit[0])
    return None


def _find_theme(q):
    themes = graph.all_themes()
    hit = process.extractOne(q.lower(), themes, scorer=fuzz.partial_ratio)
    return hit[0] if hit and hit[1] >= 85 else None


def route(q: str, recent_people=None):
    q = (q or "").strip()
    ql = q.lower()
    if not q:
        return {"intent": "empty"}
    if re.search(r"blind ?spot|coverage|gaps? in my network|who don.?t i know", ql):
        return {"intent": "coverage"}
    if re.search(r"expert|go-to|go to people|who knows about|specialists?|theme", ql):
        t = _find_theme(re.sub(r"expert\w*|go-?to|people|for|in|on|who knows about|specialists?|theme", " ", ql))
        if t:
            return {"intent": "expertise", "theme": t}
    # people: from recent filings (new CFO etc.) or anyone in the network
    for p in recent_people or []:
        if fuzz.partial_ratio(p["name"].lower(), ql) >= 90:
            return {"intent": "diligence", "person": p}
    person = _find_person(q) if re.search(r"who|meet|prep|know|introduc|path", ql) else None
    if not person:  # a bare name: 'Neha Iyer'
        net = load_network()
        hit = process.extractOne(q, [p["name"] for p in net["people"] + net.get("second_degree", [])],
                                 scorer=fuzz.token_sort_ratio)
        if hit and hit[1] >= 90:
            person = _find_person(hit[0])
    if person:
        if person.get("known_by") is not None or person["id"].startswith("s"):
            return {"intent": "diligence", "person": person}
        return {"intent": "person", "id": person["id"]}
    oid, ticker = _find_org(q)
    if re.search(r"\b(who|call|talk|speak|ask|introduc|contacts?|network|knows?)\b", ql):
        if oid:
            return {"intent": "who_to_call", "org": oid, "symbol": ticker}
        t = _find_theme(ql)
        if t:
            return {"intent": "expertise", "theme": t}
    if ticker:
        return {"intent": "research", "symbol": ticker}
    hit = process.extractOne(ql, graph.all_themes(), scorer=fuzz.ratio)
    if hit and hit[1] >= 88:  # a bare theme: 'semiconductors'
        return {"intent": "expertise", "theme": hit[0]}
    m = re.fullmatch(r"(?:research\s+|analy[sz]e\s+)?([A-Za-z][A-Za-z0-9&-]{1,19})", q)
    if m:  # any NSE symbol, even ones we've never heard of
        return {"intent": "research", "symbol": m.group(1).upper()}
    t = _find_theme(ql)
    if t:
        return {"intent": "expertise", "theme": t}
    out = llm.complete(
        f"Investor typed: {q!r}. Choose intent from research|who_to_call|expertise|coverage and "
        "extract an NSE symbol or theme. JSON {\"intent\":..., \"symbol\": str|null, \"theme\": str|null}", tier="fast")
    if out and out.get("intent") in ("research", "who_to_call", "expertise", "coverage"):
        if out["intent"] == "research" and out.get("symbol"):
            return {"intent": "research", "symbol": out["symbol"].upper()}
        if out["intent"] == "expertise" and out.get("theme"):
            return {"intent": "expertise", "theme": out["theme"]}
        if out["intent"] == "coverage":
            return {"intent": "coverage"}
    return {"intent": "unknown"}
