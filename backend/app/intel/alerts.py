"""Triggered EOD alerts. The investor decides what 'unusual' means.

Volume is compared with the average of the previous N sessions (today excluded,
otherwise a spike inflates its own baseline)."""
from ..ingest import prices

DEFAULTS = {"price_move_pct": 4.0, "volume_multiple": 2.0, "lookback": 20,
            "care_about": ["results", "M&A", "management_change", "litigation", "fund_raise"],
            "muted": [], "overrides": {}}


def check(holdings, settings):
    out = []
    for h in holdings:
        sym = h["symbol"]
        cfg = {**settings, **settings.get("overrides", {}).get(sym, {})}
        s = prices.series(sym, n=cfg["lookback"] + 1)
        if len(s) < 3:
            continue
        today, prev = s[-1], s[-2]
        base = s[:-1][-cfg["lookback"]:]
        avg_vol = sum(r["volume"] for r in base) / len(base)
        move = (today["close"] - prev["close"]) / prev["close"] * 100
        vm = today["volume"] / avg_vol if avg_vol else 0
        reasons = []
        if abs(move) >= cfg["price_move_pct"]:
            reasons.append(f"{move:+.1f}% on the day (your trigger ±{cfg['price_move_pct']}%)")
        if vm >= cfg["volume_multiple"]:
            reasons.append(f"volume {vm:.1f}× its {len(base)}-day average (trigger {cfg['volume_multiple']}×)")
        if reasons:
            out.append({"symbol": sym, "name": h["name"], "date": today["date"], "close": today["close"],
                        "move_pct": round(move, 2), "vol_multiple": round(vm, 2), "reasons": reasons,
                        "spark": [r["close"] for r in s[-30:]],
                        "severity": abs(move) / cfg["price_move_pct"] + vm / cfg["volume_multiple"]})
    return sorted(out, key=lambda a: -a["severity"])
