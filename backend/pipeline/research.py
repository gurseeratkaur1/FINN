"""Deep research per company, including the "Promised vs. Delivered" analysis.

    python -m backend.pipeline.research RELIANCE TCS        # generate + cache
    python -m backend.pipeline.research --force ITC

generate_research(ticker) works for any NSE ticker:
  1. make sure Screener fundamentals are ingested (annual report + concall transcripts);
  2. retrieve grounding passages with BM25 (`search_chunks`), plus the latest extracted quarterly
     results, recent MUST_READ filings and the price snapshot;
  3. call_cheap writes overview / financial_health / growth_catalysts / key_risks;
     call_strong writes promised_vs_delivered;
  4. Python verifies every citation and quote before anything is stored in `company_research`.

Grounding: every source the model may use gets an id (P1.. passages, F1 results, A1.. filings, M1 price).
The model cites ids only. Citation text ("AR FY26, p. 42") is built here from the stored chunk
metadata, so a page number can't be invented, and a quote is kept only if it appears in the passage it cites.
Page numbers are PDF page indices (page 1 = first page of the file), which can differ from printed numbers.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from backend import llm
from backend.data import fundamentals, prices
from backend.pipeline import classify
from backend.pipeline.results_extract import get_latest_results

log = logging.getLogger("finn.research")

STATUSES = ["On Track", "Beat", "Delayed / Missed", "Partially Disclosed"]
PASSAGE_CHARS = 2400


class ResearchError(RuntimeError):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code            # not_found | no_documents | error


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = prices.connect()
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_research (
            ticker        TEXT PRIMARY KEY,
            research_json TEXT NOT NULL,
            models        TEXT,
            generated_at  TEXT NOT NULL
        )
        """
    )
    return conn


