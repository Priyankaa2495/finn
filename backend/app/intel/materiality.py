"""Materiality scoring: decides what the investor sees first and what gets collapsed.

score = category base (adjusted by what the filing actually says)
        + boost for categories the investor said they care about
        + position size
Tiers:  >= 70 'Act on'   |  40-69 'Worth knowing'   |  < 40 'Routine' (collapsed)

Additive and hand-tuned on purpose: an investor can read the breakdown and disagree
with it, and the next step is learning the weights from what they open vs. dismiss."""
import re

BASE = {
    "M&A": 82, "results": 78, "management_change": 70, "litigation": 68, "fund_raise": 66,
    "capex": 62, "pledging": 58, "credit_rating": 48, "insider_trading": 45,
    "press_release": 38, "bonus_split": 38, "dividend": 30, "earnings_call": 26,
    "agm_egm": 14, "postal_ballot": 12, "investor_meetings": 10, "unclassified": 4,
}


def adjust(category, text, mgmt=None, kind=None):
    """Read the filing for signals that move it within its category."""
    t = (text or "").lower()
    adj, why = 0, []
    if category == "management_change" and mgmt:
        top = mgmt.get("role") in ("CEO", "CFO", "MD", "Chairman", "COO")
        if top and mgmt.get("leaving"):
            adj, why = 22, [f"{mgmt['role']} exit"]
        elif top:
            adj, why = 14, [f"new {mgmt['role']}"]
        elif mgmt.get("role") in ("Company Secretary", "Independent Director"):
            adj, why = -35, ["non-executive / secretarial change"]
    elif category == "credit_rating":
        if "downgrad" in t or "negative" in t:
            adj, why = 30, ["downgrade / negative outlook"]
        elif "upgrad" in t:
            adj, why = 10, ["upgrade"]
        elif "reaffirm" in t:
            adj, why = -25, ["reaffirmed"]
    elif category == "pledging":
        adj, why = (15, ["pledge created"]) if re.search(r"creat|invok", t) else (-20, ["pledge released"])
    elif category == "results":
        if re.search(r"intimation|board meeting (is )?scheduled|to consider", t):
            adj, why = -62, ["results date notice, not results"]
    elif category == "earnings_call":
        if "transcript" in t:
            adj, why = 10, ["transcript available"]
    elif category == "press_release":
        if re.search(r"order|contract|win|bag", t):
            adj, why = 15, ["order win"]
        if re.search(r"clarification|rumou?r", t):
            adj, why = 20, ["rumour clarification"]
    elif category == "fund_raise" and kind == "bank" and re.search(r"debenture|ncd|commercial paper", t):
        adj, why = -45, ["routine debt issuance for a lender"]
    elif category == "litigation":
        if re.search(r"(crore|cr\.?)\b", t) or "nclt" in t or "closure" in t:
            adj, why = 10, ["financial / operational impact stated"]
    elif category == "insider_trading":
        if "promoter" in t and re.search(r"sale|sold|dispos", t):
            adj, why = 20, ["promoter selling"]
    return adj, why


def score(category, text, weight=0.0, prefs=None, mgmt=None, kind=None):
    prefs = prefs or {}
    s = BASE.get(category, 4)
    adj, why = adjust(category, text, mgmt, kind)
    s += adj
    if category in prefs.get("care_about", []):
        s += 6
        why.append("you follow this type")
    if category in prefs.get("muted", []):
        s = min(s, 20)
        why.append("muted by you")
    s += min(8, weight)  # a 8% position matters more than a 2% one
    s = max(0, min(100, round(s)))
    tier = "act" if s >= 70 else "know" if s >= 40 else "routine"
    return s, tier, why
