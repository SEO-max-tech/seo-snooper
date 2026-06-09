"""Ahrefs keyword metrics — swappable real/stub provider.

Real: Ahrefs API v3, Bearer AHREFS_API_KEY, keywords-explorer overview,
batched keywords, country from config. Respects ahrefs_max_keywords cap.
Stub: returns deterministic fake metrics (seeded on keyword md5) and logs
loudly that stub data is in use.
"""
from __future__ import annotations
import os


def get_provider():
    return RealProvider() if os.getenv("AHREFS_API_KEY") else StubProvider()


class RealProvider:
    def keyword_overview(self, keywords: list[str], country: str) -> dict[str, dict]:
        """Returns {keyword: {volume, difficulty, traffic_potential, cpc}}.
        Batch into as few requests as the endpoint allows; tenacity retry on
        429/5xx. Keywords beyond the cap are returned with volume=None."""
        raise NotImplementedError  # TODO(M6)


class StubProvider:
    def keyword_overview(self, keywords: list[str], country: str) -> dict[str, dict]:
        raise NotImplementedError  # TODO(M6) deterministic fakes, md5-seeded
