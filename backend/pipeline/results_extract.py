"""Extract headline P&L numbers from quarterly results filings.

    python -m backend.pipeline.results_extract                   # all `results` filings with a PDF
    python -m backend.pipeline.results_extract --ticker INFY
    python -m backend.pipeline.results_extract --limit 5 --force

For each filing:
  1. Download the PDF (cached in backend/cache/pdfs/; BSE copy if the NSE one is broken).
  2. Pick the pages holding the P&L table (consolidated preferred), max 8 pages.
  3. cheap LLM -> strict JSON of raw line items for current / previous / year-ago quarter.
  4. Python: verify every number actually appears in the PDF text (anything that
     doesn't is nulled), derive EBITDA if not stated, compute QoQ / YoY / margins.
Results are cached in `results_enrichment`, keyed by pdf_url.
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
from backend.data.prices import DB_PATH
from backend.pipeline.pdf_utils import PDFError, is_scanned, load_filing

log = logging.getLogger("finn.results")

MAX_PAGES = 8
PAGE_CHAR_LIMIT = 9000
PERIODS = ("current_qtr", "prev_qtr", "yoy_qtr")
LINE_ITEMS = ("revenue", "ebitda_reported", "finance_costs", "depreciation", "pbt", "pat")
METRICS = ("revenue", "ebitda", "pbt", "pat")

BEFORE_TAX = re.compile(r"before\s+(exceptional\s+items\s+(and|&)\s+)?tax", re.I)
INCOME_LINE = re.compile(r"revenue from operations|total income|interest earned|income from operations", re.I)
QUARTER_COLS = re.compile(r"quarter\s*ended|three\s*months\s*ended", re.I)
CASH_FLOW = re.compile(r"statement\s+of\s+cash\s*flows?", re.I)
CONSOLIDATED = re.compile(r"consolidated", re.I)
STANDALONE = re.compile(r"standalone|stand-alone", re.I)

# --------------------------------------------------------------------------- #
# Page selection
# --------------------------------------------------------------------------- #
def _is_consolidated(text: str) -> bool:
    head = text[:2500]
    return bool(CONSOLIDATED.search(head)) or (bool(CONSOLIDATED.search(text)) and not STANDALONE.search(text))


def select_pages(texts: list[str]) -> tuple[list[int], str]:
    """Indices of pages to send and the basis they were chosen for.

    Results tables are often far from the start (ICICI p.13, INFY p.18 of 195),
    so instead of "first N pages" we take the first P&L statement page (and the
    one after it, where statements spill over), preferring a consolidated one.
    A P&L page has a before-tax line, an income line and quarter columns, and
    isn't a cash-flow statement. Falls back to the first pages (press releases).
    """
    def is_pnl(t: str, strict: bool) -> bool:
        return bool(BEFORE_TAX.search(t) and INCOME_LINE.search(t) and not CASH_FLOW.search(t[:600])
                    and (QUARTER_COLS.search(t) or not strict))

    for strict in (True, False):
        pnl = [i for i, t in enumerate(texts) if is_pnl(t, strict)]
        if pnl:
            consolidated = [i for i in pnl if _is_consolidated(texts[i])]
            start = (consolidated or pnl)[0]
            pages = [start] + ([start + 1] if start + 1 < len(texts) else [])
            return pages[:MAX_PAGES], "consolidated" if consolidated else "standalone_or_unknown"
    return list(range(min(len(texts), 6))), "no_pnl_table_found"


# --------------------------------------------------------------------------- #
# LLM extraction
# --------------------------------------------------------------------------- #
_NUM = {"type": ["number", "null"]}
_PERIOD = {
    "type": "object",
    "properties": {item: _NUM for item in LINE_ITEMS} | {"period_end": {"type": ["string", "null"]}},
    "required": [*LINE_ITEMS, "period_end"],
}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "has_numbers": {"type": "boolean"},
        "is_financial_institution": {"type": "boolean"},
        "basis": {"type": ["string", "null"], "enum": ["consolidated", "standalone", None]},
        "currency_unit": {"type": ["string", "null"]},
        "period_label": {"type": ["string", "null"]},
        "current_qtr": _PERIOD,
        "prev_qtr": _PERIOD,
        "yoy_qtr": _PERIOD,
        "notes": {"type": ["string", "null"]},
    },
    "required": ["has_numbers", "is_financial_institution", "basis", "currency_unit", "period_label",
                 *PERIODS, "notes"],
}

PROMPT = """You extract numbers from an Indian listed company's quarterly results filing (text below, extracted from PDF).

