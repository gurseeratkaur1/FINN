"""Corporate announcements ingestion (NSE primary, BSE fallback).

Usage (run from the repo root):
    python -m backend.data.announcements                   # all tickers in companies.json
    python -m backend.data.announcements --ticker INFY     # any single NSE ticker
    python -m backend.data.announcements --offline         # replay cached raw JSON, no network
    python -m backend.data.announcements --source bse      # force BSE

Every raw API response is saved to backend/cache/announcements_raw/ so a run can
be replayed with --offline if the live APIs block (e.g. during a demo).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from backend.data.prices import DB_PATH, load_config_tickers, normalize_ticker

BACKEND_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BACKEND_DIR / "cache"
RAW_DIR = CACHE_DIR / "announcements_raw"
BSE_CODES_PATH = CACHE_DIR / "bse_codes.json"

LOOKBACK_DAYS = 30
REQUEST_TIMEOUT = 20
MAX_ATTEMPTS = 2            # per request; NSE is declared blocked after this many failures

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

NSE_HOME = "https://www.nseindia.com"
NSE_FILINGS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"

BSE_SEARCH_API = "https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w"
BSE_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_PDF_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_PDF_HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
BSE_MAX_PAGES = 10

log = logging.getLogger("finn.announcements")


class SourceBlocked(Exception):
    """The source refused us (401/403, HTML instead of JSON, repeated errors)."""


def polite_pause() -> None:
    time.sleep(random.uniform(0.5, 1.0))


# --------------------------------------------------------------------------- #
# Normalized record + database
# --------------------------------------------------------------------------- #
@dataclass
class Announcement:
    ticker: str
    announced_at: str           # ISO 8601, exchange local time (IST), e.g. 2026-11-12T11:19:58
    subject: str
    description: str | None
    raw_category: str | None
    pdf_url: str | None
    source: str                 # "NSE" or "BSE"

    @property
    def id(self) -> str:
        # pdf_url is part of the key because exchanges often publish several distinct
        # filings for one company in the same second under the same generic subject
        # (e.g. three director re-appointments). Without it they would collapse into one.
        key = f"{self.ticker}|{self.announced_at}|{self.subject}|{self.pdf_url or ''}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id           TEXT PRIMARY KEY,   -- sha1(ticker|announced_at|subject|pdf_url)
            ticker       TEXT NOT NULL,
            announced_at TEXT NOT NULL,
            subject      TEXT NOT NULL,
            description  TEXT,
            raw_category TEXT,
            pdf_url      TEXT,
            source       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_announcements_ticker_time
            ON announcements (ticker, announced_at DESC);
        """
    )
    return conn


def upsert_announcements(conn: sqlite3.Connection, items: list[Announcement]) -> int:
    conn.executemany(
        """
        INSERT INTO announcements (id, ticker, announced_at, subject, description, raw_category, pdf_url, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            description = excluded.description,
            raw_category = excluded.raw_category,
            pdf_url = excluded.pdf_url,
            source = excluded.source
        """,
        [(a.id, a.ticker, a.announced_at, a.subject, a.description, a.raw_category, a.pdf_url, a.source) for a in items],
    )
    conn.commit()
    return len({a.id for a in items})


# --------------------------------------------------------------------------- #
# Raw cache (for --offline replay)
# --------------------------------------------------------------------------- #
def raw_path(source: str, ticker: str) -> Path:
    return RAW_DIR / f"{source.lower()}_{ticker}.json"


def save_raw(source: str, ticker: str, payload, from_date: date, to_date: date) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = {
        "source": source,
        "ticker": ticker,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "payload": payload,
    }
    raw_path(source, ticker).write_text(json.dumps(wrapper, ensure_ascii=False, indent=1), encoding="utf-8")


