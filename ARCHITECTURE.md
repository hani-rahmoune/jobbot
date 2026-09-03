# jobbot: architecture, decisions, and post-mortem

A Discord alerting system that watches French employers' own applicant tracking
systems for internship (stage) and apprenticeship (alternance) postings in data
and AI, and posts new matches to a Discord channel within minutes of appearing.

Built over roughly ten milestones. Runs unattended on GitHub Actions at zero
cost. 462 tests, 8 ATS adapters, 29 employer sources, ~2,200 postings fetched
per poll cycle.

---

## 1. The problem

Job aggregators are hostile to job seekers in a specific way: they recycle. The
same opening reappears with a refreshed date weeks after it was posted, or after
it was filled. You cannot tell a genuinely new opening from an old one wearing a
new timestamp, so you either apply late to everything or waste time on dead
listings.

The requirement was therefore not "show me jobs" but "tell me the moment a
genuinely new one appears, and never tell me twice."

Secondary requirements that shaped everything:

- Zero cost. No paid hosting, no API keys, no LLM inference at runtime.
- French internships and alternance specifically, in data and AI, around Paris
  and Nantes, at companies of any size.
- The search criteria had to be changeable without touching code, because
  location and interests move.

---

## 2. The three decisions that shaped the system

### 2.1 First-party sources only

The single most important architectural constraint. A source is the employer's
own applicant tracking system, never an aggregator.

Greenhouse, Lever, Ashby, Workday, SmartRecruiters, iCIMS, and Talentsoft are
not job boards. They are the software the employer's recruiters actually work
in, and their public endpoints serve exactly what that employer has published.
There is no republishing layer between the fetch and the truth, so the recycling
problem structurally cannot occur.

This was encoded as a rule in the project's constitution file and enforced by a
test that walks every registered adapter class and asserts `tier == 1` and
`first_party is True`. Adding an Indeed scraper would fail CI.

A related distinction had to be drawn explicitly, because it is easy to blur:

> Using a company directory to **find** an employer is permitted. Fetching job
> listings **from** a directory is forbidden. Discovery finds employers,
> adapters fetch from the employer's own system.

### 2.2 Source dates are never trusted

Employers bump `updated_at` when a recruiter fixes a typo. They close and reopen
requisitions. They run evergreen postings for a year. A source's own date is
therefore evidence of nothing.

Freshness is decided by **our own clock**: `first_seen_at`, recorded the first
time the system observes a posting. Source dates are stored and displayed, but
labelled explicitly as reported by the company, and never used in any decision.

### 2.3 Configuration is data, never code

No city, region, department code, or job keyword appears anywhere in the
application source. They live in `filters.yaml`. Relocating from Paris to
Toulouse, or pivoting from data to fintech, is a YAML edit.

Enforced by two tests. One greps the source tree for forbidden literals and
fails if any appear. The other builds two different filter configs inline,
runs the same job list through both, and asserts they produce different
non-empty result sets while the filter module itself contains none of the
strings that differentiate them.

The only exception is the contract-type vocabulary (alternance, stage, intern,
and their inflections), which is language detection rather than user preference,
and lives in a dedicated module exempted by name.

---

## 3. Architecture

