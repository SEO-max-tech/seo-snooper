"""LLM scope judge — the ONLY module that calls Claude.

One call per gap/partial URL. Model from env JUDGE_MODEL (default
claude-haiku-4-5). Prompt template: prompts/scope_judge.md, with the
murf_taxonomy block from config.yaml injected — taxonomy NEVER lives in
the prompt file.

Output contract (strict JSON, retry ONCE on malformed):
{in_scope: bool, reason: str, target_keyword: str,
 suggested_page_type: 'blog'|'listicle'|'landing_page'|'tool_page',
 confidence: 'high'|'medium'|'low'}
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "scope_judge.md"

_PAGE_TYPES = {"blog", "listicle", "landing_page", "tool_page"}
_CONFIDENCES = {"high", "medium", "low"}

FAIL_OPEN = {
    "in_scope": True,
    "reason": "judge_parse_failure",
    "target_keyword": "",
    "suggested_page_type": "blog",
    "confidence": "low",
}


def build_prompt(taxonomy: dict, item: dict) -> str:
    """Render prompts/scope_judge.md with taxonomy + item fields injected."""
    template = PROMPT_PATH.read_text()
    in_scope = "\n".join(f"- {t}" for t in taxonomy["in_scope"])
    out_of_scope = "\n".join(f"- {t}" for t in taxonomy["out_of_scope"])
    nearest = item.get("nearest_segment_text")
    nearest_context = (
        f"\nNearest existing Murf content: {nearest}" if nearest and
        item.get("bucket") == "partial" else "")
    return (template
            .replace("{in_scope_taxonomy}", in_scope)
            .replace("{out_of_scope_taxonomy}", out_of_scope)
            .replace("{topic_text}", item["topic_text"])
            .replace("{url}", item["url"])
            .replace("{bucket}", item.get("bucket", "gap"))
            .replace("{nearest_context}", nearest_context))


def _parse(raw: str) -> dict | None:
    """Strict-ish JSON parse: tolerate accidental markdown fences, nothing else."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("in_scope"), bool):
        return None
    if data.get("suggested_page_type") not in _PAGE_TYPES:
        return None
    if data.get("confidence") not in _CONFIDENCES:
        return None
    if not isinstance(data.get("target_keyword"), str):
        return None
    data.setdefault("reason", "")
    return {k: data[k] for k in
            ("in_scope", "reason", "target_keyword",
             "suggested_page_type", "confidence")}


def evaluate(client, model: str, taxonomy: dict, item: dict) -> dict:
    """item: {topic_text, url, bucket, nearest_segment_text}.
    Returns the JSON contract above plus the original item fields.
    On double JSON failure: in_scope=True, confidence='low',
    reason='judge_parse_failure' — fail open, let the human filter.
    Accept `client` as a parameter so smoke tests can pass a lambda mock.
    """
    prompt = build_prompt(taxonomy, item)
    verdict = None
    for attempt in (1, 2):
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        verdict = _parse(raw)
        if verdict is not None:
            break
        log.warning("judge returned malformed JSON (attempt %d) for %s",
                    attempt, item["url"])

    if verdict is None:
        verdict = dict(FAIL_OPEN)

    return {**item, **verdict}