Return JSON only. STRICT RULES:
- Use ONLY numbers written in the text. Copy them exactly as printed (drop thousands separators).
  Never calculate, estimate, convert units, or fill gaps. If a value is not printed, return null.
- Negative numbers are often printed in brackets, e.g. (12.5) -> -12.5.
- Columns in these tables are usually: current quarter, preceding quarter, same quarter of the
  previous year, then half-year / nine-month / full-year columns. Use ONLY the three quarter columns:
  current_qtr, prev_qtr (preceding quarter) and yoy_qtr (same quarter last year).
- If both consolidated and standalone statements are present, use consolidated. Set `basis`.
- Line items:
  revenue: "Revenue from operations". For banks/NBFCs/insurers (is_financial_institution=true) use
           "Total income" (or "Total revenue"); if only its parts are printed, return null.
  ebitda_reported: only if a line explicitly labelled EBITDA is printed in the table; else null.
  finance_costs: "Finance costs" (for banks: null; interest expended is not a finance cost here).
  depreciation: "Depreciation and amortisation expense".
  pbt: "Profit before tax" (after exceptional items, before tax).
  pat: "Net profit for the period" / "Profit after tax" for the whole entity (before other
       comprehensive income; for consolidated, the total before attributing to minority interest).
- period_end per column as YYYY-MM-DD if the column date is printed.
- currency_unit as printed, e.g. "INR Crores", "INR Lakhs", "INR Millions".
- period_label like "Q2 FY27" (Indian fiscal year: Apr-Mar; quarter ending 30 Sep 2026 = Q2 FY27).
- If the text has no quarterly financial table (e.g. a board-meeting notice, newspaper ad, or a press
  release without a table), set has_numbers=false, leave numbers null, and explain briefly in notes.
- notes: one short line on anything a reader should know (e.g. exceptional items, restated figures).

Company: {ticker}
Filing text:
{text}
"""


def extract_with_llm(ticker: str, text: str) -> dict:
    return llm.call_cheap(PROMPT.format(ticker=ticker, text=text), EXTRACT_SCHEMA, feature="results_extract")


# --------------------------------------------------------------------------- #
# Verification + derived metrics (pure Python)
# --------------------------------------------------------------------------- #
def numbers_in_text(text: str) -> set[float]:
    """Every number printed in the text, tolerant of PDF artefacts like '21 .62' / '48 ,03'."""
    t = re.sub(r"(?<=\d)\s+(?=[.,]\s*\d)", "", text)          # '21 .62' -> '21.62'
    t = re.sub(r"(?<=\d[.,])\s+(?=\d)", "", t)                 # '48, 03' -> '48,03'
    values = set()
    # OCR variants seen in real filings: '54269 95' (decimal point lost) and '533-402' (hyphen as separator).
    for m in re.finditer(r"(?<![\d.,])(\d{2,})\s(\d{2})(?![\d.,])", t):
        values.add(float(f"{m.group(1)}.{m.group(2)}"))
    for m in re.finditer(r"(?<![\d.,-])(\d{1,3}(?:-\d{3})+)(?:[.,](\d+))?(?![\d-])", t):
        values.add(float(m.group(1).replace("-", "") + (f".{m.group(2)}" if m.group(2) else "")))
    # OCR sometimes turns a thousands comma into a dot: '31.658' printed for 31,658.
    for m in re.finditer(r"(?<![\d.,])(\d{1,3})\.(\d{3})(?![\d.,])", t):
        values.add(float(m.group(1) + m.group(2)))
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", t):
        token = m.group(0)
        # A comma followed by exactly two digits at the end is an OCR'd decimal ('48,03' -> 48.03).
        if re.fullmatch(r"\d+,\d{2}", token):
            values.add(float(token.replace(",", ".")))
        try:
            values.add(float(token.replace(",", "")))
        except ValueError:
            pass
    return values


def verify(extracted: dict, text: str) -> list[str]:
    """Null out any number that isn't printed in the source text. Returns the removed fields."""
    printed = numbers_in_text(text)
    removed = []
    for period in PERIODS:
        for item in LINE_ITEMS:
            value = extracted.get(period, {}).get(item)
            if value is None:
                continue
            if not any(abs(abs(value) - p) < 0.011 for p in printed):
                extracted[period][item] = None
                removed.append(f"{period}.{item}={value}")
    return removed


