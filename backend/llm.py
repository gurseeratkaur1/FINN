"""Model routing for all LLM calls.

    call_cheap(prompt, json_schema=None, feature=...)   bulk extraction / classification
    call_strong(prompt, json_schema=None, feature=...)  complex reasoning

Every call is logged to SQLite table `llm_usage` with token counts and an
estimated USD cost, tagged by `feature`, so cost can be reported per feature.

Free-tier friendly: when a model's *daily* quota is exhausted (429 "per day"), the
call moves on to the next API key, then to the next fallback model, instead of failing.
Free-tier limits are per model, so falling back to a lighter model keeps the pipeline running.

Configuration (backend/.env):
    GEMINI_API_KEYS       comma-separated keys (optional; tried in order)
    GEMINI_API_KEY        single key (used if GEMINI_API_KEYS is not set)
    GEMINI_CHEAP_MODEL    default gemini-2.5-flash
    GEMINI_CHEAP_FALLBACKS comma-separated, default gemini-3.1-flash-lite,gemini-3.5-flash-lite
    GEMINI_STRONG_MODEL   default gemini-3.5-flash (thinking_level=high); fallbacks gemini-3.8-flash, gemini-3.1-flash-lite
    GEMINI_CHEAP_THINKING thinking level for call_cheap on Gemini 3+ models (default low)
    GEMINI_BACKEND        auto (default) | vertex | gemini
                          auto = Vertex AI if the key starts with "AQ.", else Gemini API
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from backend.data.prices import DB_PATH

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")

CHEAP_MODEL = os.getenv("GEMINI_CHEAP_MODEL", "gemini-2.5-flash")
STRONG_MODEL = os.getenv("GEMINI_STRONG_MODEL", "gemini-3.5-flash")   # 2.5-pro is unavailable on new free-tier projects
STRONG_THINKING = os.getenv("GEMINI_STRONG_THINKING", "high")
STRONG_FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_STRONG_FALLBACKS", "gemini-3.8-flash,gemini-3.1-flash-lite").split(",") if m.strip()]
CHEAP_THINKING = os.getenv("GEMINI_CHEAP_THINKING", "low")
CHEAP_FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_CHEAP_FALLBACKS", "gemini-3.1-flash-lite,gemini-3.5-flash-lite").split(",") if m.strip()]
API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()] or (
    [os.environ["GEMINI_API_KEY"]] if os.getenv("GEMINI_API_KEY") else [])
MAX_RATE_LIMIT_WAIT = 60.0               # seconds; longer per-minute waits are capped

MAX_ATTEMPTS = 5
BASE_BACKOFF = 2.0                       # seconds, doubled each retry, plus jitter
RETRYABLE_CODES = {429, 500, 502, 503, 504}
REQUEST_TIMEOUT_MS = 120_000             # per request; overloaded models otherwise hang for minutes

# USD per 1M tokens (input, output). Thinking tokens are billed as output.
# Standard paid-tier list prices for prompts <= 200k tokens; update if pricing changes.
PRICING = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.5-flash": (0.30, 2.50),        # estimate: priced like 2.5-flash until official rates are checked
}
DEFAULT_PRICE = PRICING["gemini-2.5-flash"]   # used (with a warning) for unlisted models

log = logging.getLogger("finn.llm")
_unpriced_warned: set[str] = set()


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
_clients: dict[int, genai.Client] = {}
_exhausted: set[tuple[int, str]] = set()     # (key index, model) whose daily quota is used up
_unavailable: set[str] = set()               # models this account can't use at all (404)
last_model_used: str | None = None           # model that served the most recent call


def get_client(key_index: int = 0) -> genai.Client:
    if not API_KEYS:
        raise LLMError("No Gemini key: set GEMINI_API_KEYS or GEMINI_API_KEY in backend/.env")
    if key_index not in _clients:
        api_key = API_KEYS[key_index]
        backend = os.getenv("GEMINI_BACKEND", "auto").lower()
        use_vertex = backend == "vertex" or (backend == "auto" and api_key.startswith("AQ."))
        # Disable the SDK's own silent retries so our logged backoff is the only retry layer.
        http_options = types.HttpOptions(timeout=REQUEST_TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1))
        _clients[key_index] = (
            genai.Client(vertexai=True, api_key=api_key, http_options=http_options) if use_vertex
            else genai.Client(api_key=api_key, http_options=http_options)
        )
    return _clients[key_index]


# --------------------------------------------------------------------------- #
# Usage logging
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            feature       TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,   -- includes thinking tokens
            est_cost_usd  REAL NOT NULL,
            created_at    TEXT NOT NULL,      -- UTC ISO timestamp
            search_queries INTEGER NOT NULL DEFAULT 0   -- reserved; grounding is not used (free tier only)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    if "search_queries" not in columns:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN search_queries INTEGER NOT NULL DEFAULT 0")
    return conn


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICING.get(model, DEFAULT_PRICE)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def log_usage(feature: str, model: str, input_tokens: int, output_tokens: int, search_queries: int = 0) -> float:
    if model not in PRICING and model not in _unpriced_warned:
        _unpriced_warned.add(model)
        log.warning("No price listed for %s; estimating with gemini-2.5-flash rates.", model)
    cost = estimate_cost(model, input_tokens, output_tokens)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO llm_usage (feature, model, input_tokens, output_tokens, est_cost_usd, created_at, search_queries) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feature, model, input_tokens, output_tokens, cost,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), search_queries),
        )
        conn.commit()
    return cost


def usage_summary() -> list[dict]:
    """Totals per feature and model (for reporting)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT feature, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens, SUM(est_cost_usd) AS est_cost_usd,
                   SUM(search_queries) AS search_queries
            FROM llm_usage GROUP BY feature, model ORDER BY feature, model
            """
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    return any(word in name for word in ("timeout", "connect", "transport", "network", "protocol"))


def _thinking_config(model: str, level: str | None) -> types.ThinkingConfig | None:
    """Keep thinking (billed as output tokens) small for bulk calls."""
    if level is None:
        return None
    if model.startswith("gemini-2.5-flash"):
        return types.ThinkingConfig(thinking_budget=0)       # 2.5 Flash: thinking off
    if model.startswith("gemini-2."):
        return None                                           # 2.5 Pro can't disable thinking
    return types.ThinkingConfig(thinking_level=level)         # Gemini 3+


class LLMQuotaError(LLMError):
    """Every key/model combination is out of quota."""


class _DailyQuota(Exception):
    pass


class _ModelUnavailable(Exception):
    pass


def _message(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc)


def _is_daily_quota(exc: errors.APIError) -> bool:
    text = _message(exc).lower()
    return exc.code == 429 and ("per day" in text or "perday" in text or "daily" in text)


def _retry_after(exc: errors.APIError) -> float | None:
    match = re.search(r"retry in ([\d.]+)s", _message(exc))
    return float(match.group(1)) if match else None


def _generate_once(key_index: int, model: str, contents: str, config: types.GenerateContentConfig, feature: str):
    """One key + model, with retry/backoff for transient errors. Logs token usage."""
    global last_model_used
    delay = BASE_BACKOFF
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = get_client(key_index).models.generate_content(model=model, contents=contents, config=config)
            break
        except Exception as exc:
            if isinstance(exc, errors.APIError):
                if _is_daily_quota(exc):
                    raise _DailyQuota(_message(exc)) from exc
                if exc.code == 404:
                    raise _ModelUnavailable(_message(exc)) from exc
                code, retryable = exc.code, exc.code in RETRYABLE_CODES
            else:
                # Timeouts / dropped connections from the SDK's HTTP layer (no HTTP code).
                code, retryable = type(exc).__name__, _is_network_error(exc)
            if not retryable or attempt == MAX_ATTEMPTS:
                raise LLMError(f"{model} failed ({code}): {_message(exc)}") from exc
            hint = _retry_after(exc) if isinstance(exc, errors.APIError) else None
            wait = min(hint if hint is not None else delay + random.uniform(0, 1), MAX_RATE_LIMIT_WAIT)
            log.warning("%s returned %s; retrying in %.1fs (attempt %d/%d)", model, code, wait, attempt, MAX_ATTEMPTS)
            time.sleep(wait)
            delay *= 2

    usage = response.usage_metadata
    input_tokens = ((usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)) if usage else 0
    output_tokens = ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0
    log_usage(feature, model, input_tokens, output_tokens)
    last_model_used = model
    return response


def _generate(models: list[str], contents: str, config_for, feature: str):
    """Try each model in order, each across all keys, skipping exhausted/unavailable ones."""
    tried = []
    for model in models:
        if model in _unavailable:
            continue
        for key_index in range(len(API_KEYS) or 1):
            if (key_index, model) in _exhausted:
                continue
            try:
                return _generate_once(key_index, model, contents, config_for(model), feature)
            except _DailyQuota:
                _exhausted.add((key_index, model))
                log.warning("Daily quota used up for %s on key #%d; trying next key/model.", model, key_index + 1)
                tried.append(f"{model}#key{key_index + 1}")
            except _ModelUnavailable as exc:
                _unavailable.add(model)
                log.warning("%s unavailable for this account (%s); trying next model.", model, exc)
                tried.append(f"{model} (unavailable)")
                break
    raise LLMQuotaError(f"All models/keys exhausted or unavailable: {', '.join(tried) or models}")


def _call(models: list[str], prompt: str, json_schema: dict | None, feature: str, temperature: float,
          thinking: str | None = None):
    def config_for(model: str) -> types.GenerateContentConfig:
        config = types.GenerateContentConfig(temperature=temperature, thinking_config=_thinking_config(model, thinking))
        if json_schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = json_schema
        return config

    response = _generate(models, prompt, config_for, feature)
    text = response.text or ""
    if json_schema is None:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{last_model_used} returned invalid JSON: {text[:200]}") from exc


def call_cheap(prompt: str, json_schema: dict | None = None, feature: str = "unspecified",
               temperature: float = 0.0):
    """Fast, low-cost model (with free-tier fallbacks). Returns parsed JSON if `json_schema` is given, else text."""
    models = [CHEAP_MODEL] + [m for m in CHEAP_FALLBACKS if m != CHEAP_MODEL]
    return _call(models, prompt, json_schema, feature, temperature, thinking=CHEAP_THINKING)


def call_strong(prompt: str, json_schema: dict | None = None, feature: str = "unspecified",
                temperature: float = 0.2):
    """Most capable model, for complex reasoning. Same return contract as call_cheap."""
    models = [STRONG_MODEL] + [m for m in STRONG_FALLBACKS if m != STRONG_MODEL]
    return _call(models, prompt, json_schema, feature, temperature, thinking=STRONG_THINKING)
