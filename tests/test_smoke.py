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


# ---------------------------------------------------------------- fakes

class FakeQuery:
    """Chainable stand-in for supabase-py's query builder."""

    def __init__(self, store: dict, table: str):
        self.store, self.table_name = store, table
        self._filters: list = []
        self._range = (0, 10**9)
        self._payload = None
        self._op = "select"

    def select(self, *_cols):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, set(vals)))
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def upsert(self, rows):
        self._op, self._payload = "upsert", rows
        return self

    def insert(self, rows):
        self._op, self._payload = "insert", rows
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        rows = self.store.setdefault(self.table_name, [])
        if self._op == "upsert":
            payload = (self._payload if isinstance(self._payload, list)
                       else [self._payload])
            by_id = {r["id"]: r for r in rows if "id" in r}
            for new in payload:
                if new.get("id") in by_id:
                    by_id[new["id"]].update(new)
                else:
                    rows.append(dict(new))
            return type("R", (), {"data": payload})()
        if self._op == "insert":
            payload = (self._payload if isinstance(self._payload, list)
                       else [self._payload])
            for new in payload:
                row = dict(new)
                row.setdefault("id", len(rows) + 1)
                rows.append(row)
            return type("R", (), {"data": [dict(r) for r in payload]})()

        matched = [r for r in rows if all(
            (r.get(c) in v) if isinstance(v, set) else r.get(c) == v
            for c, v in self._filters)]
        if self._op == "delete":
            self.store[self.table_name] = [r for r in rows if r not in matched]
            return type("R", (), {"data": matched})()
        lo, hi = self._range
        return type("R", (), {"data": matched[lo:hi + 1]})()


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return FakeQuery(self.store, name)


# ---------------------------------------------------------------- M2

class TestDiff:
    def _df(self, urls):
        return pd.DataFrame({"url": urls, "lastmod": pd.NaT})

    def test_first_run_guard(self):
        from tools.diff import detect_new

        sb = FakeSupabase()
        new = detect_new(sb, "elevenlabs", self._df(["https://a.com/1",
                                                     "https://a.com/2"]))
        assert new == []
        rows = sb.store["cm_urls"]
        assert len(rows) == 2
        assert all(r["status"] == "baseline" for r in rows)

    def test_second_run_detects_only_new(self):
        from tools.diff import detect_new

        sb = FakeSupabase()
        detect_new(sb, "elevenlabs", self._df(["https://a.com/1"]))
        new = detect_new(sb, "elevenlabs",
                         self._df(["https://a.com/1", "https://a.com/2"]))
        assert [n["url"] for n in new] == ["https://a.com/2"]
        by_url = {r["url"]: r for r in sb.store["cm_urls"]}
        assert by_url["https://a.com/2"]["status"] == "new"
        assert by_url["https://a.com/1"]["status"] == "seen"

    def test_competitors_are_isolated(self):
        from tools.diff import detect_new

        sb = FakeSupabase()
        detect_new(sb, "elevenlabs", self._df(["https://a.com/1"]))
        # same URL, different competitor -> still that competitor's baseline
        new = detect_new(sb, "playht", self._df(["https://a.com/1"]))
        assert new == []
        slugs = {r["competitor_slug"] for r in sb.store["cm_urls"]}
        assert slugs == {"elevenlabs", "playht"}

    def test_stable_ids_no_duplicates_on_rerun(self):
        from tools.diff import detect_new

        sb = FakeSupabase()
        detect_new(sb, "elevenlabs", self._df(["https://a.com/1"]))
        detect_new(sb, "elevenlabs", self._df(["https://a.com/1"]))
        detect_new(sb, "elevenlabs", self._df(["https://a.com/1"]))
        assert len(sb.store["cm_urls"]) == 1
