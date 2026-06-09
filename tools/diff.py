"""Upsert seen URLs into cm_urls and detect genuinely new ones.

'New' = URL id never present for this competitor. lastmod is stored but
NEVER used for newness. MD5 stable IDs: md5(f"{slug}|{url}").
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger(__name__)

UPSERT_CHUNK = 500


def url_id(competitor_slug: str, url: str) -> str:
    return hashlib.md5(f"{competitor_slug}|{url}".encode()).hexdigest()


def _existing_ids(supabase, competitor_slug: str) -> set[str]:
    """Page through all cm_urls ids for this competitor (1000/row cap)."""
    ids: set[str] = set()
    offset, page = 0, 1000
    while True:
        resp = (supabase.table("cm_urls").select("id")
                .eq("competitor_slug", competitor_slug)
                .range(offset, offset + page - 1).execute())
        rows = resp.data or []
        ids.update(r["id"] for r in rows)
        if len(rows) < page:
            return ids
        offset += page


def _upsert_chunks(supabase, rows: list[dict]) -> None:
    for i in range(0, len(rows), UPSERT_CHUNK):
        supabase.table("cm_urls").upsert(rows[i:i + UPSERT_CHUNK]).execute()


def detect_new(supabase, competitor_slug: str, urls_df: pd.DataFrame) -> list[dict]:
    """Returns list of {url, lastmod} for never-before-seen URLs.

    FIRST RUN GUARD: if cm_urls has zero rows for competitor_slug, upsert
    everything with status='baseline' and return []. No alerts on first run.

    For existing competitors: upsert all (updates last_seen), new rows get
    status='new'. Batch upserts (500/chunk).
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = _existing_ids(supabase, competitor_slug)
    first_run = not existing

    rows, new_items = [], []
    for rec in urls_df.to_dict("records"):
        url = rec["url"]
        lastmod = rec.get("lastmod")
        lastmod_iso = lastmod.isoformat() if pd.notna(lastmod) else None
        rid = url_id(competitor_slug, url)
        is_new = rid not in existing
        rows.append({
            "id": rid,
            "competitor_slug": competitor_slug,
            "url": url,
            "last_seen": now,
            "lastmod": lastmod_iso,
            "status": "baseline" if first_run else ("new" if is_new else "seen"),
        })
        if not first_run and is_new:
            new_items.append({"url": url, "lastmod": lastmod_iso})

    _upsert_chunks(supabase, rows)
    log.info("%s: %d urls upserted, %d new%s", competitor_slug, len(rows),
             len(new_items), " (baseline run)" if first_run else "")
    return new_items
