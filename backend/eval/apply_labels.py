"""Gold labels for the classifier eval, assigned blind (subject + body only, before any scoring).

Annotator: Claude (the coding agent that also wrote the rules) -> NOT independent. Labels follow
the reference taxonomy plus two conventions fixed in Phase 1 before this eval existed:
  - newspaper ads, ESOP/ESPS allotments & grants, RTA/address changes, certificates -> unclassified
  - results decks, results press releases, results-board-meeting notices, auditors' review
    reports on results -> results ("results decks and shareholder letters")
Rows marked `ambiguous` are judgement calls a second annotator could reasonably label differently.
Run: python eval/apply_labels.py   (idempotent; rewrites gold/labeled_by in both JSONL files)
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from app.intel.classify_eval import load_jsonl, write_jsonl  # noqa: E402

ANNOTATOR = "claude-blind-2026-11-15"

R, MA, FR, MC, LIT = "results", "M&A", "fund_raise", "management_change", "litigation"
EC, IM, CR, DIV, CAP = "earnings_call", "investor_meetings", "credit_rating", "dividend", "capex"
PR, PB, U = "press_release", "postal_ballot", "unclassified"

# Index-aligned with filings_holdout.jsonl (sorted by filed_at, id).
HOLDOUT = [
    LIT, U, IM, U, PR, R, IM, PR, U, EC,          # 0-9
    PR, PR, R, FR, IM, EC, MA, PR, PR, PB,        # 10-19
    PR, CR, PR, U, U, EC, IM, R, DIV, PR,         # 20-29
    IM, EC, R, EC, U, MA, U, FR, U, PR,           # 30-39
    PR, PR, U, IM, PR, U, R, EC, R, U,            # 40-49
    EC, IM, R, PR, EC, IM, LIT, R, FR, FR,        # 50-59
    MA, R, U, PR, EC, IM, IM, U, EC, MA,          # 60-69
    LIT, R, DIV, R, DIV, R, PR, U, R, PR,         # 70-79
    R, EC, U, R, PR, LIT, U, R, U, U,             # 80-89
    R, MC, R, EC, CAP, R, R, R, R, EC,            # 90-99
    EC, LIT, MA, U, EC, U, PB, MC,                # 100-107
]
HOLDOUT_AMBIGUOUS = {
    6: "bare 'Presentation' under analyst-meet subject; could be a results deck",
    7: "opaque Reg 30 'General Updates'", 39: "opaque Reg 30 'General Updates'",
    16: "WOS incorporation is a Section 8 foundation; M&A by taxonomy, not by substance",
    20: "guarantee/surety disclosure has no taxonomy home; labelled press_release",
    35: "Jio Platforms IPO: stake sale in subsidiary (M&A) vs fund_raise",
    53: "subsidiary listing logistics; press_release vs M&A",
    69: "'Other Restructuring' (voluntary ... of subsidiary): M&A vs litigation/restructuring",
    72: "subject 'Dividend' but body leads with results approval",
    94: "development-centre expansion: capex vs press_release",
}

# Index-aligned with filings_dev.jsonl.
DEV = [
    EC, PR, PR, IM, EC, MA, PR, IM, U, U,         # 0-9
    EC, U, U, FR, PR, EC, U, LIT, PR, EC,         # 10-19
    U, EC, R, CR, IM, PB, R, PR, U, IM,           # 20-29
    R, PR, U, EC, R, R, U, EC, U, FR,             # 30-39
    U, MC, U, U, EC, U, LIT, U, MC, IM,           # 40-49
    EC, U, IM, PB, MC, IM, MC, PB, PR, PR,        # 50-59
    IM, FR, IM, PB, IM, U, EC, IM, U, R,          # 60-69
    R, PR, CAP, IM, IM, MC, MC, EC, EC, PR,       # 70-79
    IM, U, U, IM, MC, EC, FR, MA, IM, IM,         # 80-89
    MC, IM, U, R, MA,                             # 90-94
]
DEV_AMBIGUOUS = {
    2: "exchange news-verification of a regulatory-order story", 14: "same",
    13: "call-money reminder on partly paid shares: fund_raise by taxonomy, routine in substance",
    27: "commissioning press release: press_release vs capex", 31: "opaque press release",
    32: "'Quarterly Report w.r.t. Financial...' compliance report",
    71: "press release likely on results; body truncated",
}


# Index-aligned with filings_fresh.jsonl (1-15 Oct 2026). Pulled and labelled AFTER the rules
# were tuned on holdout; scored once with rules frozen. No rule was changed after scoring it.
FRESH = [
    LIT, FR, IM, PR, PR, MC, PB, U, U, PR,        # 0-9
    PR, PR, U, U, U, U, PR, PR, R, DIV,           # 10-19
    EC, PR, PR, U, MA, U, PB, PR, PB, MC,         # 20-29
    LIT, LIT, PR, PR, U, FR, LIT, LIT, U, U,      # 30-39
    U, U, LIT, IM, IM, PR, PR, U, PR, MC,         # 40-49
    MC, U, PR, PR, IM, LIT, MC, MC, MC, CR,       # 50-59
    PR, LIT, LIT, U, U, MA, MA, U, R, R,          # 60-69
    R, R, U, IM, EC, U,                           # 70-75
]
FRESH_AMBIGUOUS = {
    9: "opaque LODR intimation", 33: "opaque 'General Updates'", 45: "opaque 'General Updates'",
    16: "Titan pre-results 'Quarterly Update': business update vs results",
    18: "board-meeting notice, purpose truncated (likely results)",
    20: "investor conference on quarterly performance: earnings_call vs investor_meetings",
    48: "board outcome with no stated purpose", 55: "press release about an NCLAT order: litigation vs press_release",
    64: "ESG rating is not a credit rating; labelled routine", 65: "Jio IPO (see holdout #35)", 66: "same",
}


def apply(path: Path, labels: list[str], ambiguous: dict[int, str]) -> int:
    rows = load_jsonl(path)
    if len(rows) != len(labels):
        raise SystemExit(f"{path.name}: {len(rows)} rows but {len(labels)} labels")
    for i, (row, gold) in enumerate(zip(rows, labels)):
        row["gold"], row["labeled_by"] = gold, ANNOTATOR
        row["ambiguous"] = ambiguous.get(i)
    write_jsonl(path, rows)
    return len(rows)


if __name__ == "__main__":
    print("holdout", apply(HERE / "filings_holdout.jsonl", HOLDOUT, HOLDOUT_AMBIGUOUS))
    print("dev", apply(HERE / "filings_dev.jsonl", DEV, DEV_AMBIGUOUS))
    print("fresh", apply(HERE / "filings_fresh.jsonl", FRESH, FRESH_AMBIGUOUS))
