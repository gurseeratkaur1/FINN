"""Daily price/volume ingestion and per-ticker metrics.

Usage (run from the repo root):
    python -m backend.data.prices                 # all tickers in companies.json
    python -m backend.data.prices --ticker INFY   # any single NSE ticker

Other modules should use `get_metrics()` / `get_all_metrics()` to read metrics.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BACKEND_DIR / "config" / "companies.json"
DB_PATH = BACKEND_DIR / "data" / "finn.db"

LOOKBACK_DAYS = 90          # calendar days of history to download
AVG_VOLUME_WINDOW = 20      # trading sessions in the volume baseline
MAX_RETRIES = 3
BACKOFF_SECONDS = 2.0       # doubles on each retry
PAUSE_BETWEEN_TICKERS = 0.5 # be polite to Yahoo

# Thresholds used to highlight rows in the CLI summary.
VOLUME_SPIKE_RATIO = 1.5
BIG_MOVE_PCT = 3.0

log = logging.getLogger("finn.prices")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # we report failures ourselves


# --------------------------------------------------------------------------- #
# Tickers
# --------------------------------------------------------------------------- #
def normalize_ticker(raw: str) -> str:
    """'infy', 'INFY.NS' -> 'INFY'."""
    t = raw.strip().upper()
    return t[:-3] if t.endswith(".NS") else t


def yahoo_symbol(ticker: str) -> str:
    return f"{normalize_ticker(ticker)}.NS"


def load_config_tickers(path: Path = CONFIG_PATH) -> list[str]:
    """Unique tickers from the `portfolio` and `research_demo` lists, in order."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    seen: dict[str, None] = {}
    for key in ("portfolio", "research_demo"):
        for company in cfg.get(key, []):
            seen[normalize_ticker(company["ticker"])] = None
    return list(seen)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,   -- ISO yyyy-mm-dd
            close  REAL NOT NULL,
            volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    return conn


def upsert_prices(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> int:
    rows = [
        (
            ticker,
            idx.strftime("%Y-%m-%d"),
            float(row["Close"]),
            None if pd.isna(row["Volume"]) else int(row["Volume"]),
        )
        for idx, row in df.iterrows()
    ]
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, close, volume) VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            close = excluded.close,
            volume = excluded.volume
        """,
        rows,
    )
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def fetch_history(ticker: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Daily Close/Volume for the last `days` calendar days, with retries.

    Returns an empty DataFrame if no data is available after all retries
    (e.g. an invalid or delisted symbol).
    """
    symbol = yahoo_symbol(ticker)
    start = date.today() - timedelta(days=days)
    delay = BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=True)
            if df is not None and not df.empty:
                df = df[["Close", "Volume"]].dropna(subset=["Close"])
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df
            log.debug("%s: empty response (attempt %d)", symbol, attempt)
        except Exception as exc:  # network errors, YFRateLimitError, etc.
            log.debug("%s: %s (attempt %d)", symbol, exc, attempt)
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    return pd.DataFrame(columns=["Close", "Volume"])


def ingest(tickers: list[str], days: int = LOOKBACK_DAYS) -> dict[str, int]:
    """Download and upsert each ticker. Returns {ticker: rows_written} (0 = failed)."""
    results: dict[str, int] = {}
    with closing(connect()) as conn:
        for i, ticker in enumerate(tickers):
            if i:
                time.sleep(PAUSE_BETWEEN_TICKERS)
            df = fetch_history(ticker, days)
            results[ticker] = upsert_prices(conn, ticker, df) if not df.empty else 0
    return results


# --------------------------------------------------------------------------- #
# Metrics (public helpers for the alerts engine and API)
# --------------------------------------------------------------------------- #
@dataclass
class PriceMetrics:
    ticker: str
    date: str                       # date of the latest session
    close: float
    change_1d_pct: float | None     # vs previous session close
    volume: int | None              # latest session volume
    avg_volume_20d: float | None    # mean of the previous N sessions, excluding latest
    volume_ratio: float | None      # volume / avg_volume_20d
    baseline_days: int              # sessions actually used in the average (<= 20)

    def to_dict(self) -> dict:
        return asdict(self)


