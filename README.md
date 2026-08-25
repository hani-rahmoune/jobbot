# jobbot

Discord alerting for internship and apprenticeship openings in France, pulled
directly from employer applicant tracking systems (ATS) — no aggregators, no
recycled listings.

The project's non-negotiables, architecture, and milestone plan live in
[CLAUDE.md](CLAUDE.md); read that first if you're changing anything here.

## Status

- **M0/M1** — done: `Job` model, company/settings config loading, the
  Greenhouse ATS adapter.
- **M2** — done: the `filters.yaml`-driven filter engine.
- Everything after that (store, Discord publisher, orchestration, discovery)
  is not built yet.

## Quickstart

```bash
uv sync                 # install dependencies into .venv
uv run pytest -v        # run the test suite (offline, no network calls)
uv run ruff check .     # lint
```

## Configuration

Nothing you'd want to change lives in code:

- `companies/*.yaml` — which employers to poll, and on which ATS. See the
  comment block at the top of `companies/hot.yaml` for how to find a
  Greenhouse board token from a careers page URL.
- `settings.yaml` — runtime knobs (poll interval, User-Agent contact, etc.).
  Any key can be overridden with a `JOBBOT_`-prefixed environment variable.
- `filters.yaml` — location and keyword preferences. Edit this to relocate
  or change what you're searching for; `jobbot/` itself contains no
  hardcoded city, keyword, or search term (enforced by
  `test_no_hardcoded_search_terms`).
