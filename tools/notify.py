"""Google Chat incoming-webhook notifier.

Single cardsV2 message per run, grouped by competitor. Each item: topic
title (linked to competitor URL), target keyword, volume, KD, suggested
format; partials show similarity + nearest Murf URL. Below-volume-floor
items collapse into a one-line footer count.

If GCHAT_WEBHOOK_URL is unset: print the card JSON to stdout (dev mode).
Keep the interface generic (send(payload_items)) so an email notifier can
slot in later without touching main.py.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_FORMAT_LABEL = {"blog": "Blog post", "listicle": "Listicle",
                 "landing_page": "Landing page", "tool_page": "Tool page"}


def _item_widget(item: dict) -> dict:
    kw = item.get("target_keyword") or "—"
    vol = item.get("volume")
    kd = item.get("keyword_difficulty")
    parts = [
        f"<b>Keyword:</b> {kw}",
        f"<b>Volume:</b> {vol:,}" if vol is not None else "<b>Volume:</b> n/a",
        f"<b>KD:</b> {kd}" if kd is not None else "<b>KD:</b> n/a",
        f"<b>Format:</b> {_FORMAT_LABEL.get(item.get('suggested_page_type'), '—')}",
    ]
    if item.get("bucket") == "partial":
        sim = item.get("similarity")
        near = item.get("nearest_murf_url") or ""
        parts.append(
            f"<b>Partially covered</b> (sim {sim:.2f}) — nearest: "
            f"<a href=\"{near}\">{near}</a>")
    title = item.get("topic_text") or item.get("url")
    return {
        "decoratedText": {
            "topLabel": item.get("bucket", "gap").upper(),
            "text": f"<a href=\"{item['url']}\">{title}</a><br>"
                    + " · ".join(parts),
            "wrapText": True,
        }
    }


def build_card(run_stats: dict, items: list[dict], min_volume: int) -> dict:
    """Pure function returning the cardsV2 dict — offline-testable."""
    main_items = [i for i in items
                  if (i.get("volume") or 0) >= min_volume]
    low_volume = [i for i in items
                  if (i.get("volume") or 0) < min_volume]

    sections = []
    by_comp: dict[str, list[dict]] = {}
    for item in main_items:
        by_comp.setdefault(item.get("competitor_name")
                           or item.get("competitor_slug", "?"), []).append(item)

    for comp, comp_items in sorted(by_comp.items()):
        comp_items.sort(key=lambda i: -(i.get("volume") or 0))
        sections.append({
            "header": f"{comp} — {len(comp_items)} topic(s) we don't cover",
            "widgets": [_item_widget(i) for i in comp_items],
        })

    if not main_items:
        sections.append({"widgets": [{"textParagraph": {
            "text": "No new in-scope content gaps this week. 🎉"}}]})

    footer_bits = []
    if low_volume:
        footer_bits.append(
            f"{len(low_volume)} low-volume topic(s) (&lt;{min_volume}/mo) "
            "suppressed — see cm_alerts table for the full list.")
    if run_stats.get("stub_data"):
        footer_bits.append("⚠️ Keyword metrics are STUB data (no Ahrefs key).")
    if footer_bits:
        sections.append({"widgets": [{"textParagraph": {
            "text": "<i>" + " ".join(footer_bits) + "</i>"}}]})

    totals = run_stats.get("totals", {})
    subtitle = (f"{totals.get('new', 0)} new URLs · "
                f"{totals.get('gaps', 0)} gaps · "
                f"{len(main_items)} alert(s)")

    return {
        "cardsV2": [{
            "cardId": "competitor-monitor",
            "card": {
                "header": {
                    "title": "Weekly competitor content-gap report",
                    "subtitle": subtitle,
                },
                "sections": sections,
            },
        }]
    }


def send(webhook_url: str | None, card: dict) -> None:
    if not webhook_url:
        log.warning("GCHAT_WEBHOOK_URL unset — printing card to stdout")
        print(json.dumps(card, indent=2))
        return

    import httpx
    resp = httpx.post(webhook_url, json=card, timeout=20)
    resp.raise_for_status()
    log.info("google chat notification sent (%d)", resp.status_code)
