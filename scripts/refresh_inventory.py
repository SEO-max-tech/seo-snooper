"""Incremental Murf inventory refresh. Runs before every weekly scan.

1. sitemaps.fetch_urls(config.murf) — Murf's own sitemap, same filters.
2. diff.detect_new(slug='murf') for new URLs; ALSO re-extract URLs whose
   stored segments are missing (recovery from partial failures).
3. extract.fetch_segments() on that delta only.
4. For each segment: content_hash = md5(segment_text). Skip embedding if an
   inventory row with same id and content_hash exists.
5. gap.embed_texts() on changed/new segments, upsert into cm_murf_inventory
   (id = md5(f"{url}|{segment_index}"), embedding as float32 bytes).
6. Delete inventory rows for URLs that vanished from the Murf sitemap.

Callable as a function from main.py AND runnable standalone:
    python scripts/refresh_inventory.py [--full]   # --full forces re-crawl
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import diff, extract, gap, sitemaps  # noqa: E402

log = logging.getLogger(__name__)

MURF_SLUG = "murf"
UPSERT_CHUNK = 200          # embeddings are fat rows — smaller chunks


def segment_id(url: str, segment_index: int) -> str:
    return hashlib.md5(f"{url}|{segment_index}".encode()).hexdigest()


def _inventory_state(supabase) -> tuple[set[str], dict[str, str]]:
    """Returns (urls present in inventory, {row_id: content_hash})."""
    urls: set[str] = set()
    hashes: dict[str, str] = {}
    offset, page = 0, 1000
    while True:
        resp = (supabase.table("cm_murf_inventory")
                .select("id,url,content_hash")
                .range(offset, offset + page - 1).execute())
        rows = resp.data or []
        for r in rows:
            urls.add(r["url"])
            hashes[r["id"]] = r["content_hash"]
        if len(rows) < page:
            return urls, hashes
        offset += page


def refresh(supabase, model, config, full: bool = False) -> dict:
    """Returns {urls_added, segments_embedded, segments_skipped, removed}."""
    murf_cfg = config["murf"]
    settings = config["settings"]

    sitemap_df = sitemaps.fetch_urls(
        [murf_cfg["sitemap"]] if isinstance(murf_cfg["sitemap"], str)
        else murf_cfg["sitemap"],
        murf_cfg.get("include_patterns") or [],
        murf_cfg.get("exclude_patterns") or [],
        settings["user_agent"],
    )
    sitemap_urls = set(sitemap_df["url"])

    new_items = diff.detect_new(supabase, MURF_SLUG, sitemap_df)
    inv_urls, inv_hashes = _inventory_state(supabase)

    if full:
        to_fetch = sorted(sitemap_urls)
    else:
        new_urls = {n["url"] for n in new_items}
        # recovery: URL known in cm_urls but has no inventory segments yet.
        # On the very first run detect_new returns [] (baseline guard), so
        # the missing-set is what actually populates the inventory.
        missing = sitemap_urls - inv_urls
        to_fetch = sorted(new_urls | missing)

    embedded = skipped = 0
    if to_fetch:
        log.info("inventory: fetching %d urls", len(to_fetch))
        records = extract.fetch_segments(to_fetch, settings["user_agent"],
                                         settings["request_timeout"])
        pending: list[dict] = []
        for rec in records:
            if rec.get("error"):
                log.warning("inventory fetch failed: %s (%s)",
                            rec["url"], rec["error"])
                continue
            for seg in rec["segments"]:
                sid = segment_id(rec["url"], seg["segment_index"])
                chash = hashlib.md5(seg["segment_text"].encode()).hexdigest()
                if inv_hashes.get(sid) == chash:   # unchanged — skip re-embed
                    skipped += 1
                    continue
                pending.append({"id": sid, "url": rec["url"],
                                "segment_type": seg["segment_type"],
                                "segment_text": seg["segment_text"],
                                "content_hash": chash})

        if pending:
            vecs = gap.embed_texts(model, [p["segment_text"] for p in pending])
            for p, v in zip(pending, vecs):
                p["embedding"] = "\\x" + v.tobytes().hex()   # bytea via REST
            for i in range(0, len(pending), UPSERT_CHUNK):
                (supabase.table("cm_murf_inventory")
                 .upsert(pending[i:i + UPSERT_CHUNK]).execute())
            embedded = len(pending)

    # prune segments for URLs gone from the sitemap
    removed = 0
    for gone_url in sorted(inv_urls - sitemap_urls):
        (supabase.table("cm_murf_inventory").delete()
         .eq("url", gone_url).execute())
        removed += 1

    stats = {"urls_added": len(to_fetch), "segments_embedded": embedded,
             "segments_skipped": skipped, "removed": removed}
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
