"""Network intelligence: deterministic graph queries over the local store.

Edges (strength in 0..1, combined per pair with noisy-OR: 1 - prod(1 - s)):
  direct     you <-> contact      0.25 + 0.50*closeness + 0.25*recency(last spoke, 3y decay)
  colleague  same org, overlapping dates   0.30 + 0.50*min(1, overlap_years / 3)
  classmate  same school, overlapping years 0.45 (0.55 for the identical batch)
  knows      declared acquaintance          0.60

Queries: who_to_call, intro_paths, who_knows, expertise_map, reconnect_triggers,
insider_status, network_links. Every "why" is a template filled from stored facts.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from . import resolve
from .store import NetworkStore, Person, Stint

ME = "me"

INSIDER_CAUTION = ("{name} currently works at {org}, a listed company. Keep it to public information: "
                   "do not seek or accept unpublished price-sensitive information (SEBI PIT Regulations, 2015).")

RECONNECT_CATEGORIES = ("results", "M&A")
_CATEGORY_LABEL = {"results": "results", "M&A": "an M&A announcement"}


@dataclass(frozen=True)
class Edge:
    kind: str
    strength: float
    evidence: str


@dataclass(frozen=True)
class Insider:
    is_insider: bool
    org: str | None = None
    caution: str | None = None


@dataclass(frozen=True)
class Suggestion:
    person_id: str
    name: str
    score: float
    reasons: tuple[str, ...]
    insider: Insider


@dataclass(frozen=True)
class Path:
    nodes: tuple[str, ...]
    names: tuple[str, ...]
    strength: float
    hops: tuple[str, ...]   # evidence for each hop


@dataclass(frozen=True)
class Trigger:
    person_id: str
    name: str
    symbol: str
    category: str
    filed_at: str
    days_silent: int | None
    message: str


# ----------------------------------------------------------------- helpers
def _fy(s: str | None, today: date) -> float:
    """'YYYY' / 'YYYY-MM' -> fractional year; None -> today."""
    if s is None:
        return today.year + (today.month - 1) / 12
    parts = s.split("-")
    return int(parts[0]) + ((int(parts[1]) - 1) / 12 if len(parts) > 1 else 0)


def _span(s: Stint) -> str:
    return f"{s.start[:4] if s.start else '?'}–{s.end[:4] if s.end else 'present'}"


def _theme_tokens(s: str) -> frozenset[str]:
    toks = re.findall(r"[a-z0-9]+", s.lower())
    return frozenset(t[:-1] if len(t) > 3 and t.endswith("s") else t
                     for t in toks if t not in {"and", "the", "of"})


def _theme_match(query: str, themes: Iterable[str]) -> bool:
    q = _theme_tokens(query)
    return bool(q) and any(q <= _theme_tokens(t) for t in themes)


def _first(name: str) -> str:
    return name.split()[0]


# ------------------------------------------------------------------- graph
class NetworkGraph:
    def __init__(self, store: NetworkStore, today: date | None = None, extra: Iterable[Person] = ()):
        self.today = today or date.today()
        self.people: dict[str, Person] = dict(store.people)
        for p in extra:  # transient people (e.g. an incoming CFO from a filing); never persisted
            self.people[p.id] = p
        self._stints = {pid: [(s, resolve.resolve_org(s.org)) for s in p.stints]
                        for pid, p in self.people.items()}
        self.adj: dict[str, dict[str, list[Edge]]] = defaultdict(lambda: defaultdict(list))
        self._build()

    # ------------------------------------------------------------- building
    def _add(self, a: str, b: str, edge: Edge) -> None:
        if a != b:
            self.adj[a][b].append(edge)
            self.adj[b][a].append(edge)

    def _days_since(self, iso: str | None) -> int | None:
        return (self.today - date.fromisoformat(iso)).days if iso else None

    def _build(self) -> None:
        for p in self.people.values():
            if p.is_contact:
                close = (min(5, max(1, p.closeness)) - 1) / 4
                days = self._days_since(p.last_contacted)
                recency = max(0.0, 1 - days / 1095) if days is not None else 0.0
                ev = f"your contact · closeness {p.closeness}/5" + (f" · last spoke {days}d ago" if days is not None else "")
                self._add(ME, p.id, Edge("direct", round(0.25 + 0.5 * close + 0.25 * recency, 3), ev))
            for other in p.knows:
                if other in self.people:
                    self._add(p.id, other, Edge("knows", 0.6, "know each other"))

        by_org: dict[str, list[tuple[str, Stint, resolve.OrgMatch]]] = defaultdict(list)
        for pid, stints in self._stints.items():
            for s, m in stints:
                if s.start:
                    by_org[m.org_id].append((pid, s, m))
        for entries in by_org.values():
            for i, (pa, sa, ma) in enumerate(entries):
                for pb, sb, _ in entries[i + 1:]:
                    if pa == pb:
                        continue
                    lo = max(_fy(sa.start, self.today), _fy(sb.start, self.today))
                    hi = min(_fy(sa.end, self.today), _fy(sb.end, self.today))
                    if hi - lo >= 0.25:
                        org_name = ma.org.name if ma.org else sa.org
                        until = "present" if sa.end is None and sb.end is None else str(int(hi))
                        self._add(pa, pb, Edge("colleague", round(0.3 + 0.5 * min(1.0, (hi - lo) / 3), 3),
                                               f"overlapped at {org_name} {int(lo)}–{until}"))

        by_school: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
        for p in self.people.values():
            for e in p.education:
                if e.start and e.end:
                    by_school[resolve.normalize_org(e.school)].append((p.id, e.start, e.end, e.school))
        for entries in by_school.values():
            for i, (pa, sa, ea, school) in enumerate(entries):
                for pb, sb, eb, _ in entries[i + 1:]:
                    if pa != pb and max(sa, sb) < min(ea, eb):
                        same = (sa, ea) == (sb, eb)
                        self._add(pa, pb, Edge("classmate", 0.55 if same else 0.45,
                                               f"{'same batch' if same else 'overlapped'} at {school} "
                                               f"({max(sa, sb)}–{min(ea, eb)})"))

    # ---------------------------------------------------------- primitives
    def strength(self, a: str, b: str) -> float:
        edges = self.adj.get(a, {}).get(b, [])
        miss = 1.0
        for e in edges:
            miss *= 1 - e.strength
        return round(1 - miss, 3)

    def evidence(self, a: str, b: str) -> str:
        edges = sorted(self.adj.get(a, {}).get(b, []), key=lambda e: -e.strength)
        return "; ".join(e.evidence for e in edges)

    def name(self, pid: str) -> str:
        return "You" if pid == ME else self.people[pid].name

    def insider_status(self, pid: str) -> Insider:
        p = self.people[pid]
        for s, m in self._stints[pid]:
            if s.current and m.org and m.org.listed:
                return Insider(True, m.org.name, INSIDER_CAUTION.format(name=p.name, org=m.org.name))
        return Insider(False)

    def network_links(self, ticker: str) -> int:
        return sum(1 for p in self.people.values() if p.is_contact
                   and any(m.org and m.org.ticker == ticker for _, m in self._stints[p.id]))

    # ------------------------------------------------------------- queries
    def who_to_call(self, target: str, top: int = 5) -> list[Suggestion]:
        """target: NSE ticker, company name, or a sector/theme ('rural lending')."""
        match = resolve.resolve_org_or_ticker(target)
        org = match.org
        out = []
        for p in self.people.values():
            if not p.is_contact:
                continue
            best, reasons = 0.0, []
            for s, m in self._stints[p.id]:
                title = f"{s.title}, " if s.title else ""
                if org and m.org_id == match.org_id:
                    if s.current:
                        base, why = 0.8, f"Currently {title}{org.name}"
                    else:
                        years_out = _fy(None, self.today) - _fy(s.end, self.today)
                        base = max(0.5, 1.0 - max(0.0, years_out - 2) * 0.125)
                        why = f"Was {title}{org.name} ({_span(s)})"
                elif org and m.org and m.org.sector == org.sector:
                    base, why = 0.6, f"{title}{m.org.name} ({_span(s)}): same sector"
                elif not org and m.org and _theme_match(target, m.org.all_themes):
                    base, why = (0.7 if s.current else 0.8), f"{title}{m.org.name} ({_span(s)})"
                else:
                    continue
                best = max(best, base)
                reasons.append(why)
            for tag in p.expertise:
                hit = (_theme_match(tag, org.all_themes) or any(_theme_match(t, [tag]) for t in org.all_themes)) \
                    if org else _theme_match(target, [tag])
                if hit:
                    best = max(best, 0.5 if org else 0.6)
                    reasons.append(f"Expertise: {tag}")
            if best:
                out.append(Suggestion(p.id, p.name, round(best * self.strength(ME, p.id), 3),
                                      tuple(reasons), self.insider_status(p.id)))
        return sorted(out, key=lambda s: -s.score)[:top]

    def intro_paths(self, target_id: str, max_hops: int = 3, k: int = 3) -> list[Path]:
        found: list[tuple[list[str], float]] = []

        def dfs(node: str, path: list[str], acc: float) -> None:
            if node == target_id:
                found.append((path, acc))
                return
            if len(path) - 1 >= max_hops:
                return
            for nb in self.adj.get(node, {}):
                if nb not in path:
                    dfs(nb, path + [nb], acc * self.strength(node, nb))

        dfs(ME, [ME], 1.0)
        found.sort(key=lambda x: (-x[1], len(x[0])))
        return [Path(tuple(p), tuple(self.name(n) for n in p), round(s, 3),
                     tuple(self.evidence(a, b) for a, b in zip(p, p[1:])))
                for p, s in found[:k]]

    def who_knows(self, name: str, org_hints: Iterable[str] = (), k: int = 3):
        """Match a name from a filing to people in the store, then find routes to them."""
        candidates = [(pid, p.name, frozenset(m.org_id for _, m in self._stints[pid]))
                      for pid, p in self.people.items()]
        return [(m, self.intro_paths(m.person_id, k=k))
                for m in resolve.match_person(name, candidates, org_hints)]

    def expertise_map(self, theme: str | None = None, top: int = 5) -> dict[str, list[tuple[str, float, str]]]:
        """theme -> [(person name, weight, evidence)]. With `theme`, only matching themes."""
        themes: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)

        def put(t: str, p: Person, w: float, ev: str) -> None:
            cur = themes[t.lower()].get(p.name)
            if cur is None or w > cur[0]:
                themes[t.lower()][p.name] = (w, ev)

        for p in self.people.values():
            if not p.is_contact:
                continue
            for tag in p.expertise:
                put(tag, p, 1.0, "self-described")
            for s, m in self._stints[p.id]:
                if not m.org:
                    continue
                yrs = _fy(s.end, self.today) - _fy(s.start, self.today) if s.start else None
                if yrs is None or yrs >= 1:
                    for t in m.org.all_themes:
                        put(t, p, 0.8, f"{m.org.name} {_span(s)}")
        result = {}
        for t, people in themes.items():
            if theme and not _theme_match(theme, [t]):
                continue
            result[t] = sorted(((n, w, ev) for n, (w, ev) in people.items()), key=lambda x: -x[1])[:top]
        return result

    def reconnect_triggers(self, filings: Iterable[Mapping], min_days_silent: int = 120) -> list[Trigger]:
        """Former employees only. Current employees are excluded on purpose: around a
        results/M&A filing they are the people most likely to hold UPSI."""
        out, seen = [], set()
        for f in filings:
            if f.get("category") not in RECONNECT_CATEGORIES:
                continue
            sym = f["symbol"]
            for p in self.people.values():
                if not p.is_contact or (p.id, sym) in seen:   # one nudge per person per company
                    continue
                stints = [(s, m) for s, m in self._stints[p.id] if m.org and m.org.ticker == sym]
                if not stints or any(s.current for s, _ in stints):
                    continue
                days = self._days_since(p.last_contacted)
                if days is not None and days < min_days_silent:
                    continue
                seen.add((p.id, sym))
                s, m = max(stints, key=lambda x: x[0].end or "")
                silent = f"you last spoke {days} days ago" if days is not None else "you haven't logged a conversation"
                out.append(Trigger(p.id, p.name, sym, f["category"], f.get("filed_at", ""), days,
                                   f"{_first(p.name)} left {m.org.name} in {s.end[:4]}; it just filed "
                                   f"{_CATEGORY_LABEL[f['category']]}. {silent.capitalize()}: "
                                   f"a natural reason to reconnect."))
        return sorted(out, key=lambda t: -(t.days_silent if t.days_silent is not None else 10**6))
