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

# (category, pattern on exchange 'desc', pattern on text or None) — order matters
DESC_RULES = [
    ("results", r"financial result|integrated filing.*financial", None),
    ("management_change", r"change in (management|directorate)|appointment|resignation|cessation|re-appointment", None),
    ("credit_rating", r"credit rating", None),
    ("M&A", r"acquisition|amalgamation|merger|scheme of arrangement|demerger|joint venture", None),
    ("insider_trading", r"insider trading|sebi \(pit\)|substantial acquisition|takeover", None),
    ("pledging", r"pledge|encumbrance", None),
    ("fund_raise", r"raising of funds|qualified institutional|preferential issue|rights issue|buy ?back|commercial paper|debentures", None),
    ("earnings_call", r"con\.? ?call|conference call|earnings call|audio recording|transcript", None),
    ("investor_meetings", r"analysts?/institutional investor meet|investor meet|investor presentation", None),
    ("dividend", r"dividend", None),
    ("bonus_split", r"bonus|sub-division|split", None),
    ("litigation", r"litigation|dispute|action\(s\) taken|orders passed|fraud|default|insolvency|penalty", None),
    ("postal_ballot", r"postal ballot", None),
    ("agm_egm", r"shareholders? meeting|annual general meeting|\bagm\b|\begm\b|extra-ordinary", None),
    ("unclassified", r"trading window|newspaper publication|reg\.? 74|depositories and participants|loss of share|"
                     r"esop|esos|esps|allotment of securities|book closure|record date|compliance|certificate|"
                     r"spurt in volume|duplicate share|scrutinizer|business responsibility|esg", None),
]

# Keyword rules on the attachment text (used for generic descs like 'Updates', 'Outcome of Board Meeting')
TEXT_RULES = [
    ("results", r"(un)?audited financial results|results for the (quarter|half year|year) ended"),
    ("management_change", r"\b(appoint(ed|ment)|resign(ed|ation)|cessation|step(s|ped)? down)\b.*\b(chief|cfo|ceo|md|managing director|director|kmp|company secretary)\b"),
    ("M&A", r"\b(acquir(e|ed|ing|sition)|merger|amalgamation|demerger|joint venture|stake sale|divest)"),
    ("capex", r"\b(capex|capital expenditure|new (plant|facility|unit)|greenfield|brownfield|capacity expansion)"),
    ("fund_raise", r"\b(qip|preferential (issue|allotment)|rights issue|buyback|non-convertible debentures|ncds?|raise (up to )?(rs|₹|inr))"),
    ("litigation", r"\b(show cause|penalty|order (passed|received)|demand notice|tribunal|nclt|search (and|&) seizure|tax demand)"),
    ("dividend", r"\b(interim|final|special) dividend"),
    ("credit_rating", r"\b(crisil|icra|care ratings|india ratings|brickwork)\b"),
    ("press_release", r"\b(order (win|worth|received)|bags? (an? )?order|launch(es|ed)?|partnership|mou|clarification)"),
    ("earnings_call", r"(earnings|conference|analyst) call"),
    ("investor_meetings", r"(investor|analyst) (meet|meeting|conference)"),
]


def classify(exchange_desc: str, text: str, use_llm=True):
    """Returns (category, method, confidence)."""
    d = (exchange_desc or "").lower()
    t = (text or "").lower()
    generic = re.search(r"^(updates?|general updates?|outcome of board meeting|press release|board meeting intimation)$", d.strip())
    if not generic:
        for cat, pat, _ in DESC_RULES:
            if re.search(pat, d):
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
            f"Exchange subject: {exchange_desc}\nFiling text: {text[:1500]}\n\n"
            f"Classify into exactly one of {CATEGORIES}. Reply as JSON "
            '{"category": "...", "confidence": 0-1}.',
            tier="fast",
            system="You classify Indian listed-company exchange filings. Be literal; routine compliance paperwork is 'unclassified'.")
        if out and out.get("category") in CATEGORIES:
            return out["category"], "llm:fast", float(out.get("confidence", 0.6))
    return "unclassified", "fallback", 0.3


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
