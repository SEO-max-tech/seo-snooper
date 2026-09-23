"""Semantic gap check: competitor topic vs your own site inventory.

Embeds with sentence-transformers all-MiniLM-L6-v2 (384d). Loads the FULL
cm_site_inventory into a normalized numpy matrix once per run; cosine is
matrix @ query. Max over all segments = the score.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

DIM = 384


class Inventory:
    """Holds (matrix: np.ndarray[N,384] normalized, meta: list[{url,
    segment_type, segment_text}]) loaded from Supabase."""

    def __init__(self, matrix: np.ndarray, meta: list[dict]):
        self.matrix = matrix
        self.meta = meta

    def __len__(self) -> int:
        return len(self.meta)

    @classmethod
    def load(cls, supabase) -> "Inventory":
        rows: list[dict] = []
        offset, page = 0, 1000
        while True:
            resp = (supabase.table("cm_site_inventory")
                    .select("url,segment_type,segment_text,embedding")
                    .range(offset, offset + page - 1).execute())
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page

        if not rows:
            return cls(np.empty((0, DIM), dtype=np.float32), [])

        vecs = np.vstack([_decode_embedding(r["embedding"]) for r in rows])
        # normalize defensively — stored vectors should already be unit-norm
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = (vecs / norms).astype(np.float32)
        meta = [{"url": r["url"], "segment_type": r["segment_type"],
                 "segment_text": r["segment_text"]} for r in rows]
        log.info("inventory loaded: %d vectors", len(meta))
        return cls(matrix, meta)


def _decode_embedding(raw) -> np.ndarray:
    """bytea comes back as bytes locally, hex string ('\\x...') via REST."""
    if isinstance(raw, str):
        raw = bytes.fromhex(raw[2:] if raw.startswith("\\x") else raw)
    return np.frombuffer(raw, dtype=np.float32)


def embed_texts(model, texts: list[str]) -> np.ndarray:
    """Normalized float32 embeddings. Shared by gap check and inventory refresh."""
    vecs = np.asarray(model.encode(texts), dtype=np.float32)
    if vecs.ndim == 1:
        vecs = vecs.reshape(1, -1)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype(np.float32)


def check(model, inventory: Inventory, items: list[dict],
          gap_threshold: float, partial_threshold: float) -> list[dict]:
    """items: competitor records with topic_text.
    Adds: similarity (float), bucket ('gap'|'partial'|'covered'),
    nearest_site_url, nearest_segment_text.
    'covered' items are returned too (caller drops them) so smoke tests can
    assert all three buckets.
    """
    items = [dict(it) for it in items]
    if not items:
        return []

    if len(inventory) == 0:
        # empty inventory: everything is a gap by definition
        for it in items:
            it.update(similarity=0.0, bucket="gap",
                      nearest_site_url=None, nearest_segment_text=None)
        return items

    queries = embed_texts(model, [it["topic_text"] for it in items])
    sims = inventory.matrix @ queries.T          # [N_inventory, N_items]

    for col, it in enumerate(items):
        best = int(np.argmax(sims[:, col]))
        sim = float(sims[best, col])
        if sim < gap_threshold:
            bucket = "gap"
        elif sim < partial_threshold:
            bucket = "partial"
        else:
            bucket = "covered"
        it.update(similarity=round(sim, 4), bucket=bucket,
                  nearest_site_url=inventory.meta[best]["url"],
                  nearest_segment_text=inventory.meta[best]["segment_text"])
    return items
