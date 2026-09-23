"""Incremental inventory refresh for your own site. Runs before every scan.

1. sitemaps.fetch_urls(config.site) — your own sitemap, same filters.
2. diff.detect_new(slug=config.site.slug) for new URLs; ALSO re-extract
   URLs whose
   stored segments are missing (recovery from partial failures).
3. extract.fetch_segments() on that delta only.
4. For each segment: content_hash = md5(segment_text). Skip embedding if an
   inventory row with same id and content_hash exists. A URL that fetched
   fine but has no headings at all gets a zero-vector tombstone row instead,
   so it counts as processed and is not re-fetched every week.
5. gap.embed_texts() on changed/new segments, upsert into cm_site_inventory
   (id = md5(f"{url}|{segment_index}"), embedding as float32 bytes).
6. Delete inventory rows for URLs that vanished from the sitemap.

Callable as a function from main.py AND runnable standalone:
    python scripts/refresh_inventory.py [--full]   # --full forces re-crawl
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import diff, extract, gap, sitemaps  # noqa: E402

log = logging.getLogger(__name__)

UPSERT_CHUNK = 200          # embeddings are fat rows — smaller chunks
RECHECK_DAYS = 30           # default cadence for re-checking headless URLs

_EMPTY_HASH = hashlib.md5(b"").hexdigest()
_ZERO_VECTOR = "\\x" + (b"\x00" * 4 * gap.DIM).hex()   # bytea via REST


def segment_id(url: str, segment_index: int) -> str:
    return hashlib.md5(f"{url}|{segment_index}".encode()).hexdigest()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(raw) -> dt.datetime | None:
    """Postgres timestamptz (or our own isoformat) -> aware datetime."""
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _is_stale(raw_ts, now: dt.datetime, days: int) -> bool:
    """A missing or unparseable timestamp counts as stale — re-check the URL
    rather than strand it behind a tombstone we can't date."""
    ts = _parse_ts(raw_ts)
    return ts is None or (now - ts) >= dt.timedelta(days=days)


def tombstone_row(url: str, now: dt.datetime) -> dict:
    """Sentinel row marking a URL as fetched-but-headless.

    Without it, `missing = sitemap_urls - inv_urls` never shrinks for pages
    that yield zero segments (bare landing pages, redirect stubs,
    JS-rendered pages), so they are re-fetched on every single run. It lives
    in cm_site_inventory rather than a side table so sitemap pruning deletes
    it like any other row; its zero vector is excluded from the similarity
    matrix by gap.Inventory.load, so it cannot become a nearest match.

    Keyed at segment_index 0 — the slot the page segment would occupy — so
    a page that later gains a title/H1 simply overwrites it.
    """
    return {"id": segment_id(url, 0), "url": url,
            "segment_type": gap.EMPTY_SEGMENT, "segment_text": "",
            "content_hash": _EMPTY_HASH, "embedding": _ZERO_VECTOR,
            "updated_at": now.isoformat()}


def _inventory_state(supabase) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """Returns (urls present in inventory, {row_id: content_hash},
    {tombstoned url: its updated_at})."""
    urls: set[str] = set()
    hashes: dict[str, str] = {}
    tombstoned: dict[str, str] = {}
    offset, page = 0, 1000
    while True:
        resp = (supabase.table("cm_site_inventory")
                .select("id,url,content_hash,segment_type,updated_at")
                .range(offset, offset + page - 1).execute())
        rows = resp.data or []
        for r in rows:
            urls.add(r["url"])
            hashes[r["id"]] = r["content_hash"]
            if r.get("segment_type") == gap.EMPTY_SEGMENT:
                tombstoned[r["url"]] = r.get("updated_at")
        if len(rows) < page:
            return urls, hashes, tombstoned
        offset += page


