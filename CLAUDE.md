# SEO Snooper

Build spec, pipeline contract, conventions and failure policy live in
[SPEC.md](SPEC.md). Read that before changing pipeline behaviour.

Contributor workflow and house rules: [CONTRIBUTING.md](CONTRIBUTING.md).

Quick orientation:
- `main.py` — deterministic linear orchestration, no LLM
- `tools/judge.py` — the only module that calls Claude
- `config.yaml` — site identity, product taxonomy, competitor list
- `tests/test_smoke.py` — fully offline, run with `pytest tests/test_smoke.py -q`
