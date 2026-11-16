"""Push: the ranked Brief and the Network overview. Deterministic composition, no model.

Brief = filings in the window (anchored to the newest filing in the data, so a snapshot still
shows a real day) scored by materiality, grouped ACT / KNOW / ROUTINE (routine folded), each
enriched with its eagerly parsed attachment (results card or bio) and the Network:
  - management change -> who in your network knows the incoming person (path + strength)
  - material event     -> the person best placed to give context
  - results / M&A      -> warm-up prompt for a former employee you haven't spoken to
Plus EOD threshold alerts. Every sentence, including the voice script, is a template.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .. import config, db
from ..network.graph import NetworkGraph
from . import alerts as A
from . import materiality as MAT
from .classify import classify
from .sanitize import sanitize

DEMO_PATH = config.BACKEND_DIR / "snapshot" / "brief" / "demo_cards.json"
DEFAULT_WATCHED = frozenset({"results", "M&A", "management_change"})
CALL_CATEGORIES = {"M&A", "litigation", "capex", "fund_raise", "results", "credit_rating", "pledging"}
_INFORMED = re.compile(r"^.*?has informed the Exchange (?:about|regarding|that)\s*", re.I)
CATEGORY_LABEL = {"M&A": "M&A", "management_change": "Management change", "fund_raise": "Fund raise",
                  "insider_trading": "Insider trade", "earnings_call": "Earnings call",
                  "investor_meetings": "Investor meet", "credit_rating": "Credit rating",
                  "bonus_split": "Bonus / split", "press_release": "Update", "postal_ballot": "Postal ballot",
                  "agm_egm": "AGM / EGM", "unclassified": "Routine"}


def _label(cat: str) -> str:
    return CATEGORY_LABEL.get(cat, cat.replace("_", " ").capitalize())


def _summary(subject: str, body: str) -> str | None:
    text = sanitize(_INFORMED.sub("", body or ""), 400).strip(" .'\"")
    if not text or text.lower() == (subject or "").lower():
        return None
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return (first[:157] + "…") if len(first) > 160 else first


def _demo_cards(anchor: datetime, path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for c in json.loads(path.read_text(encoding="utf-8"))["cards"]:
        cls = classify(c["subject"], c["body"])
        out.append({"id": c["id"], "symbol": c["symbol"], "subject": c["subject"], "body": c["body"],
                    "filed_at": (anchor - timedelta(hours=c["offset_hours"])).isoformat(timespec="seconds"),
                    "attachment_url": None, "category": cls.category, "demo": True,
                    "_bio": c.get("bio")})
    return out


def _anchor(conn) -> datetime | None:
    marks = ",".join("?" * len(config.PORTFOLIO_SYMBOLS))
    with db.LOCK:
        row = conn.execute(f"SELECT MAX(filed_at) m FROM filings WHERE symbol IN ({marks})",
                           config.PORTFOLIO_SYMBOLS).fetchone()
    return datetime.fromisoformat(row["m"]) if row and row["m"] else None


def _filings_between(conn, start: datetime, end: datetime) -> list[dict]:
    marks = ",".join("?" * len(config.PORTFOLIO_SYMBOLS))
    with db.LOCK:
        return [dict(r) for r in conn.execute(
            f"""SELECT * FROM filings WHERE symbol IN ({marks}) AND filed_at >= ? AND filed_at <= ?
                ORDER BY filed_at DESC""",
            [*config.PORTFOLIO_SYMBOLS, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")])]


def _fmt_cr_speech(v: float) -> str:
    return f"{v:,.0f} crore"


def _network_for(f: dict, bio: dict | None, graph: NetworkGraph) -> dict | None:
    sym, cat = f["symbol"], f["category"]
    if cat == "management_change" and bio and bio.get("name"):
        hints = [*bio.get("employers", []), sym]
        for match, paths in graph.who_knows(bio["name"], hints):
            if paths:
                p = paths[0]
                return {"kind": "knows_person", "person": match.name, "confidence": match.confidence,
                        "path": list(p.names), "strength": p.strength, "hops": list(p.hops),
                        "insider": graph.insider_status(p.nodes[1]).__dict__ if len(p.nodes) > 2 else None}
    if cat in CALL_CATEGORIES or cat == "management_change":
        sugg = graph.who_to_call(sym, top=1)
        if sugg:
            s = sugg[0]
            return {"kind": "call", "person": s.name, "person_id": s.person_id, "score": s.score,
                    "why": list(s.reasons[:2]), "insider": s.insider.__dict__}
    return None


def _speech(item: dict) -> str:
    parts = ["Demo card." if item.get("demo") else "", f"{item['name']}.", _label(item["category"]) + "."]
    rc = item.get("results_card")
    if rc and rc.get("rows", {}).get("revenue"):
        r = rc["rows"]["revenue"]
        s = f"Revenue {_fmt_cr_speech(r['value'])}"
        if r.get("yoy") is not None:
            s += f", {'up' if r['yoy'] >= 0 else 'down'} {abs(r['yoy']):.0f} percent year on year"
        parts.append(s + ".")
        p = rc["rows"].get("pat")
        if p and p.get("yoy") is not None:
            parts.append(f"Profit after tax {'up' if p['yoy'] >= 0 else 'down'} {abs(p['yoy']):.0f} percent.")
    elif item.get("summary"):
        parts.append(item["summary"].rstrip(".") + ".")
    else:
        parts.append(item["subject"].rstrip(".") + ".")
    if item.get("bio") and item["bio"].get("name"):
        parts.append(f"Incoming: {item['bio']['name']}, {item['bio'].get('role') or 'role not stated'}.")
    n = item.get("network")
    if n and n["kind"] == "knows_person":
        via = n["path"][1] if len(n["path"]) > 2 else None
        parts.append(f"{via} can introduce you to {n['person']}." if via else f"You know {n['person']}.")
    elif n and n["kind"] == "call":
        parts.append(f"Who to call: {n['person']}.")
        if n["insider"]["is_insider"]:
            parts.append("Insider caution applies.")
    return " ".join(p for p in parts if p)


def build_brief(conn, graph: NetworkGraph, *, window_hours: int = 24, alert_cfg: A.AlertConfig = A.AlertConfig(),
                watched: frozenset[str] = DEFAULT_WATCHED, include_demo: bool = True,
                demo_path: Path = DEMO_PATH) -> dict:
    anchor = _anchor(conn)
    if anchor is None:
        return {"as_of": None, "window_hours": window_hours, "act": [], "know": [], "routine": [],
                "routine_count": 0, "alerts": [], "warmups": [], "speech": []}
    start = anchor - timedelta(hours=window_hours)
    rows = _filings_between(conn, start, anchor)
    if include_demo:
        rows += _demo_cards(anchor, demo_path)
    with db.LOCK:
        enrich = db.get_enrichment(conn, [r["id"] for r in rows])
    names = {h.symbol: h.name for h in config.PORTFOLIO}

    items = []
    for f in rows:
        e = enrich.get(f["id"], {})
        bio = e.get("bio") or f.get("_bio")
        net = _network_for(f, bio, graph)
        m = MAT.score(f["category"], f["subject"], f.get("body", ""),
                      position_weight_pct=config.weight_of(f["symbol"]),
                      network_links=graph.network_links(f["symbol"]), watched_categories=watched)
        item = {"id": f["id"], "symbol": f["symbol"], "name": names.get(f["symbol"], f["symbol"]),
                "category": f["category"], "label": _label(f["category"]), "tier": m.tier, "score": m.score,
                "reasons": list(m.reasons), "filed_at": f["filed_at"], "subject": f["subject"],
                "summary": _summary(f["subject"], f.get("body", "")), "url": f.get("attachment_url"),
                "demo": bool(f.get("demo")), "results_card": e.get("results_card"), "bio": bio,
                "network": net}
        item["speak"] = _speech(item)
        items.append(item)
    items.sort(key=lambda i: i["filed_at"], reverse=True)   # newest first among equal scores
    items.sort(key=lambda i: -i["score"])                     # stable

    warm = graph.reconnect_triggers([{"symbol": i["symbol"], "category": i["category"], "filed_at": i["filed_at"]}
                                     for i in items])
    with db.LOCK:
        alerts = [a.__dict__ for a in A.scan(conn, config.PORTFOLIO_SYMBOLS, alert_cfg)]
    act = [i for i in items if i["tier"] == MAT.ACT]
    know = [i for i in items if i["tier"] == MAT.KNOW]
    routine = [{"id": i["id"], "symbol": i["symbol"], "subject": i["subject"], "filed_at": i["filed_at"],
                "url": i["url"], "label": i["label"]} for i in items if i["tier"] == MAT.ROUTINE]
    speech = ([f"{len(alerts)} price or volume alert{'s' if len(alerts) != 1 else ''}."] if alerts else []) + \
        [a["message"] for a in alerts] + [i["speak"] for i in act + know] + \
        ([f"{len(routine)} routine filings folded."] if routine else [])
    return {"as_of": anchor.isoformat(timespec="seconds"), "window_hours": window_hours,
            "thresholds": {"price_move_pct": alert_cfg.price_move_pct, "volume_multiple": alert_cfg.volume_multiple},
            "act": act, "know": know, "routine": routine, "routine_count": len(routine),
            "alerts": alerts, "warmups": [t.__dict__ for t in warm], "speech": speech}


def network_overview(conn, graph: NetworkGraph, include_demo: bool = True, demo_path: Path = DEMO_PATH) -> dict:
    holdings = config.PORTFOLIO
    calls, blind = [], []
    for h in holdings:
        s = graph.who_to_call(h.symbol, top=1)
        if s:
            calls.append({"symbol": h.symbol, "name": h.name, "person": s[0].name, "score": s[0].score,
                          "why": list(s[0].reasons[:2]), "insider": s[0].insider.__dict__})
        else:
            blind.append({"symbol": h.symbol, "name": h.name, "sector": h.sector, "weight_pct": h.weight_pct})
    calls.sort(key=lambda c: -c["score"])
    blind.sort(key=lambda b: -b["weight_pct"])

    paths = []
    for p in graph.people.values():
        if p.is_contact:
            continue
        best = graph.intro_paths(p.id, k=1)
        if best:
            b = best[0]
            paths.append({"person": p.name, "role": next((s.title + ", " + s.org for s in p.stints if s.current),
                                                          p.stints[-1].org if p.stints else None),
                          "path": list(b.names), "strength": b.strength, "hops": list(b.hops)})
    paths.sort(key=lambda x: -x["strength"])

    anchor = _anchor(conn) if conn else None
    recent = _filings_between(conn, anchor - timedelta(days=30), anchor) if anchor else []
    if include_demo and anchor:
        recent += _demo_cards(anchor, demo_path)
    warm = graph.reconnect_triggers(recent)

    exp = graph.expertise_map(top=3)
    expertise = sorted(({"theme": t, "people": [{"name": n, "evidence": ev} for n, _, ev in ppl]}
                        for t, ppl in exp.items()), key=lambda x: (-len(x["people"]), x["theme"]))
    insiders = [{"name": p.name, **graph.insider_status(p.id).__dict__}
                for p in graph.people.values() if p.is_contact and graph.insider_status(p.id).is_insider]
    return {"who_to_call": calls, "intro_paths": paths, "warmups": [t.__dict__ for t in warm],
            "blind_spots": blind, "expertise": expertise[:12], "insiders": insiders,
            "contacts": sum(1 for p in graph.people.values() if p.is_contact)}
