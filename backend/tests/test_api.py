"""API tests: FastAPI TestClient against a seeded in-memory DB. Offline, zero key."""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.llm import LLMClient
from app.main import FRONTEND_DIR, create_app
from app.network.store import MAX_CSV_BYTES, NetworkStore
from app.research.engine import ResearchEngine

KAYNES_CARD = {
    "symbol": "KAYNES", "quarter": "Q2 FY27", "period_end": "2026-09-30", "source": "Primary Source (Filing)",
    "rows": {"revenue": {"value": 952.51, "qoq": 0.7, "yoy": 4.4, "prev_quarter": 946.21, "year_ago": 912.12},
             "pat": {"value": 39.8, "qoq": -12.4, "yoy": -65.6, "prev_quarter": 45.41, "year_ago": 115.71}},
    "basis": "Consolidated", "page": 8, "unit_note": "Filing reports in ₹ million; converted to ₹ crore",
    "flags": ["EBITDA not disclosed in filing; not shown"], "pdf_url": "https://nsearchives.nseindia.com/k.pdf",
}


def seed(conn):
    db.upsert_filings(conn, [
        {"id": "NSE:1", "symbol": "KAYNES", "filed_at": "2026-11-14T23:42:28+05:30", "subject": "Outcome of Board Meeting",
         "body": "Kaynes Technology India Limited has informed the Exchange regarding unaudited financial results "
                 "for the quarter ended September 30, 2026.", "attachment_url": "https://nsearchives.nseindia.com/k.pdf",
         "category": "results"},
        {"id": "NSE:2", "symbol": "INFY", "filed_at": "2026-11-15T09:00:00+05:30", "subject": "Trading Window-XBRL",
         "body": "", "category": "unclassified"},
        {"id": "NSE:3", "symbol": "DIXON", "filed_at": "2026-11-15T10:00:00+05:30", "subject": "Credit Rating",
         "body": "Dixon Technologies has informed the Exchange regarding CRISIL reaffirmed its AA rating.",
         "category": "credit_rating"},
        {"id": "NSE:4", "symbol": "TITAN", "filed_at": "2026-11-01T10:00:00+05:30", "subject": "Dividend",
         "body": "", "category": "dividend"},                       # outside the 24h window
    ])
    db.put_enrichment(conn, "NSE:1", "results_card", KAYNES_CARD)
    d0 = date(2026, 10, 1)
    db.upsert_prices(conn, [{"symbol": "TITAN", "date": (d0 + timedelta(days=i)).isoformat(),
                             "close": 100.0 if i < 20 else 106.0, "volume": 1000 if i < 20 else 3000}
                            for i in range(21)])


