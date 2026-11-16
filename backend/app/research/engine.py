"""Pull orchestrator.

Data (Option C): committed snapshot (backend/snapshot/research/<SYM>.json) first, then a
local live-fetch cache younger than LIVE_TTL_HOURS, then a live fetch. Live fetches for one
symbol are serialised by a per-symbol asyncio.Lock with a double-check after acquiring it,
so N concurrent requests for an unseen ticker trigger exactly one scrape.
(Per-process only: with several workers, a DB lease would replace the lock.)

Analysis: metrics (code) -> guidance (fast model selects IDs, or rules) -> verdicts
(code) -> facts pack -> bull/bear (deep model must cite fact IDs; numbers checked) or
deterministic fallback.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import config, db
from . import guidance as G
from . import metrics as M

SNAPSHOT_DIR = config.BACKEND_DIR / "snapshot" / "research"
CACHE_DIR = config.DATA_DIR / "research_cache"

BULL_BEAR_SYSTEM = (
    "You write a tight bull case and bear case for an equity investor using ONLY the numbered "
    "facts provided. No outside knowledge, no news, no estimates. Every point must cite one or "
    "more fact IDs, and every number you write must appear in a cited fact. Max 3 points per side, "
    "max 25 words each. Reply with JSON only: "
    '{"bull": [{"point": "...", "facts": ["F1"]}], "bear": [{"point": "...", "facts": ["F2"]}]}'
)
MAX_POINTS = 3


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class ResearchEngine:
    def __init__(self, llm: Callable | None = None, *, snapshot_dir: Path = SNAPSHOT_DIR,
                 cache_dir: Path = CACHE_DIR, fetch_live: Callable[[str], dict] | None = None,
                 ttl_hours: float = config.LIVE_TTL_HOURS):
        self.llm = llm
        self.snapshot_dir = Path(snapshot_dir)
        self.cache_dir = Path(cache_dir)
        self.ttl = timedelta(hours=ttl_hours)
        self._fetch_live = fetch_live
        self._locks: dict[str, asyncio.Lock] = {}
        self.live_fetches = 0

    # ---------------------------------------------------------------- data
    def _cached(self, sym: str) -> dict | None:
        snap = _load_json(self.snapshot_dir / f"{sym}.json")
        if snap:
            return snap
        live = _load_json(self.cache_dir / f"{sym}.json")
        if live and live.get("fetched_at"):
            age = datetime.now(timezone.utc) - datetime.fromisoformat(live["fetched_at"])
            if age < self.ttl:
                return live
        return None

    def _store(self, sym: str, bundle: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{sym}.json").write_text(json.dumps(bundle), encoding="utf-8")

    def _live(self, sym: str) -> dict:
        if self._fetch_live is None:
            from ..ingest.screener import fetch_bundle
            return fetch_bundle(sym)
        return self._fetch_live(sym)

    async def get_bundle(self, symbol: str) -> dict:
        sym = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
            raise ValueError(f"invalid symbol {symbol!r}")
        bundle = self._cached(sym)
        if bundle:
            return bundle
        lock = self._locks.setdefault(sym, asyncio.Lock())  # atomic: no await between get and set
        async with lock:
            bundle = self._cached(sym)                       # someone may have fetched while we waited
            if bundle:
                return bundle
            self.live_fetches += 1
            bundle = await asyncio.to_thread(self._live, sym)
            bundle.setdefault("fetched_at", db.utcnow_iso())
            self._store(sym, bundle)
            return bundle

    async def research(self, symbol: str) -> dict:
        bundle = await self.get_bundle(symbol)
        return await asyncio.to_thread(analyse, bundle, self.llm)


# ------------------------------------------------------------------ analysis
def analyse(bundle: dict, llm: Callable | None = None) -> dict:
    sym = bundle["symbol"]
    fin = M.Financials.from_dict(sym, bundle["financials"])
    rows = M.annual_rows(fin)
    book = M.FactBook()
    M.financial_facts(fin, book)

    g = G.extract_guidance(bundle.get("transcripts", []), llm)
    items: list[G.GuidanceItem] = sorted(g["items"], key=lambda i: (i.fy, i.metric, i.call_date))
    verdicts = {i: G.evaluate(it, fin) for i, it in enumerate(items)}
    drifts = G.drift(items)
    G.guidance_facts(items, verdicts, drifts, book)

    return {
        "symbol": sym,
        "data": {"source": bundle.get("source", "snapshot"), "fetched_at": bundle.get("fetched_at"),
                 "synthetic": bool(bundle.get("synthetic"))},
        "sources": _sources(bundle),
        "financial_snapshot": M.quarter_snapshot(fin),
        "annual": rows,
        "trajectory": M.trajectory(rows),
        "guidance": {
            "mode": g["mode"], "sentences": g["sentences"], "candidates": g["candidates"],
            "items": [_item_view(it, verdicts[i]) for i, it in enumerate(items)],
            "drift": [_drift_view(d) for d in drifts],
            "rejected": [{"sentence_id": r.sentence_id, "reason": r.reason} for r in g["rejections"]],
        },
        "facts": [{"id": f.id, "text": f.text, "polarity": f.polarity, "kind": f.kind} for f in book.facts],
        "bull_bear": bull_bear(book, llm),
    }


def _sources(bundle: dict) -> list[dict]:
    out = [{"kind": "financial tables (screener.in)", "url": bundle["financials"].get("source_url")}]
    out += [{"kind": f"{t['call']} concall transcript", "url": t.get("url"), "pages": len(t["pages"])}
            for t in bundle.get("transcripts", [])]
    return out


def _item_view(it: G.GuidanceItem, v: G.Verdict) -> dict:
    return {"metric": it.metric, "label": G.METRIC_LABEL[it.metric], "period": M.fy_label(it.fy),
            "target": it.target.display(), "quote": it.quote, "call": it.call, "page": it.page,
            "sentence_id": it.sentence_id, "source_url": it.url, "selected_by": it.selected_by,
            "verdict": {"status": v.status, "actual": v.actual, "basis": v.basis, "text": v.text}}


def _drift_view(d: G.Drift) -> dict:
    return {"metric": d.metric, "period": M.fy_label(d.fy), "from": d.before.display(),
            "to": d.after.display(), "from_call": d.before_call, "to_call": d.after_call,
            "direction": d.direction}


# ----------------------------------------------------------------- bull/bear
def validate_points(points, facts: dict[str, M.Fact]) -> tuple[list[dict], list[dict]]:
    ok, rejected = [], []
    if not isinstance(points, list):
        return ok, [{"point": None, "reason": "not a list"}]
    for p in points:
        if not isinstance(p, dict):
            rejected.append({"point": None, "reason": "malformed point"})
            continue
        text = " ".join(str(p.get("point", "")).split())[:240]
        ids = p.get("facts") if isinstance(p.get("facts"), list) else []
        ids = [str(i) for i in ids]
        if not text or not ids:
            rejected.append({"point": text, "reason": "no fact citation"})
            continue
        unknown = [i for i in ids if i not in facts]
        if unknown:
            rejected.append({"point": text, "reason": f"cites unknown fact(s) {unknown}"})
            continue
        allowed = set().union(*(G.numbers_in(facts[i].text) for i in ids))
        stray = G.numbers_in(text) - allowed
        if stray:
            rejected.append({"point": text, "reason": f"number(s) {sorted(stray)} not in cited facts"})
            continue
        ok.append({"point": text, "facts": ids})
    return ok[:MAX_POINTS], rejected


def _rules_bull_bear(book: M.FactBook) -> dict:
    ranked = sorted(book.facts, key=lambda f: -f.weight)
    bull = [{"point": f.text, "facts": [f.id]} for f in ranked if f.polarity > 0][:MAX_POINTS]
    bear = [{"point": f.text, "facts": [f.id]} for f in ranked if f.polarity < 0][:MAX_POINTS]
    return {"mode": "rules", "bull": bull, "bear": bear, "rejected": []}


def bull_bear(book: M.FactBook, llm: Callable | None) -> dict:
    if llm is None or not book.facts:
        return _rules_bull_bear(book)
    prompt = "FACTS:\n" + "\n".join(f"{f.id}: {f.text}" for f in book.facts)
    raw = llm("deep", BULL_BEAR_SYSTEM, prompt)
    if raw is None:
        return _rules_bull_bear(book)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        out = _rules_bull_bear(book)
        out["rejected"] = [{"point": None, "reason": "model returned invalid JSON"}]
        return out
    facts = book.by_id()
    bull, rej_b = validate_points(data.get("bull"), facts)
    bear, rej_r = validate_points(data.get("bear"), facts)
    rules = _rules_bull_bear(book)
    return {"mode": "model",
            "bull": bull or rules["bull"],      # a side with no valid point falls back to rules
            "bear": bear or rules["bear"],
            "rejected": rej_b + rej_r}
