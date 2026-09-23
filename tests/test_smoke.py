"""Offline smoke tests — NO network, NO credentials. One test class per
module, built milestone by milestone. Fixtures live in tests/fixtures/:
  - sitemap_index.xml, sitemap_blog.xml  (M1: index handling + filters)
  - competitor_page.html                  (M3: title/h1/meta extraction)
  - site_guide.html                       (M3: h1 + h2/h3 segment extraction)

Mock patterns (match the seo-agents repo):
  - supabase: minimal fake exposing .table().upsert/.select with canned data
  - anthropic: lambda/stub client returning fixed JSON (and one malformed
    response to exercise the retry path)
  - ahrefs: StubProvider directly

Must-have assertions:
  - diff first-run guard returns [] and writes status='baseline'
  - gap buckets at sim 0.70 / 0.80 / 0.90 -> gap / partial / covered
  - judge fail-open on double malformed JSON
  - notify builders put sub-min_volume items in footer, not main list
"""
from __future__ import annotations

import json
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
            inserted = []
            for new in payload:
                row = dict(new)
                row.setdefault("id", len(rows) + 1)
                rows.append(row)
                inserted.append(dict(row))
            return type("R", (), {"data": inserted})()

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
        self.fetched_urls: list[list[str]] = []   # one entry per refresh run

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


# ---------------------------------------------------------------- M3

class TestExtract:
    def test_competitor_meta(self):
        from tools.extract import parse_meta

        html = (FIXTURES / "competitor_page.html").read_text()
        rec = parse_meta(html, "https://examplelabs.com/blog/best")
        assert rec["error"] is None
        assert rec["title"].startswith("10 Best AI Voice Generators")
        # h1 from <main>, not the nav h2
        assert rec["h1"] == "The 10 Best AI Voice Generators (Tested in 2026)"
        assert rec["meta_description"].startswith("We tested")
        # brand segment 'ExampleLabs' stripped from the title; h1 kept
        assert "ExampleLabs" not in rec["topic_text"]
        assert "10 Best AI Voice Generators in 2026" in rec["topic_text"]
        assert rec["h1"] in rec["topic_text"]

    def test_title_cleaning_strips_brand_and_dedupes(self):
        from tools.extract import _page_topic

        # the real Bolna case that mis-bucketed as 'partial'
        title = ("AI Voice Agents for Healthcare | Patient Calls & Scheduling "
                 "| Bolna | Bolna Voice AI")
        h1 = "AI Voice Agents for Healthcare"
        topic = _page_topic(title, h1, "https://www.bolna.ai/healthcare")
        assert "Bolna" not in topic                     # brand gone
        assert topic.startswith("AI Voice Agents for Healthcare")
        # h1 duplicate of lead segment collapsed, descriptor retained
        assert topic == ("AI Voice Agents for Healthcare — "
                         "Patient Calls & Scheduling")

    def test_title_cleaning_all_brand_falls_back(self):
        from tools.extract import _page_topic

        # pathological: title is only the brand — keep the lead, use h1
        topic = _page_topic("Bolna | Bolna Voice AI", "Pricing",
                            "https://www.bolna.ai/pricing")
        assert "Pricing" in topic

    def test_inventory_segments(self):
        from tools.extract import parse_segments

        html = (FIXTURES / "site_guide.html").read_text()
        rec = parse_segments(html, "https://acme.example/resources/tts-guide")
        assert rec["error"] is None
        segs = rec["segments"]
        assert segs[0]["segment_type"] == "page"
        assert segs[0]["segment_index"] == 0
        assert "Complete Guide to Text to Speech" in segs[0]["segment_text"]
        sections = [s for s in segs if s["segment_type"] == "section"]
        assert len(sections) == 3
        # h3s appended to their h2
        assert sections[0]["segment_text"] == (
            "How text to speech works: Neural TTS models / Voice synthesis pipeline")
        assert sections[1]["segment_text"] == (
            "Best use cases for TTS: E-learning narration")
        # h2 with no h3s stays bare
        assert sections[2]["segment_text"] == "Choosing a TTS voice"
        # nav h2 ("Site navigation") never appears — main-scope only
        assert all("navigation" not in s["segment_text"].lower() for s in segs)

    def test_no_headings_is_error_record(self):
        from tools.extract import parse_meta

        rec = parse_meta("<html><body><p>nothing</p></body></html>",
                         "https://example.com/empty")
        assert rec["error"] is not None
        assert rec["topic_text"] == ""


# ---------------------------------------------------------------- M4

import numpy as np  # noqa: E402


class FakeModel:
    """Deterministic 384d 'embeddings': text -> preset vector, else basis
    vector seeded on the text hash."""

    def __init__(self, preset: dict[str, np.ndarray] | None = None):
        self.preset = preset or {}

    def encode(self, texts):
        out = []
        for t in texts:
            if t in self.preset:
                out.append(self.preset[t])
            else:
                v = np.zeros(384, dtype=np.float32)
                v[hash(t) % 384] = 1.0
                out.append(v)
        return np.vstack(out)


