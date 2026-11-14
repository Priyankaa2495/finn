"""Network intelligence. Pure, deterministic Python over a few hundred records — no
graph DB, no LLM. Every score comes with a human-readable 'why', because an investor
will only make the call if they trust the reason.

Scoring model (kept simple and explainable on purpose):
  path_score   = link_strength(contact -> target) x (0.4 + 0.6 x warmth(me -> contact))
  call_score   = relevance(contact's experience -> company) x (0.5 + 0.5 x warmth)
"""
import math
from dataclasses import dataclass, asdict
from datetime import date

from rapidfuzz import fuzz

from .resolve import org_index, same_person
from .store import load

def _year():
    # evaluated per call so a long-running server doesn't freeze "last spoke" / warmth at boot date
    return date.today().year

FUNCTIONS = {
    "finance": ["cfo", "finance", "controller", "accounts", "treasury", "audit", "ca"],
    "operations": ["plant", "production", "operations", "coo", "manufacturing", "projects"],
    "strategy": ["strategy", "consult", "principal", "engagement manager"],
    "credit": ["credit", "risk", "lending", "underwriting"],
    "sales": ["sales", "business head", "category", "brand", "merchandising", "marketing"],
    "technology": ["engineer", "engineering", "product", "delivery", "technology", "cto"],
    "regulatory": ["regulatory", "compliance", "legal", "secretary"],
    "r&d": ["r&d", "chemist", "synthesis", "research", "process"],
}
SENIOR = ["head", "director", "vp", "chief", "cfo", "ceo", "coo", "md", "partner", "president", "controller", "executive director"]


def _func(role: str):
    r = (role or "").lower()
    for f, kws in FUNCTIONS.items():
        if any(k in r for k in kws):
            return f
    return None


def _senior(role: str) -> bool:
    r = (role or "").lower()
    return any(k in r.split() or k in r for k in SENIOR)


def _span(e):
    return (e.get("from") or e.get("to") or _year(), e.get("to") or _year())


def _org(e):
    return org_index().resolve(e.get("org", ""))[0]


def warmth(p) -> float:
    """How easy is it to pick up the phone? Declared strength x recency of contact."""
    s = (p.get("strength") or 2) / 5
    lc = p.get("last_contact")
    if not lc:
        return round(s * 0.4, 3)
    days = (date.today() - date.fromisoformat(lc)).days
    return round(s * (0.45 + 0.55 * math.exp(-max(days, 0) / 240)), 3)


def days_since(p):
    lc = p.get("last_contact")
    return (date.today() - date.fromisoformat(lc)).days if lc else None


def _ago(days):
    if days is None:
        return "never logged"
    if days < 45:
        return f"{days}d ago"
    if days < 400:
        return f"{round(days/30)} months ago"
    return f"{days/365:.1f} years ago"


@dataclass
class Link:
    kind: str       # worked_together | same_function | batchmates | alumni | ex_colleagues_diff_years | knows
    label: str      # human sentence
    strength: float


