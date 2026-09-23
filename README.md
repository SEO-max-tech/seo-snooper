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

Asana — 2 topic(s) we don't cover
------------------------------------------------------------
  [GAP] Sprint Retrospective Templates That Teams Actually Use
      https://asana.com/resources/sprint-retrospective-template
      Keyword: sprint retrospective template · Volume: 2,400 · KD: 24 · Format: Blog post
  [PARTIAL] Resource Capacity Planning for Agencies
      https://asana.com/resources/capacity-planning
      Keyword: resource capacity planning · Volume: 880 · KD: 18 · Format: Blog post
      Partially covered (sim 0.82) — nearest: https://yoursite.com/guides/resource-planning

Monday.com — 1 topic(s) we don't cover
------------------------------------------------------------
  [GAP] Best Gantt Chart Software Compared
      https://monday.com/blog/gantt-chart-software
      Keyword: gantt chart software · Volume: 1,600 · KD: 31 · Format: Listicle

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
post on "resource capacity planning" correctly matches the *section* on
capacity buried inside your 4,000-word resource planning guide, and isn't
reported as a gap.

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

Edit [`config.yaml`](config.yaml). **It ships as a template — every value
in it is a placeholder, and the example sitemaps point at `example.com`
on purpose.** Three blocks to fill in:

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

Everything up to here takes about fifteen minutes. This block is the part
that decides whether the tool is useful or noise, so it's worth an hour.

The `taxonomy` block is what the judge reasons over. Be specific — vague
taxonomies let noise through. `out_of_scope` matters as much as `in_scope`:
it's how you stop a competitor's adjacent product line from generating
useless suggestions every week.

For competitors, open each sitemap in a browser before adding it. If what
you see is mostly templated integration and solution pages, restrict it with
`include_patterns: ["/blog/"]`. Five to fifteen competitors is the sweet
spot — start with fewer than you think you want, because every extra one is
weekly noise and adding more later is trivial.

`site.slug` is a storage key. Pick it once and leave it alone (changing it
re-baselines your whole inventory), and don't let it collide with a
competitor slug.

### 5. Build your content inventory

**Do this locally, before you ever schedule the job.**

```bash
python scripts/refresh_inventory.py
```

The first inventory crawl fetches *every URL in your own sitemap*, one page
at a time. On a 5,000-page site that's roughly an hour. The GitHub Actions
job times out at 45 minutes, so on a site of any size a cold first run in CI
will simply die. Do it locally once and every later run is incremental and
takes seconds.

Pass `--full` to force a complete re-crawl and re-embed later, for example
after changing `embedding_model`.

Check it worked before moving on:

```sql
select count(*) from cm_site_inventory;
select segment_type, count(*) from cm_site_inventory group by segment_type;
```

You want several rows per page — one `page` segment plus one `section` per
H2. If the table is empty or suspiciously small, either your sitemap filters
are wrong or your pages are client-rendered and have no server-side
headings for the parser to find.

### 6. Baseline run

```bash
python main.py --dry-run
```

**This reports nothing, and that's correct.** With no history, every URL a
competitor has ever published would look "new". The first run records
everything as `baseline` and stays deliberately quiet.

Check the baseline landed:

```sql
select competitor_slug, count(*) from cm_urls group by competitor_slug;
```

This is where a too-loose `include_patterns` shows up. A competitor with
40,000 rows is a filter problem, not a content machine — go back and narrow
it before you run again.

### 7. Wait a week, then the real run

```bash
python main.py --dry-run
```

