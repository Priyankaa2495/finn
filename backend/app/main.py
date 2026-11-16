"""FINN API + static UI in one FastAPI app.

Run (single worker by decision; the per-symbol fetch lock is per process):
    uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT --workers 1

Access (decision 2026-11-15: token-gated, frictionless for reviewers):
- FINN_ACCESS_TOKEN set (deployed): open `/?key=TOKEN` once. The key is checked in constant
  time, an httpOnly SameSite=Lax cookie `finn_key` is set, and a 303 redirect to `/` strips the
  key from the address bar (so it never sits in history, screenshots or Referer). The cookie
  holds an HMAC of the token, not the token. Every /api route except /api/health then needs
  that cookie (or `Authorization: Bearer <token>` for scripts). Imports persist (0600 file).
- FINN_ACCESS_TOKEN unset (local dev): open; LinkedIn imports are analysed in memory only.
"""
from __future__ import annotations

import copy
import hmac
import os
import re
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, refresh
from .intel import alerts as A
from .intel.brief import build_brief, network_overview
from .llm import LLMClient
from .network import resolve
from .network.graph import NetworkGraph
from .network.store import DEFAULT_PATH, MAX_CSV_BYTES, ImportTooLarge, NetworkStore
from .research.engine import ResearchEngine

FRONTEND_DIR = config.BACKEND_DIR.parent / "frontend"
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SESSION_COOKIE = "finn_key"
SESSION_MAX_AGE = 30 * 24 * 3600


def session_value(token: str) -> str:
    """What the cookie carries: an HMAC of the token, so a leaked cookie isn't the key itself."""
    return hmac.new(token.encode(), b"finn-session-v1", "sha256").hexdigest()


def _security_headers(resp, path: str):
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    # API responses can carry personal network data: never cached anywhere.
    resp.headers["Cache-Control"] = "no-store" if path.startswith("/api/") or path == "/" else "no-cache"
    return resp


