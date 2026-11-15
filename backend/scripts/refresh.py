"""Daily ingestion job. Run after market close (~16:30 IST):

    python -m scripts.refresh            # fetch prices, filings, fundamentals into the DB
    python -m scripts.refresh --export   # ...and write data/snapshot/*.json for the deployed app

Runs from a laptop, a GitHub Actions cron, or any box NSE doesn't block."""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, snapshot  # noqa: E402
from app.config import DATA_DIR  # noqa: E402
from app.ingest import filings, fundamentals, prices  # noqa: E402
from app.intel import classify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--skip-fundamentals", action="store_true")
    ap.add_argument("--reclassify", action="store_true",
                    help="no fetching: re-run the classifier over the committed snapshot and re-export it")
    a = ap.parse_args()

    db.init()
    if a.reclassify:
        return reclassify_snapshot()
    pf = json.loads((DATA_DIR / "portfolio.json").read_text(encoding="utf-8"))
    hold = [h["symbol"] for h in pf["holdings"]]
    watch = [w["symbol"] for w in pf["research_watchlist"]]

    print(f"prices:   {prices.fetch(hold)} rows")
    db.kv_set("prices_synthetic", False)
    print(f"filings:  {filings.fetch(hold, days=a.days)} rows")

    # classify everything now so the web tier never has to (rules first, LLM only if ambiguous)
    with db.conn() as c:
        todo = [dict(r) for r in c.execute("SELECT id, exchange_desc, text FROM filings WHERE category IS NULL")]
    stats = {}
    for f in todo:
        cat, method, _ = classify.classify(f["exchange_desc"], f["text"])
        stats[method] = stats.get(method, 0) + 1
        with db.conn() as c:
            c.execute("UPDATE filings SET category=?, cls_method=? WHERE id=?", (cat, method, f["id"]))
    print(f"classified {len(todo)}: {stats}")

    if not a.skip_fundamentals:
        ok = sum(1 for s in hold + watch if fundamentals.fetch(s, force=True))
        print(f"fundamentals: {ok}/{len(hold + watch)}")
    if a.export:
        print("snapshot:", snapshot.export())


def reclassify_snapshot():
    """After a rule change, relabel the snapshot without hitting NSE again. Labels a model
    already assigned are kept (they were paid for); everything else is re-derived."""
    snapshot.load()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id, exchange_desc, text, cls_method FROM filings WHERE demo=0")]
    stats, changed = {}, 0
    for f in rows:
        if (f["cls_method"] or "").startswith("llm"):
            stats["llm (kept)"] = stats.get("llm (kept)", 0) + 1
            continue
        cat, method, _ = classify.classify(f["exchange_desc"], f["text"])
        stats[method] = stats.get(method, 0) + 1
        with db.conn() as c:
            changed += c.execute("UPDATE filings SET category=?, cls_method=?, summary=NULL WHERE id=? "
                                 "AND (category IS NOT ? OR cls_method IS NOT ?)",
                                 (cat, method, f["id"], cat, method)).rowcount
    rules = stats.get("rule:desc", 0) + stats.get("rule:text", 0)
    print(f"reclassified {len(rows)}: {stats}  changed {changed}  "
          f"no model call: {rules}/{len(rows)} ({100 * rules / max(len(rows), 1):.0f}%)")
    print("snapshot:", snapshot.export())


if __name__ == "__main__":
    main()
