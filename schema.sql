-- competitor-monitor schema (Supabase Postgres)
-- Safe to apply alongside existing rank-tracker tables.

create table if not exists cm_urls (
  id text primary key,                      -- md5(competitor_slug || '|' || url)
  competitor_slug text not null,            -- 'murf' is a valid slug here too
  url text not null,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  lastmod timestamptz,                      -- stored for reference, never trusted
  status text not null default 'seen'       -- baseline | seen | new | alerted | excluded
);
create index if not exists cm_urls_competitor_idx on cm_urls (competitor_slug);

create table if not exists cm_murf_inventory (
  id text primary key,                      -- md5(url || '|' || segment_index)
  url text not null,
  segment_type text not null,               -- 'page' (title+h1) | 'section' (h2+h3s)
  segment_text text not null,
  content_hash text not null,               -- md5(segment_text) for change detection
  embedding bytea not null,                 -- np.float32[384], all-MiniLM-L6-v2
  updated_at timestamptz not null default now()
);
create index if not exists cm_inventory_url_idx on cm_murf_inventory (url);

create table if not exists cm_runs (
  id bigint generated always as identity primary key,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  stats jsonb,                              -- per-competitor counts
  errors jsonb
);

create table if not exists cm_alerts (
  id text primary key,                      -- md5(run_id || '|' || competitor_url)
  run_id bigint references cm_runs(id),
  competitor_slug text not null,
  competitor_url text not null,
  topic_title text,
  bucket text not null,                     -- gap | partial
  similarity real,
  nearest_murf_url text,
  target_keyword text,
  volume int,
  keyword_difficulty int,
  suggested_page_type text,
  judge_reason text,
  created_at timestamptz not null default now()
);
