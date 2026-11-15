"""The investor's private network store.

Kept as its own file (not in the shared market-data DB) so that in a multi-user
deployment it maps 1:1 to a per-investor encrypted bucket/row-level-secured table.
Nothing in here is ever sent to an LLM by the matching engine."""
import csv
import io
import json
import re
from datetime import date
from functools import lru_cache

from ..config import DATA_DIR

NETWORK_PATH = DATA_DIR / "network.json"


@lru_cache(maxsize=1)
def load():
    return json.loads(NETWORK_PATH.read_text(encoding="utf-8"))


def save(data):
    NETWORK_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    load.cache_clear()


def import_linkedin_csv(text: str) -> dict:
    """Import LinkedIn's own 'Connections.csv' data export (Settings > Data privacy >
    Get a copy of your data). This is the consent-friendly route: the investor exports
    their own data; we never scrape. Email addresses are dropped (data minimisation)."""
    lines = text.lstrip("﻿").splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip('﻿"').startswith("First Name")), 0)
    rows = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    data = load()
    existing = {p["name"].lower() for p in data["people"]}
    added = 0
    for r in rows:
        name = f"{r.get('First Name','').strip()} {r.get('Last Name','').strip()}".strip()
        if not name or name.lower() in existing:
            continue
        connected = r.get("Connected On", "")
        yr = re.search(r"(\d{4})", connected)
        data["people"].append({
            "id": f"li{len(data['people'])+1:04d}",
            "name": name,
            "headline": f"{r.get('Position','')}, {r.get('Company','')}".strip(", "),
            "how_met": f"LinkedIn connection since {connected}" if connected else "LinkedIn connection",
            "strength": 2,
            "last_contact": None,
            "experience": [{"org": r.get("Company", ""), "role": r.get("Position", ""),
                            "from": None}] if r.get("Company") else [],
            "education": [],
            "tags": [],
            "source": "linkedin_export",
            "imported_on": date.today().isoformat(),
        })
        existing.add(name.lower())
        added += 1
    save(data)
    return {"added": added, "total": len(data["people"])}
