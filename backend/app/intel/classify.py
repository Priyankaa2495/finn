"""Announcement classification into the 17 reference categories.

Rules first. NSE already attaches a 'desc' (subject type) to every filing; combined
with keywords from the attachment text that resolves the large majority of filings
for free, instantly and explainably. Only genuinely ambiguous ones (generic
'Updates', 'Outcome of Board Meeting' with no clear signal) go to the fast LLM tier.
"""
import re

from .. import llm

CATEGORIES = ["results", "M&A", "fund_raise", "management_change", "litigation",
              "insider_trading", "earnings_call", "investor_meetings", "credit_rating",
              "dividend", "bonus_split", "capex", "press_release", "postal_ballot",
              "agm_egm", "pledging", "unclassified"]

# (category, pattern on exchange 'desc', pattern the text must ALSO match, or None) — order matters.
# NSE's subject lines are a fixed taxonomy, so most rules key on them; the text condition
# splits subjects that NSE uses for two different things.
DESC_RULES = [
    ("results", r"financial result|integrated filing.*financial", None),
    # Reference doc: results include decks, shareholder letters and ads confirming results were published.
    # (Held-out set, 16 Oct–7 Nov 2026: these were the largest block of results filed as routine.)
    ("results", r"^investor presentation$", None),
    ("results", r"newspaper publication|press release", r"financial results|results for the"),
    ("management_change", r"change in (management|directorate|directors?\b)|appointment|resignation|cessation|"
                          r"re-appointment|retirement", None),
    ("press_release", r"bagging|receiving of orders|award of (order|contract)", None),
    ("credit_rating", r"credit rating", None),
    ("M&A", r"acquisition|amalgamation|merger|scheme of arrangement|demerger|joint venture|sale or disposal|"
            r"restructuring", None),
    ("insider_trading", r"insider trading|sebi \(pit\)|substantial acquisition|takeover", None),
    ("pledging", r"pledge|encumbrance", None),
    # NSE files earnings-call items and one-on-one fund meetings under the same subject
    ("earnings_call", r"analysts?/institutional investor meet", r"transcript|recording|audio|earnings call|conference call"),
    ("investor_meetings", r"analysts?/institutional investor meet|investor meet", None),
    ("fund_raise", r"allotment of securities", r"preferential|qualified institutional|\bqip\b|rights issue|"
                                               r"non[- ]convertible|debenture"),
    ("fund_raise", r"raising of funds|issue of securities|qualified institutional|preferential issue|rights issue|"
                   r"buy ?back|commercial paper|debentures", None),
    ("earnings_call", r"con\.? ?call|conference call|earnings call|audio recording|transcript", None),
    ("dividend", r"dividend", None),
    ("dividend", r"record date", r"dividend"),
    ("postal_ballot", r"shareholders? meeting", r"postal ballot"),
    ("bonus_split", r"bonus|sub-division|split", None),
    ("litigation", r"litigation|dispute|action\(s\) taken|orders passed|fraud|default|insolvency|penalty", None),
    ("postal_ballot", r"postal ballot", None),
    ("agm_egm", r"shareholders? meeting|annual general meeting|\bagm\b|\begm\b|extra-ordinary", None),
    ("unclassified", r"trading window|newspaper publication|reg\.? 74|depositories and participants|loss of share|"
                     r"esop|esos|esps|allotment of securities|book closure|record date|compliance|certificate|"
                     r"spurt in volume|duplicate share|scrutinizer|business responsibility|esg|statement of deviation|"
                     r"monitoring agency", None),
]

# Keyword rules on the attachment text (used for generic descs like 'Updates', 'Outcome of Board Meeting')
TEXT_RULES = [
    ("results", r"(un)?audited financial results|results for the (quarter|period|half[- ]year|year)"
                r"( and half[- ]year)? ended"),
    ("management_change", r"\b(appoint(ed|ment)|resign(ed|ation)|cessation|step(s|ped)? down)\b.*\b(chief|cfo|ceo|md|managing director|director|kmp|company secretary)\b"),
    ("M&A", r"\b(acquir(e|ed|ing|sition)|merger|amalgamation|demerger|joint venture|stake sale|divest|"
            r"binding term sheet|share purchase agreement)"),
    ("capex", r"\b(capex|capital expenditure|new (plant|facility|unit)|greenfield|brownfield|capacity expansion)"),
    ("fund_raise", r"\b(qip|preferential (issue|allotment)|rights issue|buyback|non-convertible debentures|ncds?|raise (up to )?(rs|₹|inr))"),
    ("litigation", r"\b(show cause|penalty|order (passed|received)|demand notice|tribunal|nclt|search (and|&) seizure|tax demand)"),
    ("dividend", r"\b(interim|final|special) dividend"),
    ("credit_rating", r"\b(crisil|icra|care ratings|india ratings|brickwork)\b"),
    ("press_release", r"\b(order (win|worth|received)|bags? (an? )?order|launch(es|ed)?|partnership|mou|clarification)"),
    ("earnings_call", r"(earnings|conference|analyst) call"),
    ("investor_meetings", r"(investor|analyst) (meet|meeting|conference)"),
    # routine paperwork that arrives under a generic subject; settling it here saves a model call
    ("unclassified", r"redemption of commercial paper|dematerial|loss of share|duplicate share"),
]


