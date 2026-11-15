"""Alerts engine: user-tunable thresholds -> structured alerts for the portfolio.

    get_settings() / update_settings(...)     thresholds + watched categories (SQLite `user_settings`)
    evaluate_alerts(settings=None, hours=168) price / volume alerts and material-filing alerts

No LLM calls. Price metrics come from `backend.data.prices.get_metrics`; filings come from
`announcements` joined with `announcement_insights`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from backend.data import prices

DEFAULT_SETTINGS = {
    "volume_multiple": 2.0,
    "price_move_pct": 5.0,
    "categories": ["results", "M&A", "management_change", "litigation", "fund_raise", "credit_rating"],
}
STALE_AFTER_DAYS = 5      # price data older than this (calendar days) is ignored


def _connect() -> sqlite3.Connection:
    conn = prices.connect()
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            volume_multiple REAL NOT NULL,
            price_move_pct  REAL NOT NULL,
            categories_json TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_settings() -> dict:
    """Saved settings; the defaults are written on first use."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO user_settings (id, volume_multiple, price_move_pct, categories_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (DEFAULT_SETTINGS["volume_multiple"], DEFAULT_SETTINGS["price_move_pct"],
                 json.dumps(DEFAULT_SETTINGS["categories"]), _now()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM user_settings WHERE id = 1").fetchone()
    return {
        "volume_multiple": row["volume_multiple"],
        "price_move_pct": row["price_move_pct"],
        "categories": json.loads(row["categories_json"]),
        "updated_at": row["updated_at"],
    }


def update_settings(volume_multiple: float | None = None, price_move_pct: float | None = None,
                    categories: list[str] | None = None) -> dict:
    """Change any subset of the settings; returns the saved result."""
    current = get_settings()
    if volume_multiple is not None and volume_multiple <= 0:
        raise ValueError("volume_multiple must be positive")
    if price_move_pct is not None and price_move_pct <= 0:
        raise ValueError("price_move_pct must be positive")
    new = (
        current["volume_multiple"] if volume_multiple is None else float(volume_multiple),
        current["price_move_pct"] if price_move_pct is None else float(price_move_pct),
        json.dumps(current["categories"] if categories is None else list(categories)),
        _now(),
    )
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE user_settings SET volume_multiple = ?, price_move_pct = ?, categories_json = ?, updated_at = ? "
            "WHERE id = 1", new)
        conn.commit()
    return get_settings()


def _alert_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _is_stale(metric_date: str, today: date) -> bool:
    return (today - date.fromisoformat(metric_date)).days > STALE_AFTER_DAYS


def _price_alerts(settings: dict, tickers: list[str], today: date) -> list[dict]:
    alerts = []
    for m in prices.get_all_metrics(tickers):
        if _is_stale(m.date, today):
            continue
        if m.volume_ratio is not None and m.volume_ratio >= settings["volume_multiple"]:
            ratio = m.volume_ratio
            alerts.append({
                "id": _alert_id(m.ticker, "volume_spike", m.date),
                "ticker": m.ticker, "type": "volume_spike", "date": m.date,
                "severity": "high" if ratio >= 2 * settings["volume_multiple"] else "medium",
                "headline": f"{m.ticker}: volume {ratio:.1f}x its 20-day average",
                "why": (f"{m.ticker} traded {m.volume:,} shares on {m.date}, {ratio:.1f}x its recent daily average "
                        f"({m.avg_volume_20d:,.0f}). Your alert triggers at {settings['volume_multiple']:g}x. "
                        "Unusual volume often means large investors are buying or selling, or news is out."),
                "value": round(ratio, 2),
            })
        if m.change_1d_pct is not None and abs(m.change_1d_pct) >= settings["price_move_pct"]:
            move = m.change_1d_pct
            alerts.append({
                "id": _alert_id(m.ticker, "price_move", m.date),
                "ticker": m.ticker, "type": "price_move", "date": m.date,
                "severity": "high" if abs(move) >= 2 * settings["price_move_pct"] else "medium",
                "headline": f"{m.ticker}: {'up' if move > 0 else 'down'} {abs(move):.1f}% in one session",
                "why": (f"{m.ticker} closed at {m.close:,.2f} on {m.date}, {'up' if move > 0 else 'down'} "
                        f"{abs(move):.1f}% from the previous close. Your alert triggers at moves of "
                        f"{settings['price_move_pct']:g}% or more."),
                "value": round(move, 2),
            })
    return alerts


