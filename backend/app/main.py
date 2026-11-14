"""FINN API + static frontend, one process.

The web tier is read-mostly: it serves what the scheduled refresh job wrote, and only
does live work for on-demand research (cached per symbol per day)."""
import json
import logging
from datetime import datetime

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ask, db, demo, llm, snapshot
from .config import ACCESS_TOKEN, DATA_DIR, DEMO_MODE, FRONTEND_DIR, LLM_ENABLED, LLM_DEEP_MODEL, LLM_FAST_MODEL
from .intel import alerts, digest
from .network import graph, store
from .network.resolve import org_index
from .research import engine

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="FINN", docs_url="/api/docs")
PORTFOLIO = json.loads((DATA_DIR / "portfolio.json").read_text())
HOLDINGS = PORTFOLIO["holdings"]


def bootstrap():
    db.init()
    with db.conn() as c:
        has_prices = c.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"] > 0
    if not has_prices and not snapshot.load():
        demo.synthetic_prices([h["symbol"] for h in HOLDINGS])
    demo.demochem()
    with db.conn() as c:
        live = c.execute("SELECT COUNT(*) n, MAX(ts) m FROM filings WHERE demo=0").fetchone()
    if DEMO_MODE:
        anchor = datetime.fromisoformat(live["m"]) if live["m"] else None
        demo.seed_filings(include_routine=live["n"] == 0, anchor=anchor)
        demo.sample_results_fundamentals("POLYCAB")


bootstrap()


@app.middleware("http")
async def gate(request: Request, call_next):
    """Shared-secret gate: the network is personal data, so even the prototype isn't open.
    Visit /?key=<token> once; it's stored as an httpOnly cookie."""
    if ACCESS_TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/health":
        tok = request.headers.get("x-finn-key") or request.cookies.get("finn_key")
        if tok != ACCESS_TOKEN:
            return JSONResponse({"error": "locked"}, status_code=401)
    resp = await call_next(request)
    k = request.query_params.get("key")
    if ACCESS_TOKEN and k == ACCESS_TOKEN:
        resp.set_cookie("finn_key", k, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                        max_age=60 * 60 * 24 * 30)
    return resp


def settings():
    return {**alerts.DEFAULTS, **db.kv_get("settings", {})}


@app.get("/api/health")
def health():
    """Unauthenticated liveness probe for the host (reveals nothing)."""
    return {"ok": True}


@app.get("/api/meta")
def meta():
    with db.conn() as c:
        n_live = c.execute("SELECT COUNT(*) n FROM filings WHERE demo=0").fetchone()["n"]
    return {"portfolio": PORTFOLIO, "settings": settings(),
            "data": {"live_filings": n_live, "prices_synthetic": db.kv_get("prices_synthetic", False),
                     "demo_mode": DEMO_MODE},
            "llm": {"enabled": LLM_ENABLED, "fast": LLM_FAST_MODEL, "deep": LLM_DEEP_MODEL, **llm.STATS},
            "themes": graph.all_themes()}


@app.get("/api/brief")
def brief(hours: int = 24):
    return digest.build(HOLDINGS, hours=max(1, min(hours, 24 * 30)), settings=settings())


class Settings(BaseModel):
    price_move_pct: float | None = None
    volume_multiple: float | None = None
    lookback: int | None = None
    care_about: list[str] | None = None
    muted: list[str] | None = None


@app.put("/api/settings")
def put_settings(s: Settings):
    cur = db.kv_get("settings", {})
    cur.update({k: v for k, v in s.model_dump().items() if v is not None})
    db.kv_set("settings", cur)
    return settings()


@app.get("/api/research/{symbol}")
def research(symbol: str):
    return engine.research(symbol)


@app.get("/api/prices/{symbol}")
def prices(symbol: str):
    from .ingest import prices as p
    return p.series(symbol.upper(), 60)


@app.get("/api/network/call")
def call(org: str = "", symbol: str = ""):
    oid = org or org_index().by_ticker.get(symbol.upper())
    if not oid:
        raise HTTPException(404, "unknown company")
    return {"org": org_index().get(oid), "people": graph.who_to_call(oid, limit=5)}


@app.get("/api/network/expertise")
def expertise(theme: str):
    return {"theme": theme, "people": graph.expertise(theme)}


@app.get("/api/network/coverage")
def coverage():
    return graph.coverage(HOLDINGS)


@app.get("/api/network/person/{pid}")
def person(pid: str):
    try:
        return graph.person(pid)
    except StopIteration:
        raise HTTPException(404)


@app.get("/api/network/overview")
def overview():
    net = store.load()
    b = digest.build(HOLDINGS, hours=72, settings=settings())
    return {"size": len(net["people"]), "second_degree": len(net.get("second_degree", [])),
            "coverage": graph.coverage(HOLDINGS), "warmups": b["warmups"],
            "themes": graph.all_themes()}


@app.post("/api/network/import")
async def import_csv(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="ignore")
    return store.import_linkedin_csv(text)


class Ask(BaseModel):
    q: str


@app.post("/api/ask")
def ask_(a: Ask):
    b = digest.build(HOLDINGS, hours=72, settings=settings())
    recent = [c["person"]["profile"] for c in b["act"] + b["know"] if c.get("person")]
    r = ask.route(a.q, recent)
    i = r["intent"]
    if i == "research":
        return {**r, "data": engine.research(r["symbol"])}
    if i == "who_to_call":
        return {**r, "data": {"org": org_index().get(r["org"]), "people": graph.who_to_call(r["org"], limit=5)}}
    if i == "expertise":
        return {**r, "data": {"theme": r["theme"], "people": graph.expertise(r["theme"])}}
    if i == "coverage":
        return {**r, "data": graph.coverage(HOLDINGS)}
    if i == "person":
        return {**r, "data": graph.person(r["id"])}
    if i == "diligence":
        return {**r, "data": {"name": r["person"]["name"], **graph.diligence(r["person"])}}
    return r


class Draft(BaseModel):
    person_id: str
    reason: str


@app.post("/api/draft")
def draft(d: Draft):
    """Reconnect note. Only the first name and the public reason go to the model —
    never the relationship notes or contact history."""
    p = graph.person(d.person_id)
    first = p["name"].split()[0]
    out = None
    if LLM_ENABLED:
        out = llm.complete(f"Write a warm, brief WhatsApp message (max 45 words) from an investor to {first}, "
                           f"an old contact, to reconnect. Hook: {d.reason}. Suggest a short call. No emojis, "
                           "no flattery, sound like a person.", tier="fast", want_json=False)
    return {"text": (out or f"Hi {first}, it's been a while! Saw the news — {d.reason.split(' — ')[0]}. "
                            "Would love your take when you have 15 minutes this week. Coffee or a call?").strip()}


# ---- frontend -----------------------------------------------------------------
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
