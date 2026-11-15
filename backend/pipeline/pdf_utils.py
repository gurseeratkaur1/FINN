"""Download exchange filing PDFs (cached) and extract their text with PyMuPDF."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta
import logging
import random
import time
from pathlib import Path

import pymupdf
import requests

from backend.data.announcements import USER_AGENT

BACKEND_DIR = Path(__file__).resolve().parent.parent
PDF_CACHE_DIR = BACKEND_DIR / "cache" / "pdfs"
MAX_ATTEMPTS = 3
TIMEOUT = 60

log = logging.getLogger("finn.pdf")


class PDFError(RuntimeError):
    pass


def _headers(url: str) -> dict:
    referer = "https://www.bseindia.com/" if "bseindia" in url else "https://www.nseindia.com/"
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }


def cache_path(url: str) -> Path:
    return PDF_CACHE_DIR / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.pdf"


def _valid_pdf(path: Path) -> bool:
    """Opens and has at least one page (catches truncated downloads)."""
    try:
        with pymupdf.open(path) as doc:
            return doc.page_count > 0
    except Exception:
        return False


def _pdf_from_zip(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        pdfs = sorted((i for i in zf.infolist() if i.filename.lower().endswith(".pdf")), key=lambda i: -i.file_size)
        if not pdfs:
            raise PDFError("ZIP attachment contains no PDF")
        return zf.read(pdfs[0])                 # the largest PDF is the filing itself


def download_pdf(url: str) -> Path:
    """Return a local path to the PDF, downloading it once (cached by URL)."""
    path = cache_path(url)
    if path.exists() and _valid_pdf(path):
        return path
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=_headers(url), timeout=TIMEOUT)
            expected = int(resp.headers.get("content-length") or 0)
            content = resp.content
            if resp.status_code == 200 and expected and len(content) < expected:
                raise PDFError(f"truncated download ({len(content)}/{expected} bytes)")
            if resp.status_code == 200 and content[:2] == b"PK":
                content = _pdf_from_zip(content)       # some filings are a ZIP holding the PDF
            if resp.status_code == 200 and content[:5] == b"%PDF-":
                path.write_bytes(content)
                time.sleep(random.uniform(0.5, 1.0))
                if _valid_pdf(path):
                    return path
                path.unlink(missing_ok=True)
                raise PDFError("downloaded file is not a readable PDF")
            last_error = PDFError(f"HTTP {resp.status_code}, content-type {resp.headers.get('content-type')}")
        except (requests.RequestException, PDFError) as exc:
            last_error = exc
            log.info("PDF attempt %d failed for %s: %s", attempt, url, exc)
        time.sleep(2 * attempt)
    raise PDFError(f"could not download {url}: {last_error}")


def page_texts(path: Path) -> list[str]:
    """Plain text of every page, in reading order."""
    with pymupdf.open(path) as doc:
        return [page.get_text("text", sort=True) for page in doc]


def is_scanned(texts: list[str]) -> bool:
    """True when the PDF has (almost) no text layer, i.e. it's scanned images."""
    return sum(len(t.strip()) for t in texts) < 200 * max(1, min(len(texts), 3))


def bse_alternative(ticker: str, announced_at: str, pattern: str) -> str | None:
    """PDF URL of the same filing on BSE (within a day either side), for when the
    NSE copy is broken (e.g. NSE served a truncated HDFCBANK results PDF).
    `pattern` is a regex matched against the BSE subject/description.
    """
    from backend.data.announcements import BSEClient   # local import: avoids a cycle at import time

    when = datetime.fromisoformat(announced_at)
    try:
        client = BSEClient()
        rows = client.fetch_raw(ticker, (when - timedelta(days=1)).date(), (when + timedelta(days=1)).date())
    except Exception as exc:
        log.info("BSE lookup failed for %s: %s", ticker, exc)
        return None
    candidates = [
        a for a in BSEClient.parse(ticker, rows)
        if a.pdf_url and re.search(pattern, f"{a.subject} {a.description or ''}", re.IGNORECASE)
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda a: abs((datetime.fromisoformat(a.announced_at) - when).total_seconds()))
    return best.pdf_url


def load_filing(url: str, ticker: str, announced_at: str, bse_pattern: str) -> tuple[str, list[str]]:
    """(url actually used, page texts). Falls back to the BSE copy if the PDF is unusable."""
    try:
        return url, page_texts(download_pdf(url))
    except PDFError as exc:
        alt = bse_alternative(ticker, announced_at, bse_pattern) if "nseindia" in url else None
        if not alt:
            raise
        log.warning("Using BSE copy for %s (%s)", ticker, exc)
        return alt, page_texts(download_pdf(alt))
