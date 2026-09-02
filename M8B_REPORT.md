# M8b Report — Empty-Board False Failure + Workday Coverage

## CLAUDE.md conflict flag

Part A directly contradicts the literal text in "Source adapter contract":
*"Raise `SourceEmptyError` on zero results. Zero is a breakage signal, not an
empty result."* Per the prompt's instruction, I followed the prompt (which was
written after CLAUDE.md, with full awareness of that clause) rather than
silently reconciling them, since the underlying intent — telling breakage
apart from emptiness — is what the change actually improves; it just
relocates the decision to the orchestrator, where `has_seen_postings` history
lives, instead of leaving each adapter to guess blind. `SourceEmptyError`
itself is untouched and still governs jsonld's genuine parse-failure case.
Flagging this per the "follow this file and flag the conflict" rule — CLAUDE.md
itself was not edited.

## Part A — empty-board false failure

- `greenhouse.py`, `lever.py`, `ashby.py`, `workday.py`: zero results now
  returns `[]` instead of raising `SourceEmptyError`. `jsonld.py` unchanged —
  still raises it on "no JobPosting block at all."
- `store.py`: `SCHEMA_VERSION` 2→3, `source_health.has_seen_postings INTEGER
  NOT NULL DEFAULT 0`, chained v1→v2→v3 migration in `initialize()`, latched
  via `MAX(existing, new)` in `record_success()`'s upsert, new
  `has_seen_postings()` reader.
- `run.py`: `process_source()` now returns `(publishable, fetched_count,
  verdict_counts, ok)`. On zero jobs: `has_seen_postings` true →
  `record_failure()` + `ok=False`; false → `record_success(count=0)` +
  `ok=True`. `run()` now branches on `ok`, not `fetched_count == 0`.
- Tests added: first-ever zero succeeds, zero-after-nonzero fails (and
  doesn't mark the prior job disappeared), `has_seen_postings` persists
  across reopen, v1→v3 and v2→v3 migrations, `run()` doesn't count a
  legitimately-empty source as failed. Skello-style boards no longer
  accumulate `consecutive_failures` forever.

## Part B — Workday investigation (real numbers)

Tested live against Sanofi, Michelin, Airbus:

- **`searchText`**: works, portable across all three tenants. "alternance":
  764→6 (Sanofi), 719→99 (Michelin), 2000+→12 (Airbus). "apprenticeship" /
  "internship" behave similarly. "stage" is noisier (fuzzy/stemmed match
  pulls in a few unrelated titles) but still finds genuine "stage" postings
  the others miss — harmless noise since `classify_contract_type()` correctly
  buckets it as "other".
- **`appliedFacets`**: `workerSubType` exposes Apprentice/Intern on some
  tenants, but value IDs are opaque tenant-specific hashes and Michelin lacks
  the facet entirely — not portable, rejected in favor of `searchText`.

Implemented: `WorkdaySource(search_terms=...)`, one query per term, deduped
by `externalPath`, `MAX_PAGES_PER_SEARCH_TERM=30` (vs. `MAX_PAGES=20` for the
no-search-terms fallback, unchanged). `workday_search_terms` added to
`Settings`/`settings.yaml` (`[alternance, stage, apprenticeship,
internship]`, empty default — CLAUDE.md rule 4), threaded through
`build_source()`.

### New real timings (all four terms, live fetch)

| Employer | Unique postings | Time |
|---|---|---|
| Sanofi | 111 | ~7.0s |
| Michelin | 178 | ~10.3s |
| Airbus | 513 | ~30–32s |

All under the 60s budget, and no per-term page-cap warning fired anywhere —
`MAX_PAGES_PER_SEARCH_TERM=30` wasn't hit once, so none of the three is
truncated. (For contrast: the earlier plain-pagination run on these same
boards took 100+s and truncated Sanofi at 400/764.)

## Final verification

```
396 passed in 3.43s
ruff check . → All checks passed!
```

9 new tests for Part A, 8 new tests for Part B (6 in `test_workday.py`
covering per-term querying, dedup, independent pagination, per-term and
cross-term caps; 2 in `test_run.py` for `build_source` wiring). Suite stays
well under 6s.
