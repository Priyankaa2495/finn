"""Additive, explainable materiality scoring for Push.

score = category base + content modifiers + position weight + network + your watchlist
tier  = ACT (>= 60), KNOW (>= 30), ROUTINE (< 30; collapsed in the digest)

Every point carries a reason string so the UI can show "why this is here".
No model is involved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ACT, KNOW, ROUTINE = "ACT", "KNOW", "ROUTINE"
ACT_AT, KNOW_AT = 60, 30

BASE: dict[str, int] = {
    "results": 55, "M&A": 55, "litigation": 45, "management_change": 40, "fund_raise": 40,
    "capex": 40, "pledging": 35, "credit_rating": 25, "insider_trading": 25, "bonus_split": 20,
    "press_release": 20, "dividend": 15, "earnings_call": 15, "investor_meetings": 5,
    "postal_ballot": 5, "agm_egm": 5, "unclassified": 0,
}

_CXO = re.compile(r"\b(ceo|cfo|coo|chief (executive|financial|operating)|managing director|\bmd\b"
                  r"|chair(man|person)|whole[- ]?time director|executive director)", re.I)
_ACTUAL_RESULTS = re.compile(r"(un)?audited|financial results for the", re.I)
_RESULTS_NOTICE = re.compile(r"intimation|to consider|will be held|trading window|to announce", re.I)
_DOWNGRADE = re.compile(r"downgrad|watch (with )?negative|outlook.{0,20}negative|\bsuspend", re.I)
_UPGRADE = re.compile(r"upgrad|outlook.{0,20}positive", re.I)
_REAFFIRM = re.compile(r"reaffirm", re.I)
_PLEDGE_UP = re.compile(r"creat|invok", re.I)
_TRANSCRIPT = re.compile(r"transcript", re.I)

POSITION_POINTS_PER_PCT = 1.5
POSITION_CAP = 15
NETWORK_POINTS = 15
WATCHED_POINTS = 15


@dataclass(frozen=True)
class Materiality:
    score: int
    tier: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _tier(score: int) -> str:
    return ACT if score >= ACT_AT else KNOW if score >= KNOW_AT else ROUTINE


def score(category: str, subject: str = "", body: str = "", *, position_weight_pct: float = 0.0,
          network_links: int = 0, watched_categories: frozenset[str] | set[str] = frozenset()) -> Materiality:
    text = f"{subject} {body}"
    reasons: list[str] = []
    total = 0

    def add(points: int, why: str) -> None:
        nonlocal total
        if points:
            total += points
            reasons.append(f"{points:+d} {why}")

    add(BASE.get(category, 0), f"category {category}")

    if category == "management_change":
        if _CXO.search(text):
            add(20, "top-management role")
        else:
            add(-20, "not a CXO/MD role")
    elif category == "results":
        if _RESULTS_NOTICE.search(text) and not _ACTUAL_RESULTS.search(text):
            add(-35, "notice of results, not the results")
        elif _ACTUAL_RESULTS.search(text):
            add(10, "actual results filed")
    elif category == "credit_rating":
        if _DOWNGRADE.search(text):
            add(30, "downgrade / negative watch")
        elif _UPGRADE.search(text):
            add(10, "upgrade")
        elif _REAFFIRM.search(text):
            add(-10, "rating reaffirmed")
    elif category == "pledging" and _PLEDGE_UP.search(text):
        add(15, "pledge created or invoked")
    elif category == "earnings_call" and _TRANSCRIPT.search(text):
        add(10, "transcript available for research")

    add(min(POSITION_CAP, round(position_weight_pct * POSITION_POINTS_PER_PCT)),
        f"{position_weight_pct:g}% of portfolio")
    if network_links > 0:
        add(NETWORK_POINTS, f"{network_links} person(s) in your network linked")
    if category in watched_categories:
        add(WATCHED_POINTS, "category you watch")

    final = max(0, min(100, total))
    return Materiality(final, _tier(final), tuple(reasons))
