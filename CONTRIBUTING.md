# Contributing

Thanks for looking. This is a small, deliberately boring codebase — the aim
is that anyone can read the whole thing in half an hour.

## Ground rules

These come from [`SPEC.md`](SPEC.md) and the code is consistent about them:

- **Deterministic orchestration.** `main.py` is a linear pipeline. No agent
  framework, no LLM-driven routing or error handling.
- **The LLM lives in one file.** Only `tools/judge.py` calls Claude. If you
  find yourself wanting a model call somewhere else, open an issue first.
- **Offline tests before wiring.** Every module in `tools/` is provable with
  no network and no credentials, using fixtures and mocked clients. New code
  ships with a smoke test in the same style.
- **Stable MD5 IDs for every upsert.** Reruns must never create duplicates.
- **Failures are isolated and non-fatal.** Per sitemap, per URL, per
  competitor, per notification channel. A run only hard-fails when Supabase
  is unreachable or every competitor failed.
- **Nothing brand-specific in code or prompts.** Site identity, taxonomy and
  competitors all live in `config.yaml`.
- **Secrets via env only.** Never in `config.yaml`, never in a commit.

## Getting set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/test_smoke.py -q
```

The full suite is offline and runs in about a second. If a change needs
network or credentials to test, it probably belongs behind a provider
interface like `tools/ahrefs.py` instead.

## Good first contributions

- **A new notification channel.** Write a `build_*` function in
  `tools/notify.py`, add a row to `CHANNELS`, and add it to the parametrized
  tests. Email and Microsoft Teams are both wanted.
- **A keyword provider that isn't Ahrefs.** Same shape as
  `tools/ahrefs.py`: a class with `is_stub` and `keyword_overview(keywords,
  country) -> {keyword: {volume, difficulty, traffic_potential, cpc}}`.
- **Better extraction.** `tools/extract.py` is heuristic about which headings
  matter. Fixture-driven improvements are easy to verify.

## Known rough edges

Happy to take fixes for any of these:

- Pages on your own site with no headings never land in `cm_site_inventory`,
  so they get re-fetched on every run. Harmless but wasteful.
- The judge fails *open* — a URL whose JSON can't be parsed twice is reported
  rather than dropped. Deliberate, but it does mean parse failures surface as
  report noise.
- `cm_urls.status` never transitions to `alerted`, though the schema allows
  it.
- The real Ahrefs provider has had far less real-world exercise than the rest
  of the pipeline.

## Pull requests

Keep them focused, explain the why, and make sure `pytest tests/test_smoke.py`
is green. Match the surrounding style — the codebase uses plain functions,
explicit arguments, and docstrings that say *why* rather than restating the
signature.