def get_cached(ticker: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM company_research WHERE ticker = ?", (prices.normalize_ticker(ticker),)).fetchone()
    return json.loads(row["research_json"]) if row else None


# --------------------------------------------------------------------------- #
# Retrieval -> numbered sources
# --------------------------------------------------------------------------- #
class Sources:
    """Everything the model may cite, keyed by short ids."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._chunk_ids: dict[int, str] = {}
        self._n = {"P": 0, "A": 0}

    def add_chunk(self, chunk: dict) -> str:
        if chunk["id"] in self._chunk_ids:
            return self._chunk_ids[chunk["id"]]
        self._n["P"] += 1
        sid = f"P{self._n['P']}"
        self._chunk_ids[chunk["id"]] = sid
        self.items[sid] = {"kind": "chunk", "label": f"{chunk['doc_label']}, p. {chunk['page_num']}",
                           "doc_label": chunk["doc_label"], "doc_type": chunk["doc_type"], "page": chunk["page_num"],
                           "url": chunk["source_url"], "text": chunk["chunk_text"][:PASSAGE_CHARS]}
        return sid

    def add_fixed(self, sid: str, label: str, text: str, kind: str, url: str | None = None) -> str:
        self.items[sid] = {"kind": kind, "label": label, "text": text, "url": url}
        return sid

    def add_filing(self, row: dict) -> str:
        self._n["A"] += 1
        sid = f"A{self._n['A']}"
        when = datetime.fromisoformat(row["announced_at"])
        self.items[sid] = {"kind": "filing", "label": f"NSE Filing, {when.day} {when.strftime('%b')}",
                           "url": row.get("pdf_url"),
                           "text": f"{row['subject']}. {row.get('summary') or row.get('description') or ''}".strip()}
        return sid

    def render(self, ids: list[str]) -> list[str]:
        seen, out = set(), []
        for i in ids:
            if i in self.items and self.items[i]["label"] not in seen:
                seen.add(self.items[i]["label"])
                out.append(self.items[i]["label"])
        return out

    def block(self, ids: list[str]) -> str:
        return "\n\n".join(f"[{i}] ({self.items[i]['label']})\n{self.items[i]['text']}" for i in ids)


def _quarter_key(label: str) -> tuple[int, int]:
    m = re.search(r"Q(\d) FY(\d+)", label)
    return (int(m.group(2)), int(m.group(1))) if m else (0, 0)


def _retrieve(ticker: str, queries: list[str], sources: Sources, doc_type: str, labels: set[str] | None = None,
              per_query: int = 3, limit: int = 6) -> list[str]:
    """Top passages per query, interleaved so each query gets a share of `limit`."""
    per: list[list[str]] = []
    for q in queries:
        hits = fundamentals.search_chunks(ticker, q, doc_type, top_k=per_query * 6 if labels else per_query)
        if labels:
            hits = [h for h in hits if h["doc_label"] in labels][:per_query]
        per.append([sources.add_chunk(h) for h in hits])
    ids: list[str] = []
    for rank in range(per_query):
        for hits in per:
            if rank < len(hits) and hits[rank] not in ids:
                ids.append(hits[rank])
    return ids[:limit]


def _results_block(ticker: str) -> tuple[str | None, dict | None]:
    res = get_latest_results(ticker)
    if not res:
        return None, None
    m, v = res["metrics"], res["metrics"]["values"]
    unit = res.get("currency_unit") or "units as filed"

    def row(name: str) -> str:
        cur, prev, yoy = (v[p].get(name) for p in ("current_qtr", "prev_qtr", "yoy_qtr"))
        pct = lambda x: "n/a" if x is None else f"{x:+.1f}%"
        f = lambda x: "n/a" if x is None else f"{x:,.1f}"
        return (f"{name}: {f(cur)} (prev qtr {f(prev)}, year-ago {f(yoy)}; QoQ {pct(m.get(name + '_qoq_pct'))}, "
                f"YoY {pct(m.get(name + '_yoy_pct'))})")

    lines = [f"Quarter {res['period_label']} ({res['basis']}), amounts in {unit}"] + [row(n) for n in ("revenue", "ebitda", "pbt", "pat")]
    if m.get("ebitda_margin_pct") is not None:
        lines.append(f"EBITDA margin {m['ebitda_margin_pct']:.1f}%")
    if m.get("ebitda_derived"):
        lines.append("(EBITDA derived from PBT + finance costs + depreciation, not reported)")
    return "\n".join(lines), res


def _price_block(ticker: str) -> str | None:
    m = prices.get_metrics(ticker)
    if not m:
        return None
    ch = "n/a" if m.change_1d_pct is None else f"{m.change_1d_pct:+.2f}%"
    vr = "n/a" if m.volume_ratio is None else f"{m.volume_ratio:.2f}x"
    return f"Close {m.close:,.2f} on {m.date}; 1-day change {ch}; volume {vr} the 20-day average."


def _recent_filings(ticker: str, n: int = 5) -> list[dict]:
    return classify.get_ranked(ticker=ticker, tiers=("MUST_READ",), limit=n)


# --------------------------------------------------------------------------- #
# Prompts and schemas
# --------------------------------------------------------------------------- #
_BULLET = {"type": "object", "properties": {"text": {"type": "string"},
                                            "cites": {"type": "array", "items": {"type": "string"}}},
           "required": ["text", "cites"]}
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "array", "items": _BULLET},
        "financial_health": {"type": "array", "items": _BULLET},
        "growth_catalysts": {"type": "array", "items": _BULLET},
        "key_risks": {"type": "array", "items": _BULLET},
    },
    "required": ["overview", "financial_health", "growth_catalysts", "key_risks"],
}

PVD_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "commitment_id": {"type": "string"},
            "actual_delivery": {"type": "string"},
            "delivery_ids": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": STATUSES},
            "analyst_note": {"type": "string"},
        },
        "required": ["commitment_id", "actual_delivery", "delivery_ids", "status", "analyst_note"],
    }}},
    "required": ["items"],
}

COMMIT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "commitment": {"type": "string"},
            "source_id": {"type": "string"},
            "source_quote": {"type": "string"},
            "search_terms": {"type": "string"},
        },
        "required": ["commitment", "source_id", "source_quote", "search_terms"],
    }}},
    "required": ["items"],
}

COMMIT_PROMPT = """You are an equity research analyst reading management statements for {name} ({ticker}).

{grounding}

TASK: Pick 4-6 specific, MEASURABLE commitments or guidance statements made by management, from these sources only
(the annual report and earlier concalls). Good examples: capex or investment amounts, margin / growth / ARPU targets,
capacity or store/outlet/branch rollouts with numbers or dates, debt-reduction goals, launch or commissioning timelines.
Skip vague aspirations, boilerplate, statements where management declines to disclose something, and ESG / CSR / sustainability
policy or governance-process statements. Prefer business and financial commitments (revenue, margin, capex, capacity, market share, segment targets).
- source_id: the single source id containing it. source_quote: a short VERBATIM quote (under 40 words) copied exactly from it.
- search_terms: 6-10 distinctive keywords someone would look for in later transcripts to find the outcome (product names, metrics, projects, numbers).
- Do not repeat a commitment.

SOURCES:
{sources}
"""

GROUNDING = """GROUNDING RULES (strict):
- Use ONLY the numbered sources below. Do not use outside knowledge, and never invent numbers, quotes or ids.
- Cite by source id only (for example "P3", "F1"). Do not write page numbers yourself.
- Plain English for a retail investor; say what a number means. Use the currency units shown.
- If the sources don't support a point, leave it out rather than guess."""

PROFILE_PROMPT = """You are an equity research analyst writing a company profile for {name} ({ticker}).

{grounding}

Write four sections as lists of items {{text, cites}} where cites lists the source ids supporting that item:
- overview: 3-4 bullets on the business model, revenue drivers and the current market snapshot (cite M1 for price facts).
- financial_health: 3-4 bullets on the revenue / EBITDA / profit trajectory using the latest quarterly results (F1) and annual-report or concall commentary. If F1 is missing, say results numbers are unavailable.
- growth_catalysts: 3-4 concrete catalysts that management itself mentions.
- key_risks: 3-4 concrete risks or headwinds from the risk factors or concalls.
Each item must cite at least one id.

SOURCES:
{sources}
"""

PVD_PROMPT = """You are an equity research analyst checking whether management delivered what it promised, for {name} ({ticker}).

{grounding}

Below are commitments management made (C1, C2, ...) and the LATER evidence retrieved for each: {deliver_scope}.
For every commitment decide what was delivered, using only its evidence sources (plus F1 and A-ids where relevant).
- actual_delivery: 1-2 plain sentences with numbers; delivery_ids: the source ids that support it.
- status: "Beat" (clearly ahead), "On Track", "Delayed / Missed", or "Partially Disclosed" (the evidence doesn't give
  enough to judge; say exactly what is missing; delivery_ids may be empty). Do not claim "Delayed / Missed" just because
  evidence is absent: absence of evidence is "Partially Disclosed".
- analyst_note: one sentence on why it matters to an investor.
- Return one item per commitment, with commitment_id equal to its C-number.
{extra}

COMMITMENTS:
{commitments}

EVIDENCE:
{sources}
"""


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("’", "'"))


