"""Fetch and filter competitor (and Murf) sitemap URLs.

Uses advertools.sitemap_to_df — handles sitemap indexes, gzip, and news
sitemaps transparently. Filtering (include/exclude regexes from config)
happens HERE so downstream modules only ever see clean URL lists.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

log = logging.getLogger(__name__)

# query params stripped during normalization
_TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|gclid|fbclid|msclkid|ref|source|mc_cid|mc_eid)$", re.I
)


def _normalize_url(url: str) -> str:
    """Strip fragment + tracking params; normalize trailing slash on paths."""
    scheme, netloc, path, query, _frag = urlsplit(url.strip())
    if query:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                if not _TRACKING_PARAMS.match(k)]
        query = urlencode(kept)
    # trailing-slash normalization: drop it except for the bare root
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def apply_filters(df: pd.DataFrame, include_patterns: list[str],
                  exclude_patterns: list[str]) -> pd.DataFrame:
    """Pure function, separated for offline testing against fixture XML.

    Expects a 'loc' or 'url' column; returns DataFrame[url, lastmod] with
    normalized, deduped URLs.
    """
    if df.empty:
        return pd.DataFrame(columns=["url", "lastmod"])

    url_col = "url" if "url" in df.columns else "loc"
    out = pd.DataFrame({
        "url": df[url_col].astype(str).map(_normalize_url),
        "lastmod": pd.to_datetime(df["lastmod"], errors="coerce", utc=True)
        if "lastmod" in df.columns else pd.NaT,
    })

    if include_patterns:
        inc = re.compile("|".join(include_patterns))
        out = out[out["url"].map(lambda u: bool(inc.search(u)))]
    if exclude_patterns:
        exc = re.compile("|".join(exclude_patterns))
        out = out[~out["url"].map(lambda u: bool(exc.search(u)))]

    out = out.drop_duplicates(subset="url").reset_index(drop=True)
    return out


def fetch_urls(sitemaps: list[str], include_patterns: list[str],
               exclude_patterns: list[str], user_agent: str) -> pd.DataFrame:
    """Return DataFrame with columns: url (str), lastmod (datetime | NaT).

    Concatenates all sitemaps for the competitor; a single unreachable
    sitemap is logged and skipped — raises only if ALL sitemaps fail.
    """
    import advertools as adv

    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for sm in sitemaps:
        try:
            frames.append(adv.sitemap_to_df(sm))
        except Exception as exc:  # noqa: BLE001 — per-sitemap isolation
            log.warning("sitemap fetch failed: %s (%s)", sm, exc)
            errors.append(f"{sm}: {exc}")

    if not frames:
        raise RuntimeError(f"all sitemaps failed: {'; '.join(errors)}")

    df = pd.concat(frames, ignore_index=True)
    return apply_filters(df, include_patterns, exclude_patterns)
