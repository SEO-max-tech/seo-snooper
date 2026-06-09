"""Fetch pages and extract heading structure.

Two modes — the asymmetry is deliberate (see CLAUDE.md):
- competitor mode: title + h1 (+ meta description if present). ONE topic
  string per URL: f"{title} — {h1}".
- inventory mode (Murf): title + h1 as the 'page' segment, plus one
  'section' segment per h2 with its h3s appended ("H2: h3a / h3b").

Per-URL isolation: one failed fetch returns an error record, never raises.
httpx with timeout + retries (tenacity), bs4 html.parser.
"""
from __future__ import annotations


def fetch_meta(urls: list[str], user_agent: str, timeout: int) -> list[dict]:
    """Competitor mode. Returns [{url, title, h1, meta_description,
    topic_text, error}] — error is None on success."""
    raise NotImplementedError  # TODO(M3)


def fetch_segments(urls: list[str], user_agent: str, timeout: int) -> list[dict]:
    """Inventory mode. Returns [{url, segments: [{segment_index,
    segment_type, segment_text}], error}]. segment_index 0 = page segment."""
    raise NotImplementedError  # TODO(M3)
