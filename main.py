"""Weekly competitor content-gap scan. Deterministic orchestration only —
see CLAUDE.md for the pipeline contract and failure policy.

Usage:
    python main.py                 # full run
    python main.py --competitor elevenlabs   # single competitor (debug)
    python main.py --dry-run       # everything except Chat send + alert rows
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scripts.refresh_inventory import refresh as refresh_inventory
from tools import ahrefs, diff, extract, gap, judge, notify, sitemaps

log = logging.getLogger("competitor-monitor")

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def _connect_supabase():
    """Cold-start guard: free-tier Supabase may pause — retry the first
    touch 3x with exponential backoff."""
    from supabase import create_client
    from tenacity import retry, stop_after_attempt, wait_exponential

    # strip trailing slash — supabase-py appends '/rest/v1', a trailing
    # slash yields '//rest/v1' which Kong rejects (PGRST125 invalid path)
    client = create_client(os.environ["SUPABASE_URL"].rstrip("/"),
                           os.environ["SUPABASE_SERVICE_KEY"])

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=2, max=20), reraise=True)
    def _probe():
        client.table("cm_runs").select("id").range(0, 0).execute()

    _probe()
    return client


def scan_competitor(supabase, model, inventory, comp: dict,
                    settings: dict) -> tuple[list[dict], dict]:
    """Sitemap -> diff -> extract -> gap for ONE competitor.
    Returns (gap/partial items, counts)."""
    counts = {"urls_seen": 0, "new": 0, "gaps": 0, "in_scope": 0, "alerted": 0}

    urls_df = sitemaps.fetch_urls(comp["sitemaps"],
                                  comp.get("include_patterns") or [],
                                  comp.get("exclude_patterns") or [],
                                  settings["user_agent"])
    counts["urls_seen"] = len(urls_df)

    new_items = diff.detect_new(supabase, comp["slug"], urls_df)
    counts["new"] = len(new_items)
    if not new_items:
        return [], counts

    records = extract.fetch_meta([n["url"] for n in new_items],
                                 settings["user_agent"],
                                 settings["request_timeout"])
    ok = [r for r in records if not r.get("error")]

    checked = gap.check(model, inventory, ok,
                        settings["gap_threshold"],
                        settings["partial_threshold"])
    open_items = [c for c in checked if c["bucket"] in ("gap", "partial")]
    counts["gaps"] = len(open_items)

    for item in open_items:
        item["competitor_slug"] = comp["slug"]
        item["competitor_name"] = comp.get("name", comp["slug"])
    return open_items, counts


def run(args, supabase=None, model=None, anthropic_client=None,
        provider=None) -> int:
    """Per-competitor failures are caught, logged to run errors, and do not
    stop the loop. Hard-fail (exit 1) only if Supabase is unreachable after
    retries or every competitor failed."""
    started_at = datetime.now(timezone.utc).isoformat()
    config = _load_config()
    settings = config["settings"]

    if supabase is None:
        supabase = _connect_supabase()          # hard-fail if unreachable

    if model is None:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(settings["embedding_model"])

    errors: dict[str, str] = {}
    stats: dict[str, dict] = {}

    # 1. refresh Murf's own inventory
    try:
        stats["murf_inventory"] = refresh_inventory(supabase, model, config)
    except Exception as exc:  # noqa: BLE001 — stale inventory is usable
        log.exception("inventory refresh failed — continuing with stale")
        errors["murf_inventory"] = str(exc)

    inventory = gap.Inventory.load(supabase)
    if len(inventory) == 0:
        log.warning("murf inventory is EMPTY — every topic will bucket as gap")

    # 2. scan competitors
    competitors = [c for c in config["competitors"] if c.get("active")]
    if args.competitor:
        competitors = [c for c in competitors if c["slug"] == args.competitor]
        if not competitors:
            log.error("no active competitor with slug %r", args.competitor)
            return 1

    all_items: list[dict] = []
    for comp in competitors:
        try:
            items, counts = scan_competitor(supabase, model, inventory,
                                            comp, settings)
            stats[comp["slug"]] = counts
            all_items.extend(items)
        except Exception as exc:  # noqa: BLE001 — per-competitor isolation
            log.exception("competitor %s failed", comp["slug"])
            errors[comp["slug"]] = str(exc)

    if competitors and len(errors.keys() & {c["slug"] for c in competitors}) \
            == len(competitors):
        log.error("ALL competitors failed — hard fail")
        _record_run(supabase, started_at, stats, errors)
        return 1

    # 3. judge scope (only module that talks to Claude)
    judged: list[dict] = []
    if all_items:
        if anthropic_client is None:
            import anthropic
            anthropic_client = anthropic.Anthropic()
        judge_model = os.getenv("JUDGE_MODEL", "claude-haiku-4-5")
        for item in all_items:
            try:
                verdict = judge.evaluate(anthropic_client, judge_model,
                                         config["murf_taxonomy"], item)
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                log.warning("judge failed for %s: %s", item["url"], exc)
                verdict = {**item, **judge.FAIL_OPEN, "reason": str(exc)}
            if verdict["in_scope"]:
                judged.append(verdict)
                slug = verdict["competitor_slug"]
                if slug in stats:
                    stats[slug]["in_scope"] += 1

    # 4. enrich with keyword metrics (batched across competitors)
    if provider is None:
        provider = ahrefs.get_provider(settings["ahrefs_max_keywords"])
    if judged:
        keywords = [j["target_keyword"] for j in judged if j["target_keyword"]]
        metrics = provider.keyword_overview(
            list(dict.fromkeys(keywords)), settings["country"])
        for j in judged:
            m = metrics.get(j.get("target_keyword"), {})
            j["volume"] = m.get("volume")
            j["keyword_difficulty"] = m.get("difficulty")
            j["traffic_potential"] = m.get("traffic_potential")
            if (j["volume"] or 0) >= settings["min_volume"]:
                slug = j["competitor_slug"]
                if slug in stats:
                    stats[slug]["alerted"] += 1

    # 5. notify + record
    totals = {k: sum(s.get(k, 0) for s in stats.values() if isinstance(s, dict))
              for k in ("new", "gaps", "in_scope", "alerted")}
    run_stats = {"totals": totals, "stub_data": getattr(provider, "is_stub", False)}
    card = notify.build_card(run_stats, judged, settings["min_volume"])

    if args.dry_run:
        log.info("dry run — printing card, skipping alert rows")
        notify.send(None, card)
    else:
        notify.send(os.getenv("GCHAT_WEBHOOK_URL") or None, card)

    run_id = _record_run(supabase, started_at, stats, errors)

    if judged and not args.dry_run:
        alert_rows = []
        for j in judged:
            alert_rows.append({
                "id": hashlib.md5(
                    f"{run_id}|{j['url']}".encode()).hexdigest(),
                "run_id": run_id,
                "competitor_slug": j["competitor_slug"],
                "competitor_url": j["url"],
                "topic_title": j.get("topic_text"),
                "bucket": j["bucket"],
                "similarity": j.get("similarity"),
                "nearest_murf_url": j.get("nearest_murf_url"),
                "target_keyword": j.get("target_keyword"),
                "volume": j.get("volume"),
                "keyword_difficulty": j.get("keyword_difficulty"),
                "suggested_page_type": j.get("suggested_page_type"),
                "judge_reason": j.get("reason"),
            })
        supabase.table("cm_alerts").insert(alert_rows).execute()

    log.info("run complete: %s (errors: %s)", totals, errors or "none")
    return 0


def _record_run(supabase, started_at: str, stats: dict, errors: dict) -> int:
    resp = supabase.table("cm_runs").insert({
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "errors": errors or None,
    }).execute()
    data = resp.data or [{}]
    return data[0].get("id", 0)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="competitor content-gap scan")
    parser.add_argument("--competitor", help="run a single competitor slug")
    parser.add_argument("--dry-run", action="store_true",
                        help="skip Chat send + alert rows")
    return parser.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    raise SystemExit(run(_parse_args()))