def create_app(conn=None, store: NetworkStore | None = None, engine: ResearchEngine | None = None,
               llm: LLMClient | None = None, *, frontend_dir: Path = FRONTEND_DIR,
               access_token: str | None = None, private_path: Path = DEFAULT_PATH,
               seed: bool = True, include_demo: bool = True) -> FastAPI:
    token = access_token if access_token is not None else (os.getenv("FINN_ACCESS_TOKEN") or None)
    conn = conn if conn is not None else db.connect()
    if seed:
        refresh.seed_from_snapshot(conn)
    llm = llm if llm is not None else LLMClient(conn=conn)
    engine = engine if engine is not None else ResearchEngine(llm)
    if store is None:
        store = NetworkStore.load(private_path) if Path(private_path).exists() else NetworkStore.load_demo()

    app = FastAPI(title="FINN", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    def graph() -> NetworkGraph:
        return NetworkGraph(app.state.store, today=date.today())

    # ------------------------------------------------------------ middleware
    def authorised(request: Request) -> bool:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, session_value(token)):
            return True
        return hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if token and "key" in request.query_params:
            # Key-to-cookie: verify, set the session cookie, 303 to a clean URL. Applies to any
            # path so a key pasted onto a deep link is stripped too.
            supplied = request.query_params.get("key", "")
            if not hmac.compare_digest(supplied, token):
                return _security_headers(PlainTextResponse("Invalid access key.", status_code=401), "/api/")
            resp = RedirectResponse("/", status_code=303)
            https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            resp.set_cookie(SESSION_COOKIE, session_value(token), max_age=SESSION_MAX_AGE, path="/",
                            httponly=True, samesite="lax", secure=https)
            return _security_headers(resp, "/api/")
        if token and path.startswith("/api/") and path != "/api/health" and not authorised(request):
            return _security_headers(JSONResponse({"detail": "unauthorized"}, status_code=401), path)
        return _security_headers(await call_next(request), path)

    # ------------------------------------------------------------- endpoints
    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/brief")
    def brief(window: int = Query(24, ge=1, le=168), price: float = Query(5.0, ge=0.5, le=50),
              volume: float = Query(2.0, ge=1.1, le=20), demo: bool = True):
        cfg = A.AlertConfig(price_move_pct=price, volume_multiple=volume)
        return build_brief(conn, graph(), window_hours=window, alert_cfg=cfg, include_demo=demo and include_demo)

    @app.get("/api/resolve")
    def resolve_query(q: str = Query(..., min_length=1, max_length=60)):
        cleaned = " ".join(q.split())
        if SYMBOL_RE.fullmatch(cleaned.upper().replace(" ", "")) and " " not in cleaned:
            m = resolve.resolve_org_or_ticker(cleaned)
            return {"symbol": cleaned.upper(), "name": m.org.name if m.org else None}
        m = resolve.resolve_org(cleaned)
        if m.org and m.org.ticker:
            return {"symbol": m.org.ticker, "name": m.org.name}
        return {"symbol": None, "name": None}

    @app.get("/api/research/{symbol}")
    async def research(symbol: str):
        sym = symbol.strip().upper()
        if not SYMBOL_RE.fullmatch(sym):           # before any I/O: no odd input reaches screener
            raise HTTPException(400, "Symbol must be 1-20 characters: A-Z, 0-9, & or -")
        try:
            report = await engine.research(sym)
        except LookupError:
            raise HTTPException(404, f"{sym} not found on screener.in")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception:
            raise HTTPException(502, "Primary sources are unreachable right now. Try again shortly.")
        report["network"] = [{"person": s.name, "score": s.score, "why": list(s.reasons[:2]),
                              "insider": s.insider.__dict__} for s in graph().who_to_call(sym, top=3)]
        return report

    @app.get("/api/network/overview")
    def overview():
        return network_overview(conn, graph(), include_demo=include_demo)

    @app.post("/api/network/import")
    async def import_csv(file: UploadFile = File(...)):
        data = await file.read(MAX_CSV_BYTES + 1)
        if len(data) > MAX_CSV_BYTES:
            raise HTTPException(413, "File exceeds the 5 MB limit")
        target = app.state.store if token else NetworkStore(copy.deepcopy(list(app.state.store.people.values())))
        try:
            report = target.import_linkedin_csv(data)
        except ImportTooLarge:
            raise HTTPException(413, "File exceeds the 5 MB limit")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if token:
            target.save(private_path)
        g = NetworkGraph(target, today=date.today())
        return {"report": report.__dict__, "persisted": bool(token),
                "note": ("Saved to your private network store." if token else
                         "Demo mode: analysed in memory for this response only. Nothing was stored."),
                "overview": network_overview(conn, g, include_demo=include_demo)}

    @app.get("/api/meta")
    def meta():
        with db.LOCK:
            filings = conn.execute("SELECT COUNT(*) c, MAX(filed_at) m FROM filings").fetchone()
            enriched = conn.execute("SELECT kind, COUNT(*) c FROM enrichment GROUP BY kind").fetchall()
            cache_rows = conn.execute("SELECT COUNT(*) c FROM llm_cache").fetchone()["c"]
        cached = sorted(p.stem for p in Path(engine.cache_dir).glob("*.json")) if Path(engine.cache_dir).exists() else []
        snap = sorted(p.stem for p in Path(engine.snapshot_dir).glob("*.json"))
        m = llm.meta()
        return {"mode": "llm" if m["enabled"] else "zero-key", "access": "private" if token else "public-demo",
                "llm": m, "deep_fallbacks": m["totals"].get("deep_fallbacks", 0),
                "llm_cache_rows": cache_rows,
                "research": {"live_fetches": engine.live_fetches, "cached_tickers": cached, "snapshot_tickers": snap},
                "data": {"filings": filings["c"], "as_of": filings["m"],
                         "enriched": {r["kind"]: r["c"] for r in enriched}}}

    if Path(frontend_dir).exists():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="ui")
    return app
