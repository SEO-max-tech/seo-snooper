"""Offline smoke tests — NO network, NO credentials. One test class per
module, built milestone by milestone. Fixtures live in tests/fixtures/:
  - sitemap_index.xml, sitemap_blog.xml  (M1: index handling + filters)
  - competitor_page.html                  (M3: title/h1/meta extraction)
  - murf_guide.html                       (M3: h1 + h2/h3 segment extraction)

Mock patterns (match the seo-agents repo):
  - supabase: minimal fake exposing .table().upsert/.select with canned data
  - anthropic: lambda/stub client returning fixed JSON (and one malformed
    response to exercise the retry path)
  - ahrefs: StubProvider directly

Must-have assertions:
  - diff first-run guard returns [] and writes status='baseline'
  - gap buckets at sim 0.70 / 0.80 / 0.90 -> gap / partial / covered
  - judge fail-open on double malformed JSON
  - notify.build_card puts sub-min_volume items in footer, not main list
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"

EXCLUDES = [
    "/customers?/",
    "/(de|fr|es|it|pt|ja|ko|zh|hi|nl)/",
]


def _sitemap_df() -> pd.DataFrame:
    """Parse fixture XML into the loc/lastmod shape sitemap_to_df returns."""
    import xml.etree.ElementTree as ET

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.parse(FIXTURES / "sitemap_blog.xml").getroot()
    rows = []
    for url in root.findall("sm:url", ns):
        loc = url.find("sm:loc", ns).text
        lastmod_el = url.find("sm:lastmod", ns)
        rows.append({"loc": loc,
                     "lastmod": lastmod_el.text if lastmod_el is not None else None})
    return pd.DataFrame(rows)


class TestSitemaps:
    def test_fixture_parses(self):
        df = _sitemap_df()
        assert len(df) == 6

    def test_apply_filters_excludes(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(_sitemap_df(), [], EXCLUDES)
        urls = set(out["url"])
        assert not any("/de/" in u or "/customers/" in u for u in urls)

    def test_normalization_dedupes_fragment_and_slash(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(_sitemap_df(), [], [])
        urls = list(out["url"])
        # trailing-slash URL and its #fragment twin collapse to one
        assert urls.count("https://example.com/blog/ai-voice-cloning-guide") == 1
        assert not any("#" in u for u in urls)

    def test_tracking_params_stripped_real_query_kept(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(_sitemap_df(), [], [])
        match = [u for u in out["url"] if "text-to-speech-apps" in u]
        assert match == ["https://example.com/blog/text-to-speech-apps?page=2"]

    def test_include_patterns(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(_sitemap_df(), ["/blog/"], [])
        assert all("/blog/" in u for u in out["url"])
        assert len(out) > 0

    def test_lastmod_parsed_and_nat_ok(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(_sitemap_df(), [], [])
        guide = out[out["url"] == "https://example.com/blog/ai-voice-cloning-guide"]
        assert pd.notna(guide["lastmod"].iloc[0])
        pricing = out[out["url"] == "https://example.com/pricing"]
        assert pd.isna(pricing["lastmod"].iloc[0])

    def test_empty_df(self):
        from tools.sitemaps import apply_filters

        out = apply_filters(pd.DataFrame(), ["/blog/"], EXCLUDES)
        assert out.empty and list(out.columns) == ["url", "lastmod"]
