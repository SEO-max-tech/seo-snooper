# SEO Snooper

Find out what your competitors published this week that you don't cover — and
get it in Slack, Google Chat or Discord every Monday morning.

SEO Snooper crawls your competitors' sitemaps, spots genuinely new URLs,
checks each one semantically against your own site's content, throws away
anything outside your product scope, attaches keyword volume and difficulty,
and posts the survivors grouped by competitor.

```
Weekly competitor content-gap report
47 new URLs · 9 gaps · 3 alert(s)
============================================================

ElevenLabs — 2 topic(s) we don't cover
------------------------------------------------------------
  [GAP] AI Dubbing Workflow for Video Teams
      https://elevenlabs.io/blog/ai-dubbing-workflow
      Keyword: ai dubbing workflow · Volume: 2,400 · KD: 24 · Format: Blog post
  [PARTIAL] Voice Cloning Ethics and Consent
      https://elevenlabs.io/blog/voice-cloning-ethics
      Keyword: voice cloning ethics · Volume: 880 · KD: 18 · Format: Blog post
      Partially covered (sim 0.82) — nearest: https://yoursite.com/guides/voice-cloning

Retell AI — 1 topic(s) we don't cover
------------------------------------------------------------
  [GAP] AI Receptionist Pricing Compared
      https://www.retellai.com/blog/ai-receptionist-pricing
      Keyword: ai receptionist pricing · Volume: 1,600 · KD: 31 · Format: Listicle

------------------------------------------------------------
  1 low-volume topic(s) (<100/mo) suppressed — see the cm_alerts table for the full list.
```

That's the console fallback. With a webhook configured you get the same
report as a Slack Block Kit message, a Google Chat card, or a Discord embed.

---

## Why it isn't just a sitemap diff

A raw "new URLs this week" feed is noise. Three filters make it useful:

**1. Semantic coverage check, not keyword matching.** Your own pages are
embedded as *multiple* vectors — one for the page (title + H1) and one per
H2 section (with its H3s as context). Competitor pages get a single vector.
A gap score is the max cosine across all of them, so a competitor's dedicated
post on "voice cloning ethics" correctly matches the *section* about ethics
buried inside your 4,000-word voice cloning guide, and doesn't get reported
as a gap.

Three buckets fall out: below `gap_threshold` is a **gap**, between the two
thresholds is **partial** (reported with the nearest matching page of yours,
so you can decide whether to expand it instead of writing something new), and
above `partial_threshold` is covered and dropped silently.

**2. Product-scope judge.** Competitors ship content for products you don't
have. One short Claude call per surviving URL decides whether the topic is
something *you* should rank for, using the taxonomy in your `config.yaml`.
It also extracts a clean target keyword and suggests a page format.

**3. Search volume floor.** Topics below `min_volume` collapse into a footer
count instead of cluttering the report. They're still written to the database.

**`lastmod` is never trusted.** New means a URL has never been seen before,
full stop. Sites that touch `lastmod` on every deploy can't spam you.

---

## Quick start

You need Python 3.11+, a free Supabase project, and an Anthropic API key.
Everything else is optional.

### 1. Clone and install

```bash
git clone https://github.com/YOUR-USERNAME/seo-snooper.git
cd seo-snooper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create the database

Make a free project at [supabase.com](https://supabase.com), open the **SQL
Editor**, paste the contents of [`schema.sql`](schema.sql), and run it. That
creates four tables, all prefixed `cm_` so they sit safely alongside anything
already in the project.

Then grab **Settings → API → Project URL** and the **`service_role`** key.
The service role key is required — the pipeline writes tables, and the `anon`
key can't.

### 3. Add credentials

```bash
cp .env.example .env
```

Fill in `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` and `ANTHROPIC_API_KEY`.
Leave the rest empty for now — the report will print to your terminal.

### 4. Describe your site

Edit [`config.yaml`](config.yaml). It ships as a complete working example
monitoring the AI-voice market, so you can see the shape of a real setup.
Three blocks to change:

```yaml
site:
  slug: acme                 # stable storage key — changing it re-baselines
  name: Acme                 # injected into the judge prompt
  description: a project management platform
  sitemap: https://acme.com/sitemap.xml

taxonomy:
  in_scope:
    - project management software
    - team collaboration
    - gantt charts and timelines
  out_of_scope:
    - accounting software
    - CRM

