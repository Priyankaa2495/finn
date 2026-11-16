# FINN: Financial Investment Agent

A mobile-first agent for an Indian-equities investor. **Push**: what changed in the portfolio and
why it matters. **Pull**: did management deliver what they promised. **Network**: who you know
who can explain it. Rules-first: every workflow runs with no API key; a model is an upgrade.

## Try it

| | |
|---|---|
| Deployed | `https://<app>.onrender.com/?key=<FINN_ACCESS_TOKEN>`. Open once; the key becomes an httpOnly cookie and is stripped from the URL. |
| Local | `cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt` (POSIX: `.venv/bin/pip`) |
| | `uvicorn app.main:create_app --factory --port 8000` → http://localhost:8000 (open mode, no key) |
| Tests | `python -m pytest` (226 tests, offline, zero-key) |
| Eval | `python scripts/eval_classify.py run -v` · `--baseline` scores the pre-tuning rules |
| Refresh data | `python -m app.refresh --snapshot` (NSE + yfinance + eager PDF parsing → committed snapshot) |

On a phone: open the link → browser menu → **Add to Home Screen** (manifest + 192/512/maskable icons).
Tabs: **Brief** (ranked filings, alerts, read-aloud), **Ask** (any NSE ticker or company name, by
voice or text), **Network** (who to call, second-degree paths, warm-ups, blind spots).

## Architecture

```
NSE announcements ─┐                      ┌─ classify (regex rules) ─ materiality (additive, explained)
yfinance EOD ──────┼─ daily refresh ─ SQLite ─ alerts (your thresholds)
results/annexure PDFs (eager, 15 holdings) ┘   └─ results card (PDF → ₹ Cr, screener fallback) · bios
                                                         │
screener.in tables + BSE transcripts (lazy, any ticker) ─┴─ research: metrics in code → guidance
     (per-symbol lock, 20 h cache)                          (model picks sentence IDs; Python checks
                                                             every number) → verdicts → facts pack
                                                             → bull/bear (must cite fact IDs)
Investor network (local JSON, 0600) ── pure-Python graph ── woven into Brief cards, Ask, Network tab
                                         (never imported by llm/research; enforced by tests)
FastAPI (one worker) serves /api/* + the vanilla-JS UI, strict CSP, cookie auth, /api/health open
```

## Hard rules → how they are enforced

| Rule | Mechanism | Test |
|---|---|---|
| Zero-key operation | `LLMClient` returns `None` without a key; every caller has a rules path | `test_engine_offline_zero_key_no_network`, CI runs with no secret |
| Network never reaches an LLM/API | network package imports no I/O or LLM module; research never imports network | `test_network_package_imports_no_io_or_llm_modules`, socket-blocked tests |
| Arithmetic in code | `research/metrics.py`, `intel/results.py`; models only see a computed facts pack | `test_bull_bear_model_points_must_cite_facts_and_reuse_their_numbers` |
| Verbatim quotes by ID | model returns `{id, metric, target}`; target numbers must appear *as quantities* in that sentence | `test_model_selects_ids_only_and_python_grounds_every_number` |
| Third-party text fenced | `sanitize.fence` / `fence_lines` strip control/bidi chars and fence tokens | `test_prompt_is_fenced_and_ids_cannot_be_forged` |
| Mobile-first, no build | vanilla HTML/CSS/JS, 44 px targets, checked at 375 px | `test_frontend_static_contract`, CI `node --check` |

## Classifier evaluation (rules only, real NSE filings, 0 model calls)

| Set | Window | n | Coverage | Precision | Precision (unambiguous rows) | Material buried |
|---|---|---|---|---|---|---|
| dev | 1–15 Nov 2026 | 95 | 100% | 97.9% | 98.9% | 0 / 27 |
| holdout | 16 Oct–7 Nov | 108 | 100% | 97.2% | 100% | 0 / 43 |
| **fresh** (never tuned on) | 1–15 Oct | 76 | 100% | **92.1%** | **98.5%** | **2 / 28** |

Before tuning, holdout was 84.5% precision with 13/43 material filings buried. Rules were tuned
once on holdout misses, so **fresh is the honest generalization number**; it is reported, not
gated. CI gates dev + holdout at precision ≥ 95%, coverage ≥ 85%, zero buried. Gold labels were
assigned blind by the building agent (not an independent annotator); judgement calls carry an
`ambiguous` note in `backend/eval/*.jsonl` for audit.

## Data, privacy, deployment

- **Sources**: NSE announcements and attachments, screener.in tables, BSE-hosted transcripts,
  yfinance EOD. Pull/research uses primary sources only. `DEMOCHEM` and the demo network are
  **synthetic** and labelled as such in the UI.
- **Network data**: name, employer stints, education, self-declared expertise, closeness. LinkedIn
  CSV import drops emails and profile URLs, 5 MB cap. Stored owner-only (0600), gitignored.
  Production: Postgres with row-level security and customer-managed KMS keys.
- **Render (free plan)**: one Docker web service (`render.yaml`), single uvicorn worker, non-root
  user. Disk is ephemeral: every boot seeds SQLite from the committed snapshot, and the research
  cache and private-mode imports do not survive a restart. Production: Postgres, a cron worker for
  the daily refresh, and `pg_advisory_xact_lock` instead of the in-process fetch lock.
- **Models** (optional): fast `gemini-3.5-flash-lite` (sentence-ID selection), deep
  `gemini-3.8-flash` (bull/bear) with a 25 s timeout → fast-tier fallback, 2 retries with
  exponential backoff on 429/5xx, SHA-256 content-addressed cache. Overridable via env
  (`.env.example`); any OpenAI-compatible endpoint works.

## Repository

```
backend/app/        config, db, llm, main (API), refresh
  ingest/           NSE filings, yfinance prices, screener.in + transcripts
  intel/            classify, materiality, alerts, results cards & bios, brief, sanitize, eval metrics
  research/         metrics (arithmetic), guidance (select-by-ID, drift, verdicts), engine
  network/          resolve (orgs, people), store (local, import guard), graph (queries)
backend/snapshot/   committed Brief snapshot, demo cards, DEMOCHEM, synthetic network
backend/eval/       labelled dev / holdout / fresh sets, label script, frozen baseline rules, reports
backend/scripts/    eval_classify.py, make_icons.py
backend/tests/      test_core.py, test_api.py, test_eval.py
frontend/           index.html, styles.css, app.js, manifest.json, icons
Dockerfile · render.yaml · .github/workflows/tests.yml
```

## Known limits

- Rules-mode guidance recall on real transcripts is low by design (precision first); the fast
  model supplies recall.
- Results PDFs: scanned or unusual layouts fall back to screener (labelled) or "Quarter not
  tabulated yet"; a retry with layout-preserving extraction handles column-block PDFs.
- Read-aloud and voice input depend on the browser's Web Speech support; mic denial is handled.
- Model IDs above are configuration defaults and were not verified against the provider's catalogue.
