-- Upgrade an instance created before the open-source rename.
-- Safe to run more than once; skip entirely on a fresh install.
alter table if exists cm_murf_inventory rename to cm_site_inventory;
alter table if exists cm_alerts rename column nearest_murf_url to nearest_site_url;

-- The inventory rows for your own site are keyed by the slug in
-- config.yaml (`site.slug`). If you change that slug, re-point the old
-- rows rather than re-baselining the whole site:
-- update cm_urls set competitor_slug = '<new-slug>' where competitor_slug = '<old-slug>';
