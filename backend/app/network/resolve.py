"""Entity resolution for the Network layer: organisations and people. Pure Python, stdlib only.

Organisations
  1. normalize_org(): ASCII-fold, '&' -> 'and', unify abbreviations (Tech/Technologies),
     strip trailing legal/geo suffixes (Ltd, Limited, Pvt, India, Company...).
  2. Exact lookup in the alias table.
  3. Fuzzy fallback: token-set ratio >= 92, AND every token that differs must be a
     near-typo of a token on the other side (or a generic word). Fuzzy matching only
     fixes typos; it never "matches" by dropping a distinguishing word, so
     "Reliance Capital" is not Reliance Industries and "Tata" is nobody.
  4. More than one org above threshold -> ambiguous -> no match.

People
  Honorifics and post-nominals are stripped; names match on surname plus
  compatible given names/initials ("A. K. Sharma" ~ "Anil Kumar Sharma").
  A name match alone is capped at POSSIBLE; it becomes SAME_PERSON only with a
  shared employer.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .. import config

FUZZY_MIN = 92


# =============================================================== organisations
@dataclass(frozen=True)
class Org:
    ticker: str | None   # NSE symbol; None for unlisted organisations
    name: str
    sector: str
    listed: bool         # listed on NSE/BSE -> employees may hold UPSI
    themes: tuple[str, ...] = ()

    @property
    def all_themes(self) -> tuple[str, ...]:
        return (self.sector, *self.themes)


_EXTRA_THEMES = {
    "KAYNES": ("semiconductors",), "DIXON": ("consumer electronics",),
    "PIIND": ("specialty chemicals",), "CHOLAFIN": ("vehicle finance",),
}
_PORTFOLIO_ALIASES = {
    "RELIANCE": ("ril", "reliance", "reliance industries"), "HDFCBANK": ("hdfc bank",),
    "INFY": ("infosys", "infosys technologies"), "BHARTIARTL": ("airtel", "bharti airtel"),
    "LT": ("l&t", "lnt", "larsen and toubro", "larsen & toubro"), "TATAPOWER": ("tata power",),
    "SUNPHARMA": ("sun pharma", "sun pharmaceutical industries"), "TITAN": ("titan", "titan company"),
    "CHOLAFIN": ("chola", "cholamandalam", "cholamandalam investment and finance company"),
    "POLYCAB": ("polycab",), "DIXON": ("dixon", "dixon technologies (india)"),
    "KAYNES": ("kaynes", "kaynes technology india"), "ASIANPAINT": ("asian paints",),
    "PIIND": ("pi industries",), "DEEPAKNTR": ("deepak nitrite",),
}

# (org, aliases). Portfolio orgs are derived from config so sectors never drift.
_ORGS: list[tuple[Org, tuple[str, ...]]] = [
    (Org(h.symbol, h.name, h.sector, True, _EXTRA_THEMES.get(h.symbol, ())),
     _PORTFOLIO_ALIASES.get(h.symbol, ()))
    for h in config.PORTFOLIO
] + [
    (Org("HAVELLS", "Havells India", "Consumer Electricals", True), ("havells",)),
    (Org("ETERNAL", "Eternal (formerly Zomato)", "Consumer Internet", True), ("zomato", "eternal")),
    (Org("AMBER", "Amber Enterprises India", "Electronics Manufacturing", True), ("amber enterprises",)),
    (Org("SYRMA", "Syrma SGS Technology", "Electronics Manufacturing", True), ("syrma", "syrma sgs")),
    (Org("AARTIIND", "Aarti Industries", "Specialty Chemicals", True), ("aarti industries",)),
    (Org("NAVINFLUOR", "Navin Fluorine International", "Specialty Chemicals", True,
         ("fluorochemicals",)), ("navin fluorine",)),
    (Org("SRF", "SRF", "Specialty Chemicals", True, ("fluorochemicals",)), ("srf",)),
    (Org("UPL", "UPL", "Agrochemicals", True), ("upl", "united phosphorus")),
    (Org("BAJFINANCE", "Bajaj Finance", "NBFC / Consumer Lending", True), ("bajaj finance",)),
    (Org("M&MFIN", "Mahindra & Mahindra Financial Services", "NBFC / Rural Lending", True,
         ("vehicle finance",)), ("mahindra finance", "m&m finance")),
    (Org("ICICIBANK", "ICICI Bank", "Banking", True), ("icici bank",)),
    (Org("TCS", "Tata Consultancy Services", "IT Services", True), ("tcs",)),
    (Org("WIPRO", "Wipro", "IT Services", True), ("wipro",)),
    (Org("KEI", "KEI Industries", "Cables & Wires", True), ("kei industries",)),
    (Org(None, "McKinsey & Company", "Consulting", False), ("mckinsey",)),
    (Org(None, "Tata Electronics", "Electronics Manufacturing", False, ("semiconductors",)),
     ("tata electronics",)),
    (Org(None, "Micron Technology India", "Semiconductors", False), ("micron", "micron india")),
]

_SUFFIXES = {"ltd", "limited", "pvt", "private", "india", "co", "company", "corp", "corporation",
             "inc", "incorporated", "llp", "plc", "and", "the"}
_CANON = {
    "tech": "tech", "technology": "tech", "technologies": "tech",
    "inds": "industries", "industry": "industries",
    "intl": "international",
    "pharmaceutical": "pharma", "pharmaceuticals": "pharma",
    "fin": "finance", "financial": "finance",
    "svcs": "services", "mfg": "manufacturing",
}
# Words whose presence/absence alone doesn't change which company is meant.
_GENERIC = {"group", "holdings", "global", "international", "services", "solutions", "industries",
            "enterprises", "tech"}


def _ascii_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def normalize_org(raw: str) -> str:
    s = _ascii_lower(raw).replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [_CANON.get(t, t) for t in s.split()]
    while toks and toks[0] == "the":
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _SUFFIXES:
        if toks[-1] == "india" and toks[-2] == "of":  # "State Bank of India"
            break
        toks.pop()
    return " ".join(toks)


_ALIAS: dict[str, Org] = {}
for _org, _aliases in _ORGS:
    for _a in (_org.name, *_aliases):
        _k = normalize_org(_a)
        if _k in _ALIAS and _ALIAS[_k] != _org:
            raise ValueError(f"alias collision: {_k!r}")
        _ALIAS[_k] = _org
ORG_BY_TICKER: dict[str, Org] = {o.ticker: o for o, _ in _ORGS if o.ticker}
KNOWN_ORGS: tuple[Org, ...] = tuple(o for o, _ in _ORGS)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def token_set_ratio(a: str, b: str) -> int:
    """Same definition as fuzzywuzzy/rapidfuzz token_set_ratio, stdlib only."""
    ta, tb = set(a.split()), set(b.split())
    inter = " ".join(sorted(ta & tb))
    ca = " ".join(filter(None, [inter, " ".join(sorted(ta - tb))]))
    cb = " ".join(filter(None, [inter, " ".join(sorted(tb - ta))]))
    pairs = [(ca, cb)] + ([(inter, ca), (inter, cb)] if inter else [])
    return round(100 * max(_ratio(x, y) for x, y in pairs))


def _only_typos_differ(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    for tok in ta ^ tb:
        if tok in _GENERIC:
            continue
        other = (tb - ta) if tok in ta else (ta - tb)
        if not any(_ratio(tok, o) >= 0.8 for o in other):
            return False
    return True


@dataclass(frozen=True)
class OrgMatch:
    org: Org | None
    key: str
    method: str   # ticker | alias | fuzzy | ambiguous | unknown
    score: int

    @property
    def org_id(self) -> str:
        """Stable identity for joins. Unknown orgs still join on their normalised key."""
        if self.org is None:
            return f"u:{self.key}"
        return self.org.ticker or f"k:{normalize_org(self.org.name)}"


def resolve_org(raw: str) -> OrgMatch:
    key = normalize_org(raw)
    if key in _ALIAS:
        return OrgMatch(_ALIAS[key], key, "alias", 100)
    hits: dict[str, tuple[Org, int]] = {}
    for alias_key, org in _ALIAS.items():
        score = token_set_ratio(key, alias_key)
        if score >= FUZZY_MIN and _only_typos_differ(key, alias_key):
            prev = hits.get(org.name)
            if prev is None or score > prev[1]:
                hits[org.name] = (org, score)
    if len(hits) == 1:
        org, score = next(iter(hits.values()))
        return OrgMatch(org, key, "fuzzy", score)
    return OrgMatch(None, key, "ambiguous" if hits else "unknown", 0)


def resolve_org_or_ticker(text: str) -> OrgMatch:
    sym = (text or "").strip().upper()
    if sym in ORG_BY_TICKER:
        return OrgMatch(ORG_BY_TICKER[sym], normalize_org(ORG_BY_TICKER[sym].name), "ticker", 100)
    return resolve_org(text)


# ====================================================================== people
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "ca", "cs", "cma", "shri", "sri", "smt",
               "kum", "km", "adv", "er", "sir", "late"}
_POST_NOMINALS = {"ca", "cfa", "fca", "aca", "acs", "cs", "cma", "phd", "jr", "sr", "mba"}

SAME_PERSON, POSSIBLE = "same_person", "possible"


def normalize_person(name: str) -> tuple[str, ...]:
    s = re.sub(r"[^a-z ]", " ", _ascii_lower(name).replace(".", " "))
    toks = s.split()
    while toks and toks[0] in _HONORIFICS:
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _POST_NOMINALS:
        toks.pop()
    return tuple(toks)


def _given_ok(x: str, y: str) -> bool:
    return x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))


def names_compatible(a: str, b: str) -> bool:
    ta, tb = normalize_person(a), normalize_person(b)
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1]:
        return False  # surname-only ("Mr. Sharma") is never enough
    ga, gb = ta[:-1], tb[:-1]
    return all(_given_ok(x, y) for x, y in zip(ga, gb))


@dataclass(frozen=True)
class PersonMatch:
    person_id: str
    name: str
    confidence: str          # SAME_PERSON | POSSIBLE
    reason: str


def match_person(name: str, candidates: Iterable[tuple[str, str, frozenset[str]]],
                 org_hints: Iterable[str] = ()) -> list[PersonMatch]:
    """candidates: (person_id, display name, org_ids they have worked at)."""
    hint_ids = {resolve_org(h).org_id for h in org_hints if h}
    out = []
    for pid, pname, org_ids in candidates:
        if not names_compatible(name, pname):
            continue
        shared = hint_ids & org_ids
        if shared:
            out.append(PersonMatch(pid, pname, SAME_PERSON, "name matches and shared employer"))
        else:
            out.append(PersonMatch(pid, pname, POSSIBLE, "name matches; no shared employer to confirm"))
    return sorted(out, key=lambda m: m.confidence != SAME_PERSON)