def get_metrics(ticker: str, conn: sqlite3.Connection | None = None) -> PriceMetrics | None:
    """Metrics for one ticker from the database, or None if it has no data."""
    ticker = normalize_ticker(ticker)
    own_conn = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            "SELECT date, close, volume FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT ?",
            (ticker, AVG_VOLUME_WINDOW + 1),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()
    if not rows:
        return None

    latest, history = rows[0], rows[1:]
    change = None
    if history and history[0]["close"]:
        change = (latest["close"] / history[0]["close"] - 1) * 100

    baseline = [r["volume"] for r in history if r["volume"] is not None]
    avg_vol = sum(baseline) / len(baseline) if baseline else None
    ratio = latest["volume"] / avg_vol if avg_vol and latest["volume"] is not None else None

    return PriceMetrics(
        ticker=ticker,
        date=latest["date"],
        close=latest["close"],
        change_1d_pct=change,
        volume=latest["volume"],
        avg_volume_20d=avg_vol,
        volume_ratio=ratio,
        baseline_days=len(baseline),
    )


def get_all_metrics(tickers: list[str] | None = None) -> list[PriceMetrics]:
    """Metrics for the given tickers (default: every ticker in the database)."""
    with closing(connect()) as conn:
        if tickers is None:
            tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM prices ORDER BY ticker")]
        metrics = [get_metrics(t, conn) for t in tickers]
    return [m for m in metrics if m is not None]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def print_summary(tickers: list[str], written: dict[str, int]) -> None:
    header = f"{'TICKER':<14}{'DATE':<12}{'CLOSE':>11}{'1D %':>8}{'VOLUME':>13}{'AVG VOL 20D':>14}{'VOL x':>7}  FLAGS"
    print(header)
    print("-" * len(header))
    flagged = 0
    with closing(connect()) as conn:
        metrics = {t: get_metrics(t, conn) if written.get(t) else None for t in tickers}
    latest_date = max((m.date for m in metrics.values() if m), default=None)
    for t, m in metrics.items():
        if m is None:
            print(f"{t:<14}{'NO DATA':<12}")
            continue
        flags = []
        if m.date != latest_date:
            flags.append(f"STALE (no bar for {latest_date})")
        if m.volume_ratio is not None and m.volume_ratio > VOLUME_SPIKE_RATIO:
            flags.append("VOLUME SPIKE")
        if m.change_1d_pct is not None and abs(m.change_1d_pct) > BIG_MOVE_PCT:
            flags.append("BIG MOVE")
        flagged += any(not f.startswith("STALE") for f in flags)
        marker = ">> " + ", ".join(flags) if flags else ""
        print(
            f"{m.ticker:<14}{m.date:<12}{_fmt(m.close, ',.2f'):>11}"
            f"{_fmt(m.change_1d_pct, '+.2f'):>8}{_fmt(m.volume, ',d'):>13}"
            f"{_fmt(m.avg_volume_20d, ',.0f'):>14}{_fmt(m.volume_ratio, '.2f'):>7}  {marker}"
        )
    failed = [t for t in tickers if not written.get(t)]
    print("-" * len(header))
    print(
        f"{len(tickers) - len(failed)}/{len(tickers)} tickers loaded, {flagged} flagged "
        f"(volume > {VOLUME_SPIKE_RATIO}x 20d avg or |1d move| > {BIG_MOVE_PCT}%)."
    )
    if failed:
        print("No data for: " + ", ".join(failed))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download NSE daily prices into SQLite.")
    parser.add_argument("--ticker", help="Single NSE ticker (e.g. INFY). Default: all tickers in companies.json.")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="Calendar days of history (default 90).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    tickers = [normalize_ticker(args.ticker)] if args.ticker else load_config_tickers()
    print(f"Fetching {len(tickers)} ticker(s), last {args.days} days -> {DB_PATH}\n")
    written = ingest(tickers, args.days)
    print_summary(tickers, written)


if __name__ == "__main__":
    main()
