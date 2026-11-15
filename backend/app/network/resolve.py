"""Entity resolution: turning messy strings ("Havells India Ltd.", "Mr. Arvind
Kulkarni, CA") into canonical entities we can join on.

Design: exact alias lookup first (cheap, explainable), fuzzy matching second with a
high threshold, and never auto-merge a *person* on name alone — a name match must be
corroborated by a shared organisation, because India has a lot of Rahul Sharmas."""
import json
import re
from functools import lru_cache

from rapidfuzz import fuzz, process

from ..config import DATA_DIR

_SUFFIXES = r"\b(ltd|limited|pvt|private|inc|corp|corporation|co|company|the|plc|llp)\b"
_HONORIFICS = r"^(mr|mrs|ms|dr|shri|smt|sri|prof|ca|cs|cma)\.?\s+"


def norm_org(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[().,'\-]", " ", s)
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"\bindia\b$", " ", s.strip())  # "Havells India" == "Havells", but keep "Bank of India"
    return re.sub(r"\s+", " ", s).strip()


def norm_person(s: str) -> str:
    s = (s or "").lower().strip()
    for _ in range(3):  # "Mr. Dr. X" happens in filings
        s = re.sub(_HONORIFICS, "", s)
    s = re.sub(r",.*$", "", s)  # drop trailing ", CA" / ", Chartered Accountant"
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class OrgIndex:
    def __init__(self, orgs):
        self.orgs = {o["id"]: o for o in orgs}
        self.alias = {}
        self.by_ticker = {}
        for o in orgs:
            for a in [o["name"], *o.get("aliases", [])]:
                self.alias[norm_org(a)] = o["id"]
            if o.get("ticker"):
                self.by_ticker[o["ticker"].upper()] = o["id"]
                self.alias.setdefault(norm_org(o["ticker"]), o["id"])
        self._keys = list(self.alias)

    @lru_cache(maxsize=4096)
    def resolve(self, name: str):
        """Return (org_id, confidence) or (None, 0)."""
        key = norm_org(name)
        if not key:
            return None, 0.0
        if key in self.alias:
            return self.alias[key], 1.0
        hit = process.extractOne(key, self._keys, scorer=fuzz.token_set_ratio)
        if hit and hit[1] >= 92 and len(key) > 3:
            return self.alias[hit[0]], hit[1] / 100
        return None, 0.0

    def get(self, oid):
        return self.orgs.get(oid)

    def related(self, oid):
        """Value-chain neighbours, both directions. Returns {org_id: relation}."""
        o = self.orgs.get(oid) or {}
        rel = {}
        for k, label in (("competitors", "competitor"), ("suppliers", "supplier"), ("customers", "customer")):
            for x in o.get(k, []):
                rel[x] = label
        for other in self.orgs.values():  # reverse edges
            if oid in other.get("competitors", []):
                rel.setdefault(other["id"], "competitor")
            if oid in other.get("customers", []):  # other sells to us -> other is our supplier
                rel.setdefault(other["id"], "supplier")
            if oid in other.get("suppliers", []):  # other buys from us -> other is our customer
                rel.setdefault(other["id"], "customer")
        return rel


@lru_cache(maxsize=1)
def org_index() -> OrgIndex:
    data = json.loads((DATA_DIR / "orgs.json").read_text(encoding="utf-8"))
    return OrgIndex(data["orgs"])


def same_person(a_name, b_name, a_orgs: set, b_orgs: set) -> float:
    """Probability-ish score that two person records are the same human."""
    s = fuzz.token_sort_ratio(norm_person(a_name), norm_person(b_name)) / 100
    if s < 0.9:
        return 0.0
    return s if (a_orgs & b_orgs) else min(s, 0.5)  # name-only match is capped: "possible", never "same"
