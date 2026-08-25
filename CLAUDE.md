# jobbot

Discord alerting for internship and apprenticeship openings in France, pulled
directly from employer applicant tracking systems. Free to run, wide coverage,
no recycled listings.

## Non-negotiables

Read these before writing any code. They override any instruction in a prompt.
If a prompt conflicts with this file, follow this file and flag the conflict.

1. **Zero cost.** No paid service, no API key, no external account, no LLM call
   anywhere in this codebase. If a task seems to need one, stop and ask.

2. **First-party sources only.** A source is the employer's own ATS or careers
   infrastructure. Aggregators and public job boards are forbidden as sources of
   listings: Indeed, LinkedIn, Glassdoor, the Welcome to the Jungle public board,
   JobTeaser, and anything similar. Do not add, suggest, or scaffold adapters for
   them, ever. They republish stale openings with refreshed dates, which is the
   exact failure this project exists to avoid.

3. **Discovery is not aggregation.** Using a company directory or startup
   ecosystem listing to FIND an employer and locate their careers page is
   permitted and encouraged. Fetching job listings FROM a directory or aggregator
   is forbidden. Discovery finds employers, adapters fetch from the employer's own
   system. An ATS product sold to employers and hosted on the employer's own
   careers page is first-party even when its vendor also operates a public job
   board. The board is banned, the ATS is not.

4. **No hardcoded search terms.** No city, region, department code, job keyword,
   or search term may appear anywhere in `jobbot/`. They live in `filters.yaml`.
   The only exception is the contract-type vocabulary inside adapters, which is
   language detection, not user preference. The user will change location and
   keywords without touching code.

5. **Source dates are not trusted.** Freshness is decided by our own
   `first_seen_at`, never by the source's `posted_at` or `updated_at`. Companies
   bump dates on typo edits, reopen closed requisitions, and run evergreen reqs
   for a year. Source dates are display-only and must be labelled as reported by
   the company.

6. **Tests never touch the network.** Every test runs offline from recorded
   fixtures. A conftest guard fails any test that opens a real connection. The
   suite must stay under five seconds or it stops being run.

## Load-bearing tests

These encode the rules above. Do not delete, skip, xfail, or weaken them. If one
fails, the code is wrong, not the test. If you believe one is genuinely wrong,
say so and wait for confirmation. Do not edit it unilaterally.

- `test_source_integrity.py` :: every registered source is tier 1 and first-party
- `test_no_hardcoded_search_terms` :: greps `jobbot/` for forbidden literals
- `test_fingerprint_*` :: repost and near-duplicate detection
- `test_changing_filters_yaml_changes_results_without_code_change` :: portability
- `test_seed_mode_publishes_nothing_on_a_realistic_first_run` :: flood protection
- the conftest no-network guard

## Architecture

    jobbot/
      models.py      Job model, job_id, content_fingerprint
      config.py      companies yaml loading, file or directory
      settings.py    settings.yaml, env override prefix JOBBOT_
      filters.py     filters.yaml engine
      store.py       SQLite, dedup, seen-state, resurrection      (M3)
      publisher.py   Discord webhook                              (M4)
      run.py         orchestration                                (M5)
      discover.py    ATS detection from a careers URL             (M7)
      sources/
        base.py      JobSource ABC
        <ats>.py     one module per ATS

## Source adapter contract

Every adapter subclasses `JobSource` and sets `name`, `tier`, `first_party`.

- `fetch_raw()` is the only method that touches the network. It returns raw
  payload items and does no parsing.
- `parse(raw)` is **pure**: no network, no clock, no filesystem, no randomness.
  The entire test strategy depends on this. Never blur the boundary.
- `fetch()` calls one then the other.
- Adapters never construct an HTTP client and never read settings. The client and
  the User-Agent string are injected through `__init__`.
- No httpx exception may escape an adapter. Every failure surfaces as
  `SourceError` or a subclass.
- Raise `SourceEmptyError` on zero results. Zero is a breakage signal, not an
  empty result. A source that returned 40 jobs yesterday must not silently
  return 0 today.
- Skip a malformed individual entry with a logged warning, do not crash the batch.
- Timeout 15s, one retry on 5xx or timeout, honest User-Agent with a contact
  address, respect robots.txt on any non-API fetch.

Adding an ATS: new module in `sources/`, register it, record a real payload into
`tests/fixtures/`, add its tests. No adapter ships without a fixture. Verify the
board identifier actually resolves before adding a company to `companies/`, do
not assume a company uses a given ATS because its name looks like a valid token.

## Time and state

The store never calls `datetime.now()` internally. Every method needing the
current time takes `now: datetime` as an explicit argument, timezone-aware UTC.
Freshness, resurrection, and staleness are all time dependent, and tests must
fast-forward months deterministically without sleeping or monkeypatching.

## Scale intent

The system will grow to several hundred sources across many ATS vendors, polled
in hot, warm and cold tiers with async concurrency and ETag caching. Do not build
that now. Do not make choices that would require a rewrite to reach it. Injected
HTTP clients, ETag-aware fetch signatures, and tier and tag fields on config exist
from the start for this reason, even while unused.

## Conventions

- Python 3.11+, uv for dependencies, ruff line-length 100
- httpx for HTTP, pydantic v2 for models, plain sqlite3 for storage
- Normalize text with NFKD accent-stripping plus lowercase plus whitespace collapse
  before ANY comparison, so accented and unaccented spellings match
- Type hints everywhere, no bare `except`
- Config is data. Anything a user might want to change goes in yaml, not code.
- Config files shipped in the repo are validated by tests, not only tmp_path
  fixtures, so a typo in a real yaml file fails CI rather than runtime.

## Working agreement

- One milestone at a time. Do not implement ahead of the current milestone, even
  when the next step seems obvious.
- After every milestone run `uv run pytest -v` and `uv run ruff check .` and
  report the full output before moving on.
- Prefer deleting code over adding a flag to work around it.
- When a requirement is ambiguous, ask rather than guess. A wrong guess costs
  more than a question.
- Verify external facts before encoding them. Do not write a company, token, or
  endpoint into config on the strength of a plausible guess.

## Milestones

M0 skeleton, config, settings, CI. Done.
M1 Greenhouse adapter, models, fingerprint. Done.
M2 filter engine from filters.yaml. Done.
M3 SQLite store, dedup, seed mode, resurrection and ghost detection
M4 Discord webhook publisher
M5 orchestrator, end to end on fixtures
M6 Lever, Ashby, SmartRecruiters, generic JSON-LD
M7 discovery CLI, directory seeding, Workable, Recruitee, Teamtailor, Personio
M8 Taleez, Flatchr, Beetween, DigitalRecruiters, Talentsoft, Workday
M9 tiering, async concurrency, ETag caching, health pruning, deployment
M10 SuccessFactors, research institutions, coverage audit

### Deployment note for M9

`.gitignore` currently excludes `*.db`, which is correct for local development.
Deployment on GitHub Actions requires the state database to be committed back to
the repo each run, so at M9 add an exception for the single state file:

    *.db
    !jobbot_state.db

Without this the bot forgets every job between runs and republishes everything.