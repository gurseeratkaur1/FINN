# Decisions Log

Record of key technical decisions, newest at the bottom. Each entry states the
decision, why it was made, and any trade-offs.

---

## D1 — Small working product over broad scope

- **Decision:** Prioritise a small, end-to-end working product before the
  15 November deadline rather than many partially built features.
- **Why:** A complete, reliable core is more valuable to evaluate than an
  incomplete broad one.

## D2 — Separate backend and frontend

- **Decision:** FastAPI backend (`backend/`) and Next.js frontend (`app/`) are
  separate projects communicating over a JSON API.
- **Why:** Clear boundaries, each can be developed, tested and deployed
  independently.

## D3 — SQLite as the database

- **Decision:** Use SQLite, stored under `data/`.
- **Why:** Zero setup, no database server, a single file that is easy to
  deploy and reset. Sufficient for the expected data volume.
- **Trade-off:** Limited concurrent writes; acceptable because writes come
  mainly from the pipeline.

## D4 — Data pipeline separate from the API

- **Decision:** Data ingestion/processing lives in `pipeline/` and writes to
  SQLite; the API only reads.
- **Why:** Keeps API requests fast and simple, and lets the pipeline run on its
  own schedule.

## D5 — No hard-coded tickers

- **Decision:** Companies/tickers are defined in `config/`, never in code.
- **Why:** The tracked universe can change without code changes.

## D6 — Minimal dependencies

- **Decision:** Add dependencies only when a feature needs them.
- **Why:** Smaller attack surface, faster installs, simpler deployment.

## D7 — Mobile-first frontend

- **Decision:** Design the UI for phone screens first, then scale up.
- **Why:** The product is intended as a mobile-first assistant.

## D8 — All Python code under `backend/`

- **Decision:** `config/`, `data/`, `pipeline/` and `cache/` live inside
  `backend/`; the repo root holds only `backend/` and `app/`.
- **Why:** One self-contained Python project and one Next.js project, which
  makes each simple to deploy on its own.

## D9 — Company universe in `backend/config/companies.json`

- **Decision:** 18-company `portfolio` list mixing sectors (banking, IT, pharma,
  auto, FMCG, energy, PSU power, metals, infra, capital goods) and sizes
  (large/mid/small), including a high-beta name (Suzlon). A separate
  `research_demo` list of 4 companies includes 2 not in the portfolio (ITC,
  Bajaj Finance).
- **Why:** Realistic diversification for portfolio analysis, and the research
  engine is shown to work on any stock, not just portfolio holdings.
- **Note:** Cap labels are approximate and should be rechecked against the
  latest AMFI classification.

## D10 — Price data from yfinance, stored in SQLite `prices`

- **Decision:** `backend/data/prices.py` downloads 90 calendar days of daily
  close + volume per ticker via yfinance (`<TICKER>.NS`) and upserts into
  `backend/data/finn.db`, table `prices(ticker, date, close, volume)` with
  primary key `(ticker, date)`.
- **Why:** Free, no API key, covers any NSE symbol. Upserts make re-runs safe
  (no duplicates; late corrections from Yahoo overwrite old values).
- **Ticker input:** reads `portfolio` + `research_demo` from `companies.json`
  (deduplicated), or `--ticker X` for any NSE stock. Tickers are stored without
  the `.NS` suffix; the suffix is added only when calling Yahoo.

## D11 — Split/dividend-adjusted closes

- **Decision:** Use `auto_adjust=True` closes.
- **Why:** A split or bonus would otherwise look like a huge one-day crash and
  trigger false alerts. Trade-off: historical closes differ slightly from the
  raw exchange print.

## D12 — Metrics computed on read, not stored

- **Decision:** `get_metrics(ticker)` / `get_all_metrics()` compute latest
  close, 1-day % change, 20-day average volume and volume ratio from the
  `prices` table when called. The alerts engine and API import these helpers.
- **Why:** They are cheap (≤ 21 rows per ticker) and can never go out of sync
  with the price data, unlike a separate metrics table.
