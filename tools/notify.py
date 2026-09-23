"""Webhook notifiers — Slack, Google Chat, Discord, and a console fallback.

One message per run, grouped by competitor. Each item: topic title (linked
to the competitor URL), target keyword, volume, KD, suggested format;
partials also show similarity + the nearest matching page on your own site.
Items below the volume floor collapse into a one-line footer count.

Channel selection is by environment variable — every webhook that is set
receives the report:
    SLACK_WEBHOOK_URL      Slack incoming webhook (Block Kit)
    GCHAT_WEBHOOK_URL      Google Chat incoming webhook (cardsV2)
    DISCORD_WEBHOOK_URL    Discord webhook (embeds)

If none are set, the plain-text report prints to stdout (dev mode).

A failing webhook is logged and skipped — it never aborts the run, and it
never stops the other channels from delivering.
"""
from __future__ import annotations

import html
import json
import logging
import os

log = logging.getLogger(__name__)

_FORMAT_LABEL = {"blog": "Blog post", "listicle": "Listicle",
                 "landing_page": "Landing page", "tool_page": "Tool page"}

# Per-platform payload guards. Slack caps a message at 50 blocks, Discord at
# 10 embeds / 6000 chars; truncating here is cheaper than a 400 from the API.
MAX_ITEMS_PER_COMPETITOR = 10
MAX_COMPETITORS = 8

_HEADER = "Weekly competitor content-gap report"
_EMPTY_MESSAGE = "No new in-scope content gaps this week. \U0001f389"


# ─── shared shaping ──────────────────────────────────────────────────────

def partition(items: list[dict], min_volume: int) -> tuple[dict, list, dict]:
    """Split items into (grouped main items, low-volume items, overflow).

    Returns:
        by_comp   {competitor_name: [item, ...]} sorted by volume desc,
                  truncated to MAX_ITEMS_PER_COMPETITOR / MAX_COMPETITORS
        low       items below the volume floor
        overflow  {"items": n, "competitors": n} dropped by truncation
    """
    main = [i for i in items if (i.get("volume") or 0) >= min_volume]
    low = [i for i in items if (i.get("volume") or 0) < min_volume]

    by_comp: dict[str, list[dict]] = {}
    for item in main:
        key = item.get("competitor_name") or item.get("competitor_slug", "?")
        by_comp.setdefault(key, []).append(item)

    dropped_items = 0
    for comp, comp_items in by_comp.items():
        comp_items.sort(key=lambda i: -(i.get("volume") or 0))
        if len(comp_items) > MAX_ITEMS_PER_COMPETITOR:
            dropped_items += len(comp_items) - MAX_ITEMS_PER_COMPETITOR
            by_comp[comp] = comp_items[:MAX_ITEMS_PER_COMPETITOR]

    ordered = dict(sorted(by_comp.items()))
    dropped_comps = 0
    if len(ordered) > MAX_COMPETITORS:
        keys = list(ordered)[:MAX_COMPETITORS]
        dropped_comps = len(ordered) - MAX_COMPETITORS
        dropped_items += sum(len(ordered[k]) for k in list(ordered)[MAX_COMPETITORS:])
        ordered = {k: ordered[k] for k in keys}

    return ordered, low, {"items": dropped_items, "competitors": dropped_comps}


def subtitle(run_stats: dict, main_count: int) -> str:
    totals = run_stats.get("totals", {})
    return (f"{totals.get('new', 0)} new URLs · "
            f"{totals.get('gaps', 0)} gaps · "
            f"{main_count} alert(s)")


def _footer_bits(low: list[dict], overflow: dict, run_stats: dict,
                 min_volume: int, lt: str) -> list[str]:
    """Footer lines shared by every channel. `lt` is the platform's
    less-than token ('&lt;' where the payload is HTML-ish, '<' otherwise)."""
    bits = []
    if low:
        bits.append(f"{len(low)} low-volume topic(s) ({lt}{min_volume}/mo) "
                    "suppressed — see the cm_alerts table for the full list.")
    if overflow.get("items"):
        extra = f"{overflow['items']} more topic(s)"
        if overflow.get("competitors"):
            extra += f" across {overflow['competitors']} more competitor(s)"
        bits.append(f"{extra} truncated — see the cm_alerts table.")
    if run_stats.get("enrichment_failed"):
        bits.append("⚠️ Keyword lookup failed this run — volume "
                    "and KD are missing and nothing was filtered by volume.")
    elif run_stats.get("stub_data"):
        bits.append("⚠️ Keyword metrics are STUB data "
                    "(no AHREFS_API_KEY set).")
    return bits


