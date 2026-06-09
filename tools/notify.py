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


def build_card(run_stats: dict, items: list[dict], min_volume: int) -> dict:
    """Pure function returning the cardsV2 dict — offline-testable."""
    raise NotImplementedError  # TODO(M7)


def send(webhook_url: str | None, card: dict) -> None:
    raise NotImplementedError  # TODO(M7)
