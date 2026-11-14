# Build & verification log

## 15 Nov 2026

- **Model routing & deprecation audit.** Queried the Gemini `/v1beta/models` endpoint to confirm active model IDs. Defaults moved from `gemini-2.5-flash-lite` / `gemini-2.5-flash` (on Google's deprecation list) to `gemini-3.5-flash-lite` (fast tier) and `gemini-3.8-flash` (deep tier) in `config.py`, `.env.example`, `DEPLOY.md` and `ARCHITECTURE.md`. Added `load_dotenv()` in `config.py` so local Uvicorn runs read `.env` automatically.
- **First live data refresh.** Ran `python -m scripts.refresh --export` against Yahoo Finance (2,286 end-of-day price rows), NSE corporate announcements (34 filings) and screener.in (22/22 fundamentals pages). The committed snapshot is this run.
- **Classification cost, measured.** Of the 34 live filings, 21 (62%) were classified by rules with no model call: 18 from NSE's own subject line and 3 from text keywords. The remaining 13 are the ambiguous ones that go to the fast model when a key is set.
- **LLM health check.** `/api/meta` reports `llm.enabled: true` and `failures: 0`.
- **Persona & repo clean-up.** Renamed the mock investor in `portfolio.json` to `Demo_Investor` and removed private prep notes from the public repo.
- **Stale-date fix.** `TODAY` in `network/graph.py` and `NOW` in `demo.py` were fixed at server start, so on a long-running server "last spoke X ago" and warmth scores froze at the boot date. Both are now evaluated per call.
- **Docs repaired.** `DEPLOY.md` and `ARCHITECTURE.md` had been truncated and over-escaped by a Markdown editor; restored from the original and re-applied the edits above.
