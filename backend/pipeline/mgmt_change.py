"""Enrich management-change announcements: who, what role, joining/leaving, when, and a short bio.

    python -m backend.pipeline.mgmt_change            # all management_change announcements
    python -m backend.pipeline.mgmt_change --ticker HDFCBANK
    python -m backend.pipeline.mgmt_change --retry-bios   # only re-attempt bios that are pending

Per announcement:
  1. Read the filing PDF (first pages) or, if there is none, the description.
  2. cheap LLM -> people[] {person_name, role, change_type, effective_date, is_kmp, filing_profile}.
     A filing can name several people, so there is one row per person.
  3. For people joining (and re-appointed KMPs): a free DuckDuckGo search for
     "<person_name>" "<company_name>"; the top 3 snippets go to the cheap LLM, which writes a
     2-3 sentence factual bio using only those snippets (or "No public background found"),
     and says which snippets it used, which are stored as sources. No paid Search grounding.
     If the search itself fails (network / DuckDuckGo rate limit), the bio stays `pending`
     and is retried next run, so a failure is never mistaken for "nothing exists".
Cached in `mgmt_enrichment`, keyed by (announcement_id, person_idx).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from ddgs import DDGS

from backend import llm
from backend.data.prices import CONFIG_PATH, DB_PATH
from backend.pipeline.pdf_utils import PDFError, is_scanned, load_filing

log = logging.getLogger("finn.mgmt")

MAX_PAGES = 4
CHAR_LIMIT = 14000
NO_BACKGROUND = "No public background found"
CHANGE_TYPES = ["joining", "resigning", "retiring", "reappointed", "other"]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string"},
                    "role": {"type": "string"},
                    "change_type": {"type": "string", "enum": CHANGE_TYPES},
                    "effective_date": {"type": ["string", "null"]},
                    "is_individual": {"type": "boolean"},
                    "is_kmp": {"type": "boolean"},
                    "filing_profile": {"type": ["string", "null"]},
                },
                "required": ["person_name", "role", "change_type", "effective_date", "is_individual",
                             "is_kmp", "filing_profile"],
            },
        },
        "notes": {"type": ["string", "null"]},
    },
    "required": ["people", "notes"],
}

EXTRACT_PROMPT = """You read an Indian listed company's stock-exchange filing about a change in directors / management.

List every person (or firm, e.g. statutory auditors) whose position changes. Use ONLY the text; never guess.
IMPORTANT: if the filing subject / description at the top names one specific person (e.g. "Re-appointment of
Sri X as Managing Director"), list ONLY that person, even if the attached document (e.g. a combined board
outcome) also mentions other people's changes. Only if the description is generic ("change in Management",
"Change in Director(s)") list everyone whose change the document describes.
- person_name: as written, without honorifics like Mr/Ms/Shri/Sri/Dr.
- role: e.g. "Managing Director & CEO", "CFO", "Independent Director", "Whole-time Director",
  "Non-Executive Director", "Company Secretary", "Statutory Auditor".
- change_type: joining (new appointment), resigning, retiring (incl. end of tenure / superannuation),
  reappointed (re-appointment or RBI approval of a re-appointment), other (e.g. change in designation).
- effective_date: YYYY-MM-DD if stated, else null.
- is_individual: false for firms (auditors, consultants).
- is_kmp: true for MD, CEO, Whole-time/Executive Director, CFO, Company Secretary, COO, Chairman.
- filing_profile: if the filing gives a brief profile of the person (education, past roles),
  summarise it factually in at most 2 sentences; otherwise null.
If no specific person's change is described, return an empty list and explain in notes.

Company: {company} (NSE: {ticker})
Filing subject: {subject}
Filing text:
{text}
"""

SEARCH_RESULTS = 3
BIO_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "bio": {"type": ["string", "null"]},
        "used_snippets": {"type": "array", "items": {"type": "integer"}},
        "used_filing_profile": {"type": "boolean"},
    },
    "required": ["found", "bio", "used_snippets", "used_filing_profile"],
}

BIO_PROMPT = """Write a short professional bio of {name}, who is {change_phrase} as {role} of {company}{when}.