class Spy:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, sym):
        self.calls.append(sym)
        raise self.exc or LookupError(sym)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("FINN_LLM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("FINN_ACCESS_TOKEN", raising=False)

    def _make(token: str = "", fetch_exc=None):
        conn = db.connect(":memory:")
        seed(conn)
        spy = Spy(fetch_exc)
        engine = ResearchEngine(None, cache_dir=tmp_path / "cache", fetch_live=spy)
        app = create_app(conn, NetworkStore.load_demo(), engine, LLMClient(conn=conn), access_token=token,
                         private_path=tmp_path / "network_private" / "network.json", seed=False)
        return TestClient(app), spy, app
    return _make


@pytest.fixture
def client(make_client):
    return make_client()[0]


# ------------------------------------------------------------------ health / auth
def test_health_is_open(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_private_mode_requires_auth_except_health(make_client):
    c, _, _ = make_client(token="s3cret")
    assert c.get("/api/health").status_code == 200                      # Render probe never 401s
    assert c.get("/api/brief").status_code == 401
    assert c.get("/api/brief", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/brief", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/meta", headers={"Authorization": "Bearer s3cret"}).json()["access"] == "private"


def test_key_link_sets_httponly_cookie_and_303s_to_clean_url(make_client):
    from app.main import SESSION_COOKIE, session_value
    c, _, _ = make_client(token="s3cret")
    r = c.get("/?key=s3cret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    sc = r.headers["set-cookie"].lower()
    assert sc.startswith(f"{SESSION_COOKIE}=") and "httponly" in sc and "samesite=lax" in sc and "path=/" in sc
    assert "s3cret" not in r.headers["set-cookie"]                       # cookie holds an HMAC, not the key
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert c.cookies.get(SESSION_COOKIE) == session_value("s3cret")
    assert c.get("/api/brief").status_code == 200                        # cookie now authorises the API


def test_key_on_deep_link_is_also_stripped(make_client):
    c, _, _ = make_client(token="s3cret")
    r = c.get("/api/brief?key=s3cret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_wrong_key_gets_no_cookie(make_client):
    c, _, _ = make_client(token="s3cret")
    r = c.get("/?key=nope", follow_redirects=False)
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.get("/api/brief").status_code == 401


def test_forged_cookie_is_rejected(make_client):
    from app.main import SESSION_COOKIE, session_value
    c, _, _ = make_client(token="s3cret")
    c.cookies.set(SESSION_COOKIE, session_value("guess"))
    assert c.get("/api/brief").status_code == 401
    c.cookies.set(SESSION_COOKIE, "s3cret")                              # the raw key is not a valid cookie
    assert c.get("/api/brief").status_code == 401


def test_https_deploy_marks_cookie_secure(make_client):
    c, _, _ = make_client(token="s3cret")
    r = c.get("/?key=s3cret", headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


# ------------------------------------------------------------------------- brief
def test_brief_ranks_folds_routine_and_includes_alerts(client):
    b = client.get("/api/brief").json()
    assert b["as_of"].startswith("2026-11-15T10:00")
    act = {(i["symbol"], i["category"]): i for i in b["act"]}
    assert ("KAYNES", "results") in act and ("DIXON", "management_change") in act
    assert act[("KAYNES", "results")]["results_card"]["source"] == "Primary Source (Filing)"
    assert [i["symbol"] for i in b["know"]] == ["DIXON"]                       # rating reaffirmed
    assert b["routine_count"] == 1 and b["routine"][0]["subject"] == "Trading Window-XBRL"
    assert not any(i["symbol"] == "TITAN" for i in b["act"] + b["know"])       # dividend outside window
    assert sorted(a["kind"] for a in b["alerts"]) == ["price", "volume"]


def test_brief_thresholds_are_configurable_and_validated(client):
    b = client.get("/api/brief", params={"price": 10, "volume": 2}).json()
    assert [a["kind"] for a in b["alerts"]] == ["volume"]
    assert b["thresholds"] == {"price_move_pct": 10.0, "volume_multiple": 2.0}
    for bad in ({"window": 0}, {"window": 500}, {"price": 100}, {"volume": 1.0}):
        assert client.get("/api/brief", params=bad).status_code == 422


def test_brief_network_is_woven_in(client):
    b = client.get("/api/brief").json()
    cfo = next(i for i in b["act"] if i["category"] == "management_change")
    assert cfo["demo"] is True and cfo["bio"]["name"] == "Mr. Rohit Bansal"
    assert cfo["network"]["kind"] == "knows_person"
    assert cfo["network"]["path"] == ["You", "Arjun Mehta", "Rohit Bansal"]
    assert cfo["network"]["insider"]["is_insider"] is True                    # Arjun works at listed Kaynes
    kay = next(i for i in b["act"] if i["symbol"] == "KAYNES")
    assert kay["network"]["kind"] == "call" and kay["network"]["insider"]["is_insider"]
    assert any(w["name"] == "Neha Kulkarni" and w["symbol"] == "DEEPAKNTR" for w in b["warmups"])


def test_brief_speech_is_ranked_and_flags_demo(client):
    b = client.get("/api/brief").json()
    speech = b["speech"]
    assert speech[0] == "2 price or volume alerts."
    assert any(s.startswith("Demo card.") and "Arjun Mehta can introduce you to Rohit Bansal" in s for s in speech)
    assert speech[-1] == "1 routine filings folded."
    ranked = [i["speak"] for i in b["act"] + b["know"]]
    assert speech[3:3 + len(ranked)] == ranked


def test_brief_can_hide_demo_cards(client):
    b = client.get("/api/brief", params={"demo": "false"}).json()
    assert not any(i["demo"] for i in b["act"] + b["know"])


# ---------------------------------------------------------------------- research
def test_research_offline_snapshot_with_network(make_client):
    c, spy, _ = make_client()
    r = c.get("/api/research/demochem").json()
    assert r["symbol"] == "DEMOCHEM" and r["data"]["synthetic"] is True
    assert r["guidance"]["items"] and r["bull_bear"]["mode"] == "rules"
    assert isinstance(r["network"], list)
    assert spy.calls == []                                                   # snapshot, no scrape


@pytest.mark.parametrize("bad", ["DROP%20TABLE", "A" * 21, "ABC$", "dixon;rm", "%3Cscript%3E", "..%2F..%2Fetc"])
def test_research_rejects_messy_symbols_before_any_fetch(make_client, bad):
    c, spy, _ = make_client()
    resp = c.get(f"/api/research/{bad}")
    assert resp.status_code in (400, 404)
    assert spy.calls == []


def test_research_unknown_ticker_is_404_and_outage_is_502(make_client):
    c, spy, _ = make_client()
    assert c.get("/api/research/NOSUCHCO").status_code == 404 and spy.calls == ["NOSUCHCO"]
    c2, _, _ = make_client(fetch_exc=ConnectionError("screener down"))
    r = c2.get("/api/research/SOMECO")
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


@pytest.mark.parametrize("q,sym", [("dixon technologies", "DIXON"), ("Zomato Ltd", "ETERNAL"),
                                   ("kaynes", "KAYNES"), ("totally unknown corp", None)])
def test_resolve_company_names(client, q, sym):
    assert client.get("/api/resolve", params={"q": q}).json()["symbol"] == sym


# ----------------------------------------------------------------------- network
def test_network_overview(client):
    o = client.get("/api/network/overview").json()
    assert o["contacts"] == 10
    assert any(p["person"] == "Rohit Bansal" and p["path"] == ["You", "Arjun Mehta", "Rohit Bansal"]
               for p in o["intro_paths"])
    assert any(c["symbol"] == "DIXON" and c["person"] == "Priya Raman" for c in o["who_to_call"])
    blind = {b["symbol"] for b in o["blind_spots"]}
    assert {"RELIANCE", "HDFCBANK"} <= blind and "DIXON" not in blind
    assert any(w["name"] == "Neha Kulkarni" for w in o["warmups"])
    assert any(e["theme"] == "semiconductors" for e in o["expertise"])
    assert any(i["name"] == "Arjun Mehta" for i in o["insiders"])


_CSV = ("﻿Notes:\n\"Some emails may be missing.\"\n\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Meera,Pillai,https://www.linkedin.com/in/m,meera@example.com,Reliance Industries Limited,Head of Strategy,12 Mar 2024\n"
        "Karan,Desai,https://www.linkedin.com/in/k,,HDFC Bank Ltd,\"VP (karan@example.org)\",01 Jan 2023\n")


def test_import_public_mode_is_ephemeral(make_client, tmp_path):
    c, _, app = make_client()
    before = len(app.state.store.people)
    r = c.post("/api/network/import", files={"file": ("Connections.csv", _CSV.encode("utf-8"), "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] is False and body["report"]["added"] == 2 and body["report"]["emails_dropped"] == 1
    assert "@" not in r.text and "linkedin.com" not in r.text
    blind = {b["symbol"] for b in body["overview"]["blind_spots"]}
    assert "RELIANCE" not in blind and "HDFCBANK" not in blind               # the new contacts filled them
    assert len(app.state.store.people) == before                             # nothing kept
    assert not (tmp_path / "network_private" / "network.json").exists()


def test_import_private_mode_persists(make_client, tmp_path):
    c, _, app = make_client(token="t")
    r = c.post("/api/network/import", headers={"Authorization": "Bearer t"},
               files={"file": ("Connections.csv", _CSV.encode("utf-8"), "text/csv")})
    assert r.json()["persisted"] is True
    saved = tmp_path / "network_private" / "network.json"
    assert saved.exists() and "@" not in saved.read_text(encoding="utf-8")


def test_import_limits(client):
    big = client.post("/api/network/import", files={"file": ("c.csv", b"x" * (MAX_CSV_BYTES + 1), "text/csv")})
    assert big.status_code == 413
    junk = client.post("/api/network/import", files={"file": ("c.csv", b"name,email\nx,y@z.com\n", "text/csv")})
    assert junk.status_code == 400


# -------------------------------------------------------------------------- meta
def test_meta_reports_mode_counters_and_data(client):
    m = client.get("/api/meta").json()
    assert m["mode"] == "zero-key" and m["access"] == "public-demo"
    assert m["deep_fallbacks"] == 0 and "deep_fallbacks" in m["llm"]["session"]
    assert "DEMOCHEM" in m["research"]["snapshot_tickers"]
    assert m["data"]["filings"] == 4 and m["data"]["enriched"] == {"results_card": 1}


# ------------------------------------------------------------- static UI + headers
def test_ui_served_with_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>FINN</title>" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/app.js").status_code == 200 and client.get("/styles.css").status_code == 200
    assert client.get("/api/brief").headers["cache-control"] == "no-store"


# ----------------------------------------------------------------- PWA / icons
def test_pwa_manifest_and_icons_are_served_without_404(client):
    import json as _json
    import struct
    m = client.get("/manifest.json")
    assert m.status_code == 200
    manifest = _json.loads(m.text)
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    sizes = {i["sizes"]: i for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= set(sizes)
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        r = client.get("/" + icon["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        w, h = struct.unpack(">II", r.content[16:24])                     # PNG IHDR
        assert f"{w}x{h}" == icon["sizes"]
    for path, ctype in (("/favicon.ico", "image/"), ("/favicon.svg", "image/svg+xml"),
                        ("/apple-touch-icon.png", "image/png"), ("/apple-touch-icon-precomposed.png", "image/png")):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
    html = client.get("/").text
    assert 'rel="manifest" href="manifest.json"' in html and 'rel="apple-touch-icon"' in html


# ------------------------------------------------------------ dependency hygiene
import ast
import sys

BACKEND_DIR = Path(__file__).resolve().parent.parent
IMPORT_TO_DIST = {"dotenv": "python-dotenv"}                 # import name -> PyPI name, where they differ
RUNTIME_ONLY = {"uvicorn", "python-multipart"}               # needed at runtime, never imported


def _requirements(name: str) -> dict[str, str]:
    pins = {}
    for line in (BACKEND_DIR / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-r"):
            dist, _, version = line.partition("==")
            pins[dist.strip().lower()] = version.strip()
    return pins


def _third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for folder in ("app", "scripts", "eval"):
        for f in (BACKEND_DIR / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for n in names:
                    top = n.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "app":
                        found.setdefault(IMPORT_TO_DIST.get(top, top).lower(), set()).add(f.name)
    return found


def test_every_third_party_import_is_pinned():
    pins, imports = _requirements("requirements.txt"), _third_party_imports()
    missing = {d: sorted(files) for d, files in imports.items() if d not in pins}
    assert not missing, f"imported but not in requirements.txt: {missing}"
    assert all(v and v[0].isdigit() for v in pins.values()), f"every direct dep must be pinned ==: {pins}"


def test_no_unused_runtime_dependencies():
    unused = set(_requirements("requirements.txt")) - set(_third_party_imports()) - RUNTIME_ONLY
    assert not unused, f"declared but never imported: {unused}"


def test_dev_requirements_extend_runtime_and_are_pinned():
    text = (BACKEND_DIR / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    dev = _requirements("requirements-dev.txt")
    assert {"pytest", "httpx"} <= set(dev) and all(dev.values())
    assert not {"pytest", "httpx"} & set(_requirements("requirements.txt"))   # test deps stay out of the image


def test_frontend_static_contract():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    js = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND_DIR / "styles.css").read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1' in html
    assert not re.search(r"\son\w+=", html) and 'style="' not in html + js     # CSP-safe: no inline handlers/styles
    assert "<script src=\"app.js\" defer>" in html and "<script>" not in html
    assert "--tap: 44px" in css and "min-height: var(--tap)" in css
    assert "stopSpeaking();" in js.split("function showTab", 1)[1].split("}", 1)[0]   # speech dies on tab switch
    assert "not-allowed" in js and "Microphone access is blocked" in js
    assert all(s in js for s in ("Reading statements", "Checking transcripts", "Matching network"))
    assert "react" not in (html + js).lower()
