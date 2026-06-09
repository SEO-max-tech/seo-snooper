"""Semantic gap check: competitor topic vs Murf inventory.

Embeds with sentence-transformers all-MiniLM-L6-v2 (384d). Loads the FULL
cm_murf_inventory into a normalized numpy matrix once per run; cosine is
matrix @ query. Max over all segments = the score.
"""
from __future__ import annotations
import numpy as np


class Inventory:
    """Holds (matrix: np.ndarray[N,384] normalized, meta: list[{url,
    segment_type, segment_text}]) loaded from Supabase."""

    @classmethod
    def load(cls, supabase) -> "Inventory":
        raise NotImplementedError  # TODO(M4)


def check(model, inventory: Inventory, items: list[dict],
          gap_threshold: float, partial_threshold: float) -> list[dict]:
    """items: competitor records with topic_text.
    Adds: similarity (float), bucket ('gap'|'partial'|'covered'),
    nearest_murf_url, nearest_segment_text.
    'covered' items are returned too (caller drops them) so smoke tests can
    assert all three buckets.
    """
    raise NotImplementedError  # TODO(M4)


def embed_texts(model, texts: list[str]) -> np.ndarray:
    """Normalized float32 embeddings. Shared by gap check and inventory refresh."""
    raise NotImplementedError  # TODO(M4)
