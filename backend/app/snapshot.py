"""Snapshot = the shareable, diffable export of public market data (never the network).

refresh.py --export writes it after a live fetch; the web app loads it on boot when
its DB is empty. This lets the deployed demo run on hosts that NSE blocks, with data
refreshed by a scheduled job or a laptop."""
import json

from . import db
from .config import SNAPSHOT_DIR


def export():
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with db.conn() as c:
        prices = [dict(r) for r in c.execute("SELECT * FROM prices ORDER BY symbol, date")]
        filings = [dict(r) for r in c.execute("SELECT * FROM filings WHERE demo=0 ORDER BY ts")]
        fund = {r["symbol"]: json.loads(r["data"]) for r in c.execute("SELECT * FROM fundamentals")}
    fund = {k: v for k, v in fund.items() if not v.get("synthetic")}
    (SNAPSHOT_DIR / "prices.json").write_text(json.dumps(prices))
    (SNAPSHOT_DIR / "filings.json").write_text(json.dumps(filings, ensure_ascii=False))
    (SNAPSHOT_DIR / "fundamentals.json").write_text(json.dumps(fund, ensure_ascii=False))
    return {"prices": len(prices), "filings": len(filings), "fundamentals": len(fund)}


def load():
    p = SNAPSHOT_DIR / "prices.json"
    if not p.exists():
        return False
    prices = json.loads(p.read_text())
    filings = json.loads((SNAPSHOT_DIR / "filings.json").read_text())
    fund = json.loads((SNAPSHOT_DIR / "fundamentals.json").read_text())
    with db.conn() as c:
        c.executemany("INSERT OR REPLACE INTO prices VALUES (:symbol,:date,:close,:volume)", prices)
        for f in filings:
            f.setdefault("extra", None)
            c.execute("INSERT OR IGNORE INTO filings (id,symbol,ts,subject,exchange_desc,text,url,category,"
                      "cls_method,summary,extra,demo) VALUES (:id,:symbol,:ts,:subject,:exchange_desc,:text,:url,"
                      ":category,:cls_method,:summary,:extra,0)", f)
        for sym, d in fund.items():
            c.execute("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?)", (sym, d.get("fetched_at"), json.dumps(d)))
    db.kv_set("prices_synthetic", False)
    return True
