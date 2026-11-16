"""Rules-first classifier evaluation harness.

    python scripts/eval_classify.py pull      # fetch the held-out window from NSE (no labels)
    python scripts/eval_classify.py sheet     # print unlabelled rows for blind labelling (no predictions)
    python scripts/eval_classify.py run       # score dev + held-out; writes eval/report_<tag>.json

Sets (backend/eval/):
  filings_dev.jsonl      filings seen while building the rules (the committed Brief snapshot)
  filings_holdout.jsonl  NSE announcements 16 Oct - 7 Nov 2026 for the 15 holdings, minus any
                         filing in the dev set. Labelled blind (subject+body only, before scoring).
Gold labels carry `labeled_by` so they can be audited. No API key is used anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402
from app.intel.classify_eval import evaluate, load_jsonl, summary_line, write_jsonl  # noqa: E402

EVAL = BACKEND / "eval"
DEV = EVAL / "filings_dev.jsonl"
HOLDOUT = EVAL / "filings_holdout.jsonl"
FRESH = EVAL / "filings_fresh.jsonl"   # untouched window: scored once, after tuning froze
WINDOW = (date(2026, 10, 16), date(2026, 11, 7))
KEEP = ("id", "symbol", "filed_at", "subject", "body", "attachment_url")


SETS = {"dev": DEV, "holdout": HOLDOUT, "fresh": FRESH}


def cmd_pull(args) -> None:
    """Fetch a window from NSE, excluding every filing already in another set."""
    from app.ingest.filings import NSEClient
    target = SETS[args.set]
    lo, hi = (date.fromisoformat(args.start), date.fromisoformat(args.end)) if args.start else WINDOW
    taken = {r["id"] for name, p in SETS.items() if name != args.set and p.exists() for r in load_jsonl(p)}
    client = NSEClient()
    rows, seen = [], set()
    for sym in config.PORTFOLIO_SYMBOLS:
        for r in client.announcements(sym, lo, hi):
            if r["id"] in taken or r["id"] in seen or not (lo.isoformat() <= r["filed_at"][:10] <= hi.isoformat()):
                continue
            seen.add(r["id"])
            rows.append({k: r.get(k) for k in KEEP} | {"gold": None, "labeled_by": None})
    rows.sort(key=lambda r: (r["filed_at"], r["id"]))
    write_jsonl(target, rows)
    print(f"{args.set}: {len(rows)} filings ({lo} to {hi}), {len(taken)} ids from other sets excluded -> {target}")


def cmd_sheet(args) -> None:
    rows = load_jsonl(SETS[args.set])
    for i, r in enumerate(rows):
        if r.get("gold") and not args.all:
            continue
        print(f"{i:>3} | {r['symbol']:<10} | {r['subject'][:70]:<70} | {(r.get('body') or '')[:150]}")


def _baseline_classify():
    """Load eval/classify_baseline.py (rules frozen before tuning) as app.intel.classify_baseline."""
    import importlib.util
    name = "app.intel.classify_baseline"
    spec = importlib.util.spec_from_file_location(name, EVAL / "classify_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod.classify


def cmd_run(args) -> None:
    from app.intel.classify import classify
    fn = _baseline_classify() if args.baseline else classify
    report = {}
    for name, path in SETS.items():
        if not path.exists() or not any(r.get("gold") for r in load_jsonl(path)):
            continue
        m = evaluate(load_jsonl(path), fn)
        report[name] = m
        print(summary_line(name, m))
    if args.verbose:
        for name in report:
            m = report[name]
            print(f"\n== {name}: uncovered ({len(m['uncovered'])}) ==")
            for v in m["uncovered"]:
                print(f"  gold={v['gold']:<18} | {v['symbol']:<10} | {v['subject'][:90]}")
            print(f"== {name}: wrong ({len(m['misses'])}) ==")
            for v in m["misses"]:
                print(f"  gold={v['gold']:<18} pred={v['pred']:<18} rule={v['rule']:<18} | {v['subject'][:70]}")
            print(f"== {name}: BURIED material ({len(m['buried'])}) ==")
            for v in m["buried"]:
                print(f"  gold={v['gold']:<18} pred={v['pred']:<18} | {v['symbol']} | {v['subject'][:70]}")
    out = EVAL / f"report_{args.tag}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--set", choices=tuple(SETS), default="holdout")
    p.add_argument("--start")
    p.add_argument("--end")
    p.set_defaults(fn=cmd_pull)
    s = sub.add_parser("sheet")
    s.add_argument("--set", choices=tuple(SETS), default="holdout")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_sheet)
    r = sub.add_parser("run")
    r.add_argument("--tag", default="latest")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--baseline", action="store_true", help="score the frozen pre-tuning rules")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args.fn(args)


if __name__ == "__main__":
    main()