def _vec_at_cosine(base: np.ndarray, cos: float) -> np.ndarray:
    """Unit vector at exactly `cos` similarity to unit `base` (along dim 1)."""
    ortho = np.zeros(384, dtype=np.float32)
    ortho[1] = 1.0
    return (cos * base + np.sqrt(1 - cos**2) * ortho).astype(np.float32)


class TestGap:
    def _inventory(self):
        from tools.gap import Inventory

        base = np.zeros(384, dtype=np.float32)
        base[0] = 1.0
        meta = [{"url": "https://acme.example/resources/tts-guide",
                 "segment_type": "section",
                 "segment_text": "How text to speech works"}]
        return Inventory(base.reshape(1, -1), meta), base

    def test_buckets_at_thresholds(self):
        from tools.gap import check

        inv, base = self._inventory()
        preset = {
            "gap topic": _vec_at_cosine(base, 0.70),
            "partial topic": _vec_at_cosine(base, 0.80),
            "covered topic": _vec_at_cosine(base, 0.90),
        }
        model = FakeModel(preset)
        items = [{"url": f"https://c.com/{k}", "topic_text": k}
                 for k in preset]
        out = check(model, inv, items, 0.75, 0.85)
        buckets = {it["topic_text"]: it["bucket"] for it in out}
        assert buckets == {"gap topic": "gap",
                           "partial topic": "partial",
                           "covered topic": "covered"}
        sims = {it["topic_text"]: it["similarity"] for it in out}
        assert abs(sims["partial topic"] - 0.80) < 1e-3

    def test_nearest_site_url_reported(self):
        from tools.gap import check

        inv, base = self._inventory()
        model = FakeModel({"partial topic": _vec_at_cosine(base, 0.80)})
        out = check(model, inv,
                    [{"url": "https://c.com/x", "topic_text": "partial topic"}],
                    0.75, 0.85)
        assert out[0]["nearest_site_url"] == "https://acme.example/resources/tts-guide"
        assert out[0]["nearest_segment_text"] == "How text to speech works"

    def test_max_over_segments(self):
        """Competitor page must match the BEST segment, not the page vector."""
        from tools.gap import Inventory, check

        page_vec = np.zeros(384, dtype=np.float32); page_vec[5] = 1.0
        sect_vec = np.zeros(384, dtype=np.float32); sect_vec[0] = 1.0
        inv = Inventory(np.vstack([page_vec, sect_vec]), [
            {"url": "https://acme.example/g", "segment_type": "page",
             "segment_text": "Guide"},
            {"url": "https://acme.example/g", "segment_type": "section",
             "segment_text": "Matching section"},
        ])
        model = FakeModel({"topic": _vec_at_cosine(sect_vec, 0.90)})
        out = check(model, inv, [{"url": "https://c.com/t",
                                  "topic_text": "topic"}], 0.75, 0.85)
        assert out[0]["bucket"] == "covered"
        assert out[0]["nearest_segment_text"] == "Matching section"

    def test_empty_inventory_everything_gap(self):
        from tools.gap import Inventory, check

        inv = Inventory(np.empty((0, 384), dtype=np.float32), [])
        out = check(FakeModel(), inv,
                    [{"url": "https://c.com/x", "topic_text": "anything"}],
                    0.75, 0.85)
        assert out[0]["bucket"] == "gap" and out[0]["similarity"] == 0.0

    def test_embedding_roundtrip_via_bytes(self):
        """Storage encode/decode: float32 bytes -> hex -> matrix row."""
        from tools.gap import Inventory, embed_texts

        model = FakeModel()
        vec = embed_texts(model, ["roundtrip"])[0]
        sb = FakeSupabase()
        sb.store["cm_site_inventory"] = [{
            "id": "x", "url": "https://acme.example/p", "segment_type": "page",
            "segment_text": "roundtrip", "content_hash": "h",
            "embedding": "\\x" + vec.tobytes().hex(),
        }]
        inv = Inventory.load(sb)
        assert len(inv) == 1
        assert (inv.matrix @ vec).item() == pytest.approx(1.0)


