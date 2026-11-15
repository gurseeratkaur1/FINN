"""Classify, score, tier and summarize corporate announcements.

    python -m backend.pipeline.classify             # classify new rows, rescore all
    python -m backend.pipeline.classify --no-llm    # rules only (free); leftovers stay pending
    python -m backend.pipeline.classify --force     # reclassify everything from scratch

Pipeline per announcement:
  A. Keyword rules on subject + description (free, deterministic).
  B. Rows the rules can't place go to the cheap LLM, 20 per call, strict JSON.
  C. priority_score (0-100) = category weight + materiality bump + market-reaction boost.
  D. tier from score; MUST_READ / WORTH_A_LOOK rows get a 1-line "why it matters"
     summary from the cheap LLM. ROUTINE rows get none (saves cost).

Results live in table `announcement_insights`, keyed by announcement id, so no
row is ever classified or summarized twice. Scores are recomputed every run
(free) because market-reaction data can arrive after the announcement.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone

from backend.data.prices import DB_PATH
from backend import llm

log = logging.getLogger("finn.classify")

CATEGORIES = [
    "results", "M&A", "fund_raise", "management_change", "litigation", "insider_trading",
    "earnings_call", "investor_meetings", "credit_rating", "dividend", "bonus_split",
    "capex", "press_release", "routine", "unclassified",
]

LLM_BATCH_SIZE = 20
SUMMARY_BATCH_SIZE = 20
MARKET_CLOSE_IST = dtime(15, 30)

# --------------------------------------------------------------------------- #
# Scoring configuration
# --------------------------------------------------------------------------- #
CATEGORY_WEIGHT = {
    "results": 75,
    "M&A": 65,          # +10 for an actual merger / demerger / open offer
    "fund_raise": 60,   # +15 for dilutive equity (QIP, preferential, rights); plain NCD/CP stays 60
    "management_change": 60,
    "litigation": 60,
    "bonus_split": 60,
    "capex": 50,
    "credit_rating": 45,
    "dividend": 45,
    "insider_trading": 40,
    "earnings_call": 40,
    "press_release": 30,
    "unclassified": 30,
    "investor_meetings": 15,
    "routine": 5,
}

# Extra points when the text signals a bigger-than-usual event within its category.
MATERIALITY_BUMPS = [
    ("management_change", r"\b(chief executive|ceo|managing director|chief financial|cfo|chairman|whole[- ]time director)\b", 10),
    ("credit_rating", r"\b(downgrad|upgrad|watch with negative|negative outlook)\w*", 15),
    ("litigation", r"\b(sebi|supreme court|high court|nclt|cbi|enforcement directorate|fraud|search|raid)\b", 10),
    ("fund_raise", r"\b(qip|qualified institutions? placement|preferential|rights issue)\b", 15),
    ("M&A", r"\b(amalgamation|merger|demerger|scheme of arrangement|open offer|takeover)\b", 10),
]

TIER_THRESHOLDS = [(70, "MUST_READ"), (40, "WORTH_A_LOOK"), (0, "ROUTINE")]
MOVE_PCT_STRONG, MOVE_PCT = 5.0, 3.0
VOL_RATIO_STRONG, VOL_RATIO = 2.5, 1.5
MAX_MARKET_BOOST = 25
# Follow-up / administrative filings don't move prices themselves, so they never get a boost.
# Among the rest, a session's move is attributed only to that ticker's most important filing(s)
# reacting to the same session (e.g. results, not the director change filed the same evening).
NO_MARKET_BOOST = {"routine", "investor_meetings", "earnings_call", "unclassified"}


# --------------------------------------------------------------------------- #
# Step A: rules
# --------------------------------------------------------------------------- #
def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


# Order matters: the first matching rule wins. Specific/noisy patterns go first
# (e.g. newspaper ads of results are `routine`, not `results`).
RULES: list[tuple[str, str, re.Pattern]] = [
    ("earnings_call", "call_transcript_or_recording",
     _rx(r"\btranscript", r"link of (the )?recording", r"audio recording", r"video recording", r"recording of .*call", r"earnings call", r"con\.? ?call recording")),
    ("routine", "newspaper_publication", _rx(r"newspaper", r"\bpublication\b")),
    ("routine", "trading_window", _rx(r"trading window")),
    ("routine", "compliance_certificate",
     _rx(r"compliance certificate", r"74\s?\(5\)", r"regulation 40\b", r"certificate under", r"\bsecretarial (audit|compliance)")),
    ("routine", "esop", _rx(r"\besop", r"\besos\b", r"\besps\b", r"employee stock", r"stock option")),
    ("routine", "shareholder_meeting",
     _rx(r"\bagm\b", r"\begm\b", r"annual general meeting", r"extra[- ]?ordinary general meeting", r"postal ballot",
         r"e-?voting", r"scrutini[sz]er", r"\bproceedings\b")),
    ("routine", "registry_admin",
     _rx(r"loss of share certificate", r"duplicate share", r"\biepf\b", r"unclaimed", r"shareholding pattern",
         r"investor complaints", r"\bkyc\b", r"registrar", r"change in (the )?registered office",
         r"statement of deviation", r"related party transactions?")),
    ("insider_trading", "insider_disclosure",
     _rx(r"insider trading", r"\bsast\b", r"substantial acquisition", r"regulation 7\s?\(2\)", r"regulation 29",
         r"regulation 31", r"\bpledge", r"encumbrance")),
    ("results", "financial_results",
     _rx(r"financial results?", r"\b(un)?audited (standalone|consolidated|financial)", r"results? for the (quarter|half|year)",
         r"quarterly results", r"\bq[1-4] ?(fy)?\d{0,4} results")),
    ("bonus_split", "bonus_or_split", _rx(r"\bbonus (issue|shares?)", r"sub-?division", r"stock split", r"\bsplit\b")),
    ("dividend", "dividend", _rx(r"\bdividend")),
    ("M&A", "corporate_action",
     _rx(r"\bacqui(re|red|ring|sition)", r"amalgamation", r"\bmerger", r"demerger", r"scheme of arrangement",
         r"joint venture", r"divest", r"disinvest", r"slump sale", r"open offer", r"takeover",
         r"incorporation of (a )?(wholly[- ]owned )?subsidiary", r"\bstake\b")),
    ("fund_raise", "fund_raise",
     _rx(r"\bqip\b", r"qualified institutions? placement", r"preferential (issue|allotment)", r"rights issue",
         r"non[- ]convertible debentures?", r"\bncds?\b", r"commercial paper", r"raising of funds", r"fund ?rais",
         r"private placement", r"\bbonds?\b", r"debt securities", r"term loan")),
    ("management_change", "management_change",
     _rx(r"change in (director|key managerial|kmp|senior management|management)", r"re-?appointment", r"\bappoint",
         r"resignation", r"\bcessation", r"\bretire", r"chief (executive|financial|operating)", r"\b(ceo|cfo|coo)\b")),
    ("litigation", "litigation_or_regulatory",
     _rx(r"litigation", r"dispute", r"penalty", r"show cause", r"demand (order|notice)", r"tax demand", r"\bcourt\b",
         r"tribunal", r"\bnclt\b", r"arbitra", r"\bgst\b.*order", r"order (passed|received)", r"assessment order",
         r"\bsebi order", r"\bfine\b", r"\binvestigation", r"\bsearch\b.*income tax")),
    ("credit_rating", "credit_rating", _rx(r"credit rating", r"(?<!esg )\brating(s)?\b")),   # ESG ratings aren't credit ratings
    ("capex", "capex_orders_expansion",
     _rx(r"\bcapex\b", r"capacity (expansion|addition)", r"commission(ed|ing) of", r"new (plant|facility|unit)",
         r"bagging", r"receiving of orders", r"order (win|inflow|received)", r"\bcontracts?\b", r"letter of award",
         r"\bexpansion\b")),
    ("investor_meetings", "investor_meet",
     _rx(r"analysts?\b", r"institutional investor", r"investor(s)? meet", r"schedule of (analyst|investor )?meet",
         r"investor presentation", r"investor (day|conference)", r"roadshow", r"\bcon\.? ?call\b", r"conference call")),
    ("press_release", "press_release", _rx(r"press release", r"media release")),
    ("routine", "board_meeting_intimation", _rx(r"board meeting intimation", r"intimation of board meeting", r"prior intimation")),
    ("routine", "record_date", _rx(r"record date", r"book closure")),
]

# Generic NSE/BSE labels that carry no category signal by themselves.
GENERIC_SUBJECTS = {"updates", "general updates", "company update", "outcome of board meeting", "others", "other"}

# A board-meeting intimation is routine unless it signals one of these.
INTIMATION_OVERRIDES = {"fund_raise", "bonus_split", "M&A", "dividend"}


def classify_by_rules(subject: str, description: str | None) -> tuple[str, str] | None:
    """(category, rule_name), or None if the rules can't place it confidently."""
    desc = description or ""
    subject_l = subject.strip().lower()
    # Generic subjects add nothing; judge on the description alone.
    text = desc if subject_l in GENERIC_SUBJECTS else f"{subject} || {desc}"
    if not text.strip():
        return None

    hits = [(cat, name) for cat, name, rx in RULES if rx.search(text)]
    if not hits:
        return None
    category, name = hits[0]

    # Intimations ("board meeting to consider X") are routine unless X is material.
    if name == "board_meeting_intimation" or (category == "results" and re.search(r"intimation|to consider", text, re.I)):
        material = next((h for h in hits if h[0] in INTIMATION_OVERRIDES), None)
        return (material[0], f"intimation:{material[1]}") if material else ("routine", "board_meeting_intimation")
    return category, name


