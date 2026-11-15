"""Measure the filing classifier against hand-labelled filings.

    python -m scripts.eval_classify                       # rules only (free, instant)
    python -m scripts.eval_classify --llm                 # also send the residue to the fast model
    python -m scripts.eval_classify --file eval/held_out.jsonl

Reports what matters for the cost/quality trade-off:
  * coverage   — share of filings the rules settle with no model call
  * precision  — of those, how many match the human label
  * residue    — how the model (or the 'unclassified' fallback) does on the rest
  * materiality misses — material filings (results, M&A, management change, ...) that
    would land in the routine pile; the error an investor actually pays for

eval/filings_dev.jsonl is the 15 Nov 2026 live pull. The rules were tuned on it, so its
score is a regression floor, not a generalisation estimate: label a later week's
filings into a held-out file and run with --file to get that.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.intel import classify  # noqa: E402

MATERIAL = {"results", "M&A", "management_change", "litigation", "fund_raise", "capex", "pledging", "credit_rating"}


def run(path, use_llm):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if use_llm and not config.LLM_ENABLED:
        sys.exit("--llm needs GEMINI_API_KEY (or FINN_OPENAI_BASE_URL) set")
    by_method, right, errors, material_miss = Counter(), Counter(), [], []
    for r in rows:
        cat, method, _ = classify.classify(r["exchange_desc"], r["text"], use_llm=use_llm)
        group = "rules" if method.startswith("rule") else ("model" if method.startswith("llm") else "fallback")
        by_method[group] += 1
        if cat == r["label"]:
            right[group] += 1
        else:
            errors.append((group, r["symbol"], r["exchange_desc"][:40], r["label"], cat))
            if r["label"] in MATERIAL and cat not in MATERIAL:
                material_miss.append(r)

    n = len(rows)
    print(f"{n} labelled filings from {path}")
    print(f"coverage:  {by_method['rules']}/{n} ({100 * by_method['rules'] / n:.0f}%) settled by rules, no model call")
    for g in ("rules", "model", "fallback"):
        if by_method[g]:
            print(f"  {g:<9} accuracy {right[g]}/{by_method[g]} ({100 * right[g] / by_method[g]:.0f}%)")
    total = sum(right.values())
    print(f"overall:   {total}/{n} ({100 * total / n:.0f}%)")
    print(f"material filings sent to the routine pile: {len(material_miss)}")
    if errors:
        print("\nerrors (method, symbol, NSE subject, expected -> got):")
        for e in errors:
            print(f"  {e[0]:<9} {e[1]:<11} {e[2]:<40} {e[3]} -> {e[4]}")
    if use_llm:
        from app import llm
        print(f"\nmodel usage: {llm.STATS}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "eval" / "filings_dev.jsonl"))
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    db.init()  # the model cache lives in the DB
    run(a.file, a.llm)


if __name__ == "__main__":
    main()
