"""Screener.in fundamentals: latest annual report + last 4 concall transcripts, chunked for retrieval.

    ingest_ticker("TCS")                      -> structured status dict (never raises for expected failures)
    search_chunks("TCS", "deal wins", top_k=6) -> BM25-ranked chunks (no dependencies, no LLM)

Works for any NSE ticker: the Screener slug comes from companies.json (`screener_slug`) when
present, otherwise the upper-cased ticker. PDFs are cached in backend/cache/fundamentals/<TICKER>/.

    python -m backend.data.fundamentals              # the research_demo companies
    python -m backend.data.fundamentals TCS INFY     # any tickers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import requests
from bs4 import BeautifulSoup

from backend.data.prices import BACKEND_DIR, CONFIG_PATH, DB_PATH, normalize_ticker

log = logging.getLogger("finn.fundamentals")

CACHE_DIR = BACKEND_DIR / "cache" / "fundamentals"
SCREENER = "https://www.screener.in"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
N_CONCALLS = 4
CHUNK_CHARS = 3000          # ~800 tokens
CHUNK_OVERLAP = 400
MIN_PAGE_CHARS = 60         # skip blank / image-only pages
PAGE_DELAY = (1.5, 3.0)     # polite pause between requests to Screener
PDF_DELAY = (0.5, 1.2)
PDF_TIMEOUT = 180
MAX_ATTEMPTS = 3

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fundamental_chunks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT NOT NULL,
            doc_type   TEXT NOT NULL,      -- annual_report | concall
            doc_label  TEXT NOT NULL,      -- e.g. "AR FY26", "Concall Q2 FY27"
            page_num   INTEGER NOT NULL,   -- 1-based
            chunk_idx  INTEGER NOT NULL,   -- order within the page
            source_url TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            UNIQUE (source_url, page_num, chunk_idx)
        );
        CREATE INDEX IF NOT EXISTS idx_fchunks_ticker ON fundamental_chunks (ticker, doc_type);
        CREATE TABLE IF NOT EXISTS fundamental_docs (
            source_url  TEXT PRIMARY KEY,
            ticker      TEXT NOT NULL,
            doc_type    TEXT NOT NULL,
            doc_label   TEXT NOT NULL,
            local_path  TEXT,
            pages       INTEGER,
            chunks      INTEGER,
            status      TEXT NOT NULL,     -- ok | scanned | error
            error       TEXT,
            ingested_at TEXT NOT NULL
        );
        """
    )
    return conn


# --------------------------------------------------------------------------- #
# Screener page
# --------------------------------------------------------------------------- #
def screener_slug(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for group in cfg.values():
            for company in group:
                if company.get("ticker", "").upper() == ticker and company.get("screener_slug"):
                    return company["screener_slug"]
    except (OSError, ValueError, AttributeError):
        pass
    return ticker


def _get(url: str, **kwargs) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
                                      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", **kwargs.pop("headers", {})},
                        timeout=kwargs.pop("timeout", 30), **kwargs)


def _concall_label(month_text: str) -> str:
    """'Oct 2026' -> 'Concall Q2 FY27'. Indian FY runs Apr-Mar; a call discusses the quarter just ended."""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{4})", month_text.strip())
    if not m or m.group(1).lower() not in _MONTHS:
        return f"Concall {month_text.strip()}"
    month, year = _MONTHS[m.group(1).lower()], int(m.group(2))
    quarter, fy_end = {1: (3, year), 2: (3, year), 3: (3, year), 4: (4, year), 5: (4, year), 6: (4, year),
                       7: (1, year + 1), 8: (1, year + 1), 9: (1, year + 1),
                       10: (2, year + 1), 11: (2, year + 1), 12: (2, year + 1)}[month]
    return f"Concall Q{quarter} FY{fy_end % 100:02d}"


def parse_documents(html: str) -> dict:
    """Return {'annual_report': [..], 'concalls': [..]} of {'label', 'url'} from a Screener company page."""
    soup = BeautifulSoup(html, "lxml")
    docs = soup.find(id="documents")
    out = {"annual_report": [], "concalls": [], "has_documents": docs is not None}
    if docs is None:
        return out
    for a in docs.select("div.annual-reports a[href]"):
        text = a.get_text(" ", strip=True)
        year = re.search(r"(\d{4})", text)
        if a["href"].lower().startswith("http") and year and "annual report" in text.lower():
            out["annual_report"].append({"label": f"AR FY{int(year.group(1)) % 100:02d}", "url": a["href"],
                                         "year": int(year.group(1))})
    out["annual_report"].sort(key=lambda d: -d["year"])
    for li in docs.select("div.concalls li"):
        when = li.find("div")
        link = li.find("a", string=re.compile(r"^\s*Transcript\s*$", re.I))
        if when and link and link.get("href", "").startswith("http"):
            out["concalls"].append({"label": _concall_label(when.get_text()), "url": link["href"],
                                    "month": when.get_text(strip=True)})
    return out                      # concalls are already newest-first on the page