class TestRefreshInventory:
    def _run(self, sb, monkeypatch, sitemap_urls, fetched, full=False,
             settings=None):
        import scripts.refresh_inventory as ri

        monkeypatch.setattr(ri.sitemaps, "fetch_urls",
                            lambda *a, **k: pd.DataFrame(
                                {"url": sitemap_urls, "lastmod": pd.NaT}))
        def _fetch_segments(urls, *a, **k):
            sb.fetched_urls.append(list(urls))
            return [fetched[u] for u in urls]

        monkeypatch.setattr(ri.extract, "fetch_segments", _fetch_segments)
        config = {"site": {"slug": "acme",
                           "sitemap": "https://acme.example/sitemap.xml",
                           "include_patterns": [], "exclude_patterns": []},
                  "settings": {"user_agent": "t", "request_timeout": 5,
                               **(settings or {})}}
        return ri.refresh(sb, FakeModel(), config, full=full)

    @staticmethod
    def _age_rows(sb, days):
        """Backdate every inventory row — simulates time passing between
        weekly runs without monkeypatching the clock."""
        import datetime as dt

        then = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(days=days)).isoformat()
        for row in sb.store["cm_site_inventory"]:
            row["updated_at"] = then

    def test_first_run_populates_then_incremental_noop(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://acme.example/a": {
            "url": "https://acme.example/a", "error": None,
            "segments": [{"segment_index": 0, "segment_type": "page",
                          "segment_text": "A page"}]}}
        stats = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched)
        assert stats["segments_embedded"] == 1
        assert len(sb.store["cm_site_inventory"]) == 1

        # incremental rerun: URL known + inventoried -> not even fetched
        stats2 = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched)
        assert stats2["urls_added"] == 0
        assert stats2["segments_embedded"] == 0

        # full rerun: re-fetched, but unchanged hash -> skip re-embed
        stats3 = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched,
                           full=True)
        assert stats3["segments_embedded"] == 0
        assert stats3["segments_skipped"] == 1

        # content change -> re-embed under same stable id
        fetched["https://acme.example/a"]["segments"][0]["segment_text"] = "A page v2"
        stats4 = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched,
                           full=True)
        assert stats4["segments_embedded"] == 1
        assert len(sb.store["cm_site_inventory"]) == 1   # upsert, no dupe

    def test_vanished_url_pruned(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {
            "https://acme.example/a": {"url": "https://acme.example/a", "error": None,
                                  "segments": [{"segment_index": 0,
                                                "segment_type": "page",
                                                "segment_text": "A"}]},
            "https://acme.example/b": {"url": "https://acme.example/b", "error": None,
                                  "segments": [{"segment_index": 0,
                                                "segment_type": "page",
                                                "segment_text": "B"}]},
        }
        self._run(sb, monkeypatch, ["https://acme.example/a", "https://acme.example/b"],
                  fetched)
        assert len(sb.store["cm_site_inventory"]) == 2

        stats = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched)
        assert stats["removed"] == 1
        urls = {r["url"] for r in sb.store["cm_site_inventory"]}
        assert urls == {"https://acme.example/a"}

    def test_failed_fetch_does_not_kill_batch(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {
            "https://acme.example/ok": {"url": "https://acme.example/ok", "error": None,
                                   "segments": [{"segment_index": 0,
                                                 "segment_type": "page",
                                                 "segment_text": "OK"}]},
            "https://acme.example/bad": {"url": "https://acme.example/bad",
                                    "error": "HTTP 500"},
        }
        stats = self._run(sb, monkeypatch,
                          ["https://acme.example/ok", "https://acme.example/bad"], fetched)
        assert stats["segments_embedded"] == 1
        # a failed fetch is NOT tombstoned — it must be retried next run
        assert stats["tombstoned"] == 0
        self._run(sb, monkeypatch,
                  ["https://acme.example/ok", "https://acme.example/bad"], fetched)
        assert "https://acme.example/bad" in sb.fetched_urls[-1]

    # ---- headless pages (no title/h1 and no h2s) --------------------

    HEADLESS = {"url": "https://acme.example/bare", "error": "no headings found",
                "segments": []}

    def test_headless_url_tombstoned_and_not_refetched(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://acme.example/bare": self.HEADLESS}

        stats = self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched)
        assert stats["tombstoned"] == 1
        assert stats["segments_embedded"] == 0
        rows = sb.store["cm_site_inventory"]
        assert len(rows) == 1 and rows[0]["segment_type"] == "empty"

        # second run: known-processed, so not fetched at all
        stats2 = self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched)
        assert stats2["urls_added"] == 0
        assert len(sb.fetched_urls) == 1           # never fetched a second time
        assert len(sb.store["cm_site_inventory"]) == 1

    def test_tombstone_excluded_from_similarity_matrix(self, monkeypatch):
        from tools.gap import Inventory, check

        sb = FakeSupabase()
        fetched = {"https://acme.example/bare": self.HEADLESS,
                   "https://acme.example/a": {
                       "url": "https://acme.example/a", "error": None,
                       "segments": [{"segment_index": 0, "segment_type": "page",
                                     "segment_text": "A page"}]}}
        self._run(sb, monkeypatch,
                  ["https://acme.example/bare", "https://acme.example/a"], fetched)
        assert len(sb.store["cm_site_inventory"]) == 2

        inv = Inventory.load(sb)
        assert len(inv) == 1                       # tombstone filtered out
        assert inv.meta[0]["url"] == "https://acme.example/a"
        out = check(FakeModel(), inv, [{"topic_text": "unrelated"}], 0.75, 0.85)
        assert out[0]["nearest_site_url"] == "https://acme.example/a"

    def test_tombstone_rechecked_on_cadence_and_cleared_on_headings(
            self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://acme.example/bare": self.HEADLESS}
        self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched,
                  settings={"inventory_recheck_days": 30})

        # 10 days later: still inside the cadence, still not fetched
        self._age_rows(sb, 10)
        stats = self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched,
                          settings={"inventory_recheck_days": 30})
        assert stats["urls_added"] == 0

        # 40 days later: re-checked, and it has since gained headings
        self._age_rows(sb, 40)
        fetched["https://acme.example/bare"] = {
            "url": "https://acme.example/bare", "error": None,
            "segments": [{"segment_index": 0, "segment_type": "page",
                          "segment_text": "Bare page, now titled"}]}
        stats = self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched,
                          settings={"inventory_recheck_days": 30})
        assert stats["urls_added"] == 1
        assert stats["segments_embedded"] == 1
        rows = sb.store["cm_site_inventory"]
        assert len(rows) == 1                      # tombstone replaced, no dupe
        assert rows[0]["segment_type"] == "page"

        # and it stays a normal inventory row from then on
        stats = self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched,
                          settings={"inventory_recheck_days": 30})
        assert stats["urls_added"] == 0

    def test_tombstone_cleared_when_only_sections_appear(self, monkeypatch):
        """Sections start at segment_index 1, so the index-0 tombstone would
        survive the upsert unless it is deleted explicitly."""
        sb = FakeSupabase()
        fetched = {"https://acme.example/bare": self.HEADLESS}
        self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched)

        self._age_rows(sb, 60)
        fetched["https://acme.example/bare"] = {
            "url": "https://acme.example/bare", "error": None,
            "segments": [{"segment_index": 1, "segment_type": "section",
                          "segment_text": "A new H2"}]}
        self._run(sb, monkeypatch, ["https://acme.example/bare"], fetched)
        types = [r["segment_type"] for r in sb.store["cm_site_inventory"]]
        assert types == ["section"]

    def test_page_that_loses_its_headings_is_tombstoned_cleanly(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://acme.example/a": {
            "url": "https://acme.example/a", "error": None,
            "segments": [{"segment_index": 0, "segment_type": "page",
                          "segment_text": "A page"},
                         {"segment_index": 1, "segment_type": "section",
                          "segment_text": "A section"}]}}
        self._run(sb, monkeypatch, ["https://acme.example/a"], fetched)
        assert len(sb.store["cm_site_inventory"]) == 2

        fetched["https://acme.example/a"] = {"url": "https://acme.example/a",
                                             "error": "no headings found",
                                             "segments": []}
        stats = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched,
                          full=True)
        assert stats["tombstoned"] == 1
        rows = sb.store["cm_site_inventory"]
        assert len(rows) == 1 and rows[0]["segment_type"] == "empty"

    def test_tombstone_pruned_when_url_leaves_sitemap(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://acme.example/bare": self.HEADLESS,
                   "https://acme.example/a": {
                       "url": "https://acme.example/a", "error": None,
                       "segments": [{"segment_index": 0, "segment_type": "page",
                                     "segment_text": "A page"}]}}
        self._run(sb, monkeypatch,
                  ["https://acme.example/bare", "https://acme.example/a"], fetched)
        assert len(sb.store["cm_site_inventory"]) == 2

        stats = self._run(sb, monkeypatch, ["https://acme.example/a"], fetched)
        assert stats["removed"] == 1
        urls = {r["url"] for r in sb.store["cm_site_inventory"]}
        assert urls == {"https://acme.example/a"}