def refresh(supabase, model, config, full: bool = False) -> dict:
    """Returns {urls_added, segments_embedded, segments_skipped,
    tombstoned, removed}."""
    site_cfg = config["site"]
    settings = config["settings"]
    site_slug = site_cfg["slug"]
    recheck_days = settings.get("inventory_recheck_days", RECHECK_DAYS)
    now = _utcnow()

    sitemap_df = sitemaps.fetch_urls(
        [site_cfg["sitemap"]] if isinstance(site_cfg["sitemap"], str)
        else site_cfg["sitemap"],
        site_cfg.get("include_patterns") or [],
        site_cfg.get("exclude_patterns") or [],
        settings["user_agent"],
    )
    sitemap_urls = set(sitemap_df["url"])

    new_items = diff.detect_new(supabase, site_slug, sitemap_df)
    inv_urls, inv_hashes, inv_tombstoned = _inventory_state(supabase)

    if full:
        to_fetch = sorted(sitemap_urls)
    else:
        new_urls = {n["url"] for n in new_items}
        # recovery: URL known in cm_urls but has no inventory segments yet.
        # On the very first run detect_new returns [] (baseline guard), so
        # the missing-set is what actually populates the inventory.
        # A tombstoned URL has a row, so it drops out of `missing` — but a
        # headless page can gain headings later, so re-check it on a cadence.
        missing = sitemap_urls - inv_urls
        stale = {u for u, ts in inv_tombstoned.items()
                 if u in sitemap_urls and _is_stale(ts, now, recheck_days)}
        to_fetch = sorted(new_urls | missing | stale)

    embedded = skipped = tombstoned = 0
    if to_fetch:
        log.info("inventory: fetching %d urls", len(to_fetch))
        records = extract.fetch_segments(to_fetch, settings["user_agent"],
                                         settings["request_timeout"])
        pending: list[dict] = []
        tombstones: list[dict] = []
        resurrected: list[str] = []
        for rec in records:
            segments = rec.get("segments")
            if segments is None:
                # the fetch itself failed — no tombstone, retry next run
                log.warning("inventory fetch failed: %s (%s)",
                            rec["url"], rec["error"])
                continue
            if not segments:
                # fetched fine, parsed to nothing (rec["error"] is
                # "no headings found") — mark it processed, don't re-fetch
                tombstones.append(tombstone_row(rec["url"], now))
                continue
            if rec["url"] in inv_tombstoned:
                resurrected.append(rec["url"])
            for seg in segments:
                sid = segment_id(rec["url"], seg["segment_index"])
                chash = hashlib.md5(seg["segment_text"].encode()).hexdigest()
                if inv_hashes.get(sid) == chash:   # unchanged — skip re-embed
                    skipped += 1
                    continue
                pending.append({"id": sid, "url": rec["url"],
                                "segment_type": seg["segment_type"],
                                "segment_text": seg["segment_text"],
                                "content_hash": chash})

        # A resurrected page's sections start at segment_index 1, so its
        # tombstone (index 0) would outlive the upsert unless dropped here.
        for url in resurrected:
            (supabase.table("cm_site_inventory").delete()
             .eq("id", segment_id(url, 0))
             .eq("segment_type", gap.EMPTY_SEGMENT).execute())

        # Mirror case: a page that lost its headings. Clear its stale
        # segments before the tombstone lands on top of segment_index 0.
        for row in tombstones:
            if row["url"] in inv_urls and row["url"] not in inv_tombstoned:
                (supabase.table("cm_site_inventory").delete()
                 .eq("url", row["url"]).execute())

        if pending:
            vecs = gap.embed_texts(model, [p["segment_text"] for p in pending])
            for p, v in zip(pending, vecs):
                p["embedding"] = "\\x" + v.tobytes().hex()   # bytea via REST
            for i in range(0, len(pending), UPSERT_CHUNK):
                (supabase.table("cm_site_inventory")
                 .upsert(pending[i:i + UPSERT_CHUNK]).execute())
            embedded = len(pending)

        if tombstones:
            for i in range(0, len(tombstones), UPSERT_CHUNK):
                (supabase.table("cm_site_inventory")
                 .upsert(tombstones[i:i + UPSERT_CHUNK]).execute())
            tombstoned = len(tombstones)

    # prune segments for URLs gone from the sitemap
    removed = 0
    for gone_url in sorted(inv_urls - sitemap_urls):
        (supabase.table("cm_site_inventory").delete()
         .eq("url", gone_url).execute())
        removed += 1

    stats = {"urls_added": len(to_fetch), "segments_embedded": embedded,
             "segments_skipped": skipped, "tombstoned": tombstoned,
             "removed": removed}
    log.info("inventory refresh: %s", stats)
    return stats


def _main() -> int:
    import argparse

    import yaml
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                        help="force full re-crawl + re-embed")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()

    import os

    from sentence_transformers import SentenceTransformer
    from supabase import create_client

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    supabase = create_client(os.environ["SUPABASE_URL"].rstrip("/"),
                             os.environ["SUPABASE_SERVICE_KEY"])
    model = SentenceTransformer(config["settings"]["embedding_model"])
    refresh(supabase, model, config, full=args.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
