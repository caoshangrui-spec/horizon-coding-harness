# Contributing

## Local setup

Use Python 3.12 or 3.13 and keep dependencies inside the project environment:

```powershell
uv sync --locked
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked pytest -q -p no:cacheprovider
uv run --locked horizon demo run
```

The default test run is offline. Docker tests require an explicitly selected local image and never
pull one automatically. Provider calls are never part of CI and require separate, explicit budget
authorization.

## Change rules

- Preserve existing evidence, negative results, and `unknown` outcomes.
- Keep model proposals behind typed tool schemas and controller-side policy checks.
- Add a regression test for every recovery, budget, or replay semantic change.
- Do not weaken source isolation, path scope, budget gates, or required validation to make a test pass.
- Do not commit `.env`, `.horizon/`, full upstream checkouts, generated build output, or local agent
  coordination files.
- Clearly separate deterministic Harness evidence from real-model quality or benchmark claims.

Third-party benchmark material must retain its source, commit, and license metadata as described in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