def classify(exchange_desc: str, text: str, use_llm=True):
    """Returns (category, method, confidence)."""
    d = (exchange_desc or "").lower()
    t = (text or "").lower()
    generic = re.search(r"^(updates?|general updates?|outcome of board meeting|press release|board meeting intimation)$", d.strip())
    if not generic:
        for cat, pat, text_pat in DESC_RULES:
            if re.search(pat, d) and (text_pat is None or re.search(text_pat, t)):
                return cat, "rule:desc", 0.95
    for cat, pat in TEXT_RULES:
        if re.search(pat, t):
            return cat, "rule:text", 0.85
    if "board meeting intimation" in d:
        return "results" if "result" in t else "unclassified", "rule:desc", 0.7
    if "press release" in d:
        return "press_release", "rule:desc", 0.7
    if use_llm and t:
        out = llm.complete(
            f"Exchange subject: {exchange_desc}\nFiling text:\n{llm.untrusted(text, 1500)}\n\n"
            "Classify into exactly one category, using these definitions:\n" + DEFINITIONS +
            '\nReply as JSON {"category": "...", "confidence": 0-1}.',
            tier="fast", system=SYSTEM)
        if out and out.get("category") in CATEGORIES:
            return out["category"], "llm:fast", float(out.get("confidence", 0.6))
    return "unclassified", "fallback", 0.3


# The model only sees what rules couldn't settle, so it needs the taxonomy spelled out:
# given bare labels it defaulted to 'unclassified' and buried a real fund-raise.
# Definitions follow the challenge's reference document.
DEFINITIONS = "\n".join(f"- {k}: {v}" for k, v in {
    "results": "quarterly/annual results, the board meeting approving them, results decks",
    "M&A": "acquisition, merger, demerger, joint venture, buying or selling a stake",
    "fund_raise": "raising money: rights issue, QIP, preferential issue or allotment, buyback, debt such as "
                  "commercial paper or NCDs, including the committee or board outcome approving one",
    "management_change": "a CEO, CFO, MD or director joining, resigning or retiring",
    "litigation": "lawsuits, regulatory orders, fines, warnings, insolvency proceedings",
    "insider_trading": "a promoter or insider buying or selling their own shares",
    "earnings_call": "the earnings-call notice, transcript or recording",
    "investor_meetings": "meetings with specific investors or funds, investor conferences",
    "credit_rating": "a rating assigned, upgraded, downgraded or reaffirmed",
    "dividend": "a cash dividend declared, with its record date",
    "bonus_split": "a bonus issue or stock split",
    "capex": "approving spend on a new plant, facility or capacity",
    "press_release": "general company news not covered above: order wins, products, partnerships, awards, "
                     "a parent or group company's announcement, rumour clarifications",
    "postal_ballot": "postal ballot notice or result",
    "agm_egm": "AGM or EGM notice or result",
    "pledging": "a promoter pledging or releasing shares",
    "unclassified": "routine compliance paperwork that cannot move the stock: ESOP allotments, certificates, "
                    "newspaper copies, shareholder reminders",
}.items())
SYSTEM = ("You classify Indian listed-company exchange filings for an investor. Burying a material filing "
          "costs far more than surfacing a routine one, so use 'unclassified' only when the text is clearly "
          "routine paperwork. Abbreviations are common: PI = preferential issue, QIP = qualified "
          "institutional placement, NCD = non-convertible debenture.")


_BOILER = re.compile(r"^.*?has informed the exchange (about|regarding|that|of)\s*", re.I)


def clean_text(text: str) -> str:
    """NSE attachment text is mostly 'X Limited has informed the Exchange about ...'.
    Strip the boilerplate so a deterministic one-liner is readable without an LLM."""
    t = _BOILER.sub("", (text or "").strip())
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^(that |its |the company )+", "", t, flags=re.I)
    return (t[:1].upper() + t[1:]) if t else ""


def mgmt_signal(text: str) -> dict:
    """Who, which role, joining or leaving — regex over the filing text."""
    t = text or ""
    role = None
    for r, pat in (("CEO", r"\b(ceo|chief executive)"), ("CFO", r"\b(cfo|chief financial)"),
                   ("MD", r"\b(md|managing director)\b"), ("Chairman", r"\bchair(man|person)\b"),
                   ("COO", r"\b(coo|chief operating)"), ("Independent Director", r"independent director"),
                   ("Company Secretary", r"company secretary|compliance officer"), ("Director", r"\bdirector\b")):
        if re.search(pat, t, re.I):
            role = r
            break
    leaving = bool(re.search(r"resign|cessation|step(s|ped)? down|superannuat|retire", t, re.I))
    m = re.search(r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Shri)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z.]+){0,3})", t)
    return {"role": role, "leaving": leaving, "person": m.group(1).strip() if m else None}