def career_links(a, b):
    """All the ways two profiles' careers touch, strongest first."""
    links = []
    for ea in a.get("experience", []):
        oa = _org(ea)
        if not oa:
            continue
        for eb in b.get("experience", []):
            if _org(eb) != oa:
                continue
            (a0, a1), (b0, b1) = _span(ea), _span(eb)
            ov = min(a1, b1) - max(a0, b0)
            org_name = org_index().get(oa)["name"]
            if ov >= 0:
                same_fn = _func(ea.get("role")) and _func(ea.get("role")) == _func(eb.get("role"))
                st = min(0.95, 0.5 + 0.06 * min(ov, 6) + (0.2 if same_fn else 0))
                # a shared desk 15 years ago is a weaker link than one 2 years ago
                st *= 0.6 + 0.4 * math.exp(-(_year() - min(a1, b1)) / 10)
                end = "now" if not ea.get("to") and not eb.get("to") else min(a1, b1)
                yrs = f"{max(a0, b0)}–{end}"
                fn = f", both in {_func(ea.get('role'))}" if same_fn else ""
                links.append(Link("same_function" if same_fn else "worked_together",
                                  f"overlapped at {org_name} {yrs}{fn}", round(st, 2)))
            else:
                links.append(Link("ex_colleagues_diff_years",
                                  f"both ex-{org_name}, different years", 0.25))
    for da in a.get("education", []):
        for db in b.get("education", []):
            if fuzz.token_set_ratio(da["school"].lower(), db["school"].lower()) < 90:
                continue
            same_batch = (da.get("to") or 0) == (db.get("to") or 0)
            overlap = min(da.get("to", 0), db.get("to", 0)) - max(da.get("from", 0), db.get("from", 0)) >= 0
            decay = 0.5 + 0.5 * math.exp(-(_year() - (da.get("to") or _year())) / 15)
            if same_batch:
                links.append(Link("batchmates", f"same batch at {da['school']} ({da.get('to')})", round(0.7 * decay, 2)))
            elif overlap:
                links.append(Link("batchmates", f"overlapped at {da['school']}", round(0.5 * decay, 2)))
            else:
                links.append(Link("alumni", f"fellow {da['school']} alum", 0.2))
    return sorted(links, key=lambda l: -l.strength)


# ---------------------------------------------------------------- paths --------
def paths_to(target: dict, limit=3):
    """'I don't know the new CFO — who do I know who does, and how strong is that link?'
    target: {name, experience[], education[]} (e.g. extracted from a filing's bio)."""
    net = load()
    out = []
    t_orgs = {_org(e) for e in target.get("experience", [])} - {None}

    # Already in the network?
    for p in net["people"]:
        p_orgs = {_org(e) for e in p.get("experience", [])} - {None}
        if same_person(p["name"], target["name"], p_orgs, t_orgs) >= 0.9:
            return {"direct": _brief(p), "paths": []}

    # My own history with the target (you studied there too)
    for l in career_links(net["me"], target)[:1]:
        if l.strength >= 0.2:
            out.append({"via": None, "hops": 1, "score": round(l.strength * 0.7, 3),
                        "why": f"You and {target['name'].split()[0]}: {l.label}", "link": asdict(l)})

    # Declared second-degree edges (contact explicitly knows the target)
    for s in net.get("second_degree", []):
        if same_person(s["name"], target["name"], set(), set()) >= 0.9 or s["name"] == target["name"]:
            for k in s.get("known_by", []):
                c = _person(k["id"])
                # an explicit 'they know each other' beats any inferred overlap
                sc = min(1.0, k["strength"] / 5 * 1.25) * (0.4 + 0.6 * warmth(c))
                out.append({"via": _brief(c), "hops": 2, "score": round(sc, 3), "declared": True,
                            "why": f"{c['name']} knows them — {k['context']}",
                            "link": {"kind": "knows", "label": k["context"], "strength": k["strength"] / 5}})

    # Inferred second-degree edges from career overlap
    for c in net["people"]:
        ls = career_links(c, target)
        if not ls or ls[0].strength < 0.25:
            continue
        best = ls[0]
        # 'best placed to give context' is mostly about the link, partly about warmth
        sc = best.strength * (0.4 + 0.6 * warmth(c))
        out.append({"via": _brief(c), "hops": 2, "score": round(sc, 3),
                    "why": f"{c['name']} {best.label}", "link": asdict(best),
                    "also": [l.label for l in ls[1:3]]})

    # One best route per contact
    seen, uniq = set(), []
    for o in sorted(out, key=lambda x: (not x.get("declared"), -x["score"])):
        key = o["via"]["id"] if o["via"] else "me"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(o)
    uniq.sort(key=lambda x: -x["score"])
    return {"direct": None, "paths": uniq[:limit]}