def _pct(cur: float | None, base: float | None) -> float | None:
    # Growth on a zero or negative base isn't meaningful (e.g. loss -> profit).
    if cur is None or base is None or base <= 0:
        return None
    return round((cur / base - 1) * 100, 2)


def compute_metrics(extracted: dict) -> dict:
    """EBITDA (stated or derived), QoQ / YoY growth and EBITDA margin."""
    financial = extracted.get("is_financial_institution", False)
    values: dict[str, dict[str, float | None]] = {}
    derived = False
    for period in PERIODS:
        p = extracted.get(period) or {}
        ebitda = p.get("ebitda_reported")
        if ebitda is None and not financial and None not in (p.get("pbt"), p.get("finance_costs"), p.get("depreciation")):
            ebitda = round(p["pbt"] + p["finance_costs"] + p["depreciation"], 2)
            derived = True
        if financial:
            ebitda = None              # EBITDA isn't meaningful for lenders: interest is their main cost
        values[period] = {"revenue": p.get("revenue"), "ebitda": ebitda, "pbt": p.get("pbt"), "pat": p.get("pat")}

    metrics: dict = {"values": values, "ebitda_derived": derived}
    for m in METRICS:
        metrics[f"{m}_qoq_pct"] = _pct(values["current_qtr"][m], values["prev_qtr"][m])
        metrics[f"{m}_yoy_pct"] = _pct(values["current_qtr"][m], values["yoy_qtr"][m])
    for period in PERIODS:
        rev, ebitda = values[period]["revenue"], values[period]["ebitda"]
        metrics[f"ebitda_margin_pct_{period}"] = round(ebitda / rev * 100, 2) if rev and ebitda is not None else None
    metrics["ebitda_margin_pct"] = metrics["ebitda_margin_pct_current_qtr"]
    return metrics


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS results_enrichment (
            pdf_url          TEXT PRIMARY KEY,
            announcement_id  TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            source_pdf_url   TEXT,              -- differs from pdf_url when the BSE copy was used
            pages_used       TEXT,              -- 1-based page numbers sent to the LLM
            page_basis       TEXT,
            status           TEXT NOT NULL,     -- ok | no_numbers | scanned | error
            has_numbers      INTEGER,
            basis            TEXT,
            currency_unit    TEXT,
            period_label     TEXT,
            ebitda_derived   INTEGER,
            raw_llm_json     TEXT,              -- LLM output exactly as returned
            extracted_json   TEXT,              -- LLM output after verification (unprinted numbers nulled)
            metrics_json     TEXT,              -- computed values, growth and margins
            unverified       TEXT,              -- numbers the LLM returned that aren't in the PDF (nulled)
            notes            TEXT,
            error            TEXT,
            model            TEXT,
            extracted_at     TEXT NOT NULL
        )
        """
    )
    if "raw_llm_json" not in {r[1] for r in conn.execute("PRAGMA table_info(results_enrichment)")}:
        conn.execute("ALTER TABLE results_enrichment ADD COLUMN raw_llm_json TEXT")
    return conn


def _save(conn, row: dict) -> None:
    cols = ", ".join(row)
    conn.execute(
        f"INSERT OR REPLACE INTO results_enrichment ({cols}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def process(ann: sqlite3.Row) -> dict:
    base = {
        "pdf_url": ann["pdf_url"], "announcement_id": ann["id"], "ticker": ann["ticker"],
        "model": llm.CHEAP_MODEL, "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        used_url, texts = load_filing(ann["pdf_url"], ann["ticker"], ann["announced_at"], r"result")
    except PDFError as exc:
        return base | {"status": "error", "error": str(exc)}
    base["source_pdf_url"] = used_url
    if is_scanned(texts):
        return base | {"status": "scanned", "has_numbers": 0, "notes": "Scanned PDF with no text layer; OCR not supported."}

    pages, page_basis = select_pages(texts)
    text = "\n\n".join(f"--- page {i + 1} ---\n{texts[i][:PAGE_CHAR_LIMIT]}" for i in pages)
    base |= {"pages_used": ",".join(str(i + 1) for i in pages), "page_basis": page_basis}
    try:
        extracted = extract_with_llm(ann["ticker"], text)
    except llm.LLMError as exc:
        return base | {"status": "error", "error": str(exc)}

    base["model"] = llm.last_model_used     # may be a fallback model on the free tier
    raw = json.dumps(extracted)
    removed = verify(extracted, text)
    has_numbers = bool(extracted.get("has_numbers")) and any(
        extracted[p].get(i) is not None for p in PERIODS for i in ("revenue", "pbt", "pat")
    )
    metrics = compute_metrics(extracted) if has_numbers else None
    return base | {
        "status": "ok" if has_numbers else "no_numbers",
        "has_numbers": int(has_numbers),
        "basis": extracted.get("basis"),
        "currency_unit": extracted.get("currency_unit"),
        "period_label": extracted.get("period_label"),
        "ebitda_derived": int(metrics["ebitda_derived"]) if metrics else None,
        "raw_llm_json": raw,
        "extracted_json": json.dumps(extracted),
        "metrics_json": json.dumps(metrics) if metrics else None,
        "unverified": ", ".join(removed) or None,
        "notes": extracted.get("notes"),
    }


def run(ticker: str | None = None, limit: int | None = None, force: bool = False) -> list[dict]:
    with closing(connect()) as conn:
        sql = """
            SELECT a.* FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
            WHERE i.category = 'results' AND a.pdf_url IS NOT NULL
        """
        params: list = []
        if ticker:
            sql += " AND a.ticker = ?"
            params.append(ticker.upper())
        if not force:
            sql += " AND a.pdf_url NOT IN (SELECT pdf_url FROM results_enrichment WHERE status != 'error')"
        sql += " ORDER BY a.announced_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        todo = conn.execute(sql, params).fetchall()
        out = []
        for ann in todo:
            row = process(ann)
            _save(conn, row)
            out.append(row)
            print(f"  {ann['ticker']:<11} {row['status']:<11} pages={row.get('pages_used') or '-':<8} {row.get('error') or ''}")
        return out


def get_latest_results(ticker: str) -> dict | None:
    """Most recent successfully extracted results for a ticker (for the API)."""
    with closing(connect()) as conn:
        row = conn.execute(
            """
            SELECT r.*, a.announced_at FROM results_enrichment r JOIN announcements a ON a.id = r.announcement_id
            WHERE r.ticker = ? AND r.status = 'ok' ORDER BY a.announced_at DESC LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["metrics"] = json.loads(out.pop("metrics_json"))
    out["extracted"] = json.loads(out.pop("extracted_json"))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _f(v, spec=",.1f"):
    return "-" if v is None else format(v, spec)


