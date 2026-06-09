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


# ---------------------------------------------------------------- M3

class TestExtract:
    def test_competitor_meta(self):
        from tools.extract import parse_meta

        html = (FIXTURES / "competitor_page.html").read_text()
        rec = parse_meta(html, "https://example.com/blog/best")
        assert rec["error"] is None
        assert rec["title"].startswith("10 Best AI Voice Generators")
        # h1 from <main>, not the nav h2
        assert rec["h1"] == "The 10 Best AI Voice Generators (Tested in 2026)"
        assert rec["meta_description"].startswith("We tested")
        assert rec["topic_text"] == f"{rec['title']} — {rec['h1']}"

    def test_inventory_segments(self):
        from tools.extract import parse_segments

        html = (FIXTURES / "murf_guide.html").read_text()
        rec = parse_segments(html, "https://murf.ai/resources/tts-guide")
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
        meta = [{"url": "https://murf.ai/resources/tts-guide",
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

    def test_nearest_murf_url_reported(self):
        from tools.gap import check

        inv, base = self._inventory()
        model = FakeModel({"partial topic": _vec_at_cosine(base, 0.80)})
        out = check(model, inv,
                    [{"url": "https://c.com/x", "topic_text": "partial topic"}],
                    0.75, 0.85)
        assert out[0]["nearest_murf_url"] == "https://murf.ai/resources/tts-guide"
        assert out[0]["nearest_segment_text"] == "How text to speech works"

    def test_max_over_segments(self):
        """Competitor page must match the BEST segment, not the page vector."""
        from tools.gap import Inventory, check

        page_vec = np.zeros(384, dtype=np.float32); page_vec[5] = 1.0
        sect_vec = np.zeros(384, dtype=np.float32); sect_vec[0] = 1.0
        inv = Inventory(np.vstack([page_vec, sect_vec]), [
            {"url": "https://murf.ai/g", "segment_type": "page",
             "segment_text": "Guide"},
            {"url": "https://murf.ai/g", "segment_type": "section",
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
        sb.store["cm_murf_inventory"] = [{
            "id": "x", "url": "https://murf.ai/p", "segment_type": "page",
            "segment_text": "roundtrip", "content_hash": "h",
            "embedding": "\\x" + vec.tobytes().hex(),
        }]
        inv = Inventory.load(sb)
        assert len(inv) == 1
        assert (inv.matrix @ vec).item() == pytest.approx(1.0)


class TestRefreshInventory:
    def _run(self, sb, monkeypatch, sitemap_urls, fetched, full=False):
        import scripts.refresh_inventory as ri

        monkeypatch.setattr(ri.sitemaps, "fetch_urls",
                            lambda *a, **k: pd.DataFrame(
                                {"url": sitemap_urls, "lastmod": pd.NaT}))
        monkeypatch.setattr(ri.extract, "fetch_segments",
                            lambda urls, *a, **k: [fetched[u] for u in urls])
        config = {"murf": {"sitemap": "https://murf.ai/sitemap.xml",
                           "include_patterns": [], "exclude_patterns": []},
                  "settings": {"user_agent": "t", "request_timeout": 5}}
        return ri.refresh(sb, FakeModel(), config, full=full)

    def test_first_run_populates_then_incremental_noop(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {"https://murf.ai/a": {
            "url": "https://murf.ai/a", "error": None,
            "segments": [{"segment_index": 0, "segment_type": "page",
                          "segment_text": "A page"}]}}
        stats = self._run(sb, monkeypatch, ["https://murf.ai/a"], fetched)
        assert stats["segments_embedded"] == 1
        assert len(sb.store["cm_murf_inventory"]) == 1

        # incremental rerun: URL known + inventoried -> not even fetched
        stats2 = self._run(sb, monkeypatch, ["https://murf.ai/a"], fetched)
        assert stats2["urls_added"] == 0
        assert stats2["segments_embedded"] == 0

        # full rerun: re-fetched, but unchanged hash -> skip re-embed
        stats3 = self._run(sb, monkeypatch, ["https://murf.ai/a"], fetched,
                           full=True)
        assert stats3["segments_embedded"] == 0
        assert stats3["segments_skipped"] == 1

        # content change -> re-embed under same stable id
        fetched["https://murf.ai/a"]["segments"][0]["segment_text"] = "A page v2"
        stats4 = self._run(sb, monkeypatch, ["https://murf.ai/a"], fetched,
                           full=True)
        assert stats4["segments_embedded"] == 1
        assert len(sb.store["cm_murf_inventory"]) == 1   # upsert, no dupe

    def test_vanished_url_pruned(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {
            "https://murf.ai/a": {"url": "https://murf.ai/a", "error": None,
                                  "segments": [{"segment_index": 0,
                                                "segment_type": "page",
                                                "segment_text": "A"}]},
            "https://murf.ai/b": {"url": "https://murf.ai/b", "error": None,
                                  "segments": [{"segment_index": 0,
                                                "segment_type": "page",
                                                "segment_text": "B"}]},
        }
        self._run(sb, monkeypatch, ["https://murf.ai/a", "https://murf.ai/b"],
                  fetched)
        assert len(sb.store["cm_murf_inventory"]) == 2

        stats = self._run(sb, monkeypatch, ["https://murf.ai/a"], fetched)
        assert stats["removed"] == 1
        urls = {r["url"] for r in sb.store["cm_murf_inventory"]}
        assert urls == {"https://murf.ai/a"}

    def test_failed_fetch_does_not_kill_batch(self, monkeypatch):
        sb = FakeSupabase()
        fetched = {
            "https://murf.ai/ok": {"url": "https://murf.ai/ok", "error": None,
                                   "segments": [{"segment_index": 0,
                                                 "segment_type": "page",
                                                 "segment_text": "OK"}]},
            "https://murf.ai/bad": {"url": "https://murf.ai/bad",
                                    "error": "HTTP 500"},
        }
        stats = self._run(sb, monkeypatch,
                          ["https://murf.ai/ok", "https://murf.ai/bad"], fetched)
        assert stats["segments_embedded"] == 1
