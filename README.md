# FINN — AI investment assistant for Indian retail investors

FINN turns the daily flood of NSE/BSE corporate filings into a short, sourced digest, and lets you
ask questions that are answered only from company documents. It was built as a small working product
for the ENAM AI Engineer assessment, on free-tier infrastructure.

| Feature | What it does |
|---|---|
| **Push digest** | Classifies and scores every filing for an 18-company portfolio into *Must Read / Worth a Look / Routine*, with a one-line "why it matters". Results filings show extracted revenue / EBITDA / PBT / PAT with QoQ and YoY; management-change filings show who is joining or leaving, with a short background. |
| **Alerts** | Volume spikes, large price moves and material filings, with thresholds and watched categories the user can change live. |
| **Deep research** | For any NSE ticker: overview, financial health, growth catalysts and risks, built from the latest annual report and concall transcripts (via Screener.in), every bullet cited to a document and page. |
| **Promised vs. Delivered** | Finds specific, measurable commitments management made (annual report, older concalls) and checks them against later concalls and the latest quarterly results, with a status (*Beat / On Track / Delayed or Missed / Partially Disclosed*). |
| **Ask FINN** | Chat that retrieves from the stored documents, filings, results and prices and answers with inline citations such as `[Concall Q1 FY27, p. 22]`. |

The repository ships with a pre-populated database (`backend/data/finn.db`): 281 announcements,
extracted Q2 FY27 results, management-change bios, price history, and cached research for
**RELIANCE, BAJFINANCE, TCS and ITC**. A fresh deployment works immediately, with no pipeline run.

## Architecture

```
 NSE / BSE APIs ──▶ announcements.py ─┐
 Yahoo Finance  ──▶ prices.py ────────┤                      ┌────────────────────────────┐
 Screener.in    ──▶ fundamentals.py ──┤                      │  frontend/ (static SPA)    │
   (PDFs → PyMuPDF → 3k-char chunks)  ▼                      │  Tailwind + ES modules     │
                               ┌──────────────┐   JSON/HTTP  │  Today · Research · Chat   │
 pipeline/ classify, results,  │  SQLite      │◀────────────▶│  (served by FastAPI or any │
 mgmt_change, alerts, research │  finn.db     │  backend/    │   static host)             │
        │                      └──────────────┘  main.py     └────────────────────────────┘
        ▼                                         (FastAPI)
  llm.py: Gemini routing — call_cheap / call_strong, key + model fallback, cost log
```

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn (also serves the frontend at `/`) |
| Storage | SQLite (one file, tracked in git for the demo data) |
| Documents | PyMuPDF text extraction, ~3,000-character overlapping chunks, dependency-free BM25 retrieval |
| LLM | Google Gemini via `google-genai`; cheap model for bulk extraction, stronger model (high thinking) for Promised vs. Delivered; automatic key and model fallback; every call logged with token counts and estimated cost |
| Frontend | Static single-page app: Tailwind (CDN) + vanilla ES modules, mobile-first (bottom tabs on phones, top tabs on desktop) |

### How answers stay grounded
Every source passage gets an id. The model may cite ids only; the readable citation ("AR FY26, p. 42")
is built in code from stored metadata, so a page number cannot be invented. Items without a valid citation
are dropped, and a quoted commitment is kept only if it really appears in the cited passage. Numbers from
results filings are checked against the PDF text before they are stored. Details and the reasoning behind each
choice are in [`DECISIONS.md`](DECISIONS.md).

## Local quickstart

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r backend/requirements.txt

copy backend\.env.example backend\.env      # macOS/Linux: cp
# edit backend/.env and set GEMINI_API_KEY (free key: https://aistudio.google.com/apikey)