# ---------------------------------------------------------------- who to call --
def who_to_call(org_id: str, limit=4, themes=None):
    """'Who in my network has worked there, at a competitor, a supplier or a customer —
    and what would they know?'"""
    idx = org_index()
    net = load()
    target = idx.get(org_id) or {"name": org_id, "themes": themes or []}
    related = idx.related(org_id) if idx.get(org_id) else {}
    t_themes = set(target.get("themes", [])) | set(themes or [])
    res = []
    for p in net["people"]:
        best = None
        for e in p.get("experience", []):
            o = _org(e)
            current = not e.get("to")
            yrs_since = 0 if current else _year() - e["to"]
            tenure = (e.get("to") or _year()) - (e.get("from") or _year() - 1)
            if o == org_id:
                rel = 1.0 if current else 0.9 * math.exp(-yrs_since / 8)
                why = (f"{'Currently' if current else 'Was'} {e['role']} at {target['name']}"
                       f" ({e.get('from') or '?'}–{e.get('to') or 'now'})")
                kind = "insider" if current else "alumnus"
            elif o and o in related:
                rel = 0.6 * (1.0 if current else math.exp(-yrs_since / 8))
                org_name = idx.get(o)["name"]
                why = f"{e['role']} at {org_name} — a {related[o]} of {target['name']}"
                kind = related[o]
            elif o and t_themes & set(idx.get(o).get("themes", [])):
                shared = sorted(t_themes & set(idx.get(o).get("themes", [])))[0]
                rel = 0.35 * (1.0 if current else math.exp(-yrs_since / 8))
                why = f"{e['role']} at {idx.get(o)['name']} — same space ({shared})"
                kind = "sector"
            else:
                continue
            rel *= 1.15 if _senior(e.get("role")) else 1.0
            rel *= min(1.0, 0.6 + 0.08 * tenure)
            if not best or rel > best[0]:
                best = (rel, why, kind, current and o == org_id)
        if not best:
            continue
        w = warmth(p)
        score = best[0] * (0.5 + 0.5 * w)
        res.append({
            **_brief(p), "score": round(score, 3), "relation": best[2], "why": best[1],
            "warmth": w, "last_spoke": _ago(days_since(p)),
            # Compliance guardrail: a current insider of a listed company may hold UPSI.
            "caution": "Current insider — keep it to public information (SEBI PIT / UPSI)" if best[3] else None,
        })
    return sorted(res, key=lambda r: -r["score"])[:limit]


# ---------------------------------------------------------------- expertise ----
def expertise(theme: str, limit=6):
    """'Who are my go-to people for specialty chemicals?'"""
    idx = org_index()
    q = theme.lower().strip()
    out = []
    for p in load()["people"]:
        score, evidence = 0.0, []
        for t in p.get("tags", []):
            if fuzz.partial_ratio(q, t) >= 85:
                score += 1.0
                evidence.append(f"tagged '{t}'")
        for e in p.get("experience", []):
            o = idx.get(_org(e)) if _org(e) else None
            if not o or not any(fuzz.partial_ratio(q, t) >= 85 for t in o.get("themes", [])):
                continue
            tenure = (e.get("to") or _year()) - (e.get("from") or _year() - 1)
            recency = 1.0 if not e.get("to") else math.exp(-(_year() - e["to"]) / 10)
            score += min(tenure, 10) / 10 * recency * (1.2 if _senior(e.get("role")) else 1.0)
            evidence.append(f"{tenure}y at {o['name']} ({e['role']})")
        if score > 0:
            out.append({**_brief(p), "score": round(score, 2), "evidence": evidence[:3],
                        "warmth": warmth(p), "last_spoke": _ago(days_since(p))})
    return sorted(out, key=lambda r: -r["score"])[:limit]


def all_themes():
    idx = org_index()
    ts = {t for o in idx.orgs.values() for t in o.get("themes", [])}
    ts |= {t for p in load()["people"] for t in p.get("tags", [])}
    return sorted(ts)