# --------------------------------------------------------------------------- #
# Step B: cheap LLM for leftovers
# --------------------------------------------------------------------------- #
CATEGORY_GUIDE = """
results: quarterly/annual financial results, outcome of a board meeting approving results
M&A: acquisitions, mergers, demergers, stake purchases/sales, JVs, new subsidiaries, open offers
fund_raise: QIP, rights/preferential issue, NCDs, bonds, commercial paper, loans
management_change: appointment/resignation of directors, CEO/CFO/KMP, auditors
litigation: legal/tax/regulatory orders, penalties, disputes, investigations
insider_trading: SAST/insider-trading/pledge disclosures by promoters or insiders
earnings_call: earnings-call transcript, audio/video recording, call invitation for results
investor_meetings: analyst/institutional investor meeting schedules, investor presentations, conferences
credit_rating: credit rating assigned/reaffirmed/upgraded/downgraded by a credit rating agency (NOT ESG ratings)
dividend: dividend declaration or record date for dividend
bonus_split: bonus issue or stock split
capex: capacity expansion, new plants, major order wins/contracts, business expansion
press_release: product launches, awards, CSR, ESG ratings, partnerships and other company news
routine: AGM/EGM, postal ballot, ESOP allotment, compliance certificates, newspaper ads, trading window, registry admin
unclassified: genuinely impossible to tell from the text
""".strip()

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["id", "category"],
            },
        }
    },
    "required": ["items"],
}


