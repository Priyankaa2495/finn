"""Pull: on-demand research for any NSE symbol, grounded only in primary sources.

  Layer 1 — deterministic (always on, free): snapshot, trajectory, balance sheet,
            cash quality from the standardised statements.
  Layer 2 — narrative vs numbers from concall transcripts (guidance.py).
  Layer 3 — business mix + bull/bear: the model receives only Layer 1-2 facts and
            the company's own investor-presentation pages; no web, no broker notes.
  Plus the Network: who in my network can help me understand this company.
"""
import re
import threading
from datetime import date

from rapidfuzz import fuzz

from .. import db, llm
from ..config import LLM_ENABLED
from ..ingest import fundamentals
from ..network import graph
from ..network.resolve import org_index
from . import guidance, metrics


def _business_mix(d):
    """Segments / revenue mix from the latest investor presentation (company's own deck)."""
    calls = (d.get("documents") or {}).get("concalls", [])
    ppt = next((c for c in calls if c.get("ppt")), None)
    if not (LLM_ENABLED and ppt):
        return None
    pages = fundamentals.document_text(ppt["ppt"], max_pages=30) or []
    rel = []
    for i, t in enumerate(pages, 1):
        if re.search(r"segment|revenue mix|business mix|vertical|division|contribution", t or "", re.I):
            rel.append("(p%d) %s" % (i, re.sub(r"\s+", " ", t)[:1500]))
    rel = rel[:6]
    if not rel:
        return None
    out = llm.complete(
        "From these investor-presentation pages, extract the company's business segments and revenue "
        "share. JSON {\"period\": str, \"segments\": [{\"name\": str, \"share_pct\": number|null, "
        "\"growth_pct\": number|null, \"page\": int}]}. Only numbers printed on the pages.\n\n" + llm.untrusted("\n".join(rel), 9000),
        tier="fast")
    if out:
        out["source"] = {"type": "Investor presentation", "date": ppt["date"], "url": ppt["ppt"]}
    return out


def _bull_bear(name, facts):
    if LLM_ENABLED and facts:
        listing = "\n".join(f"[{i}] {f}" for i, f in enumerate(facts))
        out = llm.complete(
            f"Company: {name}. Using ONLY these numbered facts derived from its own filings and "
            f"transcripts, write a tight bull case and bear case (3 points each, <=18 words). "
            f"Each point must cite fact ids. No outside knowledge.\n{listing}\n\n"
            "JSON {\"bull\": [{\"point\": str, \"facts\": [int]}], \"bear\": [...]}",
            tier="deep", system="You are a sceptical buy-side analyst. Never introduce a number not in the facts.")
        if out and out.get("bull"):
            dropped = 0
            for side in ("bull", "bear"):
                kept = []
                for p in out.get(side, []) or []:
                    if isinstance(p, dict) and supported(p.get("point", ""), p.get("facts", []), facts):
                        kept.append(p)
                    else:
                        dropped += 1
                out[side] = kept
            out["method"], out["dropped_unsupported"] = "llm:deep", dropped
            return out
    return None


def supported(point, ids, facts):
    """A bull/bear point survives only if it cites real facts AND every number it states
    appears in those facts (to the precision written: '59%' is supported by '58.9%')."""
    ids = [i for i in (ids or []) if isinstance(i, int) and 0 <= i < len(facts)]
    if not ids:
        return False
    cited = [float(x.replace(",", "")) for i in ids for x in re.findall(r"\d[\d,]*(?:\.\d+)?", facts[i])]
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", point or ""):
        v = float(raw.replace(",", ""))
        tol = 0.5 * 10 ** -(len(raw.split(".")[1]) if "." in raw else 0)
        if not any(abs(v - c) <= tol + 1e-9 for c in cited):
            return False
    return True


