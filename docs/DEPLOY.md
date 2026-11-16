# Deploying FINN

Goal: a link the ENAM team can open on a phone, backed by real market data, that works even on hosts NSE blocks.

The approach: **fetch on your laptop (or a cron job) → commit a snapshot → the host rebuilds from the snapshot.** The web app never needs to reach NSE.

---

## 1. Run it locally first

```bash
cd finn/backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m pytest -q && uvicorn app.main:app --port 8000
```

Open http://localhost:8000. You should see "Five things need you." with an *Offline mode* banner. Press Ctrl+C to stop.

## 2. Pull live data (from your laptop, on a home or college network)

```bash
cd finn/backend && source .venv/bin/activate && rm -f data/finn.db && python -m scripts.refresh --export
```

What a healthy run prints:

```
prices:   ~2200 rows            (18 stocks x ~125 sessions)
filings:  40-150 rows           (7 days; more in results season)
classified N: {'rule:desc': ..., 'rule:text': ..., 'fallback': ...}
fundamentals: 22/22
snapshot: {...}
```

**Verified live on 15 Nov 2026** against Yahoo Finance, NSE and screener.in (see `docs/BUILD_LOG.md`). These sources change without notice, so if a later run breaks:

| Symptom | Likely cause | Fix |
|---|---|---|
| `filings: 0 rows`, log shows 401/403 | NSE cookie or IP block | Run again; try a different network; open nseindia.com in a browser first |
| `prices: 0 rows` | yfinance API change | `pip install -U yfinance` |
| `fundamentals: 0/22` with 403 | screener rate limit | Wait a minute and run again with `--skip-fundamentals` off |
| Fundamentals load but tables are empty | screener HTML changed | Print `parse()` output for one symbol; adjust the selector in `ingest/fundamentals.py` |

Then start the app again and check:

```bash
uvicorn app.main:app --port 8000
```

- The *Offline mode* banner is gone. Only the five showcase items carry *Demo*.
- The Polycab results card shows real numbers (no "Sample numbers" note).
- Ask → `DIXON`, `NAVINFLUOR`, `TATASTEEL`: real research views.

## 3. Optional: add the LLM tier

Get a free key at https://aistudio.google.com. The defaults are `gemini-3.5-flash-lite` (fast) and `gemini-3.8-flash` (deep). Model names change; if Google retires one, set the current names below.

```bash
export GEMINI_API_KEY=your_key && export FINN_LLM_FAST=gemini-3.5-flash-lite && export FINN_LLM_DEEP=gemini-3.8-flash && uvicorn app.main:app --port 8000
```

With a key you get cleaner one-liners, bios extracted from real management-change filings, model-judged guidance items, segment mix from investor decks, and a written bull/bear. Check `http://localhost:8000/api/meta` to see calls versus cache hits. `failures` should be no higher than `deep_fallbacks`: the deep tier sometimes times out, and the fast tier then answers instead (a recovered failure). Failures beyond that, or `calls` staying at 0, mean the model name or key is wrong and FINN has fallen back to its code-only path.

## 4. Push to GitHub

Create an empty **public** repo named `finn` on github.com, then:

```bash
cd finn && git init && git add . && git commit -m "FINN: push, pull and network prototype" && git branch -M main && git remote add origin https://github.com/<your-username>/finn.git && git push -u origin main
```

`backend/data/finn.db` is git-ignored; `backend/data/snapshot/*.json` is committed. That's intentional.

## 5. Deploy on Render (free)

1. render.com → sign in with GitHub → **New → Blueprint** → choose the `finn` repo. It reads `render.yaml`.
2. Set the environment variables when prompted:
   - `FINN_ACCESS_TOKEN`: any long random string (keep it out of the repo)
   - `GEMINI_API_KEY`: your key (or leave blank)
3. Deploy. The first build takes about 3–4 minutes. You get `https://finn-xxxx.onrender.com`.
4. Share **`https://finn-xxxx.onrender.com/?key=<your-token>`**. The key is stored in a cookie on first visit.
5. On a phone: open the link → Share → **Add to Home Screen**. It opens full-screen like an app.

**Free-tier catch:** Render sleeps the service after 15 minutes idle, and the first request then takes about 50 seconds. Open the link yourself a couple of minutes before anyone reviews it. Mention in your email that the first load may be slow.

### Alternative: Hugging Face Spaces (also free, Docker)

Create a Space → SDK **Docker** → push this repo to the Space. Add this to the top of `README.md`:

```
---
title: FINN
sdk: docker
app_port: 8000
---
```

Set `FINN_ACCESS_TOKEN` and `GEMINI_API_KEY` under Settings → Variables and secrets.

## 6. Keep the data fresh

**Option A: GitHub Actions (already in `.github/workflows/refresh.yml`).** Add a `GEMINI_API_KEY` repository secret (optional) → Actions tab → enable workflows → *refresh-market-data* → **Run workflow**. It runs weekdays at 17:15 IST and commits the snapshot, and Render redeploys automatically. NSE sometimes blocks GitHub's IP ranges; if the filings count is 0 in the job log, use option B.

**Option B: your laptop, before you send the link.**

```bash
cd finn/backend && source .venv/bin/activate && python -m scripts.refresh --export && cd .. && git add backend/data/snapshot && git commit -m "data: snapshot $(date +%F)" && git push
```

## 7. What to submit (from the brief)

- Deployed link with `?key=`
- Public GitHub repo
- AI build history: share links to this conversation and any others you use
- Covering email: start from `docs/ARCHITECTURE.md`, and make sure it says the network is synthetic and how real data would come in