def classify_with_llm(rows: list[sqlite3.Row]) -> dict[str, str]:
    """{announcement_id: category} for a batch of rows."""
    lines = [
        f'- id: {i}\n  subject: {r["subject"]}\n  description: {(r["description"] or "")[:600]}'
        for i, r in enumerate(rows)
    ]
    prompt = (
        "You classify Indian listed-company stock exchange announcements.\n"
        "Pick exactly one category per announcement from this list:\n\n"
        f"{CATEGORY_GUIDE}\n\n"
        "Return one item per announcement, using the given id.\n\n"
        "Announcements:\n" + "\n".join(lines)
    )
    result = llm.call_cheap(prompt, CLASSIFY_SCHEMA, feature="classify_announcements")
    out: dict[str, str] = {}
    for item in result.get("items", []):
        try:
            idx = int(item["id"])
        except (KeyError, ValueError):
            continue
        if 0 <= idx < len(rows) and item.get("category") in CATEGORIES:
            out[rows[idx]["id"]] = item["category"]
    return out


# --------------------------------------------------------------------------- #
# Step C: scoring
# --------------------------------------------------------------------------- #
@dataclass
class Reaction:
    session: str
    change_pct: float | None
    volume_ratio: float | None


def market_reaction(conn: sqlite3.Connection, ticker: str, announced_at: str) -> Reaction | None:
    """Price/volume of the first session that could react to the announcement.

    Announcements after the 15:30 IST close are matched to the next session.
    Uses the same definitions as prices.get_metrics (1d change vs previous close;
    volume vs the average of the previous 20 sessions).
    """
    ts = datetime.fromisoformat(announced_at)
    op = ">" if ts.time() > MARKET_CLOSE_IST else ">="
    rows = conn.execute(
        f"SELECT date, close, volume FROM prices WHERE ticker = ? AND date {op} ? ORDER BY date LIMIT 1",
        (ticker, ts.date().isoformat()),
    ).fetchall()
    if not rows:
        return None                        # reaction session not in the data yet
    session = rows[0]
    prior = conn.execute(
        "SELECT close, volume FROM prices WHERE ticker = ? AND date < ? ORDER BY date DESC LIMIT 20",
        (ticker, session["date"]),
    ).fetchall()
    if not prior:
        return Reaction(session["date"], None, None)
    change = (session["close"] / prior[0]["close"] - 1) * 100 if prior[0]["close"] else None
    vols = [p["volume"] for p in prior if p["volume"]]
    ratio = session["volume"] / (sum(vols) / len(vols)) if vols and session["volume"] else None
    return Reaction(session["date"], change, ratio)


