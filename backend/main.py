"""FastAPI backend.

    uvicorn backend.main:app --reload --port 8000

Endpoints: /api/health, /api/today, /api/announcements, /api/alerts, /api/settings (GET/PUT),
/api/portfolio, /api/research/{ticker}, /api/chat (POST).
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from backend import llm
from backend.data import fundamentals, prices
from backend.pipeline import alerts as alerts_engine
from backend.pipeline import classify, mgmt_change, research
from backend.pipeline.results_extract import get_latest_results

log = logging.getLogger("finn.api")

app = FastAPI(title="FINN API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
TIERS = ("MUST_READ", "WORTH_A_LOOK", "ROUTINE")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _universe() -> dict[str, dict]:
    """Portfolio + research_demo companies, de-duplicated, with group flags."""
    cfg = json.loads(prices.CONFIG_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for group in ("portfolio", "research_demo"):
        for c in cfg.get(group, []):
            entry = out.setdefault(c["ticker"], {"ticker": c["ticker"], "name": c["name"], "sector": c.get("sector"),
                                                 "cap": c.get("cap"), "in_portfolio": False, "research_demo": False})
            entry["in_portfolio" if group == "portfolio" else "research_demo"] = True
    return out


def _clean_ticker(raw: str) -> str:
    ticker = prices.normalize_ticker(raw)
    if not TICKER_RE.match(ticker):
        raise HTTPException(422, f"'{raw}' is not a valid NSE ticker symbol")
    return ticker


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    tables = ["prices", "announcements", "announcement_insights", "results_enrichment", "mgmt_enrichment",
              "fundamental_chunks", "company_research", "llm_usage"]
    counts = {}
    with closing(alerts_engine._connect()) as conn:
        research._connect().close()
        for t in tables:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                counts[t] = None
    usage = llm.usage_summary()
    return {"status": "ok", "timestamp": _now(), "db_counts": counts, "llm_usage": usage,
            "llm_total_est_cost_usd": round(sum(u["est_cost_usd"] or 0 for u in usage), 4)}


# --------------------------------------------------------------------------- #
# Announcements / today
# --------------------------------------------------------------------------- #
def _enrich(rows: list[dict]) -> list[dict]:
    """Attach extracted results numbers (results filings) and management bios (management changes)."""
    results_cache: dict[str, dict | None] = {}
    for r in rows:
        r["results"] = None
        r["management_changes"] = []
        if r["category"] == "results":
            if r["ticker"] not in results_cache:
                res = get_latest_results(r["ticker"])
                results_cache[r["ticker"]] = res and {"period": res["period_label"], "basis": res["basis"],
                                                       "unit": res["currency_unit"], "metrics": res["metrics"]}
            r["results"] = results_cache[r["ticker"]]
        elif r["category"] == "management_change":
            r["management_changes"] = [
                {k: m[k] for k in ("person_name", "role", "change_type", "effective_date", "bio", "bio_status",
                                   "bio_sources")}
                for m in mgmt_change.get_changes(r["ticker"]) if m["announcement_id"] == r["id"]]
    return rows


def _recent(tiers: tuple[str, ...], days: int, limit: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat(timespec="seconds")
    rows = [r for r in classify.get_ranked(tiers=tiers, limit=500) if r["announced_at"] >= cutoff]
    return rows[:limit]


@app.get("/api/today")
def today(days: int = Query(7, ge=1, le=60), limit: int = Query(25, ge=1, le=100)) -> dict:
    tickers = set(_universe())
    must = [r for r in _recent(("MUST_READ",), days, 500) if r["ticker"] in tickers][:limit]
    worth = [r for r in _recent(("WORTH_A_LOOK",), days, 500) if r["ticker"] in tickers][:limit]
    return {"generated_at": _now(), "window_days": days, "must_read": _enrich(must), "worth_a_look": worth,
            "alerts": alerts_engine.evaluate_alerts(hours=days * 24)}


@app.get("/api/announcements")
def announcements(ticker: str | None = None, tier: str | None = None, category: str | None = None,
                  limit: int = Query(50, ge=1, le=500)) -> dict:
    if tier and tier.upper() not in TIERS:
        raise HTTPException(422, f"tier must be one of {', '.join(TIERS)}")
    if category and category not in classify.CATEGORIES:
        raise HTTPException(422, f"category must be one of {', '.join(classify.CATEGORIES)}")
    rows = classify.get_ranked(ticker=_clean_ticker(ticker) if ticker else None,
                               tiers=(tier.upper(),) if tier else None, limit=500)
    if category:
        rows = [r for r in rows if r["category"] == category]
    rows = rows[:limit]
    return {"count": len(rows), "announcements": _enrich(rows)}


# --------------------------------------------------------------------------- #
# Alerts / settings
# --------------------------------------------------------------------------- #
class SettingsUpdate(BaseModel):
    volume_multiple: float | None = Field(None, gt=0, le=100)
    price_move_pct: float | None = Field(None, gt=0, le=100)
    categories: list[str] | None = None


@app.get("/api/settings")
def get_settings() -> dict:
    return {**alerts_engine.get_settings(), "available_categories": classify.CATEGORIES}


@app.put("/api/settings")
def put_settings(update: SettingsUpdate) -> dict:
    if update.categories is not None:
        bad = [c for c in update.categories if c not in classify.CATEGORIES]
        if bad:
            raise HTTPException(422, f"unknown categories: {bad}")
    saved = alerts_engine.update_settings(update.volume_multiple, update.price_move_pct, update.categories)
    return {"settings": saved, "alerts": alerts_engine.evaluate_alerts()}


@app.get("/api/alerts")
def get_alerts(hours: int = Query(168, ge=1, le=24 * 60)) -> dict:
    settings = alerts_engine.get_settings()
    found = alerts_engine.evaluate_alerts(hours=hours)
    return {"settings": settings, "count": len(found), "alerts": found}


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #
@app.get("/api/portfolio")
def portfolio() -> dict:
    universe = _universe()
    counts: dict[str, dict] = {}
    with closing(alerts_engine._connect()) as conn:
        for row in conn.execute(
                """SELECT a.ticker, COUNT(*) AS total, SUM(i.tier = 'MUST_READ') AS must_read,
                          SUM(i.tier = 'WORTH_A_LOOK') AS worth_a_look
                   FROM announcements a LEFT JOIN announcement_insights i ON i.announcement_id = a.id GROUP BY a.ticker"""):
            counts[row["ticker"]] = {"total": row["total"], "must_read": row["must_read"] or 0,
                                     "worth_a_look": row["worth_a_look"] or 0}
        research._connect().close()                       # makes sure company_research exists
        researched = {r[0] for r in conn.execute("SELECT ticker FROM company_research")}
    metrics = {m.ticker: m for m in prices.get_all_metrics(list(universe))}
    companies = []
    for t, c in universe.items():
        m = metrics.get(t)
        companies.append({**c,
                          "price": m and {"date": m.date, "close": m.close, "change_1d_pct": m.change_1d_pct,
                                          "volume": m.volume, "volume_ratio": m.volume_ratio},
                          "filings": counts.get(t, {"total": 0, "must_read": 0, "worth_a_look": 0}),
                          "research_ready": t in researched})
    return {"count": len(companies), "companies": companies}


# --------------------------------------------------------------------------- #
# Research
# --------------------------------------------------------------------------- #
@app.get("/api/research/{ticker}")
def get_research(ticker: str, refresh: bool = False) -> dict:
    ticker = _clean_ticker(ticker)
    try:
        return research.generate_research(ticker, force=refresh)
    except research.ResearchError as exc:
        status = {"not_found": 404, "no_documents": 404}.get(exc.code, 502)
        raise HTTPException(status, str(exc))
    except llm.LLMQuotaError as exc:
        raise HTTPException(503, f"AI quota exhausted, try again later: {exc}")
    except llm.LLMError as exc:
        raise HTTPException(502, f"AI service error: {exc}")


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    ticker: str | None = None


CHAT_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}

CHAT_PROMPT = """You are FINN, a careful assistant for Indian retail investors. Answer the question using ONLY the numbered sources.