def _filing_alerts(settings: dict, tickers: list[str], hours: int, now: datetime) -> list[dict]:
    cutoff = (now - timedelta(hours=hours)).replace(tzinfo=None).isoformat(timespec="seconds")
    cats = settings["categories"]
    sql = f"""
        SELECT a.id, a.ticker, a.announced_at, a.subject, i.category, i.tier, i.summary, i.priority_score
        FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
        WHERE a.announced_at >= ? AND a.ticker IN ({','.join('?' * len(tickers))})
          AND (i.category IN ({','.join('?' * len(cats)) or "''"}) OR i.tier = 'MUST_READ')
        ORDER BY i.priority_score DESC, a.announced_at DESC
    """
    with closing(_connect()) as conn:
        rows = conn.execute(sql, [cutoff, *tickers, *cats]).fetchall()
    alerts = []
    for r in rows:
        must = r["tier"] == "MUST_READ"
        watched = r["category"] in cats
        reason = ("it is rated Must Read" if must else "") + (" and " if must and watched else "") + \
                 (f"its category ({r['category']}) is one you watch" if watched else "")
        why = f"{r['summary']} " if r["summary"] else ""
        alerts.append({
            "id": _alert_id(r["id"], "material_filing"),
            "ticker": r["ticker"], "type": "material_filing", "date": r["announced_at"],
            "severity": "high" if must else "medium",
            "headline": f"{r['ticker']}: {r['subject']}",
            "why": f"{why}Flagged because {reason}.",
            "category": r["category"], "tier": r["tier"], "announcement_id": r["id"],
        })
    return alerts


def evaluate_alerts(settings: dict | None = None, hours: int = 168, tickers: list[str] | None = None,
                    now: datetime | None = None) -> list[dict]:
    """Alerts for the portfolio, high severity first. `settings` overrides the saved ones (without saving)."""
    settings = {**get_settings(), **(settings or {})}
    tickers = tickers or prices.load_config_tickers()
    now = now or datetime.now(timezone.utc)
    alerts = _price_alerts(settings, tickers, now.date()) + _filing_alerts(settings, tickers, hours, now)
    alerts.sort(key=lambda a: a["date"], reverse=True)
    alerts.sort(key=lambda a: a["severity"] != "high")      # stable: high first, newest first within
    return alerts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate portfolio alerts")
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--volume-multiple", type=float)
    parser.add_argument("--price-move-pct", type=float)
    parser.add_argument("--limit", type=int, default=8, help="examples to print per type")
    args = parser.parse_args(argv)
    override = {k: v for k, v in (("volume_multiple", args.volume_multiple),
                                  ("price_move_pct", args.price_move_pct)) if v is not None}
    saved = get_settings()
    print(f"Saved settings: volume x{saved['volume_multiple']:g}, price {saved['price_move_pct']:g}%, "
          f"categories {saved['categories']}")
    if override:
        print(f"Evaluating with temporary overrides (not saved): {override}")
    alerts = evaluate_alerts(override or None, hours=args.hours)
    by_type: dict[str, list[dict]] = {}
    for a in alerts:
        by_type.setdefault(a["type"], []).append(a)
    print(f"\n{len(alerts)} alerts in the last {args.hours}h: " +
          ", ".join(f"{t}={len(v)}" for t, v in sorted(by_type.items())))
    for t, items in sorted(by_type.items()):
        print(f"\n[{t}]")
        for a in items[:args.limit]:
            print(f"  {a['severity']:<6} {a['headline']}\n         {a['why']}")


if __name__ == "__main__":
    main()