def load_raw(ticker: str) -> tuple[str, list] | None:
    """Most recently fetched cached payload for a ticker, from either source."""
    candidates = [p for p in (raw_path("NSE", ticker), raw_path("BSE", ticker)) if p.exists()]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: json.loads(p.read_text(encoding="utf-8"))["fetched_at"])
    data = json.loads(newest.read_text(encoding="utf-8"))
    return data["source"], data["payload"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if text and text not in {"-", "null"} else None


def _get_json(session: requests.Session, url: str, **kwargs):
    """GET returning parsed JSON, raising SourceBlocked on 401/403 or non-JSON."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    if resp.status_code in (401, 403):
        raise SourceBlocked(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    if not resp.text.strip() or "json" not in resp.headers.get("content-type", ""):
        raise SourceBlocked(f"non-JSON response ({resp.headers.get('content-type')})")
    return resp.json()


# --------------------------------------------------------------------------- #
# NSE
# --------------------------------------------------------------------------- #
class NSEClient:
    source = "NSE"

    def __init__(self) -> None:
        self.session: requests.Session | None = None

    def _warm_up(self) -> None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )
        s.get(NSE_HOME, timeout=REQUEST_TIMEOUT)
        polite_pause()
        s.get(NSE_FILINGS_PAGE, timeout=REQUEST_TIMEOUT)
        polite_pause()
        self.session = s

    def fetch_raw(self, ticker: str, from_date: date, to_date: date) -> list:
        params = {
            "index": "equities",
            "symbol": ticker,
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": to_date.strftime("%d-%m-%Y"),
        }
        headers = {"Accept": "application/json, text/plain, */*", "Referer": NSE_FILINGS_PAGE}
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if self.session is None:
                    self._warm_up()
                data = _get_json(self.session, NSE_API, params=params, headers=headers)
                if not isinstance(data, list):
                    raise SourceBlocked(f"unexpected payload type {type(data).__name__}")
                return data
            except (SourceBlocked, requests.RequestException, ValueError) as exc:
                last_error = exc
                log.info("NSE %s attempt %d failed: %s", ticker, attempt, exc)
                self.session = None          # fresh cookies on the next attempt
                polite_pause()
        raise SourceBlocked(f"NSE failed after {MAX_ATTEMPTS} attempts: {last_error}")

    @staticmethod
    def parse(ticker: str, payload: list) -> list[Announcement]:
        items = []
        for row in payload:
            when = _clean(row.get("sort_date")) or _clean(row.get("an_dt"))
            subject = _clean(row.get("desc"))
            if not when or not subject:
                continue
            try:
                announced_at = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                announced_at = datetime.strptime(when, "%d-%b-%Y %H:%M:%S")
            items.append(
                Announcement(
                    ticker=ticker,
                    announced_at=announced_at.isoformat(timespec="seconds"),
                    subject=subject,
                    description=_clean(row.get("attchmntText")),
                    raw_category=subject,          # NSE's `desc` is both the subject and the category
                    pdf_url=_clean(row.get("attchmntFile")),
                    source="NSE",
                )
            )
        return items


# --------------------------------------------------------------------------- #
# BSE
# --------------------------------------------------------------------------- #
class BSEClient:
    source = "BSE"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
            }
        )
        self.codes: dict[str, str] = (
            json.loads(BSE_CODES_PATH.read_text(encoding="utf-8")) if BSE_CODES_PATH.exists() else {}
        )

    def _request(self, url: str, params: dict):
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return _get_json(self.session, url, params=params)
            except (SourceBlocked, requests.RequestException, ValueError) as exc:
                last_error = exc
                log.info("BSE attempt %d failed: %s", attempt, exc)
                time.sleep(2 * attempt)
        raise SourceBlocked(f"BSE failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def scrip_code(self, ticker: str) -> str:
        """NSE ticker -> BSE scrip code, cached in backend/cache/bse_codes.json."""
        if ticker in self.codes:
            return self.codes[ticker]
        html = self._request(BSE_SEARCH_API, {"Type": "SS", "text": ticker})
        polite_pause()
        # Each hit looks like: liclick('500209','INFOSYS LTD') ... <strong>INFY</strong> ISIN 500209
        hits = re.findall(r"liclick\('(\d+)','[^']*'\).*?<strong>([^<]+)</strong>", str(html), flags=re.S)
        code = next((c for c, sym in hits if normalize_ticker(sym) == ticker), None)
        if code is None:
            raise SourceBlocked(f"no BSE scrip code found for {ticker}")
        self.codes[ticker] = code
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        BSE_CODES_PATH.write_text(json.dumps(self.codes, indent=2, sort_keys=True), encoding="utf-8")
        return code

    def fetch_raw(self, ticker: str, from_date: date, to_date: date) -> list:
        code = self.scrip_code(ticker)
        rows: list = []
        for page in range(1, BSE_MAX_PAGES + 1):
            data = self._request(
                BSE_ANN_API,
                {
                    "pageno": page,
                    "strCat": -1,
                    "strPrevDate": from_date.strftime("%Y%m%d"),
                    "strScrip": code,
                    "strSearch": "P",
                    "strToDate": to_date.strftime("%Y%m%d"),
                    "strType": "C",
                    "subcategory": -1,
                },
            )
            batch = data.get("Table") or []
            rows.extend(batch)
            total = (data.get("Table1") or [{}])[0].get("ROWCNT") or 0
            polite_pause()
            if not batch or len(rows) >= total:
                break
        return rows

    @staticmethod
    def parse(ticker: str, payload: list) -> list[Announcement]:
        items = []
        for row in payload:
            when = _clean(row.get("NEWS_DT")) or _clean(row.get("DT_TM"))
            subject = _clean(row.get("NEWSSUB")) or _clean(row.get("HEADLINE"))
            if not when or not subject:
                continue
            announced_at = datetime.fromisoformat(when.split(".")[0])
            attachment = _clean(row.get("ATTACHMENTNAME"))
            pdf_url = None
            if attachment:
                base = BSE_PDF_HIST if str(row.get("PDFFLAG")) == "1" else BSE_PDF_LIVE
                pdf_url = base + attachment
            description = " ".join(filter(None, (_clean(row.get("HEADLINE")), _clean(row.get("MORE"))))) or None
            items.append(
                Announcement(
                    ticker=ticker,
                    announced_at=announced_at.isoformat(timespec="seconds"),
                    subject=subject,
                    description=description,
                    raw_category=_clean(row.get("CATEGORYNAME")),
                    pdf_url=pdf_url,
                    source="BSE",
                )
            )
        return items


PARSERS = {"NSE": NSEClient.parse, "BSE": BSEClient.parse}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class TickerResult:
    ticker: str
    source: str | None
    count: int
    error: str | None = None


def ingest(tickers: list[str], days: int = LOOKBACK_DAYS, source: str = "auto", offline: bool = False) -> list[TickerResult]:
    to_date = date.today()
    from_date = to_date - timedelta(days=days)
    cutoff = from_date.isoformat()
    nse = NSEClient() if source in ("auto", "nse") and not offline else None
    bse = BSEClient() if source in ("auto", "bse") and not offline else None
    nse_blocked = False
    results: list[TickerResult] = []

    with closing(connect()) as conn:
        for ticker in tickers:
            used, payload, errors = None, None, []

            if offline:
                cached = load_raw(ticker)
                if cached:
                    used, payload = cached
                else:
                    errors.append("no cached raw data")
            else:
                if nse and not nse_blocked:
                    try:
                        payload, used = nse.fetch_raw(ticker, from_date, to_date), "NSE"
                        save_raw("NSE", ticker, payload, from_date, to_date)
                    except SourceBlocked as exc:
                        errors.append(str(exc))
                        if source == "auto":
                            nse_blocked = True   # stop wasting time on NSE; BSE is primary from now on
                            log.warning("NSE looks blocked (%s); switching to BSE for remaining tickers.", exc)
                if payload is None and bse:
                    try:
                        payload, used = bse.fetch_raw(ticker, from_date, to_date), "BSE"
                        save_raw("BSE", ticker, payload, from_date, to_date)
                    except SourceBlocked as exc:
                        errors.append(str(exc))
                polite_pause()

            if payload is None:
                results.append(TickerResult(ticker, None, 0, "; ".join(errors)))
                continue
            items = [a for a in PARSERS[used](ticker, payload) if a.announced_at >= cutoff]
            results.append(TickerResult(ticker, used, upsert_announcements(conn, items)))
    return results


def get_announcements(ticker: str, days: int = LOOKBACK_DAYS, limit: int | None = None) -> list[dict]:
    """Recent announcements for a ticker, newest first (for the API / alerts engine)."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    sql = "SELECT * FROM announcements WHERE ticker = ? AND announced_at >= ? ORDER BY announced_at DESC"
    params: list = [normalize_ticker(ticker), cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_summary(results: list[TickerResult]) -> None:
    print(f"{'TICKER':<14}{'SOURCE':<8}{'COUNT':>6}  NOTE")
    print("-" * 50)
    for r in results:
        print(f"{r.ticker:<14}{r.source or '-':<8}{r.count:>6}  {r.error or ''}")
    print("-" * 50)
    ok = [r for r in results if r.source]
    by_source = {s: sum(1 for r in ok if r.source == s) for s in ("NSE", "BSE")}
    print(
        f"{sum(r.count for r in results)} announcements, {len(ok)}/{len(results)} tickers "
        f"(NSE: {by_source['NSE']}, BSE: {by_source['BSE']})."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch corporate announcements into SQLite.")
    parser.add_argument("--ticker", help="Single NSE ticker (e.g. INFY). Default: all tickers in companies.json.")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="Days of history (default 30).")
    parser.add_argument("--source", choices=("auto", "nse", "bse"), default="auto",
                        help="auto = NSE first, BSE fallback (default).")
    parser.add_argument("--offline", action="store_true", help="Replay cached raw JSON instead of calling APIs.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    tickers = [normalize_ticker(args.ticker)] if args.ticker else load_config_tickers()
    mode = "offline replay" if args.offline else f"source={args.source}"
    print(f"Announcements for {len(tickers)} ticker(s), last {args.days} days ({mode}) -> {DB_PATH}\n")
    print_summary(ingest(tickers, args.days, args.source, args.offline))


if __name__ == "__main__":
    main()
