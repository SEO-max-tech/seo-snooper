"""Fetch and filter competitor (and Murf) sitemap URLs.

Uses advertools.sitemap_to_df — handles sitemap indexes, gzip, and news
sitemaps transparently. Filtering (include/exclude regexes from config)
happens HERE so downstream modules only ever see clean URL lists.
"""
from __future__ import annotations
import pandas as pd


def fetch_urls(sitemaps: list[str], include_patterns: list[str],
               exclude_patterns: list[str], user_agent: str) -> pd.DataFrame:
    """Return DataFrame with columns: url (str), lastmod (datetime | NaT).

    - Concatenate all sitemaps for the competitor, dedupe on url.
    - include_patterns: if non-empty, keep only URLs matching ANY pattern.
    - exclude_patterns: drop URLs matching ANY pattern.
    - Strip URL fragments and tracking params; normalize trailing slash.
    - Must not raise on a single unreachable sitemap if others succeed;
      raise only if ALL sitemaps fail.
    """
    raise NotImplementedError  # TODO(M1)


def apply_filters(df: pd.DataFrame, include_patterns: list[str],
                  exclude_patterns: list[str]) -> pd.DataFrame:
    """Pure function, separated for offline testing against fixture XML."""
    raise NotImplementedError  # TODO(M1)
