"""Ahrefs keyword metrics — swappable real/stub provider.

Real: Ahrefs API v3, Bearer AHREFS_API_KEY, keywords-explorer overview,
batched keywords, country from config. Respects ahrefs_max_keywords cap.
Stub: returns deterministic fake metrics (seeded on keyword md5) and logs
loudly that stub data is in use.
"""
from __future__ import annotations

import hashlib
import logging
import os

log = logging.getLogger(__name__)

API_BASE = "https://api.ahrefs.com/v3"
BATCH_SIZE = 100          # keywords per overview request


def get_provider(max_keywords: int = 50):
    key = os.getenv("AHREFS_API_KEY")
    if key:
        return RealProvider(key, max_keywords)
    return StubProvider(max_keywords)


def _empty(keyword: str) -> dict:
    return {"keyword": keyword, "volume": None, "difficulty": None,
            "traffic_potential": None, "cpc": None}


class RealProvider:
    is_stub = False

    def __init__(self, api_key: str, max_keywords: int = 50):
        self.api_key = api_key
        self.max_keywords = max_keywords

    def keyword_overview(self, keywords: list[str], country: str) -> dict[str, dict]:
        """Returns {keyword: {volume, difficulty, traffic_potential, cpc}}.
        Batch into as few requests as the endpoint allows; tenacity retry on
        429/5xx. Keywords beyond the cap are returned with volume=None."""
        import httpx
        from tenacity import (retry, retry_if_exception_type,
                              stop_after_attempt, wait_exponential)

        capped = keywords[:self.max_keywords]
        overflow = keywords[self.max_keywords:]
        if overflow:
            log.warning("ahrefs: %d keywords over cap (%d) returned unenriched",
                        len(overflow), self.max_keywords)

        @retry(retry=retry_if_exception_type(httpx.HTTPStatusError),
               stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=2, max=30),
               reraise=True)
        def _call(client: httpx.Client, batch: list[str]) -> dict:
            resp = client.get(
                f"{API_BASE}/keywords-explorer/overview",
                params={"country": country,
                        "keywords": ",".join(batch),
                        "select": ("keyword,volume_monthly,difficulty,"
                                   "traffic_potential,cpc")},
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()

        out: dict[str, dict] = {kw: _empty(kw) for kw in keywords}
        with httpx.Client(
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Accept": "application/json"},
                timeout=30) as client:
            for i in range(0, len(capped), BATCH_SIZE):
                batch = capped[i:i + BATCH_SIZE]
                data = _call(client, batch)
                for row in data.get("keywords", []):
                    kw = row.get("keyword")
                    if kw in out:
                        out[kw] = {
                            "keyword": kw,
                            "volume": row.get("volume_monthly"),
                            "difficulty": row.get("difficulty"),
                            "traffic_potential": row.get("traffic_potential"),
                            "cpc": row.get("cpc"),
                        }
        return out


class StubProvider:
    is_stub = True

    def __init__(self, max_keywords: int = 50):
        self.max_keywords = max_keywords

    def keyword_overview(self, keywords: list[str], country: str) -> dict[str, dict]:
        log.warning("=== AHREFS STUB PROVIDER — fake keyword data, set "
                    "AHREFS_API_KEY for real metrics ===")
        out: dict[str, dict] = {}
        for i, kw in enumerate(keywords):
            if i >= self.max_keywords:
                out[kw] = _empty(kw)
                continue
            seed = int(hashlib.md5(kw.encode()).hexdigest(), 16)
            out[kw] = {
                "keyword": kw,
                "volume": (seed % 5000) * 10,            # 0..49,990
                "difficulty": seed // 7 % 101,           # 0..100
                "traffic_potential": (seed // 13 % 8000) * 10,
                "cpc": round((seed // 17 % 2000) / 100, 2),
            }
        return out