def print_table(ticker: str | None = None) -> None:
    with closing(connect()) as conn:
        sql = "SELECT * FROM results_enrichment"
        params: list = []
        if ticker:
            sql += " WHERE ticker = ?"
            params.append(ticker.upper())
        rows = conn.execute(sql + " ORDER BY ticker", params).fetchall()
    print(f"\n{'TICKER':<11}{'STATUS':<11}{'PERIOD':<9}{'BASIS':<7}{'UNIT':<12}"
          f"{'REVENUE':>11}{'QoQ%':>7}{'YoY%':>7}{'EBITDA':>10}{'DER':>4}{'MARGIN':>7}{'PBT':>10}{'PAT':>10}{'QoQ%':>7}{'YoY%':>7}")
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['ticker']:<11}{r['status']:<11}{(r['notes'] or r['error'] or '')[:90]}")
            continue
        m = json.loads(r["metrics_json"])
        cur = m["values"]["current_qtr"]
        print(
            f"{r['ticker']:<11}{r['status']:<11}{(r['period_label'] or '-'):<9}{(r['basis'] or '-')[:5]:<7}"
            f"{(r['currency_unit'] or '-')[:11]:<12}{_f(cur['revenue']):>11}{_f(m['revenue_qoq_pct']):>7}{_f(m['revenue_yoy_pct']):>7}"
            f"{_f(cur['ebitda']):>10}{('Y' if m['ebitda_derived'] else 'N') if cur['ebitda'] is not None else '-':>4}"
            f"{_f(m['ebitda_margin_pct']):>7}{_f(cur['pbt']):>10}{_f(cur['pat']):>10}{_f(m['pat_qoq_pct']):>7}{_f(m['pat_yoy_pct']):>7}"
        )
        if r["unverified"]:
            print(f"{'':<11}! nulled (not in PDF): {r['unverified']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Extract P&L numbers from results filings.")
    parser.add_argument("--ticker")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true", help="Re-extract even if cached.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    run(args.ticker, args.limit, args.force)
    print_table(args.ticker)


if __name__ == "__main__":
    main()
