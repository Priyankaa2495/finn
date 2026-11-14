"""Push: the windowed digest. Filings in the window -> classify -> score -> enrich the
few that matter -> group. Routine items are counted and collapsed, never deleted.

Enrichment is where the Network gets woven in:
  management_change -> who is this person, and who do I know who knows them
  results           -> Revenue/EBITDA/PBT/PAT, QoQ and YoY
  M&A / capex / litigation / fund_raise -> the person best placed to give context
"""
import json
import re
from datetime import datetime, timedelta

from .. import db, llm
from ..config import LLM_ENABLED
from ..network import graph
from ..network.resolve import org_index
from . import alerts, classify, materiality, results

LABEL = {
    "results": "Results", "M&A": "M&A", "fund_raise": "Fund raise", "management_change": "Management",
    "litigation": "Litigation", "insider_trading": "Insider trade", "earnings_call": "Earnings call",
    "investor_meetings": "Investor meet", "credit_rating": "Credit rating", "dividend": "Dividend",
    "bonus_split": "Bonus / split", "capex": "Capex", "press_release": "Press release",
    "postal_ballot": "Postal ballot", "agm_egm": "AGM / EGM", "pledging": "Pledging",
    "unclassified": "Routine filing",
}
CONTEXT_CATS = {"M&A", "capex", "litigation", "fund_raise", "pledging", "credit_rating"}


def _ensure_classified(f):
    if f.get("category"):
        return f
    cat, method, _ = classify.classify(f["exchange_desc"], f["text"])
    f["category"], f["cls_method"] = cat, method
    with db.conn() as c:
        c.execute("UPDATE filings SET category=?, cls_method=? WHERE id=?", (cat, method, f["id"]))
    return f


def _summary(f, tier):
    """Deterministic one-liner first; the fast model only rewrites it for items that
    made the cut (cached in the row, so each filing is summarised at most once)."""
    if f.get("summary"):
        return f["summary"]
    base = classify.clean_text(f["text"])
    s = base if len(base) <= 170 else base[:167].rsplit(" ", 1)[0] + "…"
    if tier != "routine" and LLM_ENABLED and len(base) > 170:
        out = llm.complete(f"Summarise this exchange filing in one plain sentence (max 22 words), "
                           f"facts only, keep any numbers:\n\n{f['text'][:2500]}", tier="fast", want_json=False)
        if out:
            s = out.strip().strip('"')
    with db.conn() as c:
        c.execute("UPDATE filings SET summary=? WHERE id=?", (s, f["id"]))
    return s


def _profile_from_filing(f, extra):
    """SEBI's disclosure rules require a 'brief profile' of an appointed director/KMP
    inside the filing itself — so the bio comes from a primary source. The fast model
    turns that paragraph into a structured career history (cached in the row)."""
    if extra.get("profile"):
        return extra["profile"]
    if not LLM_ENABLED:
        return None
    out = llm.complete(
        "From this Indian exchange filing about a management change, extract the person "
        "joining (or leaving) and their career as JSON: {\"name\": str, \"joining\": bool, "
        "\"experience\": [{\"org\", \"role\", \"from\": year|null, \"to\": year|null}], "
        "\"education\": [{\"school\", \"degree\", \"from\": year|null, \"to\": year|null}]}. "
        "Only use facts in the text.\n\n" + f["text"][:4000], tier="fast")
    if out and out.get("name"):
        extra["profile"] = out
        with db.conn() as c:
            c.execute("UPDATE filings SET extra=? WHERE id=?", (json.dumps(extra), f["id"]))
        return out
    return None


def _title(f, mgmt):
    cat = f["category"]
    if cat == "management_change" and mgmt.get("person"):
        role = mgmt.get("role") or "director"
        return f"{role} {'exit' if mgmt['leaving'] else 'appointed'}: {mgmt['person']}"
    if cat == "results":
        return "Quarterly results filed"
    t = classify.clean_text(f["text"]) or f["subject"] or f["exchange_desc"]
    first = re.split(r"(?<=[a-z0-9])[.;](\s|$)", t)[0]  # first clause reads like a headline
    t = first if 20 <= len(first) <= 110 else t
    return (t[:96].rsplit(" ", 1)[0] + "…") if len(t) > 98 else t