def _facts(item: dict) -> list[tuple[str, str]]:
    """The (label, value) pairs every channel renders, in order."""
    vol = item.get("volume")
    kd = item.get("keyword_difficulty")
    return [
        ("Keyword", item.get("target_keyword") or "—"),
        ("Volume", f"{vol:,}" if vol is not None else "n/a"),
        ("KD", str(kd) if kd is not None else "n/a"),
        ("Format", _FORMAT_LABEL.get(item.get("suggested_page_type"), "—")),
    ]


def _title(item: dict) -> str:
    return item.get("topic_text") or item.get("url", "")


# ─── Google Chat (cardsV2) ───────────────────────────────────────────────

def _gchat_widget(item: dict) -> dict:
    esc = html.escape
    parts = [f"<b>{esc(k)}:</b> {esc(v)}" for k, v in _facts(item)]
    if item.get("bucket") == "partial":
        sim = item.get("similarity")
        near = item.get("nearest_site_url") or ""
        sim_txt = f" (sim {sim:.2f})" if isinstance(sim, (int, float)) else ""
        parts.append(f"<b>Partially covered</b>{sim_txt} — nearest: "
                     f'<a href="{esc(near, quote=True)}">{esc(near)}</a>')
    url = esc(item.get("url", ""), quote=True)
    return {
        "decoratedText": {
            "topLabel": item.get("bucket", "gap").upper(),
            "text": f'<a href="{url}">{esc(_title(item))}</a><br>'
                    + " · ".join(parts),
            "wrapText": True,
        }
    }


def build_gchat_card(run_stats: dict, items: list[dict],
                     min_volume: int) -> dict:
    """Pure function returning the cardsV2 dict — offline-testable."""
    by_comp, low, overflow = partition(items, min_volume)
    main_count = sum(len(v) for v in by_comp.values())

    sections = [
        {"header": f"{html.escape(comp)} — {len(comp_items)} topic(s) "
                   "we don't cover",
         "widgets": [_gchat_widget(i) for i in comp_items]}
        for comp, comp_items in by_comp.items()
    ]
    if not main_count:
        sections.append({"widgets": [
            {"textParagraph": {"text": _EMPTY_MESSAGE}}]})

    bits = _footer_bits(low, overflow, run_stats, min_volume, "&lt;")
    if bits:
        sections.append({"widgets": [
            {"textParagraph": {"text": "<i>" + " ".join(bits) + "</i>"}}]})

    return {
        "cardsV2": [{
            "cardId": "seo-snooper",
            "card": {
                "header": {"title": _HEADER,
                           "subtitle": subtitle(run_stats, main_count)},
                "sections": sections,
            },
        }]
    }


# ─── Slack (Block Kit) ───────────────────────────────────────────────────

def _slack_escape(text: str) -> str:
    """Slack only requires &, < and > to be escaped in mrkdwn."""
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _slack_item_line(item: dict) -> str:
    esc = _slack_escape
    facts = " · ".join(f"*{esc(k)}:* {esc(v)}" for k, v in _facts(item))
    line = (f"<{item.get('url', '')}|{esc(_title(item))}>\n"
            f"`{item.get('bucket', 'gap').upper()}` {facts}")
    if item.get("bucket") == "partial":
        sim = item.get("similarity")
        near = item.get("nearest_site_url") or ""
        sim_txt = f" (sim {sim:.2f})" if isinstance(sim, (int, float)) else ""
        line += f"\n_Partially covered{sim_txt} — nearest:_ <{near}|{esc(near)}>"
    return line


def build_slack_blocks(run_stats: dict, items: list[dict],
                       min_volume: int) -> dict:
    """Pure function returning the Slack webhook payload."""
    by_comp, low, overflow = partition(items, min_volume)
    main_count = sum(len(v) for v in by_comp.values())

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": _HEADER}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": subtitle(run_stats, main_count)}]},
    ]

    for comp, comp_items in by_comp.items():
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"*{_slack_escape(comp)}* — {len(comp_items)} topic(s) "
                    "we don't cover"}})
        for item in comp_items:
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": _slack_item_line(item)[:2999]}})

    if not main_count:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": _EMPTY_MESSAGE}})

    bits = _footer_bits(low, overflow, run_stats, min_volume, "<")
    if bits:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": _slack_escape(" ".join(bits))}]})

    return {"text": f"{_HEADER} — {subtitle(run_stats, main_count)}",
            "blocks": blocks[:50]}


# ─── Discord (embeds) ────────────────────────────────────────────────────