```
                    ┌──────────────┐
   companies/*.yaml │              │ filters.yaml    settings.yaml
        │           │              │      │               │
        ▼           │              │      ▼               ▼
   ┌─────────┐      │              │  ┌────────┐    ┌──────────┐
   │ config  │      │              │  │filters │    │ settings │
   └────┬────┘      │              │  └───┬────┘    └────┬─────┘
        │           │              │      │              │
        ▼           ▼              ▼      ▼              ▼
   ┌─────────────────────────────────────────────────────────┐
   │                        run.py                            │
   │  the only module that reads env, builds HTTP clients,    │
   │  and reads the clock                                     │
   └──┬───────────────┬──────────────┬──────────────┬─────────┘
      │               │              │              │
      ▼               ▼              ▼              ▼
 ┌─────────┐    ┌──────────┐   ┌─────────┐   ┌───────────┐
 │ sources │───▶│  models  │──▶│  store  │──▶│ publisher │──▶ Discord
 │ (8 ATS) │    │   Job    │   │ SQLite  │   │  webhook  │
 └─────────┘    └──────────┘   └─────────┘   └───────────┘
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `models.py` | The `Job` model, text normalization, `job_id`, `content_fingerprint` |
| `config.py` | Loads `companies/*.yaml`, validates ATS names, merges a directory |
| `settings.py` | Runtime knobs, environment overrides with a `JOBBOT_` prefix |
| `filters.py` | Location, contract type, and keyword matching from `filters.yaml` |
| `sources/base.py` | The adapter contract, auto-registration, error hierarchy |
| `sources/<vendor>.py` | One adapter per ATS vendor, 8 total |
| `sources/classify.py` | Contract-type detection from posting text |
| `store.py` | SQLite state, deduplication, verdict assignment, source health |
| `publisher.py` | Discord embed construction, chunking, rate limit handling |
| `run.py` | Orchestration, CLI, the only place real dependencies are wired |
| `discover.py` | Finds employers' ATS from their careers page, emits config |

### The dependency injection discipline

Every module except `run.py` is injection-only. Adapters never construct an
HTTP client, never read settings, never read the clock. The store never calls
`datetime.now()`; every method that needs the current time takes `now: datetime`
as an explicit argument.

This is not architectural purity for its own sake. It is what made 462 tests
run offline in under ten seconds, and it is what allowed time-dependent logic
(reposts within 180 days, resurrections after 7 days, staleness after 90) to be
tested by fast-forwarding months in a fixed clock rather than by sleeping or
monkeypatching.

---

## 4. The data model

### Two hashes, two jobs

```python
job_id = sha256(f"{source}:{external_id}")           # identifies a posting
content_fingerprint = sha256(
    f"{norm(company)}:{norm(title)}:{norm(location)}:{norm(description[:600])}"
)                                                     # identifies an opening
```

`job_id` answers "have I seen this exact listing before."

`content_fingerprint` answers "have I seen this *opening* before, even under a
different requisition number." This is the anti-repost mechanism: an employer
deleting a posting and republishing it gets a new `external_id` and therefore a
new `job_id`, but the fingerprint is unchanged.

The 600-character cut on the description is deliberate. Employers routinely edit
trailing boilerplate, legal notices, diversity statements, without the job
changing. Hashing the full description would make every such edit look like a
new opening.

All text passes through NFKD accent-stripping, lowercasing, and whitespace
collapse before hashing, so "Île-de-France" and "ile-de-france" are the same
string.

### The verdict system

Every observed posting is assigned exactly one verdict, and **only `NEW` is ever
publishable**:

| Verdict | Meaning | Publishes |
|---|---|---|
| `NEW` | First sighting, fingerprint never seen | yes |
| `KNOWN` | Same `job_id`, nothing changed | no |
| `BUMP` | Same `job_id`, minor fields or source date changed | no |
| `REPOST` | New `job_id`, fingerprint seen within 180 days | no |
| `RESURRECTION` | Known `job_id`, absent over 7 days, now back | no |
| `SEEDED` | Seed mode, record as already published | no |

This is enforced through a single `is_publishable(verdict)` helper, with a test
that asserts over every enum member, so a future verdict added without thought
fails CI rather than silently publishing.

### Schema evolution

The SQLite schema went through three versions, each with a real `ALTER TABLE`
migration path rather than a decorative version bump:

- **v1** jobs, fingerprints, source_health
- **v2** added `publish_pending` (see §6.3)
- **v3** added `has_seen_postings` (see §6.6)

---

## 5. The adapter contract

Eight adapters were built. Every one obeys the same contract:

```python
class JobSource(ABC):
    name: ClassVar[str]
    tier: ClassVar[int]          # 1 = employer ATS
    first_party: ClassVar[bool]

    def __init__(self, identifier, company_name, client, user_agent)
    def fetch_raw(self, etag=None, last_modified=None) -> tuple[list[dict], str | None]
    def parse(self, raw: list[dict]) -> list[Job]
    def fetch(self) -> list[Job]
```

Rules, all enforced by tests:

- `fetch_raw` is the **only** method that touches the network
- `parse` is **pure**: no network, no clock, no filesystem, no randomness
- No vendor exception may escape; every failure surfaces as `SourceError`
- A malformed individual entry is skipped with a warning, never crashes the batch
- Timeout 15s, one retry on 5xx or timeout, no retry on other 4xx
- Honest User-Agent carrying a contact URL
- `robots.txt` respected on any non-API fetch

The `etag` and `last_modified` parameters are accepted and currently ignored.
They exist so conditional-request caching can be added later without touching
every adapter.

### Vendors implemented

| Adapter | Shape | Notes |
|---|---|---|
| Greenhouse | GET, single JSON response | Simplest; the reference implementation |
| Lever | GET, JSON array | `createdAt` is epoch milliseconds |
| Ashby | GET, object with `jobs` array | ISO 8601 dates |
| JSON-LD | GET a careers page, parse `schema.org/JobPosting` | The long-tail adapter; handles single-object, array, and `@graph` shapes |
| Workday | POST with pagination | Enterprise; see §6.5 and §6.8 |
| SmartRecruiters | GET with `q` search param | Clean public API |
| iCIMS Jibe | GET `/api/jobs` on employer domain | Full descriptions inline |
| Talentsoft | GET HTML listing, parse server-rendered cards | See §6.9 |

---

## 6. What went wrong, and how it was handled

This section is the actual value of the write-up. Every item below was a real
defect found in a real system, most of them by testing rather than by reasoning.

### 6.1 The classifier missed every French inflection

The first contract-type classifier matched whole words against a fixed list:
`alternance`, `apprenti`, `stage`, `intern`.

Word-boundary matching meant *alternant*, *alternants*, *apprentissage*,
*apprentis*, *stagiaires*, and *internships* all classified as "other."
*Alternant* and *apprentissage* are two of the most common words in French job
advertisements. A large share of real matches were being silently discarded.

**Fix.** Per-concept regex with explicitly enumerated suffixes, never a blanket
`\w*`, because `\bintern\w*` would swallow "internal" and "international". A
parametrized test enumerates every accepted form and every form that must not
match.

### 6.2 "Stage" is also an English word

`stage` matches "Series B stage company", "Growth Stage Manager", and "Staging
Environment Engineer".

**Fix.** Bare *stage* and *stages* only count as internship vocabulary when the
surrounding text also contains a French marker (de, du, des, le, la, les, en,
pour, mois, au sein). *Stagiaire* needs no such guard, being unambiguous.

### 6.3 Descriptions poisoned the classifier

A senior engineering role whose description said "vous encadrerez des stagiaires
et alternants" (you will supervise interns and apprentices) classified as
apprenticeship. Mentioning the juniors you will manage is extremely common in
senior job advertisements.

**Fix.** Title is authoritative. If the title yields a contract type, return it.
Only if the title is silent does the description get consulted, and then the
vocabulary must appear within roughly 60 characters of a contract-context word
(contrat, durée, poste, recherchons, nous recrutons, looking for, hiring).

### 6.4 A published job could be lost forever

The most serious defect found. `unpublished_new()` originally queried
`last_verdict = 'NEW' AND published_at IS NULL`.

Trace the failure: a job is recorded `NEW` at 09:00. The publisher then fails,
because Discord rate-limited, or the runner was killed. At 09:20 the same job is
observed again, unchanged, verdict `KNOWN`, and `last_verdict` is overwritten.
The job is now invisible to `unpublished_new()` forever. It never publishes, and
nothing errors.

**Fix.** Publish-pending became a stored fact rather than something derived from
the latest verdict. A `publish_pending` column is set to 1 exactly when a job is
assigned `NEW`, is never cleared by any subsequent verdict, and is cleared only
by `mark_published`. Schema bumped to v2 with a real migration.

Three load-bearing tests now cover this: a `NEW` job survives an unpublished
poll cycle, survives a `BUMP` in between, and survives five consecutive `KNOWN`
polls.

### 6.5 Workday's page limit is 20, not 100

The Workday adapter was written assuming a page size of 100, based on how the
endpoint had been sampled during investigation with `limit=5`.

Every Workday source failed with HTTP 400 on the first real end-to-end run.
Direct probing pinned it precisely: `limit=20` returns 200, `limit=21` returns
400, consistently across four different tenants.

This is a good illustration of why the "verify live before committing" rule
exists. The documentation did not say this. Only testing found it.

### 6.6 Empty boards failed forever

The adapter contract originally said: raise `SourceEmptyError` on zero results,
because zero is a breakage signal.

That is correct for Datadog with 448 openings. It is wrong for a hundred-person
company whose board is genuinely empty. Skello's Lever board returns HTTP 200
with an empty array, and it accumulated four consecutive failures in a day, on
its way to failing every twenty minutes indefinitely.

**Fix.** The decision moved from the adapter to the orchestrator, where the
store's history is available. Adapters now return an empty list. A
`has_seen_postings` flag on `source_health` (schema v3) records whether that
source has ever returned postings. A first-ever zero records success; a zero
after a previous non-zero success records a failure.

This required a matching change in the orchestrator, which had been using
`fetched_count == 0` as its failure signal. `process_source` now returns an
explicit success flag.

### 6.7 Discovery guessed URLs that could never be reached

The first discovery implementation tried a fixed list of eleven candidate paths
(`/careers`, `/jobs`, `/carrieres`, and so on) with a cap of four attempts per
company. Six of the eleven paths, including the site root, were mathematically
unreachable on every run.

The result: 5 companies confirmed out of 22, and all 5 were already known.

**Fix, first attempt.** Fetch the site root first, always, then parse anchor
tags for careers-looking links and follow those before falling back to guessed
paths. This found one genuinely new company.

**Fix, second attempt.** The remaining failures all reported the same pattern: a
careers link was found and followed, but that page was a culture or team page
and the real ATS link was one hop further. Depth-2 crawling was added, with
listings-suggesting links (offres, postes, opportunités, openings) sorted ahead
of generic ones.

**Honest outcome.** Depth 2 changed nothing measurable on the same seed batch.
The remaining unresolved companies mostly render their listings in JavaScript,
which is a different problem entirely. The diagnostic output improved
substantially, which is worth something, but the hit rate did not.

Discovery's real yield across two batches of roughly 100 companies was about
7%. Manual research plus live verification proved more productive.

### 6.8 Workday truncation, and the better fix

With a 20-page cap and 20 results per page, each Workday employer was limited to
400 postings. Sanofi has 760. Which 400 you got depended on Workday's return
order, so alternance postings sorting late were invisible.

Raising the cap worked but cost 100+ seconds per large employer.

**Fix.** Server-side search. Investigation confirmed `searchText` works and is
portable across tenants:

| Employer | Unfiltered | `searchText=alternance` |
|---|---|---|
| Sanofi | 764 | 6 |
| Michelin | 719 | 99 |
| Airbus | 2000+ | 12 |

`appliedFacets` was investigated and rejected: `workerSubType` exposes
Apprentice and Intern on some tenants, but the value IDs are opaque
tenant-specific hashes, and Michelin lacks the facet entirely.

Result: Sanofi went from 400 truncated postings in 100 seconds to 111 complete
relevant ones in 7 seconds. The search terms live in settings, not code.

### 6.9 Talentsoft ships two different templates

The Talentsoft HTML adapter hit two surprises that a naive implementation would
have gotten badly wrong:

1. **Different page sizes per tenant.** Crédit Agricole CIB renders a "card"
   template at 100 per page; LCL renders a "list" template at 10 per page.
   Page size is therefore *measured* from page 1 rather than assumed.

2. **Pagination wraps around.** Requesting a page number past the end silently
   returns page 1 again, rather than an empty page. A "stop when the page is
   short" loop would never terminate. Pagination is driven by the page's own
   reported "N offres" count instead.

### 6.10 The filter matched on descriptions

An early live run returned three matches, all of which looked plausible:
"Coordinator, Emerging Talent Recruiting", "Product Manager Intern", "Strategy &
Operations Intern".

None of them was a data role. They matched because the keyword filter searched
the description, and the word "data" appears in essentially every technology job
description ("data-driven decisions", "our data platform", "candidate data").

The real match count was zero, and the filter was doing almost nothing.

**Fix.** `keywords.fields: [title]`. A data role says so in its title. Match
count dropped to zero, which was the honest answer for that company list.

### 6.11 French keywords that were too broad

`analyste` was added as a French keyword. It surfaced "Analyste fiscal", a tax
analyst role at AXA. In French, *analyste* prefixes finance, legal, and business
roles at least as often as data ones. Same problem with *études* and *bi*.

**Fix.** Replaced with multi-word phrases: *analyste de données*, *ingénieur
données*, *science des données*, *business intelligence*. The word-boundary
matcher requires the whole phrase, so these are safe.

### 6.12 Operational failures

Three separate incidents where the system was correct but the operator was not:

- **Code never pushed.** Two debugging sessions were spent on "the bot isn't
  posting" when the actual cause was that a completed milestone had never
  reached GitHub. The deployed bot only ever runs what is pushed. The log line
  `sources: 12/12` against a local config of 23 was the tell.

- **`.gitignore` swallowed a config file.** Discovery's raw output file was
  gitignored by name, which also matched the promoted copy inside `companies/`.
  Five companies silently never deployed. Fixed by anchoring the ignore rule to
  the repository root with a leading slash.

- **State database merge conflicts.** The bot commits its SQLite state file, and
  so does the operator. SQLite is binary and git cannot merge it. Every pull
  conflicted. Fixed with a `.gitattributes` entry marking the file
  `merge=ours`.

### 6.13 Test suite performance

The suite budget was five seconds, chosen so that running tests stays cheap
enough to actually do constantly.

Two real threats to it were found and fixed:

- `httpx.Client()` pays roughly 0.6 to 1.2 seconds of SSL context and CA bundle
  setup per construction on Windows. Tests constructing their own clients per
  test were the dominant cost. Fixed with a single session-scoped client using
  `verify=False`, safe because respx intercepts below the TLS layer.

- Coverage instrumentation roughly doubled runtime. Fixed by setting
  `core = "sysmon"` in the coverage config, which uses Python 3.12's low
  overhead `sys.monitoring` API. Cut coverage-enabled runs roughly in half.

At 462 tests the budget was eventually raised to fifteen seconds, deliberately
and with a note explaining that the number is a means rather than a goal.

---

## 7. Vendor investigation results

Large French employers are the highest-yield source of alternance, because the
*taxe d'apprentissage* gives them direct financial incentive to hire apprentices
at scale. Reaching them meant investigating enterprise ATS vendors.

The investigation method that worked: open a real employer's careers page and
inspect what that page itself fetches, rather than reading the vendor's
documented API. A careers page has to load its listings somehow, and that
request is often reachable.

| Vendor | Public endpoint? | Verdict |
|---|---|---|
| Workday | Yes, POST with JSON body | **Built.** Unlocks Renault, Stellantis, Sanofi, Michelin, Airbus, Veolia |
| SmartRecruiters | Yes, clean REST | **Built.** Kiabi, Boulanger, Ubisoft |
| iCIMS "Jibe" | Yes, `/api/jobs` on employer domain | **Built.** AXA, with full descriptions inline |
| Cegid Talentsoft | Yes, server-rendered HTML | **Built.** Crédit Agricole CIB, LCL |
| SAP SuccessFactors | Reachable but rejected | XML feed returns 14.6 MB unauthenticated, but is not well-formed, has no location field, and no canonical URL field |
| Phenom People | No | Session-bound; CSRF handshake replicated and still 303-redirected |
| Oleeo | No | CAPTCHA on the job search page itself (BNP Paribas) |
| iCIMS "classic" | No | Pure JavaScript shell; zero content without a browser (Carrefour) |
| Avature | Not found | No in-scope French employer confirmed; one candidate returned 403 |
| Oracle Taleo | Not found | In-scope employers have migrated off it |

The rejections are as valuable as the successes. They document which ground has
already been covered, so it is not re-investigated later.

---

## 8. Operational design

### Free hosting

GitHub Actions on a public repository, cron every 20 minutes. The SQLite state
file is committed back to the repository by the workflow, which is both how
state persists between stateless runs and how the repository stays active
(GitHub disables scheduled workflows after 60 days of inactivity, and a bot that
finds jobs keeps itself alive).

Scheduled workflows on free runners are best-effort. Observed intervals ranged
from 17 minutes to over four hours. A missed run does not lose a posting; the
next run still sees it as `NEW`.

### Publish-then-mark, and why

A webhook POST and a SQLite write cannot be made atomic. One of two failure
modes must be chosen:

- Mark published before sending: a crash in between loses the job **silently and
  permanently**.
- Send before marking: a crash in between produces a **visible duplicate**.

The second was chosen. A duplicate is annoying and self-evident. Silent loss is
undetectable and never self-corrects. The window is minimized by marking the
whole confirmed batch in a single transaction rather than looping per job.

### Seed mode

The first run against a fresh state database would treat every currently-open
posting at every configured employer as new, and post hundreds of messages at
once. `--seed` records everything as already published without sending anything.

This is protected by a load-bearing test that builds 200 jobs, seeds them,
asserts nothing is publishable, re-records them the next day in normal mode,
asserts nothing is publishable, then adds three genuinely new jobs and asserts
exactly those three come back.

### Pre-flight and observability

- `--check` validates config parsing, adapter registration, webhook shape
  (without ever logging the value), and database schema version. Exits 0 or 2.
- `--stats` prints totals, pending, stale, disappeared, per-company counts, and
  the ten most recent publications, without fetching anything.
- `--dry-run` builds every payload and validates every Discord limit while
  making zero requests.

---

## 9. Where it ended up

| Metric | Value |
|---|---|
| ATS adapters | 8 |
| Employer sources | 29 |
| Postings fetched per poll | ~2,200 |
| Full poll wall time | 87 seconds, sequential |
| Slowest single source | Airbus, 28 seconds |
| Tests | 462 |
| Test suite runtime | 8.5 seconds, offline |
| Coverage | 98% overall |
| Running cost | zero |

Coverage spans French AI startups (Dust, H Company, Photoroom, Illuin, Akur8),
French scale-ups (Dataiku, Brevo, Ippon, Owkin), US technology companies with
Paris offices, and large French corporates (Renault, Sanofi, Airbus, Michelin,
Veolia, Stellantis, AXA, Crédit Agricole CIB, LCL, Kiabi, Boulanger, Ubisoft).

---

## 10. Known limitations

**JavaScript-rendered careers pages are invisible.** A significant share of
French SME careers pages render listings client-side. Neither the JSON-LD
adapter nor discovery can see them without a headless browser, which would
multiply the deployment footprint.

**Nantes coverage is thin.** Small regional companies rarely run the enterprise
ATS vendors that were profitable to build for. The French SME vendors (Flatchr,
Beetween, DigitalRecruiters, Taleez) remain unbuilt; Taleez specifically was
investigated and found to require an API key on every public route.

**Workday postings have no description or location in the list response.** The
search endpoint returns titles and paths only. Enriching requires a second
request per job.

**Search terms in settings couple to filters.** Workday and the other
search-capable adapters filter server-side using terms from `settings.yaml`. If
`filters.yaml` were widened to include full-time roles, those sources would
still only fetch alternance and stage. Two files that must agree.

**No async concurrency.** Sources are polled sequentially. Fine at 29 sources
and 87 seconds; the tiering fields (`hot`, `warm`, `cold`) exist on every config
entry but nothing yet varies poll frequency by tier.

---

## 11. What the process taught

**Verify externally, always.** The highest-value rule in the project's
constitution turned out to be "do not write a company, token, or endpoint into
config on the strength of a plausible guess." An early attempt at adding six
companies by guessing board tokens from company names had a one-in-six success
rate. Live verification caught it. It later caught two Ashby tokens that
resolved to entirely different, non-French companies with the same slug, which
would have quietly poisoned the feed.

**Load-bearing tests need to be named as such.** Several tests encode
requirements rather than behaviour: the source-integrity check, the
no-hardcoded-terms grep, the seed-mode flood test, the filter-portability test.
Marking them explicitly as not-to-be-weakened, in a file read at the start of
every session, prevented them from being quietly relaxed when they became
inconvenient.

**The specification is frequently wrong.** The `MAX_URL_ATTEMPTS = 4` cap
against an eleven-item candidate list, the `limit=100` Workday assumption, the
"zero means breakage" rule, and the requirement that seed mode needs a webhook
were all specification errors, not implementation errors. Each was found by
running the real thing.

**Zero results is a finding, not a failure.** The run that returned "0 passed
filter" after the description-matching fix was the most informative result in
the project. It proved the previous three matches were false positives and that
the company list, not the code, was the constraint.