# ---------------------------------------------------------------- warm-ups -----
def warmups(events, min_days=120, limit=5):
    """'I haven't spoken to X in a while, but their former employer just announced
    results or a deal — a natural reason to reconnect.'
    events: [{org_id, title, category}] (material filings in the window)."""
    idx = org_index()
    out = {}
    for ev in events:
        rel = idx.related(ev["org_id"])
        for p in load()["people"]:
            d = days_since(p)
            if d is not None and d < min_days:
                continue
            for e in p.get("experience", []):
                o = _org(e)
                now = not e.get("to")
                if o == ev["org_id"]:
                    hook = "they work there" if now else f"they spent {(e['to'] - (e.get('from') or e['to']))} years there"
                elif o in rel:
                    hook = f"they {'are' if now else 'were'} at {idx.get(o)['name']}, a {rel[o]}"
                else:
                    continue
                # events arrive highest-priority first, so the first hook found wins
                out.setdefault(p["id"], {**_brief(p), "last_spoke": _ago(d), "days": d,
                                         "reason": f"{ev['title']} — {hook}",
                                         "category": ev["category"], "symbol": ev.get("symbol")})
                break
    return sorted(out.values(), key=lambda r: -(r["days"] or 9999))[:limit]


# ---------------------------------------------------------------- coverage -----
def coverage(holdings):
    """What the investor wouldn't notice: which holdings have nobody in the network
    close to them. Weighted by position size, the blind spots are where to network next."""
    idx = org_index()
    rows = []
    for h in holdings:
        oid = idx.by_ticker.get(h["symbol"])
        best = who_to_call(oid, limit=1) if oid else []
        rows.append({"symbol": h["symbol"], "name": h["name"], "weight": h.get("weight", 0),
                     "best": best[0] if best else None,
                     "score": best[0]["score"] if best else 0})
    return sorted(rows, key=lambda r: (r["score"], -r["weight"]))


# ---------------------------------------------------------------- diligence ----
def diligence(target: dict):
    """'Before I meet a promoter or a management team...' Career path, overlaps with my
    network, and questions worth asking — generated by rules from the career data."""
    exp = sorted(target.get("experience", []), key=lambda e: e.get("from") or 0)
    qs = []
    for i, e in enumerate(exp):
        tenure = (e.get("to") or _year()) - (e.get("from") or _year())
        if e.get("to") and tenure < 2:
            qs.append(f"Short stint at {e['org']} ({tenure}y) — why the quick exit?")
        if i and exp[i - 1].get("to") and e.get("from") and e["from"] - exp[i - 1]["to"] > 1:
            qs.append(f"Gap {exp[i-1]['to']}–{e['from']} between {exp[i-1]['org']} and {e['org']}.")
    if exp:
        last = exp[-1]
        rel = org_index().related(_org(last) or "")
        for e in exp[:-1]:
            o = _org(e)
            if o and o in rel and rel[o] == "competitor":
                qs.append(f"Comes from a direct competitor ({e['org']}) — what playbook are they bringing, any non-compete limits?")
        if not any(_senior(e.get("role")) and ("cfo" in e.get("role", "").lower() or "chief" in e.get("role", "").lower()) for e in exp[:-1]):
            qs.append("First time in a CXO seat — who will they lean on in the first year?")
    return {"timeline": exp, "questions": qs[:4], **paths_to(target)}


# ---------------------------------------------------------------- helpers ------
def _person(pid):
    return next(p for p in load()["people"] if p["id"] == pid)


def _brief(p):
    return {"id": p["id"], "name": p["name"], "headline": p.get("headline", ""),
            "how_met": p.get("how_met", ""), "city": p.get("city", "")}


def person(pid):
    p = _person(pid)
    return {**p, "warmth": warmth(p), "last_spoke": _ago(days_since(p))}