def _cleanup_bullets(items: list[dict], sources: Sources, allowed: set[str]) -> list[dict]:
    out = []
    for item in items:
        ids = [i for i in item.get("cites", []) if i in sources.items and i in allowed]
        if not ids or not item.get("text", "").strip():
            continue                                    # no valid citation -> dropped
        labels = sources.render(ids)
        out.append({"text": f"{item['text'].strip()} " + " ".join(f"[{l}]" for l in labels),
                    "citations": labels, "source_ids": ids})
    return out


def _verify_pvd(items: list[dict], sources: Sources, allowed: set[str]) -> tuple[list[dict], list[str]]:
    out, dropped = [], []
    for it in items:
        sid = it.get("source_id")
        if sid not in sources.items or sid not in allowed or sources.items[sid]["kind"] != "chunk":
            dropped.append(f"{it.get('commitment', '?')[:50]}: commitment source '{sid}' is not a retrieved passage")
            continue
        quote = (it.get("source_quote") or "").strip().strip('"“”')
        verified = bool(quote) and _squash(quote) in _squash(sources.items[sid]["text"])
        delivery = [i for i in it.get("delivery_ids", []) if i in sources.items and i in allowed]
        status = it["status"] if it["status"] in STATUSES else "Partially Disclosed"
        if not delivery and status != "Partially Disclosed":
            status = "Partially Disclosed"              # no verifiable evidence of delivery
        out.append({
            "commitment": it["commitment"].strip(),
            "source_quote": quote if verified else None,
            "source_quote_verified": verified,
            "source_citation": sources.items[sid]["label"],
            "actual_delivery": it["actual_delivery"].strip(),
            "delivery_citation": ", ".join(sources.render(delivery)) or None,
            "status": status,
            "analyst_note": it["analyst_note"].strip(),
        })
    return out, dropped


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _company_name(ticker: str) -> str:
    import json as _json
    try:
        cfg = _json.loads(prices.CONFIG_PATH.read_text(encoding="utf-8"))
        for group in cfg.values():
            for c in group:
                if c["ticker"] == ticker:
                    return c["name"]
    except (OSError, ValueError):
        pass
    return ticker


