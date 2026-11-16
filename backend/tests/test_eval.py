"""CI gate for the rules-first claim, on real NSE filings with blind gold labels.

Gated sets (regression floors):
  dev      95 filings seen while building (Nov 1-15 2026 snapshot)
  holdout 108 filings 16 Oct - 7 Nov 2026 (rules were tuned on its misses once, 2026-11-15)
Floors: precision >= 95%, coverage >= 85%, zero material filings buried, zero model calls.

The `fresh` set (1-15 Oct 2026) is a one-shot generalization measurement taken after tuning
froze. It is integrity-checked here but deliberately NOT gated: gating it would invite tuning
on it, and then it would stop measuring generalization.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.intel.classify import CATEGORIES
from app.intel.classify_eval import COVERAGE_FLOOR, PRECISION_FLOOR, evaluate, load_jsonl

EVAL = Path(__file__).resolve().parent.parent / "eval"
SETS = {name: load_jsonl(EVAL / f"filings_{name}.jsonl") for name in ("dev", "holdout", "fresh")}


@pytest.mark.parametrize("name", ["dev", "holdout"])
def test_rules_meet_precision_and_coverage_floors(name):
    m = evaluate(SETS[name])
    assert m["model_calls"] == 0
    assert m["coverage"] >= COVERAGE_FLOOR, f"{name} coverage {m['coverage']:.1%}"
    assert m["precision"] >= PRECISION_FLOOR, f"{name} precision {m['precision']:.1%}: {m['misses']}"


@pytest.mark.parametrize("name", ["dev", "holdout"])
def test_no_material_filing_is_buried(name):
    buried = evaluate(SETS[name])["buried"]
    assert buried == [], f"{name}: material filings pushed into the routine pile: {buried}"


@pytest.mark.parametrize("name", ["dev", "holdout", "fresh"])
def test_labels_are_complete_and_auditable(name):
    rows = SETS[name]
    assert rows, f"{name} is empty"
    assert all(r.get("gold") in CATEGORIES for r in rows)
    assert all(r.get("labeled_by") for r in rows)


def test_sets_are_disjoint():
    ids = {name: {r["id"] for r in rows} for name, rows in SETS.items()}
    assert not ids["dev"] & ids["holdout"]
    assert not ids["dev"] & ids["fresh"]
    assert not ids["holdout"] & ids["fresh"]


def test_fresh_set_is_measured_not_gated():
    m = evaluate(SETS["fresh"])
    assert m["n"] == 76 and m["coverage"] == 1.0          # recorded, not a floor
