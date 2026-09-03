"""The orchestrator: fetch, filter, store, publish, in one runnable command.

This is the first module allowed to read settings.yaml, read the
environment, and construct an httpx.Client. Every other module in jobbot/
stays injection-only (see sources/base.py, filters.py, store.py,
publisher.py) precisely so that everything except this file can be tested
without any of those real things -- main() is the one place that wires them
together for a real run.

Deliberate deviations from this milestone's literal API sketch, all needed
to make RunReport (also specified by this milestone) actually fillable
without reaching into JobStore's private internals from here:

- process_source() returns a 4-tuple, not 2: (publishable, fetched_count,
  verdict_counts, ok). verdict_counts (a dict of JobVerdict name -> count,
  for every job that passed the filter this call) is what lets run()
  populate RunReport.verdicts accurately. Without it, the only verdict
  run() could ever see is "NEW" (since that's the only one implied by a job
  appearing in `publishable`), and RunReport.verdicts would be permanently
  wrong for KNOWN/BUMP/REPOST/RESURRECTION/SEEDED. `ok` is M8b's fix: once a
  legitimately empty board became a valid fetch() outcome,
  `fetched_count == 0` stopped being a usable failure signal on its own, so
  process_source reports success/failure explicitly instead.
- process_source() takes `seed: bool = False`. store.record() needs a
  per-job seed_mode flag to produce SEEDED verdicts correctly, and
  process_source is where record() is called.

M9c: concurrent polling. process_source() used to do fetch-then-record in
one call, on one thread, always -- fine when run() polled every company
sequentially, but with settings.poll_concurrency companies fetching in
parallel, JobStore (plain sqlite3, single-writer) must never be touched
from more than one thread. So process_source() is now a thin sequential
wrapper composed from two pieces that run() calls separately:

- fetch_source() does the ONLY network-touching work -- build the adapter,
  call fetch() -- and returns a plain FetchOutcome record. No store access
  at all, which is exactly what makes it safe to run inside a worker thread.
- record_fetch_outcome() does everything process_source() used to do
  *after* the fetch: record_success/record_failure, filter, store.record(),
  mark_absent(). This is the store-touching remainder, and run() calls it
  only on the main thread, once per company, strictly in the companies'
  original config order -- not completion order, so a run's observable
  behavior (ordering-sensitive things like which duplicate-content job
  "wins" a fingerprint race) doesn't depend on which worker happened to
  finish first.

Every worker builds its own httpx.Client (never shared across threads,
since httpx.Client isn't documented as thread-safe for concurrent use) via
a transport wrapped in _HostThrottledTransport, which caps concurrent
in-flight requests to any single hostname at a fixed limit shared across
every worker -- protects a vendor's shared infrastructure (e.g. every
SmartRecruiters-hosted company's requests all land on
api.smartrecruiters.com) from being hammered just because our own polling
now fans out across many companies at once. This is a real transport-level
wrapper, not an httpx event hook, deliberately: an event hook pair
(acquire on request, release on response) never releases if the request
itself raises before a response exists, which would eventually deadlock
that host's semaphore after enough timeouts/connection errors. The
transport wraps the acquire/release in a plain try/finally instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from jobbot.config import CompanySource, ConfigError, load_companies
from jobbot.filters import FilterConfigError, JobFilter, load_filters
from jobbot.models import Job
from jobbot.publisher import DiscordPublisher
from jobbot.settings import SettingsError, load_settings

# Imported for its side effect: defining a JobSource subclass registers it
# (see sources/base.py's __init_subclass__), which is what makes it visible
# to build_source() below. Add new adapter imports here as they land.
from jobbot.sources.ashby import AshbySource  # noqa: F401
from jobbot.sources.base import JobSource, SourceError, registered_sources
from jobbot.sources.greenhouse import GreenhouseSource  # noqa: F401
from jobbot.sources.jibe import JibeSource
from jobbot.sources.jsonld import JsonLdSource  # noqa: F401
from jobbot.sources.lever import LeverSource  # noqa: F401
from jobbot.sources.rendered import (
    MAX_RENDERED_SOURCES_PER_POLL,
    RenderedSource,  # noqa: F401
)
from jobbot.sources.sitemap_jsonld import SitemapJsonLdSource
from jobbot.sources.smartrecruiters import SmartRecruitersSource
from jobbot.sources.successfactors import SuccessFactorsSource
from jobbot.sources.talentsoft import TalentsoftSource
from jobbot.sources.workday import WorkdaySource
from jobbot.store import SCHEMA_VERSION, JobStore, StoreStats, is_publishable

# Adapter classes whose constructor accepts a `search_terms` kwarg (M9):
# settings.search_terms is threaded through to exactly these, by identity,
# not by name string -- every other adapter's constructor has no such
# concept and must not be passed one. See each module's docstring for its
# own confirmed server-side search parameter (or, for sitemap_jsonld, its
# client-side sitemap-slug pre-filter) and real narrowing numbers.
_SEARCH_CAPABLE_ADAPTERS = (
    WorkdaySource, SmartRecruitersSource, JibeSource, TalentsoftSource, SitemapJsonLdSource,
    SuccessFactorsSource,
)

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    sources_attempted: int = 0
    sources_failed: int = 0
    jobs_fetched: int = 0
    jobs_passing_filter: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    published: int = 0
    publish_failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class FetchOutcome:
    """The result of fetch_source() for one company -- a plain value object,
    no store or filter object attached, so it's safe to build inside a
    worker thread and hand back to the main thread for
    record_fetch_outcome() to act on.

    Exactly one of three shapes:
    - build_error set, source_name None: build_source() itself rejected this
      company's `ats` (config.py's KNOWN_ATS should already prevent this for
      a validly-loaded companies/*.yaml, but build_source doesn't assume
      that -- see its own docstring). Nothing in this case is ever a
      per-source SourceError, so record_fetch_outcome() must not touch
      source_health for it: there is no source_name to key that table by.
    - fetch_error set, source_name set: source.fetch() raised SourceError.
    - jobs set (possibly []), both errors None: a genuine fetch, however
      many postings it returned -- record_fetch_outcome() is what decides
      whether zero of them is a failure (see its own docstring, same M8b
      logic process_source() always used).
    """

    jobs: list[Job] | None = None
    source_name: str | None = None
    build_error: str | None = None
    fetch_error: str | None = None


def _order_and_cap_companies(
    companies: list[CompanySource],
) -> tuple[list[CompanySource], list[CompanySource]]:
    """M9e: every non-"rendered" company first, in their original
    companies/*.yaml order, then every "rendered" one -- a real headless
    browser launch is drastically heavier than any other adapter's fetch,
    so the cheap sources that make up the vast majority of a real poll must
    never be delayed behind a handful of slow ones. Only the first
    MAX_RENDERED_SOURCES_PER_POLL rendered entries are kept; the rest are
    returned separately (never attempted this poll, not silently dropped --
    the caller logs them) rather than let one company's config growth
    quietly blow the whole poll's time budget.

    Both groups keep their own relative order from the input list (a stable
    partition, not a re-sort), so which specific rendered entries get
    skipped when there are more than the cap is deterministic and stable
    across polls, not dependent on dict ordering or any other incidental
    detail.
    """
    non_rendered = [c for c in companies if c.ats != "rendered"]
    rendered = [c for c in companies if c.ats == "rendered"]
    kept_rendered = rendered[:MAX_RENDERED_SOURCES_PER_POLL]
    skipped_rendered = rendered[MAX_RENDERED_SOURCES_PER_POLL:]
    return non_rendered + kept_rendered, skipped_rendered


def build_source(
    company: CompanySource,
    client: httpx.Client,
    user_agent: str,
    search_terms: list[str] | None = None,
) -> JobSource:
    """Maps a company's `ats` string to its adapter class. Raises ValueError
    on an ats with no registered adapter -- config.py's KNOWN_ATS already
    keeps this from happening for a validly-loaded companies/*.yaml, but
    build_source doesn't assume that; it's a real check, not a formality.

    search_terms (M8b/M9, from settings.yaml's search_terms -- never
    hardcoded here, CLAUDE.md rule 4) is threaded through only to the
    adapters in _SEARCH_CAPABLE_ADAPTERS; every other adapter's constructor
    has no such concept.
    """
    adapters = {cls.name: cls for cls in registered_sources()}
    try:
        adapter_cls = adapters[company.ats]
    except KeyError:
        raise ValueError(
            f"No adapter registered for ats {company.ats!r} (company {company.name!r}); "
            f"registered: {sorted(adapters)}"
        ) from None

    if adapter_cls in _SEARCH_CAPABLE_ADAPTERS:
        return adapter_cls(
            company.identifier,
            company.name,
            client,
            user_agent=user_agent,
            search_terms=search_terms,
        )
    return adapter_cls(company.identifier, company.name, client, user_agent=user_agent)


def fetch_source(
    company: CompanySource,
    client: httpx.Client,
    user_agent: str,
    search_terms: list[str] | None,
) -> FetchOutcome:
    """The only network-touching step for one company -- build its adapter,
    fetch it -- returned as a plain value object. Safe to call from a worker thread
    (see M9c's module-docstring note): touches nothing but `client`, which
    the caller must not share with any other concurrent call.
    """
    try:
        source = build_source(company, client, user_agent, search_terms=search_terms)
    except ValueError as exc:
        return FetchOutcome(build_error=str(exc))

    try:
        jobs = source.fetch()
    except SourceError as exc:
        return FetchOutcome(fetch_error=str(exc), source_name=source.name)

    return FetchOutcome(jobs=jobs, source_name=source.name)


def record_fetch_outcome(
    outcome: FetchOutcome,
    company: CompanySource,
    job_filter: JobFilter,
    store: JobStore,
    now: datetime,
    seed: bool = False,
) -> tuple[list[tuple[Job, list[str]]], int, dict[str, int], bool]:
    """Filter, record, mark_absent, record_success/record_failure for one
    company's already-fetched outcome. Must only ever be called from the
    thread that owns `store` (see M9c's module docstring) -- unlike
    fetch_source(), this one writes.

    Filter runs BEFORE store.record() (B3): a job the user's filters.yaml
    doesn't want is never given a row at all. This keeps the store's size,
    and its repost/resurrection window logic, scoped to postings that
    actually matter to this user -- and it means loosening filters.yaml
    later can't retroactively "resurrect" a job that was silently dropped;
    from the store's point of view it was never seen, so it's genuinely NEW
    again if it still exists on the board.

    mark_absent() is only ever called on a genuine success, with exactly the
    job_ids this fetch actually returned -- deliberately every fetched
    job_id, not just the ones that passed the filter, since "the employer
    took this posting down" and "our filters don't want this posting" are
    different facts. A failed fetch (including a suspicious empty one, see
    below) must never mark a company's postings disappeared just because we
    couldn't trust what we got.

    outcome.build_error: build_source() itself failed -- there's no
    source_name to record anything against in source_health, so nothing is
    written; the caller reports outcome.build_error itself.

    outcome.fetch_error: the failure is recorded via store.record_failure()
    and this returns ([], 0, {}, False) rather than raising -- one source's
    outage must never abort the run for every other source.

    M8b: fetch() returning an empty list is no longer automatically a
    failure (adapters raise SourceEmptyError only where jsonld's genuine
    parse-failure case still applies). Zero jobs here is disambiguated with
    the store's own history: if this source+company has ever previously
    returned postings (has_seen_postings), a sudden zero is treated as a
    failure -- a board that had postings yesterday and none today is almost
    always broken, not emptied out overnight. If it has never returned
    postings, zero is recorded as an ordinary success; some small companies'
    boards are just legitimately, permanently quiet, and failing them on
    every single poll forever (inflating consecutive_failures for a
    condition that was never going to change) helps no one.

    The bool is the caller's success/failure signal (A4): fetched_count == 0
    stopped being a valid proxy for "this failed" the moment a legitimate
    empty board became a possible outcome.
    """
    if outcome.build_error is not None:
        return [], 0, {}, False

    source_name = outcome.source_name
    assert source_name is not None  # every non-build_error outcome sets this

    if outcome.fetch_error is not None:
        store.record_failure(source_name, company.name, outcome.fetch_error, now)
        return [], 0, {}, False

    jobs = outcome.jobs or []

    if not jobs:
        if store.has_seen_postings(source_name, company.name):
            store.record_failure(
                source_name, company.name,
                "returned zero postings after previously returning some -- "
                "likely broken, not empty",
                now,
            )
            return [], 0, {}, False

        store.record_success(source_name, company.name, 0, now)
        store.mark_absent(source_name, company.name, set(), now)
        return [], 0, {}, True

    store.record_success(source_name, company.name, len(jobs), now)

    seen_job_ids = {job.job_id for job in jobs}
    publishable: list[tuple[Job, list[str]]] = []
    verdict_counts: dict[str, int] = {}

    for job in jobs:
        result = job_filter.matches(job)
        if not result.passed:
            continue

        verdict = store.record(job, now, seed_mode=seed)
        verdict_counts[verdict.value] = verdict_counts.get(verdict.value, 0) + 1

        if is_publishable(verdict):
            publishable.append((job, result.matched_keywords))

    store.mark_absent(source_name, company.name, seen_job_ids, now)

    return publishable, len(jobs), verdict_counts, True


def process_source(
    source: JobSource,
    company: CompanySource,
    job_filter: JobFilter,
    store: JobStore,
    now: datetime,
    seed: bool = False,
) -> tuple[list[tuple[Job, list[str]]], int, dict[str, int], bool]:
    """Fetch (on an already-built `source`) then immediately record, both on
    the calling thread -- the sequential convenience every existing test and
    the non-concurrent call site used before M9c. Concurrent polling in
    run() calls fetch_source() and record_fetch_outcome() separately
    instead, precisely so the store is never touched from a worker thread --
    see this module's docstring.
    """
    try:
        jobs = source.fetch()
        outcome = FetchOutcome(jobs=jobs, source_name=source.name)
    except SourceError as exc:
        outcome = FetchOutcome(fetch_error=str(exc), source_name=source.name)

    return record_fetch_outcome(outcome, company, job_filter, store, now, seed)


class _HostThrottle:
    """Caps concurrent in-flight requests to any single hostname, shared
    across every worker thread's own httpx.Client -- see M9c's module
    docstring for why this exists and why it's a transport wrapper rather
    than an httpx event-hook pair.
    """

    def __init__(self, per_host_limit: int) -> None:
        self._per_host_limit = per_host_limit
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()

    def semaphore_for(self, host: str) -> threading.Semaphore:
        with self._lock:
            semaphore = self._semaphores.get(host)
            if semaphore is None:
                semaphore = threading.Semaphore(self._per_host_limit)
                self._semaphores[host] = semaphore
            return semaphore


class _HostThrottledTransport(httpx.BaseTransport):
    """Wraps a real transport so every request acquires its host's
    semaphore first and releases it in a `finally` -- guaranteed even when
    the wrapped transport raises (a timeout or connection error) before any
    response exists, unlike an event-hook pair, where the release-side hook
    would simply never fire.
    """

    def __init__(self, wrapped: httpx.BaseTransport, throttle: _HostThrottle) -> None:
        self._wrapped = wrapped
        self._throttle = throttle

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        semaphore = self._throttle.semaphore_for(request.url.host)
        semaphore.acquire()
        try:
            return self._wrapped.handle_request(request)
        finally:
            semaphore.release()

    def close(self) -> None:
        self._wrapped.close()


# M9c: 2 concurrent requests per hostname, regardless of poll_concurrency --
# a fixed platform-protection limit, not a user-tunable setting (unlike
# poll_concurrency itself, which trades off total poll wall-time against how
# hard we hit the *sum* of every vendor's infrastructure at once).
_PER_HOST_CONCURRENCY_LIMIT = 2


def _fetch_one_concurrently(
    company: CompanySource, user_agent: str, search_terms: list[str] | None, throttle: _HostThrottle
) -> FetchOutcome:
    """Runs inside a worker thread (see ThreadPoolExecutor in run()). Builds
    its own httpx.Client -- never shared across threads or across companies
    -- wrapped in _HostThrottledTransport so concurrent fetches across
    companies still respect the shared per-host limit.
    """
    transport = _HostThrottledTransport(httpx.HTTPTransport(), throttle)
    # M12 Part A: follow_redirects=True -- without it, a job URL that
    # 302s (locale-suffixed paths, canonicalization, trailing slashes) gets
    # its redirect response body back instead of the real page, which every
    # adapter's own parsing then silently reads as "no content here" rather
    # than as a fetch failure. Confirmed live cost: Nexans's entire board.
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        return fetch_source(company, client, user_agent, search_terms)


def run(
    config_dir: Path,
    filters_path: Path,
    settings_path: Path,
    webhook_url: str,
    error_webhook_url: str | None,
    now: datetime,
    dry_run: bool = False,
    seed: bool = False,
) -> RunReport:
    """One full poll cycle. Every dependency -- paths, webhook URLs, the
    clock -- is passed in; main() is the only caller that supplies real
    ones.

    Publish-then-mark, and why (B1): a webhook POST and a SQLite write can't
    be made atomic with each other. Publishing before marking risks a
    duplicate post if the process dies in between (publish_pending never
    got cleared, so the next run republishes the same job). Marking before
    publishing risks permanently losing the job if the process dies in
    between (publish_pending clears, but publish() was never confirmed to
    have actually sent it). We choose the former: a duplicate is visible and
    annoying; silent loss is neither, and would never self-correct. That's
    why mark_published_batch() is called only once, after publish() returns
    a confirmed set of successes, in one transaction covering every job
    that cycle's send actually confirmed -- not a loop of per-job
    mark_published() commits, which would just relocate the same crash
    window instead of shrinking it.
    """
    settings = load_settings(settings_path)
    filters_config = load_filters(filters_path)
    companies, skipped_rendered = _order_and_cap_companies(load_companies(config_dir))
    job_filter = JobFilter(filters_config)

    report = RunReport()
    user_agent = f"jobbot/0.1 (+{settings.user_agent_contact})"

    # M9e: never a silent drop -- a rendered source excluded by the cap is
    # visible in the log even though it was never attempted this poll (see
    # _order_and_cap_companies()).
    for company in skipped_rendered:
        logger.warning(
            "rendered source %s skipped this poll: over the %d-per-poll cap",
            company.name, MAX_RENDERED_SOURCES_PER_POLL,
        )

    # seed mode forces dry_run for publishing (B3): belt-and-suspenders on
    # top of the fact that SEEDED is never is_publishable() anyway, so a
    # seed run can never post to Discord even if a bug elsewhere let a job
    # slip into `publishable`.
    effective_dry_run = dry_run or seed

    # M12 Part A: follow_redirects=True for consistency with the fetch-side
    # client below -- this one only ever posts to the Discord webhook (see
    # the comment a few lines down), so it's not where the Nexans-class bug
    # lived, but "every httpx.Client in run.py follows redirects" is a
    # simpler invariant to hold than "except this one, for reasons."
    with httpx.Client(follow_redirects=True) as client, JobStore(
        settings.state_db_path,
        repost_window_days=settings.repost_window_days,
        resurrection_window_days=settings.resurrection_window_days,
        ghost_stale_after_days=settings.ghost_stale_after_days,
    ) as store:
        # This client is the main thread's own -- used only for the
        # publisher below, never for fetching. Every company's fetch gets
        # its own client, built inside its own worker (see
        # _fetch_one_concurrently); this one is never shared with them.
        publisher = DiscordPublisher(client, user_agent)
        to_publish: list[tuple[Job, list[str]]] = []

        # M9c: fetch every company concurrently (settings.poll_concurrency
        # workers, each with its own httpx.Client, sharing one
        # _HostThrottle so no single vendor host gets hammered) -- this is
        # a hard barrier: every outcome is collected before any of them
        # touches the store, which happens next, sequentially, on this
        # (the main) thread only. See this module's docstring for why.
        throttle = _HostThrottle(_PER_HOST_CONCURRENCY_LIMIT)
        with ThreadPoolExecutor(max_workers=max(1, settings.poll_concurrency)) as executor:
            outcomes = list(
                executor.map(
                    lambda company: _fetch_one_concurrently(
                        company, user_agent, settings.search_terms, throttle
                    ),
                    companies,
                )
            )

        # executor.map() preserves input order in its results regardless of
        # completion order, so this loop processes companies in the exact
        # order companies/*.yaml declared them -- a run's observable
        # behavior must not depend on which worker happened to finish first.
        for company, outcome in zip(companies, outcomes, strict=True):
            report.sources_attempted += 1

            if outcome.build_error is not None:
                report.sources_failed += 1
                report.errors.append(outcome.build_error)
                continue

            publishable, fetched_count, verdict_counts, ok = record_fetch_outcome(
                outcome, company, job_filter, store, now, seed=seed
            )
            report.jobs_fetched += fetched_count

            if not ok:
                # record_fetch_outcome() already recorded the failure in
                # source_health; surface a bit of that context here rather
                # than duplicating the exact error text (which the fixed
                # record_fetch_outcome -> run() boundary doesn't carry).
                # Note this is no longer keyed off fetched_count == 0 (A4):
                # a legitimately empty board also fetches 0 and is NOT a
                # failure, so `ok` (not the count) is the actual signal.
                consecutive = {
                    (s, c): n for s, c, n in store.unhealthy_sources(threshold=1, now=now)
                }.get((outcome.source_name, company.name))
                detail = f" ({consecutive} consecutive failures)" if consecutive else ""
                report.sources_failed += 1
                report.errors.append(
                    f"{company.name} ({outcome.source_name}): fetch failed{detail}"
                )
                continue

            report.jobs_passing_filter += sum(verdict_counts.values())
            for verdict_name, count in verdict_counts.items():
                report.verdicts[verdict_name] = report.verdicts.get(verdict_name, 0) + count

            to_publish.extend(publishable)

        # Once per cycle, after every source has been processed (B3) -- not
        # once per source, which would apply the same staleness cutoff
        # redundantly and couple it to iteration order for no benefit.
        store.age_ghosts(now)

        if to_publish:
            result = publisher.publish(webhook_url, to_publish, dry_run=effective_dry_run)
            report.published = result.sent
            report.publish_failed = len(result.failed)

            if not effective_dry_run and result.sent > 0:
                failed_ids = set(result.failed)
                succeeded_ids = [
                    job.job_id for job, _kw in to_publish if job.job_id not in failed_ids
                ]
                store.mark_published_batch(succeeded_ids, now)

        # Errors are summarized into a single message, never one post per
        # failure (B3). No Discord traffic at all during a dry/seed run.
        if report.errors and error_webhook_url and not effective_dry_run:
            summary = "\n".join(f"- {e}" for e in report.errors)
            publisher.publish_error(
                error_webhook_url,
                f"jobbot: {len(report.errors)} source(s) failed this run:\n{summary}",
            )

    return report


def print_stats(stats: StoreStats) -> None:
    """M9 B5: everything --stats prints. Pure formatting -- stats() already
    did the querying, so this can't accidentally touch the database."""
    print("=== jobbot state ===")
    print(f"Total jobs:  {stats.total_jobs}")
    print(f"Published:   {stats.published}")
    print(f"Pending:     {stats.pending}")
    print(f"Stale:       {stats.stale}")
    print(f"Disappeared: {stats.disappeared}")

    print("\nBy company:")
    if stats.by_company:
        for company, count in stats.by_company.items():
            print(f"  {company}: {count}")
    else:
        print("  (none)")

    print("\n10 most recently published:")
    if stats.recently_published:
        for title, company, published_at in stats.recently_published:
            print(f"  [{company}] {title} ({published_at})")
    else:
        print("  (none)")


# M9 D1: shape only -- confirms the value looks like a real Discord webhook
# URL without ever printing it. Discord's own format is
# https://discord.com/api/webhooks/{id}/{token} (discordapp.com still works
# too, the pre-rebrand domain).
_DISCORD_WEBHOOK_PATTERN = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/\S+$")


def _looks_like_discord_webhook(url: str) -> bool:
    return bool(_DISCORD_WEBHOOK_PATTERN.match(url))


def run_check(config_dir: Path, filters_path: Path, settings_path: Path) -> bool:
    """M9 D1: validates everything a real run would need without fetching or
    posting -- config files parse, every company has a registered adapter,
    the webhook env var is present and looks right (never logged), and the
    state database opens at the current schema version. Prints one PASS/FAIL
    line per check; returns True only if every check passed.
    """
    all_passed = True

    def check(passed: bool, message: str) -> None:
        nonlocal all_passed
        print(f"{'PASS' if passed else 'FAIL'}: {message}")
        if not passed:
            all_passed = False

    companies: list[CompanySource] = []
    try:
        companies = load_companies(config_dir)
        check(True, f"{config_dir} parses ({len(companies)} enabled company entries)")
    except ConfigError as exc:
        check(False, f"{config_dir}: {exc}")

    try:
        load_filters(filters_path)
        check(True, f"{filters_path} parses")
    except FilterConfigError as exc:
        check(False, f"{filters_path}: {exc}")

    settings = None
    try:
        settings = load_settings(settings_path)
        check(True, f"{settings_path} parses")
    except SettingsError as exc:
        check(False, f"{settings_path}: {exc}")

    if companies:
        adapters = {cls.name for cls in registered_sources()}
        unregistered = sorted({c.ats for c in companies if c.ats not in adapters})
        if unregistered:
            check(
                False,
                f"every company has a registered adapter (no adapter for: {', '.join(unregistered)})",
            )
        else:
            check(True, f"every company ({len(companies)}) has a registered adapter")

    webhook_url = os.environ.get("JOBBOT_DISCORD_WEBHOOK_URL")
    if not webhook_url:
        check(False, "JOBBOT_DISCORD_WEBHOOK_URL is set")
    elif not _looks_like_discord_webhook(webhook_url):
        check(False, "JOBBOT_DISCORD_WEBHOOK_URL looks like a Discord webhook URL")
    else:
        check(True, "JOBBOT_DISCORD_WEBHOOK_URL is set and looks like a Discord webhook URL")

    if settings is not None:
        try:
            with JobStore(settings.state_db_path) as store:
                version = store.schema_version()
            if version == SCHEMA_VERSION:
                check(True, f"state database opens, schema version {version} (current)")
            else:
                check(
                    False,
                    f"state database schema version {version}, expected {SCHEMA_VERSION}",
                )
        except sqlite3.DatabaseError as exc:
            check(False, f"state database opens: {exc}")
    else:
        check(False, "state database opens (skipped: settings.yaml did not load)")

    return all_passed


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobbot",
        description="Fetch every configured ATS, filter, and publish new postings to Discord.",
    )
    parser.add_argument(
        "--seed", action="store_true",
        help="Seed mode: record everything as already published, publish nothing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build every payload and validate every limit, but make zero Discord requests.",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=Path("companies"),
        help="Directory of company yaml files (default: companies/).",
    )
    parser.add_argument(
        "--filters", type=Path, default=Path("filters.yaml"),
        help="Path to filters.yaml (default: filters.yaml).",
    )
    parser.add_argument(
        "--settings", type=Path, default=Path("settings.yaml"),
        help="Path to settings.yaml (default: settings.yaml).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="DEBUG-level logging instead of INFO.",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print a summary of the state database and exit. Fetches nothing.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate config, env, and state without fetching or posting. Exits 0 or 2.",
    )
    return parser


def main() -> int:
    """The only place that reads os.environ and builds real clients."""
    args = _build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check:
        return 0 if run_check(args.config_dir, args.filters, args.settings) else 2

    if args.stats:
        try:
            settings = load_settings(args.settings)
        except SettingsError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        with JobStore(settings.state_db_path) as store:
            print_stats(store.stats())
        return 0

    webhook_url = os.environ.get("JOBBOT_DISCORD_WEBHOOK_URL")
    error_webhook_url = os.environ.get("JOBBOT_DISCORD_ERROR_WEBHOOK_URL")

    # --seed forces dry_run internally (run()'s effective_dry_run) and can
    # never post regardless of what's in the environment, so it belongs in
    # this exemption alongside --dry-run -- requiring the webhook for it
    # blocked exactly the sequence docs/DEPLOY.md tells the user to follow:
    # seed locally, *then* add the webhook secret.
    if not webhook_url and not args.dry_run and not args.seed:
        print(
            "JOBBOT_DISCORD_WEBHOOK_URL is required unless --dry-run or --seed is set.",
            file=sys.stderr,
        )
        return 2

    # The only clock read in the codebase: computed once, threaded through
    # everything else as an explicit argument (CLAUDE.md's "Time and state").
    now = datetime.now(UTC)

    try:
        report = run(
            config_dir=args.config_dir,
            filters_path=args.filters,
            settings_path=args.settings,
            webhook_url=webhook_url or "",
            error_webhook_url=error_webhook_url,
            now=now,
            dry_run=args.dry_run,
            seed=args.seed,
        )
    except (ConfigError, FilterConfigError, SettingsError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger.info(
        "sources: %d/%d ok, fetched: %d, passed filter: %d, verdicts: %s, "
        "published: %d, publish failures: %d",
        report.sources_attempted - report.sources_failed,
        report.sources_attempted,
        report.jobs_fetched,
        report.jobs_passing_filter,
        report.verdicts,
        report.published,
        report.publish_failed,
    )
    for error in report.errors:
        logger.warning("source error: %s", error)

    # A stable, explicitly-for-machines line -- deliberately decoupled from
    # the human-readable logger.info() line above so a future wording change
    # there can't silently break the M9 deployment workflow's
    # `grep jobbot_published_count=` (poll.yml), which builds the state
    # commit message from this. Always stdout, regardless of --verbose.
    print(f"jobbot_published_count={report.published}")

    if report.sources_attempted > 0 and report.sources_failed == report.sources_attempted:
        logger.error("all %d source(s) failed this run", report.sources_attempted)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover -- trivial CLI entrypoint, not import-testable
    sys.exit(main())