def _discord_item_line(item: dict) -> str:
    facts = " · ".join(f"**{k}:** {v}" for k, v in _facts(item))
    line = (f"[{_title(item)}]({item.get('url', '')})\n"
            f"`{item.get('bucket', 'gap').upper()}` {facts}")
    if item.get("bucket") == "partial":
        sim = item.get("similarity")
        near = item.get("nearest_site_url") or ""
        sim_txt = f" (sim {sim:.2f})" if isinstance(sim, (int, float)) else ""
        line += f"\n*Partially covered{sim_txt} — nearest:* <{near}>"
    return line


def build_discord_payload(run_stats: dict, items: list[dict],
                          min_volume: int) -> dict:
    """Pure function returning the Discord webhook payload."""
    by_comp, low, overflow = partition(items, min_volume)
    main_count = sum(len(v) for v in by_comp.values())

    embeds: list[dict] = []
    for comp, comp_items in by_comp.items():
        body = "\n\n".join(_discord_item_line(i) for i in comp_items)
        embeds.append({
            "title": f"{comp} — {len(comp_items)} topic(s) we don't cover",
            "description": body[:4000],
            "color": 0x5865F2,
        })

    if not main_count:
        embeds.append({"title": _HEADER, "description": _EMPTY_MESSAGE,
                       "color": 0x57F287})

    bits = _footer_bits(low, overflow, run_stats, min_volume, "<")
    if bits and embeds:
        embeds[-1]["footer"] = {"text": " ".join(bits)[:2048]}

    return {"content": f"**{_HEADER}** — {subtitle(run_stats, main_count)}",
            "embeds": embeds[:10]}


# ─── console fallback ────────────────────────────────────────────────────

def build_text(run_stats: dict, items: list[dict], min_volume: int) -> str:
    """Plain-text report for stdout when no webhook is configured."""
    by_comp, low, overflow = partition(items, min_volume)
    main_count = sum(len(v) for v in by_comp.values())

    lines = [_HEADER, subtitle(run_stats, main_count), "=" * 60]
    for comp, comp_items in by_comp.items():
        lines.append("")
        lines.append(f"{comp} — {len(comp_items)} topic(s) we don't cover")
        lines.append("-" * 60)
        for item in comp_items:
            lines.append(f"  [{item.get('bucket', 'gap').upper()}] "
                         f"{_title(item)}")
            lines.append(f"      {item.get('url', '')}")
            lines.append("      "
                         + " · ".join(f"{k}: {v}" for k, v in _facts(item)))
            if item.get("bucket") == "partial":
                sim = item.get("similarity")
                sim_txt = (f" (sim {sim:.2f})"
                           if isinstance(sim, (int, float)) else "")
                lines.append(f"      Partially covered{sim_txt} — nearest: "
                             f"{item.get('nearest_site_url') or 'n/a'}")

    if not main_count:
        lines += ["", _EMPTY_MESSAGE]

    bits = _footer_bits(low, overflow, run_stats, min_volume, "<")
    if bits:
        lines += ["", "-" * 60] + [f"  {b}" for b in bits]
    return "\n".join(lines)


# ─── dispatch ────────────────────────────────────────────────────────────

CHANNELS = (
    ("SLACK_WEBHOOK_URL", "slack", build_slack_blocks),
    ("GCHAT_WEBHOOK_URL", "google chat", build_gchat_card),
    ("DISCORD_WEBHOOK_URL", "discord", build_discord_payload),
)


def send(run_stats: dict, items: list[dict], min_volume: int,
         dry_run: bool = False) -> list[str]:
    """Deliver the report to every configured webhook.

    Returns the list of channels that accepted the message. With no webhook
    configured — or dry_run=True — the plain-text report goes to stdout and
    an empty list is returned. Webhook failures are logged, never raised:
    losing a notification must not lose the run.
    """
    targets = [] if dry_run else [
        (os.getenv(env), name, builder)
        for env, name, builder in CHANNELS if os.getenv(env)
    ]

    if not targets:
        reason = "dry run" if dry_run else "no webhook configured"
        log.info("%s — printing report to stdout", reason)
        print(build_text(run_stats, items, min_volume))
        return []

    import httpx

    delivered = []
    for url, name, builder in targets:
        try:
            payload = builder(run_stats, items, min_volume)
            resp = httpx.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            log.info("%s notification sent (%d)", name, resp.status_code)
            delivered.append(name)
        except Exception as exc:  # noqa: BLE001 — per-channel isolation
            log.error("%s notification failed: %s", name, exc)
    return delivered


def dump(run_stats: dict, items: list[dict], min_volume: int) -> str:
    """JSON of every channel payload — handy for `--dry-run` debugging."""
    return json.dumps({
        name: builder(run_stats, items, min_volume)
        for _env, name, builder in CHANNELS
    }, indent=2)
