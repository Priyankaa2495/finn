"""Rule-first announcement classifier.

Pipeline (first hit wins):
  1. Ordered regex rules on the NSE subject line. Generic subjects ("Outcome of
     Board Meeting") are refined with body keywords, and fall back to a fixed
     default if the body says nothing. Never a model.
  2. If no subject rule fires, the same rules run over the start of the body.
  3. Only if both miss AND a fallback is supplied is the model asked. It receives
     sanitised, fenced text and must answer with one of CATEGORIES, or the answer
     is discarded.

Order matters: routine paperwork and narrow patterns sit above broad ones, so
"Trading Window" never reads as insider_trading and "Appointment of Statutory
Auditor" never reads as management_change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .sanitize import fence

CATEGORIES: tuple[str, ...] = (
    "results", "M&A", "fund_raise", "management_change", "litigation", "insider_trading",
    "earnings_call", "investor_meetings", "credit_rating", "dividend", "bonus_split",
    "capex", "press_release", "postal_ballot", "agm_egm", "pledging", "unclassified",
)


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    category: str                                    # default when no refinement hits
    refine: tuple[tuple[str, re.Pattern], ...] = ()  # (category, body pattern), first hit wins


# ------------------------------------------------------------ body refinements
_B_RESULTS = _r(r"financial results|\bunaudited\b|\baudited\b|results for the (quarter|half|year)|quarterly results")
_B_FUND = _r(r"\bqip\b|qualified institutions? placement|preferential (issue|allotment)|rights issue|buy-?back"
             r"|non-convertible debentures|\bncds?\b|commercial paper|raising of funds|fund ?rais")
_B_MA = _r(r"acqui(re|sition)|amalgamation|\bmerger\b|demerger|scheme of arrangement|joint venture")
_B_BONUS = _r(r"\bbonus\b|sub-?division|stock split|split of")
_B_DIVIDEND = _r(r"\bdividend")
_B_CAPEX = _r(r"\bcapex\b|capital expenditure|capacity expansion|new (plant|facility|manufacturing)|greenfield|brownfield")
_B_MGMT = _r(r"\bappoint|\bresign|cessation|chief (executive|financial)|managing director")
_B_EARNINGS = _r(r"earnings|results|transcript|audio|recording|\bq[1-4]\b")
_B_RESULTS_NOTICE = _r(r"(meeting of the )?board of directors.{0,120}\b(consider|approve)\b.{0,160}\bresults"
                       r"|analyst presentation|results presentation")

_BOARD_REFINE = (
    ("results", _B_RESULTS), ("fund_raise", _B_FUND), ("M&A", _B_MA), ("bonus_split", _B_BONUS),
    ("dividend", _B_DIVIDEND), ("capex", _B_CAPEX), ("management_change", _B_MGMT),
)

# ------------------------------------------------------------------ the rules
RULES: tuple[Rule, ...] = (
    # Routine paperwork first so broad words below can't capture it.
    Rule("trading_window", _r(r"trading\s*window"), "unclassified"),
    Rule("routine_paperwork", _r(
        r"compliance certificate|certificate under (sebi|reg)|depositories and participants"
        r"|reg(ulation)?\.?\s*74\s*\(5\)|loss of (share )?certificate"
        r"|duplicate (share )?certificate|newspaper (publication|advertisement|clippings?)|copy of newspaper"
        r"|\besop\b|\besos\b|\besps\b|employee stock option|\bbrsr\b|business responsibility"
        r"|\besg\b|sustainability report|registrar and (share )?transfer agent|\brta\b"
        # eval 2026-11-15: NSE compliance subjects with no rule
        r"|monitoring agency report|statement of deviations?|deviation\(?s?\)? or variation|reg(ulation)?\.?\s*32\b"),
         "unclassified"),
    # Only auditor appointments/changes are routine; "report of auditors on financial
    # statements" is part of a results pack (eval 2026-11-15).
    Rule("auditor", _r(r"(appointment|change|resignation|re-?appointment|cessation|rotation)\s+(of|in)\s+(the\s+)?"
                       r"(statutory\s+|secretarial\s+|internal\s+|cost\s+|joint\s+)?auditors?"
                       r"|\bauditors?\s+(appointment|rotation|resignation|change)"), "unclassified"),
    # Meetings & calls. NSE's "Analysts/Institutional Investor Meet/Con. Call Updates"
    # covers both, so it is refined on the body. A board meeting to approve results filed
    # under this subject is results (eval 2026-11-15, RELIANCE).
    Rule("analyst_meet", _r(r"analysts?\s*/\s*institutional investor|investor meet|analyst meet"
                            r"|analyst\(?s?\)?\s*/\s*investor\(?s?\)?\s*meet|investor conference|non-deal roadshow"),
         "investor_meetings", (("results", _B_RESULTS_NOTICE), ("earnings_call", _B_EARNINGS))),
    Rule("earnings_call", _r(r"transcript|audio recording|video recording|earnings call|conference call"
                             r"|con\.?\s?call|concall"), "earnings_call"),
    Rule("credit_rating", _r(r"credit rating|\bratings?\b"), "credit_rating"),
    Rule("pledging", _r(r"pledg|encumbr|regulation\s*31\b"), "pledging"),
    Rule("insider_trading", _r(r"insider trading|prohibition of insider|regulation\s*7\s*\(\s*[23]|\bsast\b"
                               r"|regulation\s*29\b"), "insider_trading"),
    # Generic board subjects: refined on the body, never sent to a model.
    Rule("board_outcome", _r(r"outcome of (the )?board meeting|board meeting outcome"),
         "press_release", _BOARD_REFINE),
    Rule("board_intimation", _r(r"board meeting intimation|intimation of board meeting|prior intimation"
                                r"|notice of board meeting"), "unclassified", _BOARD_REFINE),
    Rule("postal_ballot", _r(r"postal ballot|e-?voting|scrutini[sz]er"), "postal_ballot"),
    Rule("agm_egm", _r(r"\bagm\b|\begm\b|annual general meeting|extra-?ordinary general meeting|general meeting"),
         "agm_egm"),
    Rule("results", _r(r"financial results?|\bresults?\b|integrated filing.{0,5}financ|investor presentation"
                       r"|earnings presentation|shareholders'? letter|analyst presentation"
                       r"|financial statements for the (quarter|half|year|period)|limited review report"), "results"),
    Rule("management_change", _r(r"change in (management|directors?|key managerial|kmp|senior management)"
                                 r"|\bappointment|re-?appointment|resignation|cessation|\bretire|demise"
                                 r"|chief (executive|financial|operating)|managing director"), "management_change"),
    Rule("bonus_split", _r(r"\bbonus\b|stock split|sub-?division|split of (equity )?shares"), "bonus_split"),
    Rule("dividend", _r(r"\bdividend"), "dividend"),
    Rule("record_date", _r(r"record date|book closure"), "unclassified",
         (("bonus_split", _B_BONUS), ("dividend", _B_DIVIDEND), ("fund_raise", _B_FUND))),
    Rule("fund_raise", _r(r"\bqip\b|qualified institutions?|preferential|rights issue|buy-?back|commercial paper"
                          r"|debentures?|\bncds?\b|fund ?raising|raising of funds"
                          r"|allotment of (equity|securities|shares|warrants)|issue of securities"
                          r"|first and final call|call money|partly paid|initial public offer|\bipo\b"),
         # ESOP allotments filed under "Allotment of Securities" are routine (eval 2026-11-15)
         "fund_raise", (("unclassified", _r(r"\besop|\besps|\besos|employee stock")),)),
    Rule("m_and_a", _r(r"acqui|\bmerger\b|amalgamation|demerger|scheme of arrangement|joint venture|\bjv\b"
                       r"|incorporation of (a )?(wholly[- ]owned )?subsidiary|investment in (wholly[- ]owned )?subsidiar"
                       r"|sale of (stake|shares|subsidiary)|divest|disinvest|slump sale"
                       r"|business transfer agreement|share purchase agreement|shareholders'? agreement"
                       r"|term sheet|\brestructuring\b"), "M&A"),
    Rule("capex", _r(r"\bcapex\b|capital expenditure|capacity expansion|capacity addition|new (plant|facility|unit)"
                     r"|greenfield|brownfield|commercial production|commission(ing|ed)"
                     r"|expansion of .{0,40}\b(plant|facility|centre|center|campus|capacity|unit)\b"), "capex"),
    Rule("litigation", _r(r"litigation|dispute|regulatory action|penalt|\bfines?\b|show[- ]?cause|demand (notice|order)"
                          r"|tax demand|insolvency|\bnclt\b|\bcirp\b|winding up|search (and|&) seizure"
                          r"|enforcement directorate|warning letter|import alert|form 483"
                          r"|actions?\(?s?\)? taken or orders? passed|orders? passed by"), "litigation"),
    Rule("order_win", _r(r"bagging|receiving of orders?|order (win|inflow)|bags? (an? )?order|letter of award"
                         r"|\bloa\b|\bcontracts?\b"), "press_release"),
    Rule("press_release", _r(r"press release|media release|clarification|news verification|spurt in"
                             r"|general updates?|\bupdates?\b|\blaunch|partnership|tie-?up|\bmou\b"
                             r"|memorandum of understanding|\baward"),
         # A press release on the quarter's performance is the results release (eval 2026-11-15)
         "press_release", (("results", _r(r"performance of the company for the (quarter|half|year)"
                                          r"|(un)?audited financial results")),)),
)

assert all(r.category in CATEGORIES for r in RULES)
assert all(c in CATEGORIES for r in RULES for c, _ in r.refine)


@dataclass(frozen=True)
class Classification:
    category: str
    source: str            # subject | subject+body | body | model | model_rejected | none
    rule: str | None
    model_called: bool = False


# A fallback takes fenced text and returns a category string (or anything else, which is rejected).
ModelFallback = Callable[[str], str]

_BODY_SCAN_CHARS = 1500


def _match(text: str) -> Rule | None:
    for rule in RULES:
        if rule.pattern.search(text):
            return rule
    return None


# NSE files a large share of announcements under a bare "Updates" subject with the
# real headline in the body ("... informed the Exchange regarding 'Earnings Call
# Transcript'"). Such subjects carry no signal: classify on the body, default to
# press_release, never escalate to a model.
_OPAQUE_SUBJECT = re.compile(r"^\s*(general\s+)?updates?\s*$", re.I)


def classify(subject: str, body: str = "", llm_fallback: ModelFallback | None = None) -> Classification:
    subject = subject or ""
    body = body or ""

    if _OPAQUE_SUBJECT.match(subject):
        rule = _match(body[:_BODY_SCAN_CHARS])
        if rule:
            return Classification(rule.category, "body", rule.name)
        return Classification("press_release", "subject", "opaque_update")

    rule = _match(subject)
    if rule:
        for category, pattern in rule.refine:
            if pattern.search(body):
                return Classification(category, "subject+body", rule.name)
        return Classification(rule.category, "subject", rule.name)

    rule = _match(body[:_BODY_SCAN_CHARS])
    if rule:
        return Classification(rule.category, "body", rule.name)

    if llm_fallback is None:
        return Classification("unclassified", "none", None)

    answer = llm_fallback(fence(f"SUBJECT: {subject}\nBODY: {body}", label="FILING"))
    answer = (answer or "").strip()
    if answer in CATEGORIES:
        return Classification(answer, "model", None, model_called=True)
    return Classification("unclassified", "model_rejected", None, model_called=True)
