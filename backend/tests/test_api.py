"""Deploy-level guards: the access gate, the host health check, and declared dependencies.

These exist because two real deploy blockers slipped through the unit tests:
  * `config.py` imported python-dotenv, which was installed in the local venv but not in
    requirements.txt, so a clean build would crash on import;
  * Render's health check hit /api/meta, which returns 401 once FINN_ACCESS_TOKEN is set.
"""
import ast
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FINN_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
os.environ.setdefault("FINN_DB", os.path.join(tempfile.mkdtemp(), "test_api.db"))
os.environ["GEMINI_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
TOKEN = "test-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "ACCESS_TOKEN", TOKEN)
    return TestClient(main.app)


# ---- access gate ------------------------------------------------------------------

def test_health_is_open_even_when_locked(client):
    # the host's probe has no cookie; if this needs the key, every deploy is "unhealthy"
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_api_is_locked_without_key(client):
    for path in ["/api/meta", "/api/brief", "/api/network/overview"]:
        assert client.get(path).status_code == 401, path


def test_wrong_key_is_rejected(client):
    assert client.get("/api/meta", headers={"x-finn-key": "nope"}).status_code == 401


def test_header_key_unlocks_api(client):
    r = client.get("/api/meta", headers={"x-finn-key": TOKEN})
    assert r.status_code == 200
    assert r.json()["llm"]["failures"] == 0


def test_share_link_sets_cookie_and_strips_key_from_url(client):
    r = client.get(f"/?key={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"   # key leaves the address bar
    assert "httponly" in r.headers.get("set-cookie", "").lower()
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/brief").status_code == 200  # cookie carried by the client


def test_wrong_share_link_sets_no_cookie(client):
    r = client.get("/?key=nope", follow_redirects=False)
    assert r.status_code == 200 and "finn_key" not in r.headers.get("set-cookie", "")
    assert client.get("/api/meta").status_code == 401


def test_personal_data_is_not_cacheable_and_has_no_referrer(client):
    r = client.get("/api/network/overview", headers={"x-finn-key": TOKEN})
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/").headers["x-frame-options"] == "DENY"


def test_frontend_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "renderBrief" in js.text


def test_installable_on_a_phone(client):
    """'Add to Home Screen' needs 192 and 512 px icons in the manifest, all actually served."""
    m = client.get("/static/manifest.json").json()
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in m["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200 and len(r.content) > 100, icon["src"]
    assert client.get("/static/icon-512.png").content[:8] == b"\x89PNG\r\n\x1a\n"


def test_open_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(main, "ACCESS_TOKEN", "")
    assert TestClient(main.app).get("/api/meta").status_code == 200


# ---- input validation -------------------------------------------------------------

def test_research_rejects_non_symbols_without_fetching(client, monkeypatch):
    from app.ingest import fundamentals
    monkeypatch.setattr(fundamentals, "fetch", lambda s: pytest.fail(f"fetched {s!r}"))
    h = {"x-finn-key": TOKEN}
    for bad in ["A" * 40, "DIXON;x", "DIX ON", "%2Fetc", "DIXON%3Fq%3D1"]:
        r = client.get(f"/api/research/{bad}", headers=h)
        assert r.status_code == 404 or "error" in r.json(), bad  # rejected by the router or the validator


def test_concurrent_first_looks_fetch_once(monkeypatch):
    """Two taps on a slow first look must not scrape screener / download transcripts twice."""
    import threading
    import time
    from app import db
    from app.research import engine
    calls = []

    def slow_fetch(sym):
        calls.append(sym)
        time.sleep(0.3)
        return None  # 'no data' keeps the test offline; the point is the call count
    monkeypatch.setattr(engine.fundamentals, "fetch", slow_fetch)
    hits = {}
    monkeypatch.setattr(engine.db, "kv_get", lambda k, d=None: hits.get(k, d))
    monkeypatch.setattr(engine.db, "kv_set", lambda k, v: hits.__setitem__(k, v))
    # cache the 'no data' result like a real one, so the waiting request is served from it
    orig = engine._research

    def caching(sym):
        key = f"research:{sym}:test"
        if key in hits:
            return hits[key]
        hits[key] = orig(sym)
        return hits[key]
    monkeypatch.setattr(engine, "_research", caching)
    ts = [threading.Thread(target=engine.research, args=("ZZTEST",)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == ["ZZTEST"]


def test_real_symbol_shapes_are_accepted():
    from app.research.engine import NSE_SYMBOL
    assert all(NSE_SYMBOL.fullmatch(s) for s in ["M&M", "BAJAJ-AUTO", "3MINDIA", "DIXON"])


# ---- network import ---------------------------------------------------------------

def test_linkedin_export_with_bom_and_notes_imports(client, monkeypatch):
    from app.network import store
    saved = {}
    monkeypatch.setattr(store, "save", lambda d: saved.setdefault("d", d))  # never touch network.json
    csv_text = ("﻿Notes:\n\"When exporting your connection data, you may notice...\"\n\n"
                "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                "Zara,Testperson,https://x,zara@example.com,Acme Ltd,CFO,12 Mar 2024\n")
    r = client.post("/api/network/import", headers={"x-finn-key": TOKEN},
                    files={"file": ("Connections.csv", csv_text.encode("utf-8"), "text/csv")})
    assert r.status_code == 200 and r.json()["added"] == 1
    person = saved["d"]["people"][-1]
    assert person["name"] == "Zara Testperson" and "zara@example.com" not in str(person)  # email dropped


def test_import_rejects_wrong_or_oversized_files(client, monkeypatch):
    from app.network import store
    monkeypatch.setattr(store, "save", lambda d: None)
    h = {"x-finn-key": TOKEN}
    assert client.post("/api/network/import", headers=h,
                       files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}).status_code == 400
    monkeypatch.setattr(main, "MAX_IMPORT_BYTES", 100)
    big = b"First Name,Last Name\n" + b"A,B\n" * 100
    assert client.post("/api/network/import", headers=h,
                       files={"file": ("x.csv", big, "text/csv")}).status_code == 413


# ---- encoding guard ---------------------------------------------------------------
# A snapshot exported on Windows was written as cp1252 ("world’s" -> byte 0x92). It loaded
# fine on the laptop and would have crashed the Linux container at boot.

def test_committed_data_is_utf8():
    for p in (BACKEND / "data").rglob("*.json"):
        p.read_bytes().decode("utf-8")  # raises on cp1252 bytes


def test_file_io_states_its_encoding():
    offenders = []
    for path in list((BACKEND / "app").rglob("*.py")) + list((BACKEND / "scripts").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.(read_text|write_text)\(", line) and "encoding=" not in line:
                offenders.append(f"{path.relative_to(BACKEND).as_posix()}:{n}")
    assert not offenders, "platform-dependent encoding (add encoding='utf-8'): " + ", ".join(offenders)


# ---- dependency guard -------------------------------------------------------------

# import name -> pip distribution name, where they differ
DIST_FOR = {"dotenv": "python-dotenv", "bs4": "beautifulsoup4", "yaml": "pyyaml", "dateutil": "python-dateutil"}
# guaranteed transitively by a declared requirement
TRANSITIVE = {"pydantic": "fastapi", "starlette": "fastapi"}


def _third_party_imports():
    found = {}
    for path in list((BACKEND / "app").rglob("*.py")) + list((BACKEND / "scripts").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                if top not in sys.stdlib_module_names and top not in {"app", "scripts"}:
                    found.setdefault(top, path.relative_to(BACKEND).as_posix())
    return found


def test_every_import_is_declared_in_requirements():
    """Fails locally even when the package happens to be installed in your venv —
    that was exactly how python-dotenv slipped through."""
    reqs = BACKEND / "requirements.txt"
    declared = {re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower()
                for line in reqs.read_text().splitlines() if line.strip() and not line.startswith("#")}
    missing = []
    for mod, where in _third_party_imports().items():
        dist = DIST_FOR.get(mod, mod).lower().replace("_", "-")
        if dist not in declared and TRANSITIVE.get(mod) not in declared:
            missing.append(f"{mod} (imported in {where}; add '{dist}' to requirements.txt)")
    assert not missing, "undeclared dependencies: " + ", ".join(missing)
