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


def evaluate(client, model: str, taxonomy: dict, item: dict) -> dict:
    """item: {topic_text, url, bucket, nearest_segment_text}.
    Returns the JSON contract above plus the original item fields.
    On double JSON failure: in_scope=True, confidence='low',
    reason='judge_parse_failure' — fail open, let the human filter.
    Accept `client` as a parameter so smoke tests can pass a lambda mock.
    """
    raise NotImplementedError  # TODO(M5)