def market_boost(reaction: Reaction | None) -> int:
    if reaction is None:
        return 0
    boost = 0
    move = abs(reaction.change_pct or 0)
    ratio = reaction.volume_ratio or 0
    if move >= MOVE_PCT_STRONG:
        boost += 15
    elif move >= MOVE_PCT:
        boost += 10
    if ratio >= VOL_RATIO_STRONG:
        boost += 15
    elif ratio >= VOL_RATIO:
        boost += 10
    return min(boost, MAX_MARKET_BOOST)


def score(category: str, text: str, boost: int) -> int:
    value = CATEGORY_WEIGHT[category]
    for cat, pattern, bump in MATERIALITY_BUMPS:
        if cat == category and re.search(pattern, text, re.IGNORECASE):
            value += bump
    return max(0, min(100, value + boost))


def tier_for(score_value: int) -> str:
    return next(t for threshold, t in TIER_THRESHOLDS if score_value >= threshold)


# --------------------------------------------------------------------------- #
# Step D: 1-line summaries
# --------------------------------------------------------------------------- #
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "why_it_matters": {"type": "string"}},
                "required": ["id", "why_it_matters"],
            },
        }
    },
    "required": ["items"],
}


def summarize_with_llm(rows: list[dict]) -> dict[str, str]:
    lines = []
    for i, r in enumerate(rows):
        reaction = ""
        if r["reaction_change"] is not None:
            reaction = f"\n  market_reaction: {r['reaction_change']:+.1f}% on {r['reaction_volume']:.1f}x normal volume"
        lines.append(
            f'- id: {i}\n  company: {r["ticker"]}\n  category: {r["category"]}\n  subject: {r["subject"]}\n'
            f'  description: {(r["description"] or "")[:800]}{reaction}'
        )
    prompt = (
        "You write for a retail investor who holds these Indian stocks.\n"
        "For each announcement write ONE sentence (max 25 words) on why it matters to a shareholder.\n"
        "Rules: use only facts in the text; never invent numbers; if the text is thin, say what to check "
        "in the filing; no hype, no buy/sell advice; start with the substance, not the company name.\n\n"
        "Announcements:\n" + "\n".join(lines)
    )
    result = llm.call_cheap(prompt, SUMMARY_SCHEMA, feature="summarize_announcements", temperature=0.2)
    out: dict[str, str] = {}
    for item in result.get("items", []):
        try:
            idx = int(item["id"])
        except (KeyError, ValueError):
            continue
        text = (item.get("why_it_matters") or "").strip()
        if 0 <= idx < len(rows) and text:
            out[rows[idx]["id"]] = text
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS announcement_insights (
            announcement_id TEXT PRIMARY KEY REFERENCES announcements(id),
            category        TEXT NOT NULL,
            method          TEXT NOT NULL,     -- rules | llm | pending_llm
            rule            TEXT,              -- which rule matched (rules only)
            priority_score  INTEGER,
            tier            TEXT,              -- MUST_READ | WORTH_A_LOOK | ROUTINE
            market_boost    INTEGER,
            reaction_date   TEXT,
            reaction_change_pct REAL,
            reaction_volume_ratio REAL,
            summary         TEXT,              -- 1-line "why it matters" (MUST_READ / WORTH_A_LOOK)
            summary_model   TEXT,
            classified_at   TEXT NOT NULL,
            scored_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_insights_tier_score ON announcement_insights (tier, priority_score DESC);
        """
    )
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(use_llm: bool = True, force: bool = False) -> dict:
    stats = {"rules": 0, "llm": 0, "pending_llm": 0, "already_cached": 0, "summaries": 0, "llm_errors": 0}
    with closing(connect()) as conn:
        if force:
            conn.execute("DELETE FROM announcement_insights")
            conn.commit()

        # ---- A. rules on rows never seen before --------------------------------
        new_rows = conn.execute(
            """
            SELECT a.* FROM announcements a
            LEFT JOIN announcement_insights i ON i.announcement_id = a.id
            WHERE i.announcement_id IS NULL
            """
        ).fetchall()
        stats["already_cached"] = conn.execute("SELECT COUNT(*) FROM announcement_insights").fetchone()[0]
        for r in new_rows:
            hit = classify_by_rules(r["subject"], r["description"])
            category, method, rule = (hit[0], "rules", hit[1]) if hit else ("unclassified", "pending_llm", None)
            conn.execute(
                "INSERT INTO announcement_insights (announcement_id, category, method, rule, classified_at) VALUES (?, ?, ?, ?, ?)",
                (r["id"], category, method, rule, _now()),
            )
            stats["rules" if hit else "pending_llm"] += 1
        conn.commit()

        # ---- B. cheap LLM for anything still pending ---------------------------
        pending = conn.execute(
            """
            SELECT a.* FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
            WHERE i.method = 'pending_llm' ORDER BY a.announced_at DESC
            """
        ).fetchall()
        if use_llm and pending:
            stats["pending_llm"] = 0
            for batch in _batches(pending, LLM_BATCH_SIZE):
                try:
                    labels = classify_with_llm(batch)
                except llm.LLMError as exc:
                    log.error("LLM classification failed for a batch of %d: %s", len(batch), exc)
                    stats["llm_errors"] += 1
                    stats["pending_llm"] += len(batch)
                    continue
                for r in batch:
                    category = labels.get(r["id"], "unclassified")
                    conn.execute(
                        "UPDATE announcement_insights SET category = ?, method = 'llm', classified_at = ? WHERE announcement_id = ?",
                        (category, _now(), r["id"]),
                    )
                    stats["llm"] += 1
                conn.commit()
        elif pending:
            stats["pending_llm"] = len(pending)

        # ---- C. score + tier every row (free; reaction data may be new) --------
        rows = conn.execute(
            """
            SELECT a.id, a.ticker, a.announced_at, a.subject, a.description, i.category
            FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
            """
        ).fetchall()
        scored = []
        for r in rows:
            reaction = market_reaction(conn, r["ticker"], r["announced_at"])
            base = score(r["category"], f'{r["subject"]} {r["description"] or ""}', 0)
            scored.append((r, reaction, base))

        # Best base score per (ticker, reaction session) among boost-eligible filings.
        top_base: dict[tuple[str, str], int] = {}
        for r, reaction, base in scored:
            if reaction and r["category"] not in NO_MARKET_BOOST:
                key = (r["ticker"], reaction.session)
                top_base[key] = max(top_base.get(key, 0), base)

        for r, reaction, base in scored:
            eligible = (
                reaction is not None
                and r["category"] not in NO_MARKET_BOOST
                and base == top_base.get((r["ticker"], reaction.session))
            )
            boost = market_boost(reaction) if eligible else 0
            value = max(0, min(100, base + boost))
            conn.execute(
                """
                UPDATE announcement_insights
                SET priority_score = ?, tier = ?, market_boost = ?, reaction_date = ?,
                    reaction_change_pct = ?, reaction_volume_ratio = ?, scored_at = ?
                WHERE announcement_id = ?
                """,
                (value, tier_for(value), boost, reaction.session if reaction else None,
                 reaction.change_pct if reaction else None, reaction.volume_ratio if reaction else None,
                 _now(), r["id"]),
            )
        conn.commit()

        # ---- D. summaries for important rows that don't have one yet -----------
        if use_llm:
            to_summarize = [
                dict(r) for r in conn.execute(
                    """
                    SELECT a.id, a.ticker, a.subject, a.description, i.category,
                           i.reaction_change_pct AS reaction_change, i.reaction_volume_ratio AS reaction_volume
                    FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
                    WHERE i.tier IN ('MUST_READ', 'WORTH_A_LOOK') AND i.summary IS NULL
                    ORDER BY i.priority_score DESC
                    """
                )
            ]
            for batch in _batches(to_summarize, SUMMARY_BATCH_SIZE):
                try:
                    summaries = summarize_with_llm(batch)
                except llm.LLMError as exc:
                    log.error("LLM summary failed for a batch of %d: %s", len(batch), exc)
                    stats["llm_errors"] += 1
                    continue
                for ann_id, text in summaries.items():
                    conn.execute(
                        "UPDATE announcement_insights SET summary = ?, summary_model = ? WHERE announcement_id = ?",
                        (text, llm.last_model_used, ann_id),
                    )
                    stats["summaries"] += 1
                conn.commit()
    return stats


def get_ranked(ticker: str | None = None, tiers: tuple[str, ...] | None = None, limit: int = 50) -> list[dict]:
    """Announcements with insights, highest priority first (for the API / feed)."""
    sql = """
        SELECT a.*, i.category, i.method, i.priority_score, i.tier, i.summary,
               i.reaction_date, i.reaction_change_pct, i.reaction_volume_ratio
        FROM announcements a JOIN announcement_insights i ON i.announcement_id = a.id
        WHERE 1 = 1
    """
    params: list = []
    if ticker:
        sql += " AND a.ticker = ?"
        params.append(ticker.upper())
    if tiers:
        sql += f" AND i.tier IN ({','.join('?' * len(tiers))})"
        params.extend(tiers)
    sql += " ORDER BY i.priority_score DESC, a.announced_at DESC LIMIT ?"
    params.append(limit)
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_report(stats: dict) -> None:
    with closing(connect()) as conn:
        method = dict(conn.execute("SELECT method, COUNT(*) FROM announcement_insights GROUP BY method").fetchall())
        tiers = dict(conn.execute("SELECT tier, COUNT(*) FROM announcement_insights GROUP BY tier").fetchall())
        cats = conn.execute(
            "SELECT category, COUNT(*) n FROM announcement_insights GROUP BY category ORDER BY n DESC"
        ).fetchall()
    total = sum(method.values())
    print(f"This run: {stats}")
    print(f"\nAll {total} announcements by method: " + ", ".join(f"{k}={v}" for k, v in sorted(method.items())))
    print("By tier: " + ", ".join(f"{t}={tiers.get(t, 0)}" for t in ("MUST_READ", "WORTH_A_LOOK", "ROUTINE")))
    print("By category: " + ", ".join(f"{c}={n}" for c, n in cats))
    usage = llm.usage_summary()
    if usage:
        print("\nLLM usage (all time):")
        for u in usage:
            print(f"  {u['feature']:<26}{u['model']:<20}calls={u['calls']:<4}in={u['input_tokens']:<8}"
                  f"out={u['output_tokens']:<8}${u['est_cost_usd']:.4f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Classify, score and summarize announcements.")
    parser.add_argument("--no-llm", action="store_true", help="Rules and scoring only; no LLM calls.")
    parser.add_argument("--force", action="store_true", help="Discard cached insights and redo everything.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print_report(run(use_llm=not args.no_llm, force=args.force))


if __name__ == "__main__":
    main()