- **Volume baseline excludes the latest session**, so a spike day doesn't
  inflate its own baseline. `baseline_days` reports how many sessions were
  actually averaged (fewer than 20 for newly listed stocks).

## D13 — Failure handling

- **Decision:** Each ticker is retried up to 3 times with exponential backoff
  (2s, 4s), with a 0.5s pause between tickers. A ticker that still fails is
  reported as `NO DATA` and the run continues.
- **Why:** Yahoo rate-limits and occasionally returns empty frames; one bad
  symbol (e.g. a renamed company like ZOMATO → ETERNAL) shouldn't stop the
  whole run.
- **Stale data:** if a ticker's latest bar is older than the newest date seen
  in the run, the CLI flags it `STALE` so its 1-day change isn't mistaken for
  today's move.

## D14 — Python environment

- **Decision:** A single virtualenv at the repo root (`.venv/`, git-ignored),
  with exact versions pinned in `backend/requirements.txt`. Modules run from
  the repo root as packages, e.g. `python -m backend.data.prices`.
- **Why:** Reproducible installs; package-style imports work the same for the
  CLI, the pipeline and the FastAPI app.

## D15 — Announcements: NSE primary, BSE automatic fallback

- **Decision:** `backend/data/announcements.py` calls NSE's
  `/api/corporate-announcements` first, after warming up a `requests.Session`
  on the NSE home page and the corporate-filings page (to get cookies) with
  browser headers. If NSE returns 401/403, HTML or a non-list payload twice,
  NSE is marked blocked for the rest of the run and every remaining ticker
  goes to BSE (`AnnSubCategoryGetData`). `--source nse|bse` forces one.
- **What happened (14 Nov 2026):** NSE worked on the first attempt for all 20
  tickers (275 announcements, ~40s), so no time was spent on bot-block
  debugging. BSE was still implemented and tested (forced run on ETERNAL, and
  a simulated NSE block on TCS/INFY switched to BSE correctly) because NSE's
  bot protection is known to change without notice.
- **Why NSE first:** our tickers are NSE symbols (no mapping step), and NSE
  returns direct `nsearchives` PDF links plus a short description per filing.
- **BSE mapping:** NSE ticker → BSE scrip code via BSE's `PeerSmartSearch`
  (matched on the exact NSE symbol in the result), cached in
  `backend/cache/bse_codes.json`. PDF URL = `AttachLive/` or `AttachHis/`
  (per `PDFFLAG`) + `ATTACHMENTNAME`.

## D16 — Announcement normalization and IDs

- **Fields:** `id, ticker, announced_at (ISO, IST), subject, description,
  raw_category, pdf_url, source`.
  - NSE: `subject` = `raw_category` = `desc` (NSE's subject is a category
    label such as "Outcome of Board Meeting"); `description` = `attchmntText`.
  - BSE: `subject` = `NEWSSUB`; `description` = `HEADLINE` + `MORE`;
    `raw_category` = `CATEGORYNAME`.
- **ID = sha1(ticker | announced_at | subject | pdf_url).** The spec asked for
  ticker + announced_at + subject, but that collapsed 4 of 275 real filings:
  companies often file several documents in the same second under the same
  generic subject (NCC filed 3 separate director re-appointments at
  18:17:59; Reliance filed 2 results PDFs at 19:40:11). Adding `pdf_url`
  keeps the ID deterministic (re-runs upsert, verified: 275 rows after two
  runs) while keeping distinct filings.
- **Known limitation:** the same filing fetched from NSE on one day and BSE on
  another gets two different IDs (different subjects and timestamps). Rare in
  practice since BSE is only a fallback; acceptable for the deadline.

## D17 — Raw response cache for offline replay

- **Decision:** every raw API response is saved to
  `backend/cache/announcements_raw/{nse|bse}_{TICKER}.json` (with fetch time
  and date range). `--offline` rebuilds the table from these files with no
  network calls.
