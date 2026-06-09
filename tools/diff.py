"""Upsert seen URLs into cm_urls and detect genuinely new ones.

'New' = URL id never present for this competitor. lastmod is stored but
NEVER used for newness. MD5 stable IDs: md5(f"{slug}|{url}").
"""
from __future__ import annotations
import pandas as pd


def detect_new(supabase, competitor_slug: str, urls_df: pd.DataFrame) -> list[dict]:
    """Returns list of {url, lastmod} for never-before-seen URLs.

    FIRST RUN GUARD: if cm_urls has zero rows for competitor_slug, upsert
    everything with status='baseline' and return []. No alerts on first run.

    For existing competitors: upsert all (updates last_seen), new rows get
    status='new'. Batch upserts (500/chunk).
    """
    raise NotImplementedError  # TODO(M2)