uvicorn backend.main:app --port 8000
```

Open <http://localhost:8000>. The Today and Research tabs work from the bundled database even without a key;
a key is only needed for Ask FINN, research on new tickers, and re-running the AI pipeline steps.
API docs are at <http://localhost:8000/docs>.

### Refreshing the data (optional)
Run from the repo root, in this order:

```bash
python -m backend.data.prices                    # Yahoo Finance prices
python -m backend.data.announcements             # NSE (BSE fallback) filings, last 30 days
python -m backend.pipeline.classify              # categories, scores, summaries (LLM)
python -m backend.pipeline.results_extract       # results numbers from PDFs (LLM)
python -m backend.pipeline.mgmt_change           # management changes + bios (LLM + web search)
python -m backend.data.fundamentals              # Screener annual reports + concalls (no LLM)
python -m backend.pipeline.research RELIANCE     # research for a ticker (LLM)
```

The bundled prices end on 13 Nov 2026. Price/volume alerts ignore data older than 5 days, so they will stop
firing on a deployment until you run the price step again.

## Deployment

The app is one Python service that also serves the frontend, so a single web service is enough.
Neither option below has been run by the author; the commands are the standard ones for each platform.

### Option A — one service on Render (recommended)
1. Push this repo to GitHub.
2. Render dashboard → **New → Web Service** → connect the repo.
3. Settings: **Runtime** Python 3 · **Build command** `pip install -r backend/requirements.txt` ·
   **Start command** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
4. **Environment**: `GEMINI_API_KEY` = your key, `GEMINI_BACKEND` = `gemini`, `PYTHON_VERSION` = `3.13.3`.
5. Deploy and open the service URL. (Railway works the same way: same build and start commands, same variables.)

`render.yaml` in the repo describes this setup if you prefer a Blueprint.

### Option B — frontend on Vercel, API on Render
1. Deploy the API as in Option A.
2. Edit `frontend/config.js` and set `window.FINN_API_URL = "https://<your-render-service>.onrender.com";`
3. Vercel → **Add New Project** → import the repo → **Root Directory** `frontend`, Framework preset **Other**, no build command.
4. CORS already allows any origin, so no backend change is needed.

## Engineering trade-offs and known limitations

- **Free-tier AI quota.** Free Gemini keys have daily limits per model. `llm.py` moves to the next key and then the next model
  when a limit is hit, which keeps things running, but answers may then come from a lighter model, and during
  heavy use or provider overload calls can still fail (the UI shows the error and lets you retry). `gemini-2.5-pro` is
  not available on new free projects, so the "strong" model is `gemini-3.5-flash` with high thinking. Its cost estimate
  is approximate.
- **Keyword search, not vector search.** Retrieval is BM25 over chunks: fast, free and easy to explain, but it misses
  synonyms ("bad loans" vs "NPA"). In testing, a Reliance ARPU question missed a relevant Q2 FY27 passage. Embeddings would improve recall.
- **Page numbers are PDF page positions**, not the numbers printed on the page, so they can differ by a few pages.
- **ITC has no concall transcripts on Screener.in** (the link is greyed out for every call), so its Promised vs. Delivered
  compares the annual report with the latest results only, and mostly returns "Partially Disclosed". Its annual report's
  forward-looking targets are largely sustainability goals. NSE/BSE earnings-call filings could fill the gap.
- **Statuses are AI judgement.** "Beat / On Track / Missed" is the model's reading of the cited passages. For example,
  Reliance's Jio ARPU growth of 3.5% against a "5–6%" aim is labelled Missed although management said about 4–5% in Q1. Read the cited page.
- **Quarter labels for concalls are derived from the call month**, since Screener shows no quarter. Off-cycle calls can be mislabelled.
- **Scraping fragility.** NSE, BSE and Screener are scraped without official APIs and can change or block cloud IP addresses,
  so refreshing data from a hosted server may fail even though it works locally. This is why the database is bundled.
- **Single-user, no authentication.** Alert settings are global, and anyone with the URL can call the AI endpoints and use your API quota.
  Fine for a demo; add authentication and rate limiting before real use.
- **Ephemeral storage on free hosts.** Render's free disk resets on each deploy, so research generated live and changed settings are lost
  on redeploy (the bundled database is restored). New-ticker research takes 1–2 minutes and free instances sleep when idle.
- **Tailwind is loaded from its CDN**, which is not meant for production; compiled CSS needs Node (not available where this was built).
- **Not financial advice.** Output is AI-generated from public documents; check the cited source before acting.

## Repository layout

```
backend/
  main.py            FastAPI app (also serves frontend/)
  llm.py             Gemini routing, fallback, usage/cost log
  config/            companies.json (18 portfolio + 4 research-demo companies)
  data/              prices.py, announcements.py, fundamentals.py, finn.db (tracked)
  pipeline/          classify, results_extract, mgmt_change, alerts, research, pdf_utils
  cache/             downloaded PDFs and raw API responses (git-ignored)
frontend/            index.html, config.js, js/ (today, research, chat, app)
DECISIONS.md         every technical decision and its reasoning
DEPLOY_AND_SUBMIT.md handoff and submission guide
```