Use ONLY the company's own filing profile and the web search snippets below. Rules:
- 2-3 factual sentences: previous companies and roles, and education, only if the snippets state them.
- The snippets must clearly be about THIS person (same name AND a link to {company} or the stated role).
  Ignore snippets about other people with similar names.
- Do not add anything from your own knowledge, no opinions, no preamble.
- If neither source gives reliable background beyond the appointment itself, set found=false and bio=null.
- used_snippets: the numbers of the snippets you relied on. used_filing_profile: true if you used the filing profile.

Profile disclosed in the company's own exchange filing (reliable): {filing_profile}

Snippets:
{snippets}
"""

CHANGE_PHRASE = {"joining": "being appointed", "reappointed": "being re-appointed"}


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mgmt_enrichment (
            announcement_id TEXT NOT NULL,
            person_idx      INTEGER NOT NULL,    -- -1 = processed, but no person found / error
            ticker          TEXT NOT NULL,
            person_name     TEXT,
            role            TEXT,
            change_type     TEXT,                -- joining | resigning | retiring | reappointed | other
            effective_date  TEXT,
            is_individual   INTEGER,
            is_kmp          INTEGER,
            filing_profile  TEXT,                -- profile disclosed in the filing itself
            bio             TEXT,                -- bio from web-search snippets, or "No public background found"
            bio_status      TEXT,                -- found | not_found | pending | not_needed
            bio_sources     TEXT,                -- JSON [{title, uri}] of the snippets the bio relies on
            source          TEXT,                -- pdf | description
            notes           TEXT,
            extracted_at    TEXT NOT NULL,
            PRIMARY KEY (announcement_id, person_idx)
        )
        """
    )
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def company_names() -> dict[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {c["ticker"]: c["name"] for key in ("portfolio", "research_demo") for c in cfg.get(key, [])}


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def filing_text(ann: sqlite3.Row) -> tuple[str, str]:
    """(text, source) for the LLM: PDF pages if readable, else the exchange description."""
    if ann["pdf_url"]:
        try:
            _, texts = load_filing(ann["pdf_url"], ann["ticker"], ann["announced_at"],
                                   r"appoint|director|management|resign|retire|kmp")
            if not is_scanned(texts):
                body = "\n\n".join(texts[:MAX_PAGES])[:CHAR_LIMIT]
                return f"{ann['description'] or ''}\n\n{body}", "pdf"
        except PDFError as exc:
            log.warning("%s: PDF unavailable (%s); using description", ann["ticker"], exc)
    return ann["description"] or ann["subject"], "description"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def only_named_people(people: list[dict], ann: sqlite3.Row) -> list[dict]:
    """If the exchange's own description names some of the extracted people, keep only those.

    Combined board-outcome PDFs list several people's changes, but each exchange filing
    is about one person (NCC filed 3 filings, one per director, all attaching the same document).
    Generic descriptions ("change in Management") name nobody, so everyone is kept.
    """
    haystack = _norm(f"{ann['subject']} {ann['description'] or ''}")
    named = [p for p in people if _norm(p["person_name"]) and f" {_norm(p['person_name'])} " in f" {haystack} "]
    return named or people


def needs_bio(person: dict) -> bool:
    return bool(person.get("is_individual")) and (
        person["change_type"] == "joining" or (person["change_type"] == "reappointed" and person.get("is_kmp"))
    )


BLOCKED_DOMAINS = ("linkedin.com",)      # we don't use LinkedIn content, not even search snippets


def web_search(name: str, company: str) -> list[dict]:
    """Top DuckDuckGo results for the person + company (exact-phrase query first, then a looser one).
    Returns [] if nothing was found; raises on genuine failures (network, rate limit)."""
    for query in (f'"{name}" "{company}"', f"{name} {company} director profile"):
        try:
            found = DDGS().text(query, max_results=SEARCH_RESULTS + 3) or []
        except Exception as exc:
            if "no results" not in str(exc).lower():
                raise
            found = []
        time.sleep(random.uniform(1.0, 2.0))      # be polite to the search endpoint
        results = [r for r in found if not any(d in (r.get("href") or "") for d in BLOCKED_DOMAINS)][:SEARCH_RESULTS]
        if results:
            return [{"title": r.get("title"), "uri": r.get("href"), "snippet": r.get("body")} for r in results]
    return []


def fetch_bio(person: dict, company: str, ticker: str) -> tuple[str | None, str, list[dict]]:
    """(bio, status, sources). status: found | not_found | pending."""
    try:
        results = web_search(person["person_name"], company)
    except Exception as exc:                  # network error / DuckDuckGo rate limit
        log.warning("Search failed for %s: %s", person["person_name"], exc)
        return None, "pending", []
    profile = (person.get("filing_profile") or "").strip()
    if not results and not profile:
        return NO_BACKGROUND, "not_found", []

    snippets = "\n".join(f"[{i + 1}] {r['title']}: {r['snippet']}" for i, r in enumerate(results))
    when = f", effective {person['effective_date']}" if person.get("effective_date") else ""
    prompt = BIO_PROMPT.format(
        name=person["person_name"], change_phrase=CHANGE_PHRASE.get(person["change_type"], "appointed"),
        role=person["role"], company=company, when=when, snippets=snippets or "(none found)",
        filing_profile=profile or "(none)",
    )
    out = None
    for attempt in (1, 2):      # a lightweight fallback model occasionally gives up on usable evidence
        try:
            out = llm.call_cheap(prompt, BIO_SCHEMA, feature="mgmt_bio", temperature=0.0 if attempt == 1 else 0.4)
        except llm.LLMError as exc:
            log.warning("Bio LLM failed for %s: %s", person["person_name"], exc)
            return None, "pending", []
        if out.get("found") and out.get("bio"):
            break
    used = [results[i - 1] for i in out.get("used_snippets", []) if 1 <= i <= len(results)]
    from_filing = bool(profile) and bool(out.get("used_filing_profile"))
    if not out.get("found") or not out.get("bio") or not (used or from_filing):
        return NO_BACKGROUND, "not_found", results       # keep what was searched, for audit
    sources = [{"title": r["title"], "uri": r["uri"]} for r in used]
    if from_filing:
        sources.insert(0, {"title": "Company's exchange filing (disclosed profile)", "uri": None})
    return out["bio"].strip(), "found", sources


def process(conn: sqlite3.Connection, ann: sqlite3.Row, names: dict[str, str]) -> int:
    company = names.get(ann["ticker"], ann["ticker"])
    text, source = filing_text(ann)
    try:
        result = llm.call_cheap(
            EXTRACT_PROMPT.format(company=company, ticker=ann["ticker"], subject=ann["subject"], text=text),
            EXTRACT_SCHEMA, feature="mgmt_extract",
        )
    except llm.LLMError as exc:
        log.error("%s: extraction failed: %s", ann["ticker"], exc)
        return 0       # nothing stored, so the next run retries

    people = only_named_people(result.get("people") or [], ann)
    # All network/LLM work first: the LLM usage logger writes to the same SQLite file,
    # so we must not hold a write transaction open while calling it.
    bios = [fetch_bio(p, company, ann["ticker"]) if needs_bio(p) else (None, "not_needed", []) for p in people]

    conn.execute("DELETE FROM mgmt_enrichment WHERE announcement_id = ?", (ann["id"],))
    if not people:
        conn.execute(
            "INSERT INTO mgmt_enrichment (announcement_id, person_idx, ticker, source, notes, extracted_at) "
            "VALUES (?, -1, ?, ?, ?, ?)",
            (ann["id"], ann["ticker"], source, result.get("notes") or "No person-level change found", _now()),
        )
    for idx, (p, (bio, status, sources)) in enumerate(zip(people, bios)):
        conn.execute(
            """
            INSERT INTO mgmt_enrichment (announcement_id, person_idx, ticker, person_name, role, change_type,
                effective_date, is_individual, is_kmp, filing_profile, bio, bio_status, bio_sources, source,
                notes, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ann["id"], idx, ann["ticker"], p["person_name"], p["role"], p["change_type"], p.get("effective_date"),
             int(bool(p.get("is_individual"))), int(bool(p.get("is_kmp"))), p.get("filing_profile"),
             bio, status, json.dumps(sources) if sources else None, source, result.get("notes"), _now()),
        )
    conn.commit()
    return len(people)


def retry_pending_bios(conn: sqlite3.Connection, names: dict[str, str]) -> int:
    rows = conn.execute(
        "SELECT * FROM mgmt_enrichment WHERE bio_status IN ('pending', 'not_found') AND person_idx >= 0"
    ).fetchall()
    done = 0
    for r in rows:
        bio, status, sources = fetch_bio(dict(r), names.get(r["ticker"], r["ticker"]), r["ticker"])
        if status != "pending":
            conn.execute(
                "UPDATE mgmt_enrichment SET bio = ?, bio_status = ?, bio_sources = ? "
                "WHERE announcement_id = ? AND person_idx = ?",
                (bio, status, json.dumps(sources) if sources else None, r["announcement_id"], r["person_idx"]),
            )
            conn.commit()
            done += 1
    return done


def run(ticker: str | None = None, force: bool = False) -> dict:
    names = company_names()
    stats = {"announcements": 0, "people": 0}
    with closing(connect()) as conn:
        sql = """
            SELECT a.* FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
            WHERE i.category = 'management_change'
        """
        params: list = []
        if ticker:
            sql += " AND a.ticker = ?"
            params.append(ticker.upper())
        if not force:
            sql += " AND a.id NOT IN (SELECT announcement_id FROM mgmt_enrichment)"
        for ann in conn.execute(sql + " ORDER BY a.announced_at DESC", params).fetchall():
            stats["people"] += process(conn, ann, names)
            stats["announcements"] += 1
        pending = conn.execute("SELECT COUNT(*) FROM mgmt_enrichment WHERE bio_status = 'pending'").fetchone()[0]
        if pending:
            stats["bios_still_pending"] = pending     # run with --retry-bios to fill these
    return stats


def get_changes(ticker: str) -> list[dict]:
    """Management changes for a ticker, newest first (for the API)."""
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT m.*, a.announced_at, a.pdf_url FROM mgmt_enrichment m JOIN announcements a ON a.id = m.announcement_id
            WHERE m.ticker = ? AND m.person_idx >= 0 ORDER BY a.announced_at DESC, m.person_idx
            """,
            (ticker.upper(),),
        ).fetchall()
    return [dict(r) | {"bio_sources": json.loads(r["bio_sources"]) if r["bio_sources"] else []} for r in rows]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_table() -> None:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT m.*, substr(a.announced_at, 1, 10) AS day FROM mgmt_enrichment m
            JOIN announcements a ON a.id = m.announcement_id ORDER BY m.ticker, a.announced_at DESC, m.person_idx
            """
        ).fetchall()
    print(f"\n{'TICKER':<11}{'FILED':<11}{'PERSON':<26}{'ROLE':<34}{'CHANGE':<12}{'EFFECTIVE':<11}{'KMP':<4}BIO")
    for r in rows:
        if r["person_idx"] < 0:
            print(f"{r['ticker']:<11}{r['day']:<11}-- {r['notes']}")
            continue
        print(f"{r['ticker']:<11}{r['day']:<11}{(r['person_name'] or '')[:25]:<26}{(r['role'] or '')[:33]:<34}"
              f"{r['change_type']:<12}{r['effective_date'] or '-':<11}{'Y' if r['is_kmp'] else 'N':<4}{r['bio_status']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Extract people + bios from management-change filings.")
    parser.add_argument("--ticker")
    parser.add_argument("--force", action="store_true", help="Re-extract even if cached.")
    parser.add_argument("--retry-bios", action="store_true", help="Only retry pending bios.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.retry_bios:
        with closing(connect()) as conn:
            print(f"bios filled: {retry_pending_bios(conn, company_names())}")
    else:
        print(run(args.ticker, args.force))
    print_table()


if __name__ == "__main__":
    main()