RULES:
- Be concise (under 180 words) and plain-English; explain jargon in a few words.
- Support every factual statement with an inline citation of source ids in square brackets, e.g. "[P2]" or "[F1][A3]". Use only ids that exist below.
- Never invent numbers, quotes, dates or ids. If the sources don't answer the question, say so plainly and say what is missing.
- You may give a balanced view of positives and risks, but do not tell the user to buy or sell.

QUESTION: {question}

SOURCES:
{sources}
"""


def _detect_tickers(message: str, explicit: str | None) -> list[str]:
    if explicit:
        return [_clean_ticker(explicit)]
    found: list[str] = []
    upper = message.upper()
    known = _universe()
    with closing(alerts_engine._connect()) as conn:
        known_all = set(known) | {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM announcements")}
    for t in sorted(known_all, key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(t)}(?![A-Z0-9])", upper):
            found.append(t)
    lowered = message.lower()
    for t, c in known.items():
        words = re.sub(r"\b(ltd|limited|india|industries|services|the)\b", "", c["name"].lower()).split()
        short = " ".join(words[:2]) if len(words) > 1 else (words[0] if words else "")
        if t not in found and short and len(short) > 3 and short in lowered:
            found.append(t)
    return found[:3]


def _chat_context(tickers: list[str], message: str) -> tuple[research.Sources, list[str], list[str]]:
    src = research.Sources()
    ids: list[str] = []
    notes: list[str] = []
    for n, t in enumerate(tickers, 1):
        chunks = fundamentals.search_chunks(t, message, None, top_k=5)
        if not chunks:
            notes.append(f"No annual report or concall text is loaded for {t}.")
        ids += [src.add_chunk(c) for c in chunks]
        res_text, res = research._results_block(t)
        if res_text:
            ids.append(src.add_fixed(f"F{n}", f"{t} {res['period_label']} Filing", f"{t}: {res_text}", "results", res.get("pdf_url")))
        if p_text := research._price_block(t):
            ids.append(src.add_fixed(f"M{n}", f"{t} market data", f"{t}: {p_text}", "price"))
        recent = classify.get_ranked(ticker=t, limit=6)
        ids += [src.add_filing(r) for r in recent]
        for m in mgmt_change.get_changes(t)[:3]:
            sid = f"G{n}_{len(ids)}"
            ids.append(src.add_fixed(sid, f"NSE Filing, {t} management change",
                                     f"{t}: {m['person_name']} ({m['role']}) - {m['change_type']}. Bio: {m['bio'] or m['filing_profile'] or 'n/a'}",
                                     "management"))
    return src, list(dict.fromkeys(ids)), notes


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    tickers = _detect_tickers(req.message, req.ticker)
    if not tickers:
        return {"reply": "I couldn't tell which company you mean. Mention a company or ticker (for example RELIANCE or TCS), "
                         "or pass `ticker` in the request.", "citations": [], "ticker": None, "tickers": []}
    sources, ids, notes = _chat_context(tickers, req.message)
    if not ids:
        return {"reply": f"I have no data loaded for {', '.join(tickers)}, so I can't answer from sources. "
                         "Open its research page first to load its documents.",
                "citations": [], "ticker": tickers[0], "tickers": tickers}
    complex_q = len(tickers) > 1 or bool(re.search(r"\b(compare|versus|vs\.?|better than|difference between)\b", req.message, re.I))
    prompt = CHAT_PROMPT.format(question=req.message, sources=sources.block(ids))
    try:
        try:
            out = (llm.call_strong if complex_q else llm.call_cheap)(prompt, CHAT_SCHEMA, feature="chat")
        except llm.LLMError:
            if not complex_q:
                raise
            log.warning("call_strong failed for chat; retrying with call_cheap")
            out = llm.call_cheap(prompt, CHAT_SCHEMA, feature="chat")
    except llm.LLMQuotaError as exc:
        raise HTTPException(503, f"AI quota exhausted, try again later: {exc}")
    except llm.LLMError as exc:
        raise HTTPException(502, f"AI service error: {exc}")

    used: list[str] = []

    def label(match: re.Match) -> str:
        parts = [p.strip() for p in match.group(1).split(",")]
        known = [p for p in parts if p in sources.items and p in ids]
        for p in known:
            if p not in used:
                used.append(p)
        return " ".join(f"[{sources.items[p]['label']}]" for p in known)

    reply = re.sub(r"\[([A-Z]\d+(?:_\d+)?(?:\s*,\s*[A-Z]\d+(?:_\d+)?)*)\]", label, out["reply"]).strip()
    reply = re.sub(r"\s{2,}", " ", reply)
    citations = [{"id": i, "label": sources.items[i]["label"], "type": sources.items[i]["kind"],
                  "url": sources.items[i].get("url"), "excerpt": sources.items[i]["text"][:240]} for i in used]
    return {"reply": reply, "citations": citations, "ticker": tickers[0], "tickers": tickers,
            "model": llm.last_model_used, "notes": notes}


# --------------------------------------------------------------------------- #
# Frontend (static). Mounted last so /api/* routes win. The same folder also deploys as a static site.
# --------------------------------------------------------------------------- #
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