def _org_for(symbol, d):
    idx = org_index()
    oid = idx.by_ticker.get(symbol)
    if oid:
        return oid, None
    oid, conf = idx.resolve(d.get("name", ""))
    if oid:
        return oid, None
    # Unknown company: map screener's industry labels onto the themes our network knows
    themes = set()
    for label in d.get("industry", []):
        for t in graph.all_themes():
            if fuzz.token_set_ratio(label.lower(), t) >= 80:
                themes.add(t)
    return None, sorted(themes)


NSE_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&-]{0,19}")  # e.g. M&M, BAJAJ-AUTO, 3MINDIA


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def research(symbol: str):
    symbol = (symbol or "").upper().strip()
    if not NSE_SYMBOL.fullmatch(symbol):  # validate before it becomes part of an outbound URL
        return {"symbol": symbol[:24], "error": "That isn't an NSE symbol (letters, digits, & or -, up to 20)."}
    # A first look scrapes screener, downloads 4 transcripts and may call the model (~20s).
    # A double tap must not do that twice: the second request waits, then reads the cache.
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(symbol, threading.Lock())
    with lock:
        return _research(symbol)


def _research(symbol: str):
    cache_key = f"research:{symbol}:{date.today().isoformat()}"
    cached = db.kv_get(cache_key)
    if cached:
        return cached
    d = fundamentals.fetch(symbol)
    if not d:
        return {"symbol": symbol, "error": f"No data found for {symbol}. Is it a valid NSE symbol?"}

    snap = metrics.snapshot(d)
    traj = metrics.trajectory(d)
    bs = metrics.balance_sheet(d)
    cash = metrics.cash_quality(d)
    g = guidance.analyse(d)
    mix = _business_mix(d)

    # Facts pack for the bull/bear: only things we computed or quoted
    facts = []
    for sec in (traj, bs, cash):
        for sign, text in (sec or {}).get("flags", []):
            facts.append(text)
    for it in g["items"][:8]:
        # Only outcomes that were actually checked are facts. "Not checkable" notes made the
        # model write bear points like "capacity claims are not checkable" (live DIXON).
        checked = it["check"]["status"] in ("delivered", "on_track", "close", "slightly_behind", "behind", "missed")
        facts.append(f"Guidance ({it['call']}): \"{it['quote'][:200]}\""
                     + (f" — {it['check'].get('detail', '')} ({it['check']['status']})" if checked else ""))
    for dr in g["drift"]:
        facts.append(f"{dr['metric']} guidance for {dr['fy']} was {dr['trend']} across calls")
    if snap.get("pe"):
        facts.append(f"Trades at {snap['pe']}x earnings; ROCE {snap.get('roce')}%, ROE {snap.get('roe')}%")
    bb = _bull_bear(d.get("name", symbol), facts) or {
        "bull": [{"point": t} for sec in (traj, bs, cash) for s, t in (sec or {}).get("flags", []) if s == "+"][:3],
        "bear": [{"point": t} for sec in (traj, bs, cash) for s, t in (sec or {}).get("flags", []) if s == "-"][:3],
        "method": "rules"}

    oid, themes = _org_for(symbol, d)
    people = graph.who_to_call(oid, limit=4) if oid else graph.who_to_call(symbol.lower(), limit=4, themes=themes)

    out = {
        "symbol": symbol, "name": d.get("name") or symbol, "basis": d.get("basis"),
        "fetched_at": d.get("fetched_at"), "synthetic": d.get("synthetic", False),
        "about": d.get("about", ""), "industry": d.get("industry", []),
        "snapshot": snap, "mix": mix, "trajectory": traj, "quarterly": metrics.quarterly(d),
        "balance_sheet": bs, "cash": cash, "guidance": g, "bull_bear": bb,
        "people": people,
        "sources": [{"type": "Standardised financials", "url": f"https://www.screener.in/company/{symbol}/"}]
                   + g["sources"] + ([mix["source"]] if mix and mix.get("source") else []),
        "llm": LLM_ENABLED,
    }
    db.kv_set(cache_key, out)
    return out
