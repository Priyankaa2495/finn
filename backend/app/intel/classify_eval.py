"""Evaluation of the rules-first classifier against hand-labelled filings.

Definitions (all on the rules path, no model):
  covered      a rule fired (source != 'none'); uncovered filings would be the ones a model sees
  coverage     covered / all
  precision    correct / covered            (is a rule's answer right when it gives one?)
  accuracy     correct / all                (uncovered counted as 'unclassified')
  buried       gold category is MATERIAL but prediction is not  -> the costly error
  noise        gold is not material but prediction is            -> the cheap error
Materiality here is category-level (the Brief's ACT/KNOW/ROUTINE tiers add weight/network on top).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from .classify import CATEGORIES, classify

MATERIAL = frozenset({"results", "M&A", "fund_raise", "management_change", "litigation", "capex",
                      "pledging", "credit_rating", "insider_trading", "dividend", "bonus_split"})

PRECISION_FLOOR = 0.95
COVERAGE_FLOOR = 0.85


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def evaluate(rows: list[dict], classify_fn: Callable = classify) -> dict:
    labelled = [r for r in rows if r.get("gold")]
    assert all(r["gold"] in CATEGORIES for r in labelled), "unknown gold label"
    out = {"n": len(labelled), "covered": 0, "correct": 0, "correct_covered": 0, "model_calls": 0,
           "buried": [], "noise": [], "misses": [], "uncovered": []}
    confusion: Counter = Counter()
    for r in labelled:
        c = classify_fn(r["subject"], r.get("body", ""))
        out["model_calls"] += int(c.model_called)
        pred, gold = c.category, r["gold"]
        covered = c.source != "none"
        out["covered"] += covered
        ok = pred == gold
        out["correct"] += ok
        out["correct_covered"] += ok and covered
        view = {"id": r["id"], "symbol": r["symbol"], "subject": r["subject"], "gold": gold, "pred": pred,
                "rule": c.rule, "source": c.source}
        if not covered:
            out["uncovered"].append(view)
        elif not ok:
            out["misses"].append(view)
        if gold in MATERIAL and pred not in MATERIAL:
            out["buried"].append(view)
        if gold not in MATERIAL and pred in MATERIAL:
            out["noise"].append(view)
        if not ok:
            confusion[(gold, pred)] += 1
    n = max(out["n"], 1)
    out["coverage"] = out["covered"] / n
    out["precision"] = out["correct_covered"] / max(out["covered"], 1)
    # Same metric on rows whose gold label the annotator did not flag as a judgement call.
    # Same definition as `precision` (correct / covered), restricted to clear-cut rows.
    uncovered_ids = {v["id"] for v in out["uncovered"]}
    wrong_ids = {v["id"] for v in out["misses"]}
    clear = [r for r in labelled if not r.get("ambiguous") and r["id"] not in uncovered_ids]
    out["n_unambiguous"] = len(clear)
    out["precision_unambiguous"] = (sum(1 for r in clear if r["id"] not in wrong_ids) / len(clear)) if clear else 0.0
    out["accuracy"] = out["correct"] / n
    out["material"] = sum(1 for r in labelled if r["gold"] in MATERIAL)
    out["confusion"] = [{"gold": g, "pred": p, "count": k} for (g, p), k in confusion.most_common()]
    return out


def summary_line(name: str, m: dict) -> str:
    return (f"{name:<8} n={m['n']:>3}  coverage={m['coverage']:6.1%}  precision={m['precision']:6.1%}  "
            f"(unambiguous {m['precision_unambiguous']:6.1%}, n={m['n_unambiguous']})  "
            f"buried={len(m['buried'])}/{m['material']}  noise={len(m['noise'])}  model_calls={m['model_calls']}")