def fetch_documents(slug: str) -> tuple[dict | None, str, str | None]:
    """(documents, page_url, error). Tries /consolidated/ first, then the standalone page."""
    last_error = None
    best: tuple[dict, str] | None = None
    for suffix in ("/consolidated/", "/"):
        url = f"{SCREENER}/company/{slug}{suffix}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = _get(url)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                time.sleep(2 * attempt)
                continue
            if resp.status_code == 404:
                last_error = "not_found"
                break
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(5 * attempt)
                continue
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                break
            docs = parse_documents(resp.text)
            if docs["annual_report"] or docs["concalls"]:
                return docs, url, None
            best = best or (docs, url)
            last_error = "no_documents"
            break
        time.sleep(random.uniform(*PAGE_DELAY))
        if last_error == "not_found" and suffix == "/consolidated/":
            continue                                    # maybe only a standalone page exists
    if best and last_error == "no_documents":
        return best[0], best[1], None
    return None, f"{SCREENER}/company/{slug}/", last_error


# --------------------------------------------------------------------------- #
# PDFs and chunks
# --------------------------------------------------------------------------- #
def _download(url: str, dest: Path) -> None:
    referer = "https://www.bseindia.com/" if "bseindia" in url else SCREENER + "/"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8", "Referer": referer}
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with requests.get(url, headers=headers, timeout=PDF_TIMEOUT, stream=True) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                tmp = dest.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for block in resp.iter_content(1 << 16):
                        fh.write(block)
                expected = int(resp.headers.get("content-length") or 0)
                if expected and tmp.stat().st_size < expected:
                    raise RuntimeError("truncated download")
            with open(tmp, "rb") as fh:
                if fh.read(5) != b"%PDF-":
                    raise RuntimeError("not a PDF")
            tmp.replace(dest)
            time.sleep(random.uniform(*PDF_DELAY))
            return
        except (requests.RequestException, RuntimeError, OSError) as exc:
            last = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"download failed: {last}")


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into ~size-char chunks with overlap, preferring to break at whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + size // 2, end)
            end = cut if cut != -1 else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _ingest_pdf(conn: sqlite3.Connection, ticker: str, doc_type: str, label: str, url: str) -> dict:
    result = {"doc_type": doc_type, "label": label, "url": url}
    done = conn.execute("SELECT * FROM fundamental_docs WHERE source_url = ? AND status = 'ok'", (url,)).fetchone()
    if done:
        return {**result, "status": "cached", "pages": done["pages"], "chunks": done["chunks"],
                "path": done["local_path"]}
    folder = CACHE_DIR / ticker
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    path = folder / f"{safe}_{hashlib.sha1(url.encode()).hexdigest()[:8]}.pdf"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        if not path.exists():
            _download(url, path)
        rows, pages = [], 0
        with pymupdf.open(path) as doc:
            pages = len(doc)
            for number, page in enumerate(doc, 1):
                text = page.get_text("text", sort=True)
                if len(text.strip()) < MIN_PAGE_CHARS:
                    continue
                for idx, piece in enumerate(chunk_text(text)):
                    rows.append((ticker, doc_type, label, number, idx, url, piece))
        if not rows:
            conn.execute("INSERT OR REPLACE INTO fundamental_docs VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (url, ticker, doc_type, label, str(path), pages, 0, "scanned",
                          "no extractable text (scanned PDF?)", now))
            conn.commit()
            return {**result, "status": "scanned", "pages": pages, "chunks": 0, "path": str(path),
                    "error": "no extractable text (scanned PDF?)"}
        conn.execute("DELETE FROM fundamental_chunks WHERE source_url = ?", (url,))
        conn.executemany("INSERT INTO fundamental_chunks (ticker, doc_type, doc_label, page_num, chunk_idx, "
                         "source_url, chunk_text) VALUES (?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO fundamental_docs VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (url, ticker, doc_type, label, str(path), pages, len(rows), "ok", None, now))
        conn.commit()
        return {**result, "status": "ok", "pages": pages, "chunks": len(rows), "path": str(path)}
    except Exception as exc:                                    # one bad PDF must not stop the others
        log.warning("%s %s failed: %s", ticker, label, exc)
        conn.execute("INSERT OR REPLACE INTO fundamental_docs VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (url, ticker, doc_type, label, None, None, 0, "error", str(exc), now))
        conn.commit()
        return {**result, "status": "error", "pages": 0, "chunks": 0, "error": str(exc)}


def ingest_ticker(ticker: str) -> dict:
    """Fetch, cache and chunk the latest annual report and last 4 concall transcripts.

    Returns {"ticker", "status", "error", "documents": [...]}. status is one of:
      ok | partial (some PDFs failed or are missing) | not_found | no_documents | error
    """
    ticker = normalize_ticker(ticker)
    slug = screener_slug(ticker)
    out = {"ticker": ticker, "slug": slug, "status": "error", "error": None, "documents": [], "page_url": None}
    docs, page_url, error = fetch_documents(slug)
    out["page_url"] = page_url
    if docs is None:
        out["status"] = "not_found" if error == "not_found" else "error"
        out["error"] = ("Ticker not found on Screener.in" if error == "not_found"
                        else f"Could not load Screener page: {error}")
        return out
    if not docs["annual_report"] and not docs["concalls"]:
        out["status"] = "no_documents"
        out["error"] = "Screener lists no annual report or concall transcript PDFs for this company"
        return out

    # Screener sometimes lists several transcripts for one quarter (re-uploads, a stale prior-year file).
    # Keep one per quarter label, newest listing first; the other links are fallbacks if a download fails.
    by_label: dict[str, list[str]] = {}
    for d in docs["concalls"]:
        by_label.setdefault(d["label"], []).append(d["url"])
    concalls = list(by_label.items())[:N_CONCALLS]
    with closing(_connect()) as conn:
        for d in docs["annual_report"][:1]:
            out["documents"].append(_ingest_pdf(conn, ticker, "annual_report", d["label"], d["url"]))
        for label, urls in concalls:
            for url in urls:
                result = _ingest_pdf(conn, ticker, "concall", label, url)
                if result["status"] in ("ok", "cached"):
                    break
            out["documents"].append(result)
    missing = []
    if not docs["annual_report"]:
        missing.append("annual report")
    if len(concalls) < N_CONCALLS:
        missing.append(f"only {len(concalls)} of {N_CONCALLS} concall transcripts with a PDF on Screener")
    failed = [d for d in out["documents"] if d["status"] in ("error", "scanned")]
    out["status"] = "ok" if not missing and not failed else "partial"
    notes = [f"missing: {', '.join(missing)}"] if missing else []
    notes += [f"{d['label']}: {d['error']}" for d in failed]
    out["error"] = "; ".join(notes) or None
    return out


# --------------------------------------------------------------------------- #
# Retrieval: BM25
# --------------------------------------------------------------------------- #
_STOP = set("a an and are as at be by for from has have in is it its of on or that the this to was were will with".split())
_TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def search_chunks(ticker: str, query: str, doc_type: str | None = None, top_k: int = 6,
                  k1: float = 1.5, b: float = 0.75) -> list[dict]:
    """BM25 keyword search over one ticker's chunks. Returns dicts with metadata, text and `score`."""
    ticker = normalize_ticker(ticker)
    sql = "SELECT * FROM fundamental_chunks WHERE ticker = ?"
    params: list = [ticker]
    if doc_type:
        sql += " AND doc_type = ?"
        params.append(doc_type)
    with closing(_connect()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    q_terms = list(dict.fromkeys(_tokens(query)))
    if not rows or not q_terms:
        return []
    docs = [Counter(_tokens(r["chunk_text"])) for r in rows]
    lengths = [sum(d.values()) for d in docs]
    avg_len = sum(lengths) / len(lengths) or 1.0
    n = len(rows)
    df = {t: sum(1 for d in docs if t in d) for t in q_terms}
    scored = []
    for row, tf, length in zip(rows, docs, lengths):
        score = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg_len))
        if score > 0:
            scored.append({**row, "score": round(score, 4)})
    scored.sort(key=lambda r: -r["score"])
    return scored[:top_k]


def list_documents(ticker: str) -> list[dict]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT doc_type, doc_label, pages, chunks, status, source_url, local_path FROM fundamental_docs "
            "WHERE ticker = ? ORDER BY doc_type, doc_label DESC", (normalize_ticker(ticker),))]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ingest Screener.in annual reports and concall transcripts")
    parser.add_argument("tickers", nargs="*", help="NSE tickers (default: research_demo companies)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    tickers = args.tickers
    if not tickers:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        tickers = [c["ticker"] for c in cfg.get("research_demo", [])]
    for ticker in tickers:
        result = ingest_ticker(ticker)
        print(f"\n{result['ticker']}  [{result['status']}]  {result['page_url']}")
        if result["error"]:
            print(f"  note: {result['error']}")
        for d in result["documents"]:
            size = f"{Path(d['path']).stat().st_size / 1e6:5.1f} MB" if d.get("path") and Path(d["path"]).exists() else "      -"
            print(f"  {d['doc_type']:<14}{d['label']:<18}{d['status']:<8}{size}  {d['pages']:>4} pages  "
                  f"{d['chunks']:>5} chunks  {d.get('error') or ''}")
        time.sleep(random.uniform(*PAGE_DELAY))


if __name__ == "__main__":
    main()
