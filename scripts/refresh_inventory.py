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

def refresh(supabase, model, config, full: bool = False) -> dict:
    """Returns {urls_added, segments_embedded, segments_skipped, removed}."""
    raise NotImplementedError  # TODO(M4)


if __name__ == "__main__":
    raise NotImplementedError  # TODO(M4) argparse + env wiring
