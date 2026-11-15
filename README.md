# FINN — a financial investment agent that tells you what matters and who to call

FINN is a mobile-first prototype for a public-markets investor. It has three layers:

- **Brief (Push).** Every filing across the portfolio in a window you choose, ranked by materiality. Routine paperwork is folded away. The few items that matter come with the next useful thing: the new CFO's career and your way to them, results numbers with QoQ/YoY, or the person best placed to explain an M&A or litigation event. End-of-day price and volume alerts fire on thresholds you set.
- **Ask (Pull).** Research on any NSE symbol, grounded only in primary sources. The headline section is *Did management deliver?*: guidance quoted from the last four concall transcripts, checked against reported quarters, with guidance drift (raised / maintained / cut) across calls.
- **Network.** Your own contacts, matched against careers, filings and companies, all inside Push and Pull. It also answers direct questions: who should I call, who can introduce me, who are my go-to people for a theme, which holdings have nobody close, and who has a natural reason to hear from you today.

Deeper docs: [Architecture & writeup](docs/ARCHITECTURE.md), [Deploying](docs/DEPLOY.md), [Build & verification log](docs/BUILD_LOG.md).

## Two minutes on a phone

1. **Brief.** Open the *Dixon: CFO appointed* card (Demo). It shows the new CFO's career from the filing's own SEBI profile and your route to them through the network.
2. Tap **7d**. These are live NSE filings: Kaynes' M&A term sheet comes with someone in your network who works there, and HDFC Bank's director changes and Bajaj Finance's fund-raises sit above the routine pile. Kaynes' results card says plainly when the quarter isn't in the standardised table yet, rather than showing last quarter's numbers.
3. The **sliders icon** sets what counts as an unusual move for you (±% on the day, × average volume). The **speaker icon** reads the Brief aloud in ranked order with who to call, for the commute.
4. **Ask** (type, or tap the mic on phones that support it) any NSE symbol, including one we never picked (`ASIANPAINT` and `TATAPOWER` both work). The first look takes about 20 s, then it's cached. Start with *Did management deliver?*: verbatim concall quotes with page numbers, checked against reported quarters.
5. Ask `who should I call about Polycab` or `specialty chemicals`, then open **Network** for blind spots and natural reasons to reconnect.

Live versus synthetic: filings, prices and fundamentals are live (refreshed 15 Nov 2026). The five *Demo* cards and every person in the network are synthetic; see [Data honesty](#data-honesty).

## Run it locally (2 minutes)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and use your browser's mobile view (or your phone on the same Wi-Fi at `http://<laptop-ip>:8000`).

With no data and no API key it starts in **offline mode**: simulated prices, a synthetic filing feed tagged *Demo*, and DEMOCHEM, a fictional company with full financials and concall transcripts for the research view.

## Make it live

```bash
cd backend
python -m scripts.refresh --export     # yfinance prices, NSE filings, screener.in fundamentals
export GEMINI_API_KEY=...               # optional: free key from aistudio.google.com
uvicorn app.main:app --port 8000
```

`--export` writes `backend/data/snapshot/*.json`. The deployed app rebuilds its database from this snapshot on boot. That matters because NSE blocks many cloud IPs, so the refresh runs somewhere NSE allows (your laptop or a scheduled job) and the web app only reads.

## Tests

```bash
cd backend && python -m pytest -q
```

The tests cover the judgement calls that would quietly mislead an investor if they broke: classification, materiality tiers, entity resolution, path ranking, the insider-compliance flag, guidance parsing and results arithmetic. They all run without an LLM.

`tests/test_api.py` adds deploy-level guards: the access gate (locked without the key, unlocked by the share link's cookie), an open `/api/health` for the host's probe, and a check that every third-party import is declared in `requirements.txt`. CI (`.github/workflows/tests.yml`) runs the suite on a clean install, then builds the Docker image and probes it with the access gate on.

## Repository map

```
backend/app/
  ingest/      prices.py (yfinance)  filings.py (NSE)  fundamentals.py (screener.in + PDFs)
  intel/       classify.py  materiality.py  results.py  alerts.py  digest.py (Push orchestrator)
  research/    metrics.py (deterministic)  guidance.py (narrative vs numbers)  engine.py (Pull)
  network/     resolve.py (entity resolution)  graph.py (paths, who-to-call, expertise, warm-ups)  store.py
  llm.py       two-tier router + content-addressed cache
  ask.py       one input box -> intent
  main.py      FastAPI, serves the frontend too
backend/data/  portfolio.json  orgs.json (org graph)  network.json (SYNTHETIC people)  snapshot/
backend/eval/  filings_dev.jsonl (34 live filings, hand-checked labels)
backend/scripts/ refresh.py (daily job; --reclassify)  eval_classify.py (coverage / precision report)
frontend/      index.html  styles.css  app.js   (no framework, no build step)
```

## Data honesty

- **Real companies get only live, fetched data.** The only exception is the five clearly tagged *Demo* filings, which make sure the management-change, results and material-event flows can be shown on any day.
- **The network is entirely synthetic.** Every person is fictional. A real deployment imports the investor's own LinkedIn data export (supported: Network → Import Connections.csv) plus their CRM notes.
- **DEMOCHEM is fictional.** No invented management quotes are ever attributed to a real company.