# ---------------------------------------------------------------- M5

GOOD_JSON = ('{"in_scope": true, "reason": "voice topic", '
             '"target_keyword": "ai voice generator", '
             '"suggested_page_type": "listicle", "confidence": "high"}')

SITE = {"slug": "acme", "name": "Acme",
        "description": "an AI voice platform"}

TAXONOMY = {"in_scope": ["text to speech", "voice cloning"],
            "out_of_scope": ["AI video generation", "music generation"]}

ITEM = {"topic_text": "Best AI Voice Generators — tested",
        "url": "https://c.com/best-voices", "bucket": "partial",
        "nearest_segment_text": "How text to speech works"}


class FakeAnthropicClient:
    """Returns queued raw responses in order; records prompts."""

    def __init__(self, responses: list[str]):
        self._queue = list(responses)
        self.prompts: list[str] = []
        outer = self

        class _Messages:
            def create(self, model, max_tokens, messages):
                outer.prompts.append(messages[0]["content"])
                raw = outer._queue.pop(0)
                block = type("B", (), {"text": raw})()
                return type("R", (), {"content": [block]})()

        self.messages = _Messages()


class TestJudge:
    def test_valid_json_passthrough(self):
        from tools.judge import evaluate

        client = FakeAnthropicClient([GOOD_JSON])
        out = evaluate(client, "claude-haiku-4-5", SITE, TAXONOMY, ITEM)
        assert out["in_scope"] is True
        assert out["target_keyword"] == "ai voice generator"
        assert out["suggested_page_type"] == "listicle"
        # original item fields preserved
        assert out["url"] == ITEM["url"] and out["bucket"] == "partial"

    def test_taxonomy_injected_not_hardcoded(self):
        from tools.judge import evaluate

        client = FakeAnthropicClient([GOOD_JSON])
        evaluate(client, "m", SITE, TAXONOMY, ITEM)
        prompt = client.prompts[0]
        assert "- voice cloning" in prompt
        assert "- music generation" in prompt
        assert "{in_scope_taxonomy}" not in prompt
        assert ITEM["topic_text"] in prompt
        # brand injected from config, not baked into the prompt file
        assert "{brand_name}" not in prompt
        assert "strategist for Acme, an AI voice platform" in prompt
        # the prompt FILE must stay brand-free — identity comes from config
        from tools.judge import PROMPT_PATH
        assert "Acme" not in PROMPT_PATH.read_text()
        # partial bucket -> nearest-content context included
        assert "How text to speech works" in prompt

    def test_retry_once_then_success(self):
        from tools.judge import evaluate

        client = FakeAnthropicClient(["not json at all", GOOD_JSON])
        out = evaluate(client, "m", SITE, TAXONOMY, ITEM)
        assert len(client.prompts) == 2
        assert out["in_scope"] is True
        assert out["reason"] == "voice topic"

    def test_fail_open_on_double_malformed(self):
        from tools.judge import evaluate

        client = FakeAnthropicClient(["garbage", '{"in_scope": "yes"}'])
        out = evaluate(client, "m", SITE, TAXONOMY, ITEM)
        assert len(client.prompts) == 2          # exactly one retry
        assert out["in_scope"] is True
        assert out["confidence"] == "low"
        assert out["reason"] == "judge_parse_failure"

    def test_markdown_fenced_json_tolerated(self):
        from tools.judge import evaluate

        client = FakeAnthropicClient(["```json\n" + GOOD_JSON + "\n```"])
        out = evaluate(client, "m", SITE, TAXONOMY, ITEM)
        assert out["target_keyword"] == "ai voice generator"

    def test_invalid_enum_rejected(self):
        from tools.judge import _parse

        bad = GOOD_JSON.replace("listicle", "video")
        assert _parse(bad) is None


