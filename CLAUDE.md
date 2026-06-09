# competitor-monitor — build spec

Weekly competitor content-gap monitor for murf.ai. Crawls competitor sitemaps,
detects newly published URLs, semantically checks them against Murf's own
content inventory, filters by product scope (LLM judge), enriches surviving
topics with Ahrefs keyword data, and pings a Google Chat space with a
distilled "topics they published that we don't cover" list.

## Conventions (follow these — they match the owner's other repos)

- Plain Python, deterministic orchestration in `main.py`. No LangGraph — this
  is a linear pipeline. LLM is used ONLY inside `tools/judge.py`, never for
  routing or failure handling.
- Every `tools/` module is proven standalone (offline smoke test in `tests/`)
  BEFORE being wired into `main.py`. Smoke tests must run with no network and
  no credentials (use fixtures + monkeypatched/lambda-mocked clients).
- MD5 stable IDs for all upserts (prevents duplicates on reruns).
- Swappable provider pattern for paid APIs: `tools/ahrefs.py` exposes a real
  provider when `AHREFS_API_KEY` is set, otherwise a clearly-flagged stub that
  returns deterministic fake data so the pipeline can run end-to-end without
  credentials.
- Failures are non-fatal per-competitor: record the error in the `runs` table,
  continue with remaining competitors. The run only hard-fails if ALL
  competitors fail or Supabase is unreachable.
- Model IDs via env (`JUDGE_MODEL`, default `claude-haiku-4-5`), never
  hardcoded in code.
- Secrets via env only (`.env` locally, GitHub Actions secrets in CI).
  See `.env.example`.

## Pipeline order (main.py)

1. `refresh_inventory` (scripts/refresh_inventory.py, callable as a function):
   diff Murf's own sitemap, fetch+extract+embed only new/changed URLs,
   upsert into `murf_inventory`. Incremental — never full re-crawl.
2. For each active competitor in config.yaml:
   a. `sitemaps.fetch_urls()` — advertools sitemap_to_df, apply per-competitor
      include/exclude path regexes from config. Output: clean URL list.
      advertools.sitemap_to_df() handles sitemap indexes, gzip-compressed
      sitemaps, and news sitemaps natively — do not write a custom parser.
      The only post-processing needed is apply_filters() on the returned df.
   b. `diff.detect_new()` — upsert into `urls`, return URLs not previously
      seen. FIRST RUN GUARD: if the competitor has zero prior rows, upsert
      everything with status='baseline' and return an empty new-list.
   c. `extract.fetch_meta()` — for each new URL: title + H1 (and meta
      description if present). Competitor side is title+H1 only by design.
      Per-URL isolation: one failed fetch must not kill the batch.
   d. `gap.check()` — embed "title — H1" per new URL (all-MiniLM-L6-v2),
      max-cosine against ALL murf_inventory vectors (page + section level).
      Buckets: sim < GAP_THRESHOLD (default 0.75) → "gap";
      0.75–0.85 → "partial" (report nearest Murf segment URL);
      > 0.85 → covered, drop.
   e. `judge.evaluate()` — one Claude call per gap/partial URL using
      prompts/scope_judge.md. Returns strict JSON:
      {in_scope: bool, reason: str, target_keyword: str,
       suggested_page_type: "blog"|"listicle"|"landing_page"|"tool_page",
       confidence: "high"|"medium"|"low"}.
      Drop in_scope=false. Product taxonomy is injected from config.yaml —
      do not hardcode it in the prompt file.
   f. `ahrefs.keyword_overview()` — batched call for all surviving
      target_keywords (volume, KD, traffic potential, country from config).
      Apply MIN_VOLUME floor from config (default 100) — below floor goes to
      the alert's collapsed "low volume" footer, not the main list.
3. `notify.send()` — single Google Chat cardsV2 message per run, grouped by
   competitor: topic, competitor URL, target keyword, volume, KD, suggested
   format, similarity + nearest Murf match for partials. Also insert every
   item into `alerts` for history. If GCHAT_WEBHOOK_URL is unset, print the
   card JSON to stdout (local dev mode).
4. Write a `runs` row: started_at, finished_at, per-competitor counts
   (urls_seen, new, gaps, in_scope, alerted), errors json.

## Build milestones (do them in order, prove each before the next)

- M1  schema.sql applied; `tools/sitemaps.py` + offline smoke (fixture XML).
- M2  `tools/diff.py` + smoke (mock supabase client; verify first-run guard).
- M3  `tools/extract.py` + smoke (fixture HTML; verify H1/H2/H3 extraction
      for inventory mode and title+H1 for competitor mode).
- M4  `scripts/refresh_inventory.py` + `tools/gap.py` + smoke (tiny embedded
      fixture vectors; verify bucket thresholds and max-cosine over segments).
- M5  `tools/judge.py` + smoke (lambda-mock the anthropic client; verify JSON
      parsing, retry-on-malformed-JSON once, and taxonomy injection).
- M6  `tools/ahrefs.py` real+stub providers + smoke (stub path only).
- M7  `tools/notify.py` + smoke (capture POST payload, validate cardsV2 shape).
- M8  wire `main.py`; full offline end-to-end smoke with all stubs.
- M9  `.github/workflows/weekly.yml` live; manual `workflow_dispatch` first,
      verify baseline run, then let the cron take over.

## Key implementation notes

- **Inventory embedding strategy (asymmetric, deliberate):**
  Murf pages are embedded as MULTIPLE vectors per URL — one `page` segment
  (title + H1) and one `section` segment per H2 (its H3s appended as
  context, "H2: h3a / h3b / h3c"). Competitor pages are embedded as ONE
  vector (title + H1). Gap = max cosine across all Murf vectors. This lets a
  competitor's dedicated page match a section inside a broader Murf guide.
- **lastmod is untrusted.** New content = URL never seen before, period.
  Store lastmod for reference only.
- **Embeddings storage:** bytea column, np.float32[384].tobytes() /
  np.frombuffer. Load the whole murf_inventory matrix into numpy at run
  start; cosine via normalized matrix @ query. No pgvector needed at this
  scale.
- **Ahrefs:** v3 REST, Bearer token. keywords-explorer overview endpoint,
  batch keywords per request. Respect a configurable per-run keyword cap
  (AHREFS_MAX_KEYWORDS, default 50) as a credit-burn guard.
- **Google Chat:** incoming webhook URL, POST cardsV2. No OAuth.
- **Supabase cold start:** free tier may pause after inactivity — wrap the
  first connection in a short retry (3 attempts, exponential backoff).
- **Locale noise:** exclude_patterns in config must catch localized paths
  (/de/, /fr/, /ja/ ...) — see config.yaml defaults.

## Out of scope for v1 (do not build)

- Detecting refreshed/updated competitor posts (lastmod-based).
- The "re-check rankings after 4–6 weeks" validation loop (v2 idea).
- Email fallback notifier (only if Google Chat webhooks turn out to be
  admin-restricted; keep notify.py's interface generic enough to add it).
- Any frontend.