def as_of():
    """The brief is anchored to the freshest data we hold, not wall-clock: a snapshot
    refreshed last night still produces a sensible 'last 24h' brief this morning."""
    with db.conn() as c:
        row = c.execute("SELECT MAX(ts) AS m FROM filings").fetchone()
    return datetime.fromisoformat(row["m"]) if row and row["m"] else datetime.now()


def build(holdings, hours=24, settings=None):
    settings = {**alerts.DEFAULTS, **(settings or {})}
    hmap = {h["symbol"]: h for h in holdings}
    end = as_of()
    since = (end - timedelta(hours=hours)).isoformat()
    with db.conn() as c:
        q = ",".join("?" * len(hmap))
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM filings WHERE symbol IN ({q}) AND ts >= ? ORDER BY ts DESC", (*hmap, since))]
    idx = org_index()
    cards = []
    for f in rows:
        f = _ensure_classified(f)
        h = hmap[f["symbol"]]
        extra = json.loads(f.get("extra") or "{}")
        mgmt = classify.mgmt_signal(f["text"]) if f["category"] == "management_change" else {}
        if extra.get("profile") and mgmt:
            mgmt["person"] = extra["profile"].get("name", mgmt.get("person"))
        sc, tier, why = materiality.score(f["category"], f["text"], h.get("weight", 0), settings, mgmt, h.get("kind"))
        card = {"id": f["id"], "symbol": f["symbol"], "company": h["name"], "ts": f["ts"],
                "category": f["category"], "category_label": LABEL.get(f["category"], f["category"]),
                "cls_method": f["cls_method"], "title": _title(f, mgmt), "url": f["url"],
                "score": sc, "tier": tier, "why": why, "demo": bool(f.get("demo"))}
        oid = idx.by_ticker.get(f["symbol"])
        if tier != "routine":
            card["summary"] = _summary(f, tier)
            if f["category"] == "management_change":
                prof = _profile_from_filing(f, extra)
                if prof:
                    d = graph.diligence(prof)
                    card["person"] = {"profile": prof, **d}
            elif f["category"] == "results":
                card["results"] = results.headline(f["symbol"])
            if f["category"] in CONTEXT_CATS and oid:
                card["call"] = graph.who_to_call(oid, limit=2)
                if f["category"] == "litigation":  # legal angle, from the expertise map
                    card["call"] += [dict(p, why="Legal angle — " + ", ".join(p["evidence"][:1]), relation="expert")
                                     for p in graph.expertise("litigation", limit=1)]
        cards.append(card)

    cards.sort(key=lambda c: (-c["score"], c["ts"]))
    routine = [c for c in cards if c["tier"] == "routine"]
    by_cat = {}
    for c in routine:
        by_cat.setdefault(c["category_label"], []).append(c)
    events = [{"org_id": idx.by_ticker.get(c["symbol"]), "title": f"{c['company']} {c['category_label'].lower()} news",
               "category": c["category"], "symbol": c["symbol"]}
              for c in cards if c["tier"] != "routine" and idx.by_ticker.get(c["symbol"])]
    return {
        "window_hours": hours, "as_of": end.isoformat(timespec="minutes"),
        "counts": {"total": len(cards), "act": sum(c["tier"] == "act" for c in cards),
                   "know": sum(c["tier"] == "know" for c in cards), "routine": len(routine)},
        "act": [c for c in cards if c["tier"] == "act"],
        "know": [c for c in cards if c["tier"] == "know"],
        "routine": {"total": len(routine), "groups": [{"label": k, "items": v} for k, v in
                                                       sorted(by_cat.items(), key=lambda kv: -len(kv[1]))]},
        "alerts": alerts.check(holdings, settings),
        "warmups": graph.warmups(events),
    }