# ---------------------------------------------------------------- M6

class TestAhrefs:
    def test_stub_selected_without_key(self, monkeypatch):
        from tools.ahrefs import StubProvider, get_provider

        monkeypatch.delenv("AHREFS_API_KEY", raising=False)
        assert isinstance(get_provider(), StubProvider)

    def test_real_selected_with_key(self, monkeypatch):
        from tools.ahrefs import RealProvider, get_provider

        monkeypatch.setenv("AHREFS_API_KEY", "k")
        assert isinstance(get_provider(), RealProvider)

    def test_stub_deterministic_and_complete(self):
        from tools.ahrefs import StubProvider

        stub = StubProvider(max_keywords=50)
        kws = ["ai voice generator", "voice cloning software"]
        a = stub.keyword_overview(kws, "us")
        b = stub.keyword_overview(kws, "us")
        assert a == b                       # deterministic
        assert set(a) == set(kws)           # every keyword present
        row = a["ai voice generator"]
        assert isinstance(row["volume"], int)
        assert 0 <= row["difficulty"] <= 100

    def test_cap_returns_unenriched_overflow(self):
        from tools.ahrefs import StubProvider

        stub = StubProvider(max_keywords=2)
        kws = [f"kw {i}" for i in range(5)]
        out = stub.keyword_overview(kws, "us")
        assert len(out) == 5
        enriched = [k for k, v in out.items() if v["volume"] is not None]
        assert len(enriched) == 2
        assert out["kw 4"]["volume"] is None


# ---------------------------------------------------------------- M7

def _alert_item(**over):
    base = {"competitor_slug": "elevenlabs", "competitor_name": "ElevenLabs",
            "url": "https://elevenlabs.io/blog/x", "topic_text": "Topic X",
            "bucket": "gap", "similarity": 0.55, "nearest_site_url": None,
            "target_keyword": "topic x", "volume": 900,
            "keyword_difficulty": 30, "suggested_page_type": "blog"}
    base.update(over)
    return base