competitors:
  - slug: asana
    name: Asana
    sitemaps: [https://asana.com/sitemap.xml]
    include_patterns: []              # empty = everything
    exclude_patterns: *default_excludes
    active: true
```

The `taxonomy` block is what the judge reasons over. Be specific — vague
taxonomies let noise through. `out_of_scope` matters as much as `in_scope`:
it's how you stop a competitor's adjacent product line from generating
useless suggestions every week.

### 5. Baseline, then run

```bash
python main.py --dry-run
```

**The first run reports nothing, and that's correct.** With no history, every
URL a competitor has ever published would look "new". The first run records
everything as `baseline` and stays quiet. It also crawls and embeds your own
site, which is the slow part — expect several minutes on a large site. Later
runs are incremental and much faster.

Run it again a week later and you'll get real results.

```bash
python main.py                          # full run, posts to webhooks
python main.py --competitor asana       # one competitor, for debugging
python main.py --dry-run                # no webhooks, no alert rows
```

---

## Notifications

Set any combination of these in `.env`. Every webhook that's set gets the
report; if none are set it prints to stdout. A channel that fails is logged
and skipped — it never takes down the run or the other channels.

| Variable | Where to get it |
|---|---|
| `SLACK_WEBHOOK_URL` | [Slack API → Your Apps](https://api.slack.com/apps) → create an app → **Incoming Webhooks** → add one to a channel |
| `GCHAT_WEBHOOK_URL` | Google Chat space → **Apps & integrations** → **Webhooks** → add. Some Workspace orgs restrict this — check with your admin |
| `DISCORD_WEBHOOK_URL` | Channel → **Edit Channel** → **Integrations** → **Webhooks** → New Webhook |

Adding a fourth channel is a builder function plus one row in the `CHANNELS`
table in [`tools/notify.py`](tools/notify.py). All the builders are pure
functions over the same shaping step, so they're trivial to test offline.

---

## Keyword data

`AHREFS_API_KEY` enables real volume / KD / traffic-potential lookups via the
Ahrefs v3 API, capped at `ahrefs_max_keywords` per run as a credit guard.

**Without a key it still works.** A stub provider returns deterministic fake
metrics derived from each keyword's hash, so you can run the whole pipeline
end to end and evaluate whether the gap detection is useful before paying for
anything. Reports built on stub data carry a visible warning, so you'll never
mistake fake numbers for real ones.

If the keyword API fails mid-run, the report still goes out — the volume
floor drops to zero so nothing is silently suppressed, and the footer says
metrics are missing.

> The real Ahrefs provider is written against the documented v3
> `keywords-explorer/overview` endpoint but has fewer road miles than the rest
> of the pipeline. If your plan's response shape differs, the mapping is a
> dozen lines in [`tools/ahrefs.py`](tools/ahrefs.py).

---

## Running it weekly on GitHub Actions

Fork the repo, then add your secrets under **Settings → Secrets and variables
→ Actions**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, and
whichever of `AHREFS_API_KEY` / `SLACK_WEBHOOK_URL` / `GCHAT_WEBHOOK_URL` /
`DISCORD_WEBHOOK_URL` you're using.

Two workflows ship with the repo:

- **`weekly.yml`** — the scan, Sundays at 02:30 UTC. Trigger it manually
  first (**Actions → weekly-competitor-scan → Run workflow**) with `dry_run`
  checked to confirm your config and credentials, and to lay the baseline.
- **`keepalive.yml`** — a trivial `SELECT` every three days. Supabase's free
  tier pauses a project after 7 days of inactivity, and a weekly scan sits
  exactly on that edge. Delete this if you're on a paid tier.

The embedding model is cached between runs, so a typical weekly run finishes
in a couple of minutes.

---

## Tuning

| Setting | Default | What it does |
|---|---|---|
| `gap_threshold` | `0.75` | Below this cosine = a real gap. Raise it to report more, lower it to report less |
| `partial_threshold` | `0.85` | Above this = already covered, dropped. The band between the two is "partial" |
| `min_volume` | `100` | Monthly searches below this go to the footer instead of the main list |
| `ahrefs_max_keywords` | `50` | Hard cap on keyword lookups per run |
| `request_timeout` | `20` | Per-page fetch timeout, seconds |
| `embedding_model` | `all-MiniLM-L6-v2` | Any sentence-transformers model. Changing it invalidates stored vectors — delete `cm_site_inventory` and let it rebuild |

Getting too much noise? Tighten `taxonomy.out_of_scope` first — it's cheaper
and more precise than moving thresholds. Then narrow `include_patterns` to
just `/blog/` for competitors whose marketing pages are boilerplate.

Getting nothing? Check that `cm_site_inventory` actually has rows. An empty
inventory makes everything a gap; a *too-broad* one makes everything covered.

---

## How the data is stored

Four tables, all `cm_`-prefixed:

| Table | What's in it |
|---|---|
| `cm_urls` | Every URL ever seen, per competitor. MD5 of `slug\|url` as the primary key, so reruns can't create duplicates |
| `cm_site_inventory` | Your own content as embedding vectors — one row per page segment. `bytea`, `float32[384]`, loaded into a numpy matrix at run start |
| `cm_alerts` | Full alert history, including the low-volume items suppressed from the report |
| `cm_runs` | Per-run stats and errors as JSON |

No pgvector needed. The whole inventory matrix is loaded once per run and
cosine similarity is a single matrix multiply — plenty fast for sites in the
low tens of thousands of pages.

**Upgrading from an older install?** Run
[`migrations/0001_generic_naming.sql`](migrations/0001_generic_naming.sql).

---

## Development

```bash
pip install -r requirements.txt
pytest tests/test_smoke.py -q
```

66 tests, all offline — no network, no credentials, no API keys. Fixture
XML and HTML, a fake Supabase client, a stubbed Anthropic client, and the
Ahrefs stub provider. They run in under a second.

The design rules the code follows are in [`SPEC.md`](SPEC.md). The short
version: plain Python with deterministic orchestration in `main.py`, no
agent framework; the LLM is used *only* inside `tools/judge.py`, never for
routing or error handling; every module in `tools/` has an offline smoke test
before it gets wired in; failures are isolated per sitemap, per URL, per
competitor and per notification channel.

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Cost

Pennies per week. Embeddings run locally on CPU and cost nothing. Supabase's
free tier is ample. The only paid Anthropic call is the scope judge: one
short Haiku request per newly-detected gap URL, typically a few dozen a week.
Ahrefs is optional and capped.

---

## License

MIT — see [LICENSE](LICENSE).
