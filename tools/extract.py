"""Fetch pages and extract heading structure.

Two modes — the asymmetry is deliberate (see CLAUDE.md):
- competitor mode: title + h1 (+ meta description if present). ONE topic
  string per URL: f"{title} — {h1}".
- inventory mode (own site): title + h1 as the 'page' segment, plus one
  'section' segment per h2 with its h3s appended ("H2: h3a / h3b").

Per-URL isolation: one failed fetch returns an error record, never raises.
httpx with timeout + retries (tenacity), bs4 html.parser.
"""
from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
# SEO title separators: pipe, bullets, chevrons, en/em dash, spaced hyphen
_TITLE_SEP = re.compile(r"\s*[|•·›»–—]\s*|\s+-\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", text).strip() if text else ""


def _brand_token(url: str) -> str:
    """Second-level domain label, e.g. https://www.bolna.ai -> 'bolna'."""
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc.split(":")[0]
    labels = [l for l in host.split(".") if l and l != "www"]
    return labels[-2].lower() if len(labels) >= 2 else (labels[0].lower()
                                                        if labels else "")


def _page_topic(title: str, h1: str, url: str) -> str:
    """Build a denoised topic string from <title> + <h1>.

    SEO titles carry brand boilerplate ('Real Title | Tagline | Brand') that
    dilutes embeddings and pushes genuinely-covered topics into the 'partial'
    band. Drop title segments containing the site's brand token, then dedupe
    the remaining segments against the H1 (substring-aware) so the vector
    reflects the actual topic, not the brand.
    """
    brand = _brand_token(url)
    segs = [s.strip() for s in _TITLE_SEP.split(title) if s.strip()]
    kept = [s for s in segs if brand and brand not in s.lower()]
    if not kept:                      # title was all-brand — fall back to lead
        kept = segs[:1]

    parts: list[str] = []
    for cand in kept + ([h1] if h1 else []):
        cl = cand.lower()
        # skip if already represented by a kept (longer) segment
        if any(cl in p.lower() for p in parts):
            continue
        # drop kept segments that are substrings of this richer candidate
        parts = [p for p in parts if p.lower() not in cl]
        parts.append(cand)
    return " — ".join(parts)


@retry(retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
       stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, max=10),
       reraise=True)
def _get(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    if resp.status_code >= 500:           # retry 5xx; 4xx fails immediately
        resp.raise_for_status()
    if resp.status_code >= 400:
        raise ValueError(f"HTTP {resp.status_code}")
    return resp.text


def _main_scope(soup: BeautifulSoup):
    """Prefer <main>/<article> to dodge nav/footer headings."""
    return soup.find("main") or soup.find("article") or soup


def parse_meta(html: str, url: str) -> dict:
    """Competitor mode, pure parse (offline-testable)."""
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.string if soup.title else "")
    scope = _main_scope(soup)
    h1_el = scope.find("h1") or soup.find("h1")
    h1 = _clean(h1_el.get_text() if h1_el else "")
    md_el = soup.find("meta", attrs={"name": "description"})
    meta_description = _clean(md_el.get("content") if md_el else "")
    topic_text = _page_topic(title, h1, url)
    return {
        "url": url,
        "title": title,
        "h1": h1,
        "meta_description": meta_description,
        "topic_text": topic_text,
        "error": None if topic_text else "no title or h1 found",
    }


def parse_segments(html: str, url: str) -> dict:
    """Inventory mode, pure parse (offline-testable).

    segment_index 0 = page (title + h1); 1..n = one per h2, with that h2's
    h3s appended as context.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.string if soup.title else "")
    scope = _main_scope(soup)
    h1_el = scope.find("h1") or soup.find("h1")
    h1 = _clean(h1_el.get_text() if h1_el else "")

    segments = []
    page_text = _page_topic(title, h1, url)
    if page_text:
        segments.append({"segment_index": 0, "segment_type": "page",
                         "segment_text": page_text})

    # walk h2/h3 in document order; h3s attach to the preceding h2
    idx = 1
    current_h2: str | None = None
    h3s: list[str] = []

    def flush():
        nonlocal idx, current_h2, h3s
        if current_h2:
            text = current_h2 + (": " + " / ".join(h3s) if h3s else "")
            segments.append({"segment_index": idx, "segment_type": "section",
                             "segment_text": text})
            idx += 1
        current_h2, h3s = None, []

    for el in scope.find_all(["h2", "h3"]):
        heading = _clean(el.get_text())
        if not heading:
            continue
        if el.name == "h2":
            flush()
            current_h2 = heading
        elif current_h2:
            h3s.append(heading)
    flush()

    return {"url": url, "segments": segments,
            "error": None if segments else "no headings found"}


def _fetch_batch(urls: list[str], user_agent: str, timeout: int,
                 parser) -> list[dict]:
    results = []
    with httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout,
                      follow_redirects=True) as client:
        for url in urls:
            try:
                results.append(parser(_get(client, url), url))
            except Exception as exc:  # noqa: BLE001 — per-URL isolation
                log.warning("fetch failed: %s (%s)", url, exc)
                results.append({"url": url, "error": str(exc)})
    return results


def fetch_meta(urls: list[str], user_agent: str, timeout: int) -> list[dict]:
    """Competitor mode. Returns [{url, title, h1, meta_description,
    topic_text, error}] — error is None on success."""
    return _fetch_batch(urls, user_agent, timeout, parse_meta)


def fetch_segments(urls: list[str], user_agent: str, timeout: int) -> list[dict]:
    """Inventory mode. Returns [{url, segments: [{segment_index,
    segment_type, segment_text}], error}]. segment_index 0 = page segment."""
    return _fetch_batch(urls, user_agent, timeout, parse_segments)