class TestNotify:
    """Every channel is built from the same partition()/_facts() shaping, so
    each builder is checked for its own payload shape plus the shared rules
    (grouping, volume sort, low-volume footer, partial context)."""

    ITEMS = None  # set per-test

    def _items(self):
        return [
            _alert_item(),
            _alert_item(url="https://elevenlabs.io/blog/y",
                        topic_text="Topic Y", volume=5000),
            _alert_item(competitor_slug="playht", competitor_name="PlayHT",
                        url="https://play.ht/blog/z", topic_text="Topic Z"),
        ]

    # ---- Google Chat -------------------------------------------------
    def test_gchat_shape_and_grouping(self):
        from tools.notify import build_gchat_card

        card = build_gchat_card({"totals": {"new": 12, "gaps": 3}},
                                self._items(), 100)
        assert "cardsV2" in card
        sections = card["cardsV2"][0]["card"]["sections"]
        headers = [s.get("header", "") for s in sections]
        assert any("ElevenLabs — 2" in h for h in headers)
        assert any("PlayHT — 1" in h for h in headers)
        # volume sort within competitor: 5000 first
        el = next(s for s in sections if "ElevenLabs" in s.get("header", ""))
        assert "Topic Y" in json.dumps(el["widgets"][0])

    def test_gchat_escapes_html_in_titles(self):
        from tools.notify import build_gchat_card

        item = _alert_item(topic_text="Tips & Tricks <b>2025</b>")
        text = json.dumps(build_gchat_card({"totals": {}}, [item], 100))
        assert "Tips &amp; Tricks" in text
        assert "<b>2025</b>" not in text        # user text never raw HTML

    # ---- Slack -------------------------------------------------------
    def test_slack_block_kit_shape(self):
        from tools.notify import build_slack_blocks

        payload = build_slack_blocks({"totals": {"new": 12, "gaps": 3}},
                                     self._items(), 100)
        assert payload["text"]                  # notification fallback text
        blocks = payload["blocks"]
        assert blocks[0]["type"] == "header"
        assert len(blocks) <= 50
        assert {b["type"] for b in blocks} <= {
            "header", "context", "divider", "section"}
        text = "\n".join(b.get("text", {}).get("text", "")
                         for b in blocks if b["type"] == "section")
        assert "*ElevenLabs* — 2 topic(s)" in text
        assert "<https://elevenlabs.io/blog/y|Topic Y>" in text

    def test_slack_escapes_mrkdwn_specials(self):
        from tools.notify import build_slack_blocks

        item = _alert_item(topic_text="A < B & C > D")
        text = json.dumps(build_slack_blocks({"totals": {}}, [item], 100))
        assert "A &lt; B &amp; C &gt; D" in text

    # ---- Discord -----------------------------------------------------
    def test_discord_embed_shape(self):
        from tools.notify import build_discord_payload

        payload = build_discord_payload({"totals": {"new": 12}},
                                        self._items(), 100)
        assert payload["content"]
        embeds = payload["embeds"]
        assert len(embeds) <= 10
        titles = [e["title"] for e in embeds]
        assert any("ElevenLabs — 2" in x for x in titles)
        assert all(len(e["description"]) <= 4096 for e in embeds)
        assert "[Topic Y](https://elevenlabs.io/blog/y)" in json.dumps(payload)

    # ---- console -----------------------------------------------------
    def test_text_report_readable(self):
        from tools.notify import build_text

        out = build_text({"totals": {"new": 12, "gaps": 3}}, self._items(), 100)
        assert "ElevenLabs — 2 topic(s)" in out
        assert "https://elevenlabs.io/blog/y" in out
        assert out.index("Topic Y") < out.index("Topic X")   # volume sort

    # ---- rules shared by every channel -------------------------------
    @pytest.mark.parametrize("builder_name", [
        "build_gchat_card", "build_slack_blocks", "build_discord_payload",
        "build_text"])
    def test_low_volume_goes_to_footer(self, builder_name):
        import tools.notify as n

        builder = getattr(n, builder_name)
        items = [_alert_item(), _alert_item(topic_text="Tiny topic",
                                            url="https://e.io/t", volume=40)]
        out = builder({"totals": {}}, items, 100)
        text = out if isinstance(out, str) else json.dumps(out)
        assert "1 low-volume topic" in text
        # the suppressed item's title never appears in the body
        assert "Tiny topic" not in text

    @pytest.mark.parametrize("builder_name", [
        "build_gchat_card", "build_slack_blocks", "build_discord_payload",
        "build_text"])
    def test_partial_shows_similarity_and_nearest(self, builder_name):
        import tools.notify as n

        items = [_alert_item(bucket="partial", similarity=0.81,
                             nearest_site_url="https://acme.example/guide")]
        out = getattr(n, builder_name)({"totals": {}}, items, 100)
        text = out if isinstance(out, str) else json.dumps(out)
        assert "0.81" in text and "https://acme.example/guide" in text

    @pytest.mark.parametrize("builder_name", [
        "build_gchat_card", "build_slack_blocks", "build_discord_payload",
        "build_text"])
    def test_empty_run_friendly_message(self, builder_name):
        import tools.notify as n

        out = getattr(n, builder_name)({"totals": {}}, [], 100)
        text = out if isinstance(out, str) else json.dumps(out)
        assert "No new in-scope content gaps" in text

    @pytest.mark.parametrize("builder_name", [
        "build_gchat_card", "build_slack_blocks", "build_discord_payload",
        "build_text"])
    def test_stub_data_flagged(self, builder_name):
        import tools.notify as n

        out = getattr(n, builder_name)({"totals": {}, "stub_data": True},
                                       [_alert_item()], 100)
        text = out if isinstance(out, str) else json.dumps(out)
        assert "STUB data" in text

    def test_enrichment_failure_supersedes_stub_note(self):
        from tools.notify import build_text

        out = build_text({"totals": {}, "stub_data": True,
                          "enrichment_failed": True}, [_alert_item()], 0)
        assert "Keyword lookup failed" in out
        assert "STUB data" not in out

    def test_truncation_reports_overflow(self):
        from tools.notify import MAX_ITEMS_PER_COMPETITOR, build_text

        items = [_alert_item(url=f"https://e.io/{i}", topic_text=f"T{i}",
                             volume=1000 + i)
                 for i in range(MAX_ITEMS_PER_COMPETITOR + 3)]
        out = build_text({"totals": {}}, items, 100)
        assert "3 more topic(s) truncated" in out

    # ---- dispatch ----------------------------------------------------
    def test_send_prints_when_no_webhook_configured(self, capsys, monkeypatch):
        from tools.notify import CHANNELS, send

        for env, _name, _b in CHANNELS:
            monkeypatch.delenv(env, raising=False)
        assert send({"totals": {}}, [_alert_item()], 100) == []
        assert "ElevenLabs" in capsys.readouterr().out

    def test_send_dry_run_never_posts(self, capsys, monkeypatch):
        import tools.notify as n

        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/x")
        monkeypatch.setattr(
            n, "httpx", None, raising=False)   # any POST attempt would blow up
        assert n.send({"totals": {}}, [_alert_item()], 100, dry_run=True) == []
        assert "ElevenLabs" in capsys.readouterr().out

    def test_send_posts_to_every_configured_channel(self, monkeypatch):
        import sys

        import tools.notify as n

        posted = []

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

        fake = type("M", (), {"post": staticmethod(
            lambda url, json, timeout: (posted.append((url, json)), _Resp())[1])})
        monkeypatch.setitem(sys.modules, "httpx", fake)
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack/x")
        monkeypatch.setenv("GCHAT_WEBHOOK_URL", "https://chat.google/y")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/z")

        delivered = n.send({"totals": {}}, [_alert_item()], 100)
        assert delivered == ["slack", "google chat", "discord"]
        assert [u for u, _ in posted] == ["https://hooks.slack/x",
                                          "https://chat.google/y",
                                          "https://discord/z"]
        assert "blocks" in posted[0][1]
        assert "cardsV2" in posted[1][1]
        assert "embeds" in posted[2][1]

    def test_one_failing_channel_does_not_block_the_others(self, monkeypatch):
        import sys

        import tools.notify as n

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

        def _post(url, json, timeout):
            if "slack" in url:
                raise RuntimeError("slack 500")
            return _Resp()

        monkeypatch.setitem(sys.modules, "httpx",
                            type("M", (), {"post": staticmethod(_post)}))
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack/x")
        monkeypatch.setenv("GCHAT_WEBHOOK_URL", "https://chat.google/y")
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

        assert n.send({"totals": {}}, [_alert_item()], 100) == ["google chat"]


