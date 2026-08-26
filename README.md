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
- **M3** — done: the SQLite state store (dedup, seed mode, resurrection,
  ghost detection).
- **M4** — done: the Discord webhook publisher.
- **M5** — done: the orchestrator (`jobbot/run.py`) tying fetch, filter,
  store, and publish into one runnable command.
- Everything after that (discovery, more ATS adapters) is not built yet.

## Running it

```bash
uv sync                 # install dependencies into .venv
uv run python -m jobbot.run --dry-run   # one cycle, zero Discord requests
uv run python -m jobbot.run --seed      # first-ever run: baseline the board without posting
uv run python -m jobbot.run             # the real thing (needs JOBBOT_DISCORD_WEBHOOK_URL)
```

`--help` lists every flag (`--config-dir`, `--filters`, `--settings`,
`--verbose`). Exit codes: `0` success, `1` every configured source failed
this cycle, `2` a config/environment problem (e.g. the webhook URL is
missing and `--dry-run` wasn't passed).

## Quickstart (development)

```bash
uv sync                 # install dependencies into .venv
uv run pytest -v        # run the test suite (offline, no network calls)
uv run ruff check .     # lint
```

Coverage isn't collected by default locally (it costs ~30% of runtime, and the
suite has a five-second budget). CI always runs with it; to check coverage
yourself:

```bash
uv run pytest --cov=jobbot --cov-report=term-missing
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

Secrets never live in yaml — they come from the environment only (a `.env`
file works too; it's git-ignored):

- `JOBBOT_DISCORD_WEBHOOK_URL` — **required** to actually publish. The
  webhook URL for the Discord channel jobs get posted to.
- `JOBBOT_DISCORD_ERROR_WEBHOOK_URL` — **optional**. A separate webhook for
  operational error messages (e.g. a source repeatedly failing), so failures
  don't get lost in the same channel as job postings. Falls back to not
  reporting errors to Discord at all if unset.

Nothing in `jobbot/` reads either of these directly (`jobbot/publisher.py`
takes a webhook URL as a plain argument) — wiring them from the environment
into a running bot is the orchestrator's (M5) job.