def _has_chunks(ticker: str) -> bool:
    with closing(fundamentals._connect()) as conn:
        return conn.execute("SELECT 1 FROM fundamental_chunks WHERE ticker = ? LIMIT 1", (ticker,)).fetchone() is not None


def generate_research(ticker: str, force: bool = False) -> dict:
    ticker = prices.normalize_ticker(ticker)
    if not force and (cached := get_cached(ticker)):
        return cached

    ingest = None
    if not _has_chunks(ticker):
        ingest = fundamentals.ingest_ticker(ticker)
        if not _has_chunks(ticker):
            raise ResearchError(ingest["error"] or "No documents could be loaded",
                                {"not_found": "not_found", "no_documents": "no_documents"}.get(ingest["status"], "error"))

    name = _company_name(ticker)
    docs = [d for d in fundamentals.list_documents(ticker) if d["status"] == "ok"]
    concalls = sorted({d["doc_label"] for d in docs if d["doc_type"] == "concall"}, key=_quarter_key)
    ar_labels = sorted({d["doc_label"] for d in docs if d["doc_type"] == "annual_report"})
    recent_calls, older_calls = set(concalls[-2:]), set(concalls[:-2])

    sources = Sources()
    f_text, results = _results_block(ticker)
    if f_text:
        sources.add_fixed("F1", f"{results['period_label']} Filing", f_text, "results", results.get("pdf_url"))
    if p_text := _price_block(ticker):
        sources.add_fixed("M1", "Market data", p_text, "price")
    filings = [sources.add_filing(r) for r in _recent_filings(ticker)]

    # ---- profile (call_cheap) ---------------------------------------------------- #
    p_ids = (
        _retrieve(ticker, ["business segments revenue drivers products customers"], sources, "annual_report", per_query=3)
        + _retrieve(ticker, ["risk factors risks headwinds regulatory competition"], sources, "annual_report", per_query=3)
        + _retrieve(ticker, ["outlook growth opportunity demand pipeline expansion", "margin revenue growth quarter performance",
                             "risks challenges headwinds uncertainty"], sources, "concall",
                    labels=recent_calls or None, per_query=2, limit=5)
    )
    profile_ids = list(dict.fromkeys(p_ids + [i for i in ("F1", "M1") if i in sources.items] + filings))
    profile = llm.call_cheap(
        PROFILE_PROMPT.format(name=name, ticker=ticker, grounding=GROUNDING, sources=sources.block(profile_ids)),
        PROFILE_SCHEMA, feature="research_profile")
    allowed_profile = set(profile_ids)

    # ---- promised vs delivered (call_strong, two stages) --------------------------- #
    # Stage 1 finds measurable commitments; stage 2 searches the later concalls with each
    # commitment's own keywords, so the judge sees evidence for that specific claim.
    commit_queries = ["outlook way forward next year", "capital expenditure planned capacity expansion investment",
                      "target aim ambition by FY27 FY28 2030", "we expect guidance margin growth", "strategy priorities roadmap rollout"]
    commit_ids = _retrieve(ticker, commit_queries, sources, "annual_report", per_query=3, limit=9)
    if older_calls:
        commit_ids += _retrieve(ticker, commit_queries, sources, "concall", labels=older_calls, per_query=3, limit=6)
    commits_raw = llm.call_strong(
        COMMIT_PROMPT.format(name=name, ticker=ticker, grounding=GROUNDING, sources=sources.block(commit_ids)),
        COMMIT_SCHEMA, feature="research_pvd_commitments").get("items", [])
    commits = [c for c in commits_raw if c.get("source_id") in commit_ids][:6]

    evidence_ids: list[str] = []
    if recent_calls:
        for c in commits:
            c["evidence"] = _retrieve(ticker, [c["search_terms"], c["commitment"]], sources, "concall",
                                      labels=recent_calls, per_query=2, limit=3)
            evidence_ids += c["evidence"]
    evidence_ids = list(dict.fromkeys(evidence_ids + [i for i in ("F1",) if i in sources.items] + filings))

    if concalls:
        deliver_scope = (f"the latest concalls ({', '.join(sorted(recent_calls, key=_quarter_key))}), the latest quarterly "
                         "results (F1) and recent filings (A-ids)")
        extra = ""
    else:
        deliver_scope = "the latest quarterly results (F1) and recent filings (A-ids); no concall transcripts were available"
        extra = ("- No concall transcripts exist for this company, so compare only against F1 and the filings, "
                 "and use \"Partially Disclosed\" where those don't show the outcome.")
    pvd, dropped = [], []
    if commits:
        listing = chr(10).join(
            f"C{n}: {c['commitment']} (made in {sources.items[c['source_id']]['label']}; likely evidence: "
            f"{', '.join(c.get('evidence', [])) or 'none found'})" for n, c in enumerate(commits, 1))
        judged = llm.call_strong(
            PVD_PROMPT.format(name=name, ticker=ticker, grounding=GROUNDING, deliver_scope=deliver_scope, extra=extra,
                              commitments=listing, sources=sources.block(evidence_ids)),
            PVD_SCHEMA, feature="research_pvd").get("items", [])
        by_id = {f"C{n}": c for n, c in enumerate(commits, 1)}
        merged = [{**by_id[j["commitment_id"]], **j} for j in judged if j.get("commitment_id") in by_id]
        pvd, dropped = _verify_pvd(merged, sources, set(commit_ids) | set(evidence_ids))

    notes = []
    if not concalls:
        notes.append("Concall transcripts were unavailable on Screener.in, so delivery was checked against the "
                     "latest quarterly results and filings only.")
    if not results:
        notes.append("No extracted quarterly results are stored for this company; financial commentary relies on "
                     "the annual report and concalls.")
    if ingest and ingest["status"] == "partial" and ingest.get("error") and concalls:
        notes.append(f"Some documents were missing: {ingest['error']}")
    out = {
        "ticker": ticker, "name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overview": _cleanup_bullets(profile["overview"], sources, allowed_profile),
        "financial_health": _cleanup_bullets(profile["financial_health"], sources, allowed_profile),
        "growth_catalysts": _cleanup_bullets(profile["growth_catalysts"], sources, allowed_profile),
        "key_risks": _cleanup_bullets(profile["key_risks"], sources, allowed_profile),
        "promised_vs_delivered": pvd,
        "sources": [{"label": d["doc_label"], "type": d["doc_type"], "pages": d["pages"], "url": d["source_url"]} for d in docs],
        "notes": notes,
        "quality": {"pvd_dropped": dropped, "pvd_quotes_unverified": sum(1 for p in pvd if not p["source_quote_verified"]),
                    "pvd_model": llm.last_model_used},
    }
    with closing(_connect()) as conn:
        conn.execute("INSERT OR REPLACE INTO company_research (ticker, research_json, models, generated_at) VALUES (?,?,?,?)",
                     (ticker, json.dumps(out), llm.last_model_used, out["generated_at"]))
        conn.commit()
    return out