- **Why:** a demo shouldn't depend on NSE/BSE being reachable. The cache folder
  is git-ignored; run the pipeline once before a demo to populate it.
- **Politeness:** random 0.5–1s pause between requests, 2 attempts per request,
  fresh NSE session (new cookies) on each retry.

## D18 — LLM routing (`backend/llm.py`) and the model switch

- **Decision:** one module owns every LLM call. `call_cheap()` handles bulk
  classification/summaries; `call_strong()` handles complex reasoning. Both take an
  optional JSON schema (strict JSON output), retry 429/5xx and network
  timeouts with exponential backoff and jitter (5 attempts), and log every call
  to `llm_usage(feature, model, input_tokens, output_tokens, est_cost_usd,
  created_at)`. Thinking tokens are counted as output, which is how they're billed.
- **Config lives in `backend/.env`:** `GEMINI_API_KEY`, `GEMINI_BACKEND`
  (auto|vertex|gemini), `GEMINI_CHEAP_MODEL`, `GEMINI_STRONG_MODEL`,
  `GEMINI_CHEAP_THINKING`. Defaults follow the spec (`auto`: an `AQ.` key goes to
  Vertex; `gemini-2.5-flash` / `gemini-2.5-pro`).
- **What actually happened (14 Nov 2026):**
  - Vertex AI with the `AQ.` key returned 403: the Vertex/"Agent Platform" API
    isn't enabled on the key's Google Cloud project.
  - The same key works on the regular Gemini API, but there `gemini-2.5-flash`
    and `gemini-2.5-pro` return 404 "no longer available to new users".
  - We therefore run `GEMINI_BACKEND=gemini`. The cheap model was first
    `gemini-3.8-flash` (user's choice), but it was overloaded: repeated 503
    errors, a 504 after 58s, and roughly 5 minutes per batch. It was switched to
    `gemini-3.5-flash`, which answered in 9–28s. Both are just `.env` values.
  - The strong model is still undecided (`gemini-4-argon` returned 429 quota);
    it isn't needed until a later step.
- **SDK retries disabled:** the google-genai SDK's own silent retries are turned
  off, and each request has a 120s timeout, so our logged backoff is the only
  retry layer and an overloaded model can't hang the pipeline for minutes.
- **Cost estimates are approximate for Gemini 3.x:** `PRICING` lists only the
  2.5 models' list prices. Unlisted models are estimated at 2.5-flash rates
  ($0.30 / $2.50 per 1M tokens), with a warning. Token counts are exact; update
  `PRICING` before reporting dollar figures in Step 12.

## D19 — Classification: rules first, cheap LLM only for leftovers

- **Step A, rules (free):** ordered regex rules run over `subject + description`.
  When the subject is a generic label ("Updates", "General Updates", "Outcome
  of Board Meeting"), only the description is used. Order matters:
  transcripts/recordings → `earnings_call` before results; newspaper ads of
  results → `routine` before `results`. A board-meeting *intimation* is
  `routine` unless it announces a fund raise, bonus/split, M&A or dividend.
  ESG ratings are excluded from `credit_rating`.
- **Step B, cheap LLM:** rows with no rule match go to `call_cheap`, 20 per call,
  with an enum-constrained JSON schema. The model returns batch indices, not
  free-text IDs, so it can't invent IDs.
- **Result:** 224 / 281 (80%) handled by rules, 57 by the LLM (4 classify calls).
  The LLM rows were genuinely ambiguous: "General Updates", committee-meeting
  outcomes, rumour/news verifications, "Allotment of Securities pursuant to
  Non Convertible Securities" (NSE's label is used for both NCDs and ESOPs).
  9 rows remain `unclassified` even for the LLM; they are vague
  "Disclosure under Regulation 30" filings.
- **Caching:** results are stored in `announcement_insights` keyed by
  announcement id. Classification and summaries are never redone (`--force`
  resets). A run that is interrupted resumes where it stopped (rows wait as
  `pending_llm`).

## D20 — Priority score and tiers

- **Score = category weight + materiality bump + market boost, capped 0–100.**
  - Category weights: results 75; M&A 65; fund_raise, management_change,
    litigation, bonus_split 60; capex 50; credit_rating, dividend 45;
    insider_trading, earnings_call 40; press_release, unclassified 30;
    investor_meetings 15; routine 5.
  - Materiality bumps: CEO/MD/CFO/chairman change +10; dilutive equity raise
    (QIP, preferential, rights) +15; actual merger/demerger/scheme/open offer
    +10; rating upgrade/downgrade +15; SEBI/court/fraud litigation +10. This
    separates an NBFC's routine NCD allotment (60) from a QIP (75), and a new
    subsidiary (65) from a merger (75).
  - Market boost (max +25): taken from the *reaction session* (the first
    trading session after the announcement; after 15:30 IST it's the next
    day). +10/+15 for a |move| ≥ 3% / 5%, and +10/+15 for volume ≥ 1.5x / 2.5x
    the 20-day average (same definitions as `prices.get_metrics`).
- **Tiers:** ≥70 MUST_READ, 40–69 WORTH_A_LOOK, <40 ROUTINE.
- **Why reaction-session rather than "latest" price data:** a filing from three
  weeks ago should be judged by how the market reacted to it, not by today's tape.
- **Attribution fix:** the first version boosted 139/281 rows. Every filing
  on a results day inherited the results-day move, including transcripts,
  recording links and director changes filed the same evening. Now (a)
  routine, investor_meetings, earnings_call and unclassified rows never get a
  boost, and (b) a session's move goes only to that ticker's highest-scoring
  filing(s) reacting to it. Boosted rows: 50. MUST_READ: 70 → 51, of which
  27 are results (it's results season).
- **Scores are recomputed every run** (free), because the reaction session may
  not be in the price data yet when an announcement first arrives.

## D21 — 1-line "why it matters" summaries only where they pay off

- **Decision:** only MUST_READ and WORTH_A_LOOK rows get a summary, from
  `call_cheap`, batched 20 per call. ROUTINE rows get none.
- **Prompt rules:** use only facts in the text, never invent numbers, say what
  to check in the filing when the text is thin, no buy/sell advice, max 25
  words. The market reaction is included as context.
- **Known limitation:** NSE descriptions are often one line ("has submitted
  financial results"), so many summaries can only say *what to check*. Reading
  the PDF content would fix this; it's a candidate for a later step.
- **Cost so far:** 13 calls, 16,641 input + 8,796 output tokens, about $0.027
  (estimated at 2.5-flash rates; see D18).

## D22 — Free-tier LLM routing: key rotation and model fallback

- **Problem (14 Nov 2026):** the Gemini key is on the free tier, which allows
  about 20 requests/day *per model*. `gemini-3.5-flash` ran out in the middle
  of Step 5, and the reset would land after the 15 Nov deadline. Search grounding
  has no free quota.
- **Decision:** `call_cheap` tries `GEMINI_CHEAP_MODEL`, then
  `GEMINI_CHEAP_FALLBACKS` (default `gemini-3.1-flash-lite`,
  `gemini-3.5-flash-lite`), across every key in `GEMINI_API_KEYS`
  (comma-separated; falls back to `GEMINI_API_KEY`). A 429 whose message says
  "per day" marks that (key, model) exhausted for the rest of the process and
  moves on immediately; other 429/5xx errors are retried with backoff, honouring
  the server's "retry in Ns" hint (capped at 60s). A 404 "no longer available"
  marks the model unusable. If everything is exhausted, `LLMQuotaError` is raised
  and callers leave work pending, so the next run resumes.
- **Fallbacks were chosen by testing, not by name:** `gemini-2.5-flash-lite` (the
  suggested one) returns 404 for this account; `gemini-3.5-flash-lite` works.
- **Trade-off:** the fallback models are lighter. They are good enough for
  structured extraction but occasionally give up on usable evidence (seen once
  on a bio), which is why bios get one retry (see D24) and why every number
  is verified in code (see D23). The model that actually served each call is
  recorded in `llm_usage.model` and `results_enrichment.model`.
- **Google Search grounding was removed**, as requested; bios use free web
  search instead (D24).

## D23 — Results extraction: LLM reads, Python verifies and computes

- **Pipeline (`results_extract.py`):** download the PDF (cached in
  `backend/cache/pdfs/`, ZIP attachments unpacked, truncated/corrupt files
  rejected) → pick the P&L pages → `call_cheap` returns raw line items for
  current / previous / year-ago quarter → Python verifies, derives, computes.
- **Page selection instead of "first 6–8 pages":** results tables are often far
  into the document (ICICI p.13, Infosys p.18 of 195, HDFC p.9). We pick the
  first page that has a before-tax line, an income line and quarter columns and
  is not a cash-flow statement (consolidated preferred), plus the next page
  (2 pages keeps token use lean). Filings with no table
  fall back to the first pages and come back `no_numbers`.
- **Anti-hallucination check:** every number the LLM returns must appear in the
  PDF text it was given; any number that doesn't is set to null and recorded in
  `unverified`. The parser tolerates real OCR artefacts (`21 .62`,
  `54269 95`, `533-402`, `31.658` for 31,658). It removed 20 values across 10
  filings; the bank values I traced (ICICI, HDFC, Federal, Sun Pharma, Eternal)
  were absent from every page of the PDF, so they were model errors, mostly
  banks' PAT. The raw model output is kept in
  `raw_llm_json` for audit.
- **Derived in Python, never by the LLM:** QoQ %, YoY %, EBITDA margin, and
  EBITDA = PBT + finance costs + depreciation when no EBITDA line is printed
  (`ebitda_derived = 1`). EBITDA is not computed for banks/NBFCs (interest is
  their main cost). Growth is `null` when the base is zero or negative (loss to
  profit), because "% change" is meaningless there.
- **Known gaps, left null on purpose:** PAT for HDFC Bank, ICICI Bank and Sun
  Pharma (current quarter) and Federal Bank (several fields); Persistent has
  revenue and PAT but no comparison columns; Infosys has PBT/PAT but no revenue;
  Maruti's previous-quarter PAT. Tata Steel's PDFs contain no table (board
  outcome only), so it stays `no_numbers`. A null is better than a wrong number;
  a later step can read more pages or the investor presentation to fill them.
- **Broken NSE PDF:** HDFC Bank's NSE results PDF is truncated (a 983,040-byte
  cut-off). The pipeline falls back to the identical filing on BSE
  automatically (`load_filing`).
- **Duplicates:** one company often files the same results several times
  (Reliance: 3 filings). Each is stored keyed by `pdf_url`; `get_latest_results`
  returns the newest.

## D24 — Management changes: per-person rows and free web-search bios

- **Extraction:** `call_cheap` reads the PDF (first 4 pages) or the description
  and returns one row per person: name, role, change type (joining / resigning /
  retiring / reappointed / other), effective date, KMP flag, and the profile
  the filing itself discloses. Stored in `mgmt_enrichment`.
- **One filing = one person:** NCC filed three filings (one per director), all
  attaching the same combined board outcome, so the model credited every filing
  with all three people (9 rows instead of 3). Prompting didn't fix it, so
  `only_named_people` keeps only people whose name appears in the exchange's own
  description (generic descriptions like "change in Management" keep everyone).
- **Bios for people joining, and re-appointed KMPs:** a DuckDuckGo search
  (`ddgs`, no key, no cost) for `"<name>" "<company>"`, falling back to a looser
  query; the top 3 snippets (LinkedIn excluded) plus the profile disclosed in the
  filing go to `call_cheap`, which may use only those texts. Sources are stored
  with each bio. If the evidence is thin, the stored bio is exactly
  "No public background found".
- **Honest states:** `found`, `not_found`, `pending` (search or LLM failed; retried
  by `--retry-bios`, never mistaken for "nothing exists") and `not_needed`.
- **Retry on "not found":** one lightweight-model answer of "not found" on
  usable evidence (Anup Bagchi, whose bio is easy to find) is not trusted;
  it is retried once with a different temperature, and `--retry-bios` also
  revisits `not_found` rows.
- **Limits:** snippets are short, so bios are often 1–2 sentences, and a
  filing's own profile is the best source for new KMPs. Snippets about
  similarly named people are a risk; the prompt requires a link to the company
  or role, but a human should spot-check bios before they're shown to users
  as fact.

## D25 — `call_strong` model

`gemini-2.5-pro` is unavailable on new free-tier projects, so `call_strong` now uses
`gemini-3.5-flash` with `thinking_level=high`, falling back to `gemini-3.8-flash`, then
`gemini-3.1-flash-lite` (override with `GEMINI_STRONG_MODEL`, `GEMINI_STRONG_THINKING`,
`GEMINI_STRONG_FALLBACKS`). The 3.5-flash price in `PRICING` is an estimate (same as 2.5-flash);
cost reports for it are approximate until checked against official rates. Not yet exercised by any call.

## D26 — Alerts engine (`backend/pipeline/alerts.py`, no LLM)

- **Settings:** one row in `user_settings` (`id=1`), created with defaults on first read:
  volume 2.0x, price move 5.0%, categories results / M&A / management_change / litigation /
  fund_raise / credit_rating. `evaluate_alerts(settings=...)` takes temporary overrides that are
  never saved, so the demo thresholds (1.1x / 2%) leave the saved defaults untouched.
- **Price/volume:** uses `prices.get_metrics` (`change_1d_pct`, `volume_ratio` = latest volume over the
  prior 20-session mean). Metrics whose latest session is more than 5 calendar days old are
  "stale" and skipped, so a broken price feed can't raise alerts.
- **Filings:** announcements in the window (default 168 h) whose category is watched **or** tier is
  MUST_READ. Severity is high for MUST_READ, else medium; `why` reuses the stored one-line summary.
- **Severity for price/volume:** high at 2x the threshold, else medium.
- **Alert ids** are hashes of ticker + type + date (or announcement id), so they are stable across runs.
- **Result at default thresholds:** 7 filing alerts, 0 price/volume. At the demo thresholds: 7 filing,
  1 price move (Ashok Leyland -2.7%), 2 volume spikes (Caplin Point 1.3x, Bajaj Finance 1.2x).
  Real moves were small that week, so the demo needs lowered thresholds.

## D27 — Screener.in fundamentals (`backend/data/fundamentals.py`, no LLM)

- **Source:** Screener's company page (`/consolidated/`, then `/`) lists documents. The latest annual
  report and the last 4 concall transcripts are downloaded to `backend/cache/fundamentals/<TICKER>/`
  (cached; re-runs download nothing) and chunked per page: ~3,000 chars with 400 overlap, blank pages skipped.
  Chunks go to `fundamental_chunks`, per-document status to `fundamental_docs`.
- **Quarter labels are derived from the call month** (Apr-Jun call = Q4 of that FY, Jul-Sep = Q1, Oct-Dec = Q2,
  Jan-Mar = Q3). Screener shows no quarter, so an off-cycle call (e.g. a call held a month late) can be
  labelled by month rather than by the true quarter. Labels are approximate.
- **One transcript per quarter:** Screener sometimes lists two for one quarter (TCS's second "Jul 2026"
  link was a 2025 file and returned HTTP 403; Bajaj has two for Q4 FY26). We keep the newest per label
  and treat the rest as fallbacks if a download fails.
- **Failures are reported, not raised:** status is `ok`, `partial`, `not_found`, `no_documents` or `error`.
- **Retrieval:** dependency-free BM25 in `search_chunks(ticker, query, doc_type, top_k)`, loaded
  from SQLite per query. Fine for thousands of chunks per ticker; keyword-only, so synonyms
  ("bad loans" vs "NPA") won't match.
- **ITC has no transcripts on Screener** (the "Transcript" chip is greyed out for every call), so only
  its annual report is stored. A fallback would be the NSE/BSE "earnings call" filings.

## D28 — Research engine (`backend/pipeline/research.py`)

- **Flow:** ingest fundamentals if no chunks exist, retrieve passages with BM25, `call_cheap` writes the profile
  (overview, financial health, catalysts, risks), `call_strong` does Promised vs. Delivered, results are cached
  in `company_research` (one row per ticker; `?refresh=true` / `--force` regenerates).
- **Grounding by construction:** each source gets an id (P passages, F1 latest results, A filings, M1 price). The
  model cites ids only; the citation text ("AR FY26, p. 42") is built in Python from stored chunk metadata, so
  a page number can't be made up. Bullets with no valid citation are dropped. A promised-vs-delivered quote is kept only if it
  appears (ignoring punctuation and case) in the passage it cites; otherwise `source_quote` is null and
  `source_quote_verified` is false. An item with no delivery evidence can only be "Partially Disclosed".
- **Page numbers are PDF page indices**, not the printed page numbers, and can differ by a few pages.
- **Two-stage Promised vs. Delivered:** the first version (one call, generic retrieval) returned all
  "Partially Disclosed" for Reliance because the evidence passages didn't match the claims. Now stage 1
  extracts 4-6 measurable commitments (annual report + the two oldest concalls) with search keywords, then each
  commitment's keywords retrieve evidence from the latest two concalls, and stage 2 judges delivery. That is
  2 `call_strong` + 1 `call_cheap` per company.
- **What it can't do:** it checks stated numbers, not business outcomes the transcripts don't mention. Statuses are an AI judgement (e.g. Reliance ARPU 3.5% vs a "5-6%" aim was labelled Missed, though management
  said ~4-5% in Q1); a human should read the cited passage.
- **ITC is weak:** no transcripts exist, and its annual report's forward-looking targets are mostly sustainability / CSR.
  The prompt now prefers financial/business commitments, but the result is still mostly "Partially Disclosed" with
  unverified quotes. The note says concalls were unavailable. Needs NSE/BSE earnings-call filings as an extra source.
- **TCS** has no extracted quarterly results (no results filing in the stored window), so its financial section says so.
- **Quota:** `gemini-3.5-flash` hit its daily free quota during ITC's rerun, so that run used `gemini-3.8-flash`
  (priced at the 2.5-flash estimate). `quality.pvd_model` records the model used.

## D29 — FastAPI (`backend/main.py`)

- Run with `uvicorn backend.main:app --port 8000`. CORS allows all origins.
- `/api/today?days=7`: MUST_READ filings (with extracted results numbers and management-change bios attached), WORTH_A_LOOK, live alerts, for the 20 companies in
  the portfolio + research_demo universe (the two groups overlap on RELIANCE and TCS, so 18 + 4 = 20, not 22).
- `/api/announcements` (ticker / tier / category / limit), `/api/alerts`, `/api/settings` (GET/PUT, categories validated), `/api/portfolio`, `/api/health`.
- `/api/research/{ticker}`: cached, else generated synchronously (can take 1-3 minutes for a new ticker: Screener
  download + 3 AI calls). Unknown ticker -> 404, invalid symbol -> 422, AI quota out -> 503, other upstream failure -> 502. The frontend should show a loading state.
- `/api/chat`: tickers come from the `ticker` field, else from ticker symbols / company names in the message (max 3). Context = BM25
  passages for the question, latest results, price, recent filings, management changes. One company -> `call_cheap`; several or "compare/vs" ->
  `call_strong`. The model cites ids; the API swaps them for real labels ("[Concall Q2 FY27, p. 8]") and drops unknown ids. Chat does **not** download
  documents: a ticker without ingested text gets an answer from filings/prices only (or a clear "no data").
- **Weakness:** chat retrieval is keyword-only, so a question phrased differently from the transcript can miss the best passage
  (the Reliance ARPU answer missed the Q2 FY27 figure). Embedding search would fix this.

## D30 — Frontend (`frontend/`): static app, no Node

- **Node.js is not installed on this machine** (not in PATH or `C:\Program Files\nodejs`), so the Next.js option wasn't
  possible. Built the fallback: Tailwind (CDN) + vanilla ES modules, no build step. FastAPI serves it at `/`
  (mounted after the `/api` routes), and the same folder deploys as a static site (e.g. Vercel). Set the backend URL in
  `frontend/config.js` (`window.FINN_API_URL`; same-origin when served by FastAPI, else `http://localhost:8000`). The
  `NEXT_PUBLIC_API_URL` convention doesn't apply. If a Next.js app is still wanted, it can be ported on a machine with Node.
- **Tailwind CDN** is the "play" build, which Tailwind says is not for production; it logs one console notice. A production
  deploy should compile the CSS (needs Node) or accept the notice.
- **Layout:** bottom tab bar on mobile, top tabs from 768px. Tabs: Today, Research, Ask FINN.
- **Safety:** all text, including model output, is inserted with `textContent` (no `innerHTML`), so AI text can't inject HTML.
- **Today:** alerts strip + settings drawer (sliders, category toggles, save / reset; the strip updates from the PUT response). Must Read and
  Worth a Look come from `/api/today` (last 7 days); "All" and the company filter come from `/api/announcements` (top 100 by priority, any date).
  Results filings show a mini table; management changes show name, role, joining/resigning badge and bio.
- **Research:** demo pills, any-ticker search with an elapsed-time loading state and Cancel, Promised vs. Delivered card with colour-coded statuses,
  four collapsible sections; each bullet has citation badges.
- **Chat:** context selector, starter chips, history kept in page memory only (lost on reload), citation pills open the source excerpt, 150 s client timeout.
  The server now falls back from `call_strong` to `call_cheap` for comparison questions.
- **Verified** with headless Chrome at a 375px viewport (iframe driven by a temporary test page, since deleted): all tabs rendered
  live data, no console errors, no horizontal overflow; alert settings changed the count (7 -> 10 at demo thresholds -> 7 after reset);
  Research loaded Bajaj Finance (6 items); an unknown ticker showed a clear message; a TCS chat question returned a cited answer.
- **Not verified:** the Bajaj "Compare ..." chat chip, which failed because Gemini was overloaded at the time (503/504 even on the fallback model) and the
  `gemini-3.5-flash` daily quota was used up. The error is shown in the chat; retry later.
- **Not tested:** real phones, Safari, or any browser other than Chrome.

## D31 — Handoff package

- **Database tracked in git:** `.gitignore` now allows `backend/data/finn.db` (all other `*.db` stay ignored) so a deployment starts
  with the 281 announcements, results, bios, prices and the four cached research reports. It is an 11 MB binary: every commit that changes it
  adds another copy to git history. The machine-specific file paths stored in `fundamental_docs.local_path` were rewritten to repo-relative.
- **Not committed:** `backend/.env`, `backend/cache/` (356 MB of PDFs and raw responses). New-ticker research re-downloads what it needs.
- **Hosting caveats:** free hosts have ephemeral disks (live-generated research and settings reset on redeploy); NSE/BSE/Screener may
  block cloud IPs; bundled prices end 13 Nov 2026 and price alerts treat data older than 5 days as stale. None of the deploy steps
  were executed by the author, since there is no Render/Vercel account in this environment.
- **Build logs:** Claude Code transcripts are copied to `ai-build-logs/` with API keys (the literal values from `.env` plus key-shaped
  strings) and the user's email address replaced by `[REDACTED]`. Local file paths (including the Windows user name) and tool output remain.
- Removed the unused empty `app/` folder (leftover from the planned Next.js app).
