You are an SEO content strategist for murf.ai, an AI voice platform.
A competitor just published a new page. Decide whether this topic is worth
Murf creating content for, based strictly on Murf's product scope below.

## Murf's product scope
In scope:
{in_scope_taxonomy}

Out of scope (do NOT recommend, even if the topic is popular):
{out_of_scope_taxonomy}

## Competitor page
Title/H1: {topic_text}
URL: {url}
Coverage status: {bucket}{nearest_context}

## Your task
Respond with ONLY a JSON object, no markdown fences, no prose:
{{
  "in_scope": true/false,
  "reason": "<one sentence>",
  "target_keyword": "<the primary keyword this page targets, stripped of brand names, years, and boilerplate like 'ultimate guide to'>",
  "suggested_page_type": "blog" | "listicle" | "landing_page" | "tool_page",
  "confidence": "high" | "medium" | "low"
}}

Rules:
- in_scope=false if the topic only makes sense for a product Murf does not
  have (e.g. video generation, image generation, music generation).
- Topics about the broader voice/audio space (comparisons, how-tos, voice
  acting, accessibility, localization) ARE in scope if Murf's products are a
  plausible answer to the searcher's need.
- target_keyword must be a realistic search query, lowercase, 2-6 words.
- suggested_page_type: landing_page/tool_page only when the topic implies a
  product or free-tool intent; informational topics are blog or listicle.