Now you get actual output. Read it critically and expect to tune — see
[Tuning](#tuning) below. Two or three rounds is normal; nobody gets the
taxonomy right on the first try.

```bash
python main.py                          # full run, posts to webhooks
python main.py --competitor asana       # one competitor, for debugging
python main.py --dry-run                # no webhooks, no alert rows
```

### What to expect

Fifteen minutes of setup, an hour on the config, a week of waiting, then two
or three tuning passes over the following month. The tool starts earning its
place around week three, once the taxonomy is dialled in and you've dropped
the competitors that turned out to be noise.

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

**Lay the baseline locally first** (steps 5 and 6 above). A cold first run
in CI has to crawl your entire sitemap and will hit the 45-minute job
timeout on any site of real size. Once `cm_site_inventory` is populated,
every scheduled run is incremental and finishes in a couple of minutes.

Fork the repo, then add your secrets under **Settings → Secrets and variables
→ Actions**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, and
whichever of `AHREFS_API_KEY` / `SLACK_WEBHOOK_URL` / `GCHAT_WEBHOOK_URL` /
`DISCORD_WEBHOOK_URL` you're using.

Two scheduled workflows ship with the repo (plus `tests.yml`, which just
runs the offline suite on push and PR):

- **`weekly.yml`** — the scan, Sundays at 02:30 UTC. Run it once by hand
  (**Actions → weekly-competitor-scan → Run workflow**) with `dry_run`
  checked, to confirm your secrets work in CI before trusting the cron.
- **`keepalive.yml`** — a trivial `SELECT` every three days. Supabase's free
  tier pauses a project after 7 days of inactivity, and a weekly scan sits
  exactly on that edge. Delete this if you're on a paid tier.

Both jobs self-skip with a green "not configured" notice if `SUPABASE_URL`
is missing, so a clone you haven't set up yet won't mail you a failure every
week.

---

## Tuning

The report is only as good as your taxonomy. Start here, in this order.

### "It's reporting things we obviously already cover"

Your inventory is too thin. Check it:

```sql
select count(*) from cm_site_inventory;
```

If that number is far below your page count, the crawl isn't finding
headings. Common causes: pages render client-side so there's no server-side
H1/H2 for the parser, or `site.exclude_patterns` is cutting more than you
meant. Fix the inventory before touching thresholds — no threshold can
rescue a corpus that doesn't contain your content.

If the inventory is healthy, raise `partial_threshold` so more near-misses
resolve as covered.

### "It's reporting topics that have nothing to do with us"

Tighten `taxonomy.out_of_scope` first. It's cheaper, more precise, and more
durable than moving thresholds, because it acts on meaning rather than on a
similarity number. Name the adjacent product categories your competitors
sell that you don't — that one edit kills most recurring noise.

Then narrow `include_patterns` for the worst offenders. A competitor whose
marketing pages are templated boilerplate should usually be `["/blog/"]`.

### "We're getting nothing at all"

Work backwards through the pipeline with one competitor:

```bash
python main.py --competitor asana --dry-run
```

The final log line reports that competitor's totals —
`run complete: {'new': N, 'gaps': N, 'in_scope': N, 'alerted': N}` — and
`tools.diff` logs the URL counts above it. Find the stage that goes to zero:

| Stage drops to zero | What it means |
|---|---|
| `urls upserted` | Sitemap unreachable, or `include_patterns` excludes everything |
| `N new` | Genuinely nothing published since last run — or you're still on the baseline run |
| gaps | Everything scored as covered. Lower `partial_threshold`, or check for an over-broad inventory |
| in-scope | The judge is rejecting everything. Your `taxonomy.in_scope` is too narrow |
| alerted | Everything fell under `min_volume`. Lower the floor, or check whether you're on stub keyword data |

### "Too many low-volume topics"

They're already collapsed into a footer count rather than the main list.
Raise `min_volume` if the footer itself is getting noisy. Nothing is lost —
every item, suppressed or not, is written to `cm_alerts`.

### The knobs

| Setting | Default | What it does |
|---|---|---|
| `gap_threshold` | `0.75` | Below this cosine = a real gap. Raise it to report more, lower it to report less |
| `partial_threshold` | `0.85` | Above this = already covered, dropped. The band between the two is "partial" |
| `min_volume` | `100` | Monthly searches below this go to the footer instead of the main list |
| `ahrefs_max_keywords` | `50` | Hard cap on keyword lookups per run |
| `request_timeout` | `20` | Per-page fetch timeout, seconds |
| `embedding_model` | `all-MiniLM-L6-v2` | Any sentence-transformers model. Changing it invalidates stored vectors — delete `cm_site_inventory` and let it rebuild |

Change one thing at a time and re-run with `--dry-run`. Thresholds interact,
and moving two at once tells you nothing about which one mattered.

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