# ---------------------------------------------------------------- M8

def _render(report: dict) -> str:
    """Render a captured notify.send() call as the plain-text report."""
    from tools.notify import build_text

    return build_text(report["run_stats"], report["items"],
                      report["min_volume"])

E2E_CONFIG = {
    "settings": {"country": "us", "gap_threshold": 0.75,
                 "partial_threshold": 0.85, "min_volume": 0,
                 "ahrefs_max_keywords": 50,
                 "embedding_model": "all-MiniLM-L6-v2",
                 "request_timeout": 5, "user_agent": "test"},
    "taxonomy": TAXONOMY,
    "site": {"slug": "acme", "name": "Acme",
             "description": "an AI voice platform",
             "sitemap": "https://acme.example/sitemap.xml",
             "include_patterns": [], "exclude_patterns": []},
    "competitors": [{"slug": "elevenlabs", "name": "ElevenLabs",
                     "sitemaps": ["https://elevenlabs.io/sitemap.xml"],
                     "include_patterns": [], "exclude_patterns": [],
                     "active": True}],
}

SITE_SEGMENTS = {"https://acme.example/tts-guide": {
    "url": "https://acme.example/tts-guide", "error": None,
    "segments": [{"segment_index": 0, "segment_type": "page",
                  "segment_text": "Guide to Text to Speech"}]}}