def print_pvd(research: dict) -> None:
    print(f"\n=== {research['ticker']} - Promised vs. Delivered ({len(research['promised_vs_delivered'])} items) ===")
    for n in research["notes"]:
        print(f"  note: {n}")
    for i, p in enumerate(research["promised_vs_delivered"], 1):
        quote = f"\"{p['source_quote']}\"" if p["source_quote"] else "(quote not verifiable)"
        print(f"\n{i}. [{p['status']}] {p['commitment']}\n   Promised  {quote}  [{p['source_citation']}]"
              f"\n   Delivered {p['actual_delivery']}  [{p['delivery_citation'] or 'no evidence cited'}]"
              f"\n   Note      {p['analyst_note']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate and cache company research")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--show", nargs="*", default=[], help="tickers whose promised-vs-delivered table to print")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for t in args.tickers:
        try:
            r = generate_research(t, force=args.force)
            q = r["quality"]
            print(f"{r['ticker']}: ok  overview={len(r['overview'])} fin={len(r['financial_health'])} "
                  f"cat={len(r['growth_catalysts'])} risk={len(r['key_risks'])} pvd={len(r['promised_vs_delivered'])} "
                  f"(unverified quotes {q['pvd_quotes_unverified']}, dropped {len(q['pvd_dropped'])}, model {q['pvd_model']})")
        except (ResearchError, llm.LLMError) as exc:
            print(f"{t}: FAILED - {exc}")
            continue
        if t.upper() in [s.upper() for s in args.show]:
            print_pvd(r)


if __name__ == "__main__":
    main()
