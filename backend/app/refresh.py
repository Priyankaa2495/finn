"""Daily refresh (run by a scheduler, e.g. a Render Cron Job) and the committed Brief snapshot.

Ingestion decision (2026-11-15):
- Portfolio (15 stocks): EAGER. Prices + filings, then the PDFs behind results and
  management-change filings are parsed now (results card, SEBI brief profile) so the morning
  Brief never waits on a download.
- Ad-hoc Ask tickers: LAZY, fetched on first request by the research engine.

Snapshot: `python -m app.refresh --snapshot` writes backend/snapshot/brief/brief_snapshot.json.
A fresh container (Render disks are ephemeral) seeds its DB from it, so the app works offline.

    python -m app.refresh               # live refresh into data/finn.db
    python -m app.refresh --snapshot    # ...and export the committed snapshot
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path

from . import config, db
from .ingest import filings as nse
from .ingest import prices
from .intel import results as R
from .research import metrics as M

log = logging.getLogger(__name__)

SNAPSHOT_PATH = config.BACKEND_DIR / "snapshot" / "brief" / "brief_snapshot.json"
EAGER_CATEGORIES = ("results", "management_change")
PRICE_DAYS = 60
FILING_DAYS = 14


def enrich_eager(conn, symbols=config.PORTFOLIO_SYMBOLS, session=None, delay_s: float = 1.5,
                 fetch_pdf=R.fetch_pdf, fetch_financials=None) -> int:
    """Parse attachments for eager categories that have no enrichment yet. Returns count."""
    marks = ",".join("?" * len(symbols))
    with db.LOCK:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT f.* FROM filings f
                WHERE f.symbol IN ({marks}) AND f.category IN ('results', 'management_change')
                  AND f.attachment_url LIKE '%.pdf'
                  AND NOT EXISTS (SELECT 1 FROM enrichment e WHERE e.filing_id = f.id)
                ORDER BY f.filed_at DESC""", list(symbols))]
    fins: dict[str, M.Financials | None] = {}
    done = 0
    for f in rows:
        try:
            data = fetch_pdf(f["attachment_url"], session)
        except Exception as exc:
            log.warning("enrich %s: %s", f["id"], exc)
            continue
        filed = date.fromisoformat(f["filed_at"][:10])
        if f["category"] == "results":
            if f["symbol"] not in fins and fetch_financials:
                try:
                    fins[f["symbol"]] = M.Financials.from_dict(f["symbol"], fetch_financials(f["symbol"]))
                except Exception as exc:
                    log.warning("screener %s: %s", f["symbol"], exc)
                    fins[f["symbol"]] = None
            card = R.card_from_pdf(f["symbol"], filed, data, fins.get(f["symbol"]), f["attachment_url"])
            if card.source == R.NOT_TABULATED and not card.rows and "no results statement" in " ".join(card.flags):
                continue  # e.g. a board-meeting notice classified as results: nothing to show
            with db.LOCK:
                db.put_enrichment(conn, f["id"], "results_card", card.__dict__)
        else:
            bio = R.extract_bio(f["body"], R.pdf_pages(data, max_pages=10))
            if bio:
                with db.LOCK:
                    db.put_enrichment(conn, f["id"], "bio", bio)
        done += 1
        time.sleep(delay_s)
    return done


def refresh_all(conn, symbols=config.PORTFOLIO_SYMBOLS) -> dict:
    from .ingest.screener import fetch_bundle
    out = {"prices": prices.refresh(conn, symbols, period="3mo", force=True)}
    client = nse.NSEClient()
    out["filings"] = nse.refresh(conn, symbols, days=FILING_DAYS, client=client, force=True)
    out["enriched"] = enrich_eager(conn, symbols, session=client.session,
                                   fetch_financials=lambda s: fetch_bundle(s, n_transcripts=0)["financials"])
    return out


# ------------------------------------------------------------------- snapshot
def export_snapshot(conn, path: Path = SNAPSHOT_PATH) -> Path:
    with db.LOCK:
        price_rows = [dict(r) for r in conn.execute(
            """SELECT symbol, date, open, high, low, close, volume, source FROM prices p
               WHERE date >= (SELECT date(MAX(date), ?) FROM prices q WHERE q.symbol = p.symbol)
               ORDER BY symbol, date""", (f"-{PRICE_DAYS} days",))]
        filing_rows = [dict(r) for r in conn.execute(
            """SELECT id, symbol, exchange, filed_at, subject, body, attachment_url, category,
                      category_source, category_rule FROM filings ORDER BY filed_at""")]
        enrich = [dict(r) for r in conn.execute("SELECT filing_id, kind, payload FROM enrichment")]
    payload = {"as_of": db.utcnow_iso(), "prices": price_rows, "filings": filing_rows,
               "enrichment": [{**e, "payload": json.loads(e["payload"])} for e in enrich]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def seed_from_snapshot(conn, path: Path = SNAPSHOT_PATH) -> bool:
    """Load the committed snapshot into an empty DB. No-op if the DB already has filings."""
    with db.LOCK:
        if conn.execute("SELECT 1 FROM filings LIMIT 1").fetchone():
            return False
        if not path.exists():
            return False
        snap = json.loads(path.read_text(encoding="utf-8"))
        db.upsert_prices(conn, snap.get("prices", []))
        db.upsert_filings(conn, snap.get("filings", []))
        for e in snap.get("enrichment", []):
            db.put_enrichment(conn, e["filing_id"], e["kind"], e["payload"])
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="FINN daily refresh")
    ap.add_argument("--snapshot", action="store_true", help="also export the committed Brief snapshot")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # 'rotated text' noise on scanned filings
    conn = db.connect()
    print(refresh_all(conn))
    if args.snapshot:
        print("snapshot ->", export_snapshot(conn))


if __name__ == "__main__":
    main()