class TestEndToEnd:
    def _setup(self, monkeypatch, competitor_urls):
        import main as m

        state = {"reports": []}
        monkeypatch.setattr(m, "_load_config", lambda: E2E_CONFIG)
        monkeypatch.setattr(
            m.sitemaps, "fetch_urls",
            lambda sm, *a, **k: pd.DataFrame(
                {"url": (["https://acme.example/tts-guide"] if "acme.example" in sm[0]
                         else competitor_urls),
                 "lastmod": pd.NaT}))
        monkeypatch.setattr(m.extract, "fetch_segments",
                            lambda urls, *a, **k: [SITE_SEGMENTS[u] for u in urls])
        monkeypatch.setattr(
            m.extract, "fetch_meta",
            lambda urls, *a, **k: [
                {"url": u, "title": "New Voice Topic", "h1": "New Voice Topic",
                 "meta_description": "", "error": None,
                 "topic_text": f"New Voice Topic {u}"} for u in urls])
        monkeypatch.setattr(
            m.notify, "send",
            lambda run_stats, items, min_volume, dry_run=False: (
                state["reports"].append(
                    {"run_stats": run_stats, "items": items,
                     "min_volume": min_volume, "dry_run": dry_run})))
        return m, state

    def _args(self, **over):
        import argparse
        base = {"competitor": None, "dry_run": False}
        base.update(over)
        return argparse.Namespace(**base)

    def test_first_run_baseline_then_alerting_run(self, monkeypatch):
        sb = FakeSupabase()

        # ---- run 1: baseline. No new URLs, no judge calls, no alerts.
        m, state = self._setup(monkeypatch,
                               ["https://elevenlabs.io/blog/a"])
        client = FakeAnthropicClient([])        # must never be called
        rc = m.run(self._args(), supabase=sb, model=FakeModel(),
                   anthropic_client=client, provider=None)
        assert rc == 0
        assert client.prompts == []
        assert "cm_alerts" not in sb.store or not sb.store["cm_alerts"]
        assert "No new in-scope content gaps" in _render(state["reports"][0])
        # our own site inventory got populated on first run
        assert len(sb.store["cm_site_inventory"]) == 1
        baseline = [r for r in sb.store["cm_urls"]
                    if r["competitor_slug"] == "elevenlabs"]
        assert all(r["status"] == "baseline" for r in baseline)

        # ---- run 2: one new competitor URL -> gap -> in-scope alert.
        m, state = self._setup(monkeypatch, ["https://elevenlabs.io/blog/a",
                                             "https://elevenlabs.io/blog/b"])
        client = FakeAnthropicClient([GOOD_JSON])
        from tools.ahrefs import StubProvider
        rc = m.run(self._args(), supabase=sb, model=FakeModel(),
                   anthropic_client=client, provider=StubProvider(50))
        assert rc == 0
        assert len(client.prompts) == 1          # only the new URL judged
        alerts = sb.store["cm_alerts"]
        assert len(alerts) == 1
        assert alerts[0]["competitor_url"] == "https://elevenlabs.io/blog/b"
        assert alerts[0]["bucket"] == "gap"
        assert alerts[0]["target_keyword"] == "ai voice generator"
        assert alerts[0]["volume"] is not None   # enriched by stub
        report = _render(state["reports"][0])
        assert "New Voice Topic" in report
        assert "STUB data" in report
        # two runs recorded with stats
        runs = sb.store["cm_runs"]
        assert len(runs) == 2
        assert runs[1]["stats"]["elevenlabs"]["new"] == 1
        assert runs[1]["stats"]["elevenlabs"]["in_scope"] == 1

    def test_dry_run_skips_alert_rows(self, monkeypatch):
        sb = FakeSupabase()
        m, state = self._setup(monkeypatch, ["https://elevenlabs.io/blog/a"])
        m.run(self._args(), supabase=sb, model=FakeModel(),
              anthropic_client=FakeAnthropicClient([]), provider=None)

        m, state = self._setup(monkeypatch, ["https://elevenlabs.io/blog/a",
                                             "https://elevenlabs.io/blog/b"])
        from tools.ahrefs import StubProvider
        rc = m.run(self._args(dry_run=True), supabase=sb, model=FakeModel(),
                   anthropic_client=FakeAnthropicClient([GOOD_JSON]),
                   provider=StubProvider(50))
        assert rc == 0
        assert not sb.store.get("cm_alerts")     # no alert rows
        assert len(sb.store["cm_runs"]) == 2     # run still recorded
        assert state["reports"][0]["dry_run"] is True   # webhooks skipped
        assert "New Voice Topic" in _render(state["reports"][0])

    def test_competitor_failure_isolated(self, monkeypatch):
        sb = FakeSupabase()
        m, state = self._setup(monkeypatch, ["https://elevenlabs.io/blog/a"])
        cfg = {**E2E_CONFIG,
               "competitors": E2E_CONFIG["competitors"] + [
                   {"slug": "broken", "name": "Broken",
                    "sitemaps": ["https://broken.io/sitemap.xml"],
                    "active": True}]}
        monkeypatch.setattr(m, "_load_config", lambda: cfg)

        real_fetch = m.sitemaps.fetch_urls

        def fetch(sm, *a, **k):
            if "broken.io" in sm[0]:
                raise RuntimeError("sitemap unreachable")
            return real_fetch(sm, *a, **k)
        monkeypatch.setattr(m.sitemaps, "fetch_urls", fetch)

        rc = m.run(self._args(), supabase=sb, model=FakeModel(),
                   anthropic_client=FakeAnthropicClient([]), provider=None)
        assert rc == 0                           # one survivor -> not fatal
        run_row = sb.store["cm_runs"][0]
        assert "broken" in run_row["errors"]
        assert "elevenlabs" in run_row["stats"]

    def test_all_competitors_failing_hard_fails(self, monkeypatch):
        sb = FakeSupabase()
        m, state = self._setup(monkeypatch, [])
        monkeypatch.setattr(m.sitemaps, "fetch_urls",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("down")))
        rc = m.run(self._args(), supabase=sb, model=FakeModel(),
                   anthropic_client=FakeAnthropicClient([]), provider=None)
        assert rc == 1
