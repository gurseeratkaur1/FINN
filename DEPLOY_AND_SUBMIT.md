# FINN — handoff, deployment and submission guide

This is for whoever is publishing FINN under their own GitHub account and submitting it.
Everything below takes about 15 minutes, plus a few minutes for the first deploy.

## 0. What you need
- A GitHub account, and [Git](https://git-scm.com/downloads) installed.
- A free Render account (<https://render.com>, sign in with GitHub).
- A free Gemini API key from <https://aistudio.google.com/apikey> (Google account; no card needed).
- The `FINN` folder from the handoff (the whole folder, including the hidden `.git` folder if you want the commit history).

> **Never commit or send `backend/.env`.** It holds the API key. It is git-ignored; the folder you receive may not
> contain it, which is fine. Create your own key and paste it only into Render's environment settings.

## 1. Put it on your GitHub
1. On GitHub: **New repository** → name it `finn` → leave it **empty** (no README, no .gitignore) → Create.
2. In a terminal, inside the `FINN` folder:
   ```bash
   git status                      # should say "nothing to commit"; backend/.env must NOT be listed
   git remote remove origin        # only if one exists already; ignore an error
   git remote add origin https://github.com/<your-username>/finn.git
   git branch -M main
   git push -u origin main
   ```
   The repo is about 25 MB or less. It includes `backend/data/finn.db` on purpose: the demo data is inside it.
3. Refresh the GitHub page. Check that `backend/data/finn.db` is there and `backend/.env` is **not**.
   If your commits should appear under your name, set `git config user.name` / `user.email` before pushing new commits.
   (Existing commits keep the original author.)

## 2. Deploy live (Render, about 5 minutes)
1. Render → **New → Web Service** → pick your `finn` repo.
2. Fill in:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r backend/requirements.txt`
   - **Start command:** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** Free
   - (Or choose **New → Blueprint** and Render reads `render.yaml` for you.)
3. **Environment** → add:
   | Key | Value |
   |---|---|
   | `GEMINI_API_KEY` | your key from step 0 |
   | `GEMINI_BACKEND` | `gemini` |
   | `PYTHON_VERSION` | `3.13.3` |
4. **Create Web Service.** The first build takes a few minutes. When it says *Live*, open the URL shown at the top.
   The first request after idle can take up to a minute (free instances sleep).
5. Check `https://<your-service>.onrender.com/api/health`: it should return JSON with `"status":"ok"` and
   `announcements: 281`.

*Optional — frontend on Vercel:* set `window.FINN_API_URL` in `frontend/config.js` to your Render URL, commit and push,
then in Vercel import the repo with **Root Directory** `frontend` and no build command. Skip this unless you need it.

## 3. Check the three mobile workflows
Open the live URL on your phone (or Chrome → right-click → Inspect → device toolbar → 375px wide).

1. **Today.** You should see a "Live alerts" strip (7 alerts) and Must Read cards. Open **Alert settings**, move *Volume multiple*
   to about 1.1x and *Price move* to 2%, tap **Save & update alerts**: the alert count should rise (about 10).
   Tap **Reset defaults**. Check that a results card (e.g. CAPLIPOINT) shows a small table, and that "Open filing PDF" opens.
2. **Research.** Tap **BAJFINANCE**. The "Promised vs. Delivered" card should appear at the top with coloured status badges and
   citations like `AR FY26, p. 94`, followed by Overview, Financial health, Growth catalysts and Key risks. Tap
   **RELIANCE**, **TCS**, **ITC** too (ITC shows a note that transcripts were unavailable). Try a new ticker, e.g. `INFY`: it will take
   1–2 minutes and needs the Gemini key. It may fail if Screener or the AI service is busy; that is expected, not a bug in your deploy.
3. **Ask FINN.** Tap *"What are TCS's key growth catalysts & risks?"*. You should get a short answer with blue citation pills;
   tap a pill to see the source excerpt. If you see "AI service error" or "taking too long", the Gemini free tier is busy or out of
   daily quota. Wait a minute and retry. Don't read it as a deployment failure.

If Today and Research load but Ask FINN fails, the usual cause is a missing or wrong `GEMINI_API_KEY`.

## 4. Before you submit
- Read the **Engineering trade-offs and known limitations** section of `README.md`. Be ready to talk about them.
- Skim `DECISIONS.md`; it records why each choice was made and what went wrong along the way.
- Free-tier warning: anyone who has your URL can use your Gemini quota. Share the link only with the people who need it, or take the service down after review.

## 5. Submission email template

> **Subject:** FINN — AI investment assistant (submission, [Your Name])
>
> Hi [Recipient],
>
> Please find my submission for the AI Engineer assessment.
>
> - **Live demo:** https://[your-service].onrender.com (open on a phone; the first load can take about a minute because it runs on a free instance)
> - **Code:** https://github.com/[your-username]/finn
>
> **What it is.** FINN is a mobile-first assistant for Indian retail investors. It (1) turns NSE/BSE filings for an 18-company portfolio
> into a prioritised "Must Read / Worth a Look" digest, with extracted quarterly numbers and management-change bios; (2) raises user-tunable
> alerts on volume, price moves and material filings; (3) builds a cited deep-research page for any NSE ticker from the annual report and
> earnings-call transcripts, including a "Promised vs. Delivered" check of management commitments against later results; and (4) answers
> questions in a chat that cites document and page for every claim.
>
> **How it's built.** FastAPI + SQLite, PyMuPDF and a BM25 retriever for documents, Gemini with cheap/strong model routing and automatic
> key/model fallback, and a static Tailwind/ES-module frontend. Answers are grounded by construction: the model cites source ids, citation text
> and page numbers are generated in code, and unverifiable quotes or uncited bullets are dropped.
>
> **Trade-offs I want to be upfront about.** It runs on free-tier AI quota, so heavy use can hit limits or provider overload; retrieval is
> keyword-based (BM25), which misses synonyms; page numbers are PDF page positions, not printed page numbers; the Promised vs. Delivered
> statuses are AI judgement that should be checked against the cited page; ITC has no transcripts on Screener.in, so its comparison is weak;
> and the data sources are scraped, so live refresh from a cloud server may be blocked (the repo ships a pre-populated database for this reason).
> There is no authentication, so it is a single-user demo.
>
> `DECISIONS.md` documents every technical decision.
>
> Thank you for your time.
> [Your Name]
