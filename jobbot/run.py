"""The orchestrator: fetch, filter, store, publish, in one runnable command.

This is the first module allowed to read settings.yaml, read the
environment, and construct an httpx.Client. Every other module in jobbot/
stays injection-only (see sources/base.py, filters.py, store.py,
publisher.py) precisely so that everything except this file can be tested
without any of those real things -- main() is the one place that wires them
together for a real run.

Two deliberate deviations from this milestone's literal API sketch, both
needed to make RunReport (also specified by this milestone) actually
fillable without reaching into JobStore's private internals from here:

- process_source() returns a 3-tuple, not 2: (publishable, fetched_count,
  verdict_counts). verdict_counts (a dict of JobVerdict name -> count, for
  every job that passed the filter this call) is what lets run() populate
  RunReport.verdicts accurately. Without it, the only verdict run() could
  ever see is "NEW" (since that's the only one implied by a job appearing in
  `publishable`), and RunReport.verdicts would be permanently wrong for
  KNOWN/BUMP/REPOST/RESURRECTION/SEEDED.
- process_source() takes `seed: bool = False`. store.record() needs a
  per-job seed_mode flag to produce SEEDED verdicts correctly, and
  process_source is where record() is called.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
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
from jobbot.sources.jsonld import JsonLdSource  # noqa: F401
from jobbot.sources.lever import LeverSource  # noqa: F401
from jobbot.sources.workday import WorkdaySource  # noqa: F401
from jobbot.store import SCHEMA_VERSION, JobStore, StoreStats, is_publishable

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


def build_source(company: CompanySource, client: httpx.Client, user_agent: str) -> JobSource:
    """Maps a company's `ats` string to its adapter class. Raises ValueError
    on an ats with no registered adapter -- config.py's KNOWN_ATS already
    keeps this from happening for a validly-loaded companies/*.yaml, but
    build_source doesn't assume that; it's a real check, not a formality.
    """
    adapters = {cls.name: cls for cls in registered_sources()}
    try:
        adapter_cls = adapters[company.ats]
    except KeyError:
        raise ValueError(
            f"No adapter registered for ats {company.ats!r} (company {company.name!r}); "
            f"registered: {sorted(adapters)}"
        ) from None
    return adapter_cls(company.identifier, company.name, client, user_agent=user_agent)


def process_source(
    source: JobSource,
    company: CompanySource,
    job_filter: JobFilter,
    store: JobStore,
    now: datetime,
    seed: bool = False,
) -> tuple[list[tuple[Job, list[str]]], int, dict[str, int]]:
    """Fetch, filter, record, mark_absent, record_success/record_failure for
    one company's source.

    Filter runs BEFORE store.record() (B3): a job the user's filters.yaml
    doesn't want is never given a row at all. This keeps the store's size,
    and its repost/resurrection window logic, scoped to postings that
    actually matter to this user -- and it means loosening filters.yaml
    later can't retroactively "resurrect" a job that was silently dropped;
    from the store's point of view it was never seen, so it's genuinely NEW
    again if it still exists on the board.

    mark_absent() is only ever called on the success path, with exactly the
    job_ids this fetch actually returned -- deliberately every fetched
    job_id, not just the ones that passed the filter, since "the employer
    took this posting down" and "our filters don't want this posting" are
    different facts. A failed fetch must never mark a company's postings
    disappeared just because we couldn't ask.

    On SourceError: the failure is recorded via store.record_failure() and
    this returns ([], 0, {}) rather than raising -- one source's outage must
    never abort the run for every other source.
    """
    try:
        jobs = source.fetch()
    except SourceError as exc:
        store.record_failure(source.name, company.name, str(exc), now)
        return [], 0, {}

    store.record_success(source.name, company.name, len(jobs), now)

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

    store.mark_absent(source.name, company.name, seen_job_ids, now)

    return publishable, len(jobs), verdict_counts


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
    companies = load_companies(config_dir)
    job_filter = JobFilter(filters_config)

    report = RunReport()
    user_agent = f"jobbot/0.1 (+{settings.user_agent_contact})"

    # seed mode forces dry_run for publishing (B3): belt-and-suspenders on
    # top of the fact that SEEDED is never is_publishable() anyway, so a
    # seed run can never post to Discord even if a bug elsewhere let a job
    # slip into `publishable`.
    effective_dry_run = dry_run or seed

    with httpx.Client() as client, JobStore(
        settings.state_db_path,
        repost_window_days=settings.repost_window_days,
        resurrection_window_days=settings.resurrection_window_days,
        ghost_stale_after_days=settings.ghost_stale_after_days,
    ) as store:
        publisher = DiscordPublisher(client, user_agent)
        to_publish: list[tuple[Job, list[str]]] = []

        for company in companies:
            report.sources_attempted += 1

            try:
                source = build_source(company, client, user_agent)
            except ValueError as exc:
                report.sources_failed += 1
                report.errors.append(str(exc))
                continue

            publishable, fetched_count, verdict_counts = process_source(
                source, company, job_filter, store, now, seed=seed
            )
            report.jobs_fetched += fetched_count

            if fetched_count == 0:
                # process_source() already recorded the failure in
                # source_health; surface a bit of that context here rather
                # than duplicating the exact error text (which the fixed
                # process_source -> run() boundary doesn't carry).
                consecutive = {
                    (s, c): n for s, c, n in store.unhealthy_sources(threshold=1, now=now)
                }.get((source.name, company.name))
                detail = f" ({consecutive} consecutive failures)" if consecutive else ""
                report.sources_failed += 1
                report.errors.append(f"{company.name} ({source.name}): fetch failed{detail}")
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
