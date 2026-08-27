"""SQLite state store: dedup, seen-state, resurrection, and ghost detection.

Per CLAUDE.md's "Time and state": this module never calls `datetime.now()`.
Every method that needs the current time takes `now: datetime` explicitly,
always timezone-aware UTC, so tests can fast-forward months deterministically
without sleeping or monkeypatching a clock.

Three columns exist on `jobs` beyond what CLAUDE.md's milestone brief listed:

- `description`: the brief's Job round-trip requirement ("Job -> row -> Job
  must be lossless for every field") is unsatisfiable without storing it.
- `last_verdict`: observability -- the last decision record() made for this
  job_id. NOT what unpublished_new() uses to decide who's pending (see next).
- `publish_pending`: the actual source of truth for "still needs to be
  published". Originally unpublished_new() queried `last_verdict = NEW AND
  published_at IS NULL`, but last_verdict is overwritten on every record()
  call -- so a NEW job that fails to publish before the next poll gets
  re-recorded as KNOWN, last_verdict becomes "KNOWN", and the job silently
  vanishes from unpublished_new() forever despite never having been sent.
  publish_pending fixes this by being a fact set once (True exactly when
  record() assigns NEW, False in seed mode) that KNOWN/BUMP/REPOST/
  RESURRECTION are never allowed to touch, and that only mark_published()
  ever clears. See test_a_new_job_survives_an_unpublished_poll_cycle.

Window settings (repost/resurrection/ghost-stale) are meant to live in
settings.yaml, but JobStore takes only `path` per the milestone's API sketch.
They're accepted here as keyword-only constructor arguments defaulting to
Settings' own defaults, so a caller wires real settings.yaml values in
explicitly (`JobStore(path, repost_window_days=settings.repost_window_days,
...)`) -- that wiring is M5's job, not this one's.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Self

from jobbot.models import Job

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    content_fingerprint TEXT NOT NULL,
    company TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    published_at TEXT,
    disappeared_at TEXT,
    is_stale INTEGER NOT NULL DEFAULT 0,
    last_verdict TEXT NOT NULL,
    publish_pending INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fingerprints (
    content_fingerprint TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    origin_job_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (source, company)
);

CREATE INDEX IF NOT EXISTS idx_jobs_content_fingerprint ON jobs(content_fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at ON jobs(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_published_at ON jobs(published_at);
"""


class JobVerdict(str, Enum):
    NEW = "NEW"                    # first time seen, never fingerprinted, publish it
    KNOWN = "KNOWN"                # job_id already present and unchanged, do nothing
    BUMP = "BUMP"                  # job_id present, minor fields changed, do not publish
    REPOST = "REPOST"              # new job_id, fingerprint seen within the repost window
    RESURRECTION = "RESURRECTION"  # job_id known, was absent beyond the resurrection window
    SEEDED = "SEEDED"              # seed mode: record everything as already published


def is_publishable(verdict: JobVerdict) -> bool:
    """Only NEW is ever publishable. A single choke point so no caller can
    get this wrong by hand-rolling the comparison."""
    return verdict is JobVerdict.NEW


@dataclass
class StoreStats:
    """M9 B5: what `jobbot.run --stats` prints. Raw counts by column state,
    not mutually exclusive categories -- a job can be both published and,
    much later, disappeared, and both counts include it."""

    total_jobs: int
    published: int
    pending: int
    stale: int
    disappeared: int
    by_company: dict[str, int] = field(default_factory=dict)
    recently_published: list[tuple[str, str, str]] = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _require_utc(now: datetime) -> None:
    # Not just aware -- specifically UTC (offset zero). age_ghosts() and every
    # other window check compares stored ISO strings; that only sorts the
    # same as chronological order when every timestamp shares one offset
    # format. A +02:00 timestamp mixed in with +00:00 ones would silently
    # break that, so it's rejected here instead of trusted.
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC (offset zero), got: " + repr(now))


class JobStore:
    def __init__(
        self,
        path: Path | str,
        *,
        repost_window_days: int = 180,
        resurrection_window_days: int = 7,
        ghost_stale_after_days: int = 90,
    ) -> None:
        self._repost_window_days = repost_window_days
        self._resurrection_window_days = resurrection_window_days
        self._ghost_stale_after_days = ghost_stale_after_days

        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        # WAL off: the state file gets committed to git (see CLAUDE.md's M9
        # deployment note), and a WAL sidecar file would complicate that.
        self._conn.execute("PRAGMA journal_mode = DELETE")
        self.initialize()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def initialize(self) -> None:
        """Idempotent: safe to call more than once, including on an
        already-populated database. `CREATE TABLE IF NOT EXISTS` alone only
        covers a brand-new file -- an existing v1 database already has a
        `jobs` table, so it needs an explicit column added, not just a
        bumped version constant."""
        cur = self._conn.cursor()
        cur.executescript(_SCHEMA)
        row = cur.execute("SELECT version FROM schema_version").fetchone()

        if row is None:
            cur.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] < 2:
            cur.execute("ALTER TABLE jobs ADD COLUMN publish_pending INTEGER NOT NULL DEFAULT 0")
            cur.execute("UPDATE schema_version SET version = 2")

        self._conn.commit()

    # -- recording ---------------------------------------------------------

    def record(self, job: Job, now: datetime, seed_mode: bool = False) -> JobVerdict:
        _require_utc(now)
        cur = self._conn.cursor()
        verdict = self._record_one(cur, job, now, seed_mode)
        self._conn.commit()
        return verdict

    def record_batch(
        self, jobs: list[Job], now: datetime, seed_mode: bool = False
    ) -> dict[str, JobVerdict]:
        """Single transaction, all or nothing: if any job raises, every
        change made so far in this call is rolled back."""
        _require_utc(now)
        results: dict[str, JobVerdict] = {}
        cur = self._conn.cursor()
        try:
            for job in jobs:
                results[job.job_id] = self._record_one(cur, job, now, seed_mode)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return results

    def _record_one(
        self, cur: sqlite3.Cursor, job: Job, now: datetime, seed_mode: bool
    ) -> JobVerdict:
        now_iso = _iso(now)
        posted_at_iso = _iso_or_none(job.posted_at)
        url_str = str(job.url)

        existing = cur.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()

        if seed_mode:
            verdict = JobVerdict.SEEDED
            first_seen_at = existing["first_seen_at"] if existing is not None else now_iso
            is_stale = existing["is_stale"] if existing is not None else 0
            self._upsert_job_row(
                cur, job, url_str, posted_at_iso, verdict,
                first_seen_at=first_seen_at, last_seen_at=now_iso,
                published_at=now_iso, disappeared_at=None, is_stale=is_stale,
                publish_pending=0,
            )
            self._touch_fingerprint(cur, job.content_fingerprint, now_iso, job.job_id)
            return verdict

        if existing is not None:
            last_seen_dt = _parse_iso(existing["last_seen_at"])
            gap_days = (now - last_seen_dt).days

            if gap_days > self._resurrection_window_days:
                verdict = JobVerdict.RESURRECTION
            else:
                changed = (
                    existing["content_fingerprint"] != job.content_fingerprint
                    or existing["title"] != job.title
                    or existing["location"] != job.location
                    or existing["company"] != job.company
                    or existing["contract_type"] != job.contract_type
                    or existing["url"] != url_str
                    or existing["description"] != job.description
                    or existing["source_posted_at"] != posted_at_iso
                    or existing["external_id"] != job.external_id
                )
                verdict = JobVerdict.BUMP if changed else JobVerdict.KNOWN

            self._upsert_job_row(
                cur, job, url_str, posted_at_iso, verdict,
                first_seen_at=existing["first_seen_at"], last_seen_at=now_iso,
                published_at=existing["published_at"], disappeared_at=None,
                is_stale=existing["is_stale"],
                # KNOWN/BUMP/RESURRECTION must never set OR clear a pending
                # publish -- carried forward unchanged (A1). _upsert_job_row
                # doesn't even put publish_pending in its UPDATE SET clause,
                # so this value is only actually used on a fresh INSERT.
                publish_pending=existing["publish_pending"],
            )
            self._touch_fingerprint(cur, job.content_fingerprint, now_iso, job.job_id)
            return verdict

        # job_id never seen before: NEW, unless its content_fingerprint was
        # seen recently enough to count as a repost.
        fp_row = cur.execute(
            "SELECT * FROM fingerprints WHERE content_fingerprint = ?",
            (job.content_fingerprint,),
        ).fetchone()

        if fp_row is None:
            verdict = JobVerdict.NEW
        else:
            fp_last_seen = _parse_iso(fp_row["last_seen_at"])
            gap_days = (now - fp_last_seen).days
            verdict = JobVerdict.REPOST if gap_days <= self._repost_window_days else JobVerdict.NEW

        self._upsert_job_row(
            cur, job, url_str, posted_at_iso, verdict,
            first_seen_at=now_iso, last_seen_at=now_iso,
            published_at=None, disappeared_at=None, is_stale=0,
            # publish_pending = 1 exactly when record() assigns NEW (A1).
            publish_pending=1 if verdict is JobVerdict.NEW else 0,
        )
        self._touch_fingerprint(cur, job.content_fingerprint, now_iso, job.job_id)
        return verdict

    def _upsert_job_row(
        self,
        cur: sqlite3.Cursor,
        job: Job,
        url_str: str,
        posted_at_iso: str | None,
        verdict: JobVerdict,
        *,
        first_seen_at: str,
        last_seen_at: str,
        published_at: str | None,
        disappeared_at: str | None,
        is_stale: int,
        publish_pending: int,
    ) -> None:
        cur.execute(
            """
            INSERT INTO jobs (
                job_id, content_fingerprint, company, source, external_id,
                title, location, contract_type, url, description,
                source_posted_at, first_seen_at, last_seen_at, published_at,
                disappeared_at, is_stale, last_verdict, publish_pending
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                content_fingerprint = excluded.content_fingerprint,
                company = excluded.company,
                source = excluded.source,
                external_id = excluded.external_id,
                title = excluded.title,
                location = excluded.location,
                contract_type = excluded.contract_type,
                url = excluded.url,
                description = excluded.description,
                source_posted_at = excluded.source_posted_at,
                last_seen_at = excluded.last_seen_at,
                published_at = excluded.published_at,
                disappeared_at = excluded.disappeared_at,
                is_stale = excluded.is_stale,
                last_verdict = excluded.last_verdict
                -- publish_pending deliberately absent from this SET clause:
                -- KNOWN/BUMP/REPOST/RESURRECTION must never set or clear a
                -- pending publish, only a fresh NEW row or mark_published()
                -- may (see the module docstring and A1's load-bearing test).
            """,
            (
                job.job_id, job.content_fingerprint, job.company, job.source, job.external_id,
                job.title, job.location, job.contract_type, url_str, job.description,
                posted_at_iso, first_seen_at, last_seen_at, published_at,
                disappeared_at, is_stale, verdict.value, publish_pending,
            ),
        )

    def _touch_fingerprint(
        self, cur: sqlite3.Cursor, content_fingerprint: str, now_iso: str, job_id: str
    ) -> None:
        existing = cur.execute(
            "SELECT 1 FROM fingerprints WHERE content_fingerprint = ?",
            (content_fingerprint,),
        ).fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO fingerprints "
                "(content_fingerprint, first_seen_at, last_seen_at, origin_job_id) "
                "VALUES (?, ?, ?, ?)",
                (content_fingerprint, now_iso, now_iso, job_id),
            )
        else:
            cur.execute(
                "UPDATE fingerprints SET last_seen_at = ? WHERE content_fingerprint = ?",
                (now_iso, content_fingerprint),
            )

    # -- publishing ----------------------------------------------------------

    def mark_published(self, job_id: str, now: datetime) -> None:
        _require_utc(now)
        self._conn.execute(
            "UPDATE jobs SET published_at = ?, publish_pending = 0 WHERE job_id = ?",
            (_iso(now), job_id),
        )
        self._conn.commit()

    def mark_published_batch(self, job_ids: list[str], now: datetime) -> None:
        """Marks every job_id published in ONE transaction: all or nothing.

        Used instead of a loop of mark_published() calls when a single
        Discord message (one HTTP POST) carried multiple jobs -- see
        run.py's publish-then-mark discussion for why the commit boundary
        matters. If anything raises partway through, every update made so
        far in this call is rolled back, not just the one that failed.
        """
        _require_utc(now)
        now_iso = _iso(now)
        cur = self._conn.cursor()
        try:
            for job_id in job_ids:
                cur.execute(
                    "UPDATE jobs SET published_at = ?, publish_pending = 0 WHERE job_id = ?",
                    (now_iso, job_id),
                )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    def unpublished_new(self) -> list[Job]:
        # publish_pending, not last_verdict (A1): last_verdict is overwritten
        # on every record() call, so a NEW job re-recorded as KNOWN before it
        # publishes would otherwise vanish from this query forever.
        # disappeared_at IS NULL (A3): a job pulled by the employer before we
        # ever announced it shouldn't be announced now.
        rows = self._conn.execute(
            "SELECT * FROM jobs "
            "WHERE publish_pending = 1 AND is_stale = 0 AND disappeared_at IS NULL"
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    # -- presence / staleness ------------------------------------------------

    def mark_absent(
        self, source: str, company: str, seen_job_ids: set[str], now: datetime
    ) -> int:
        """Jobs from this source+company not in seen_job_ids get
        disappeared_at set (only if not already set). Never deletes rows."""
        _require_utc(now)
        now_iso = _iso(now)
        cur = self._conn.cursor()
        if seen_job_ids:
            placeholders = ",".join("?" for _ in seen_job_ids)
            cur.execute(
                f"UPDATE jobs SET disappeared_at = ? "
                f"WHERE source = ? AND company = ? AND disappeared_at IS NULL "
                f"AND job_id NOT IN ({placeholders})",
                (now_iso, source, company, *seen_job_ids),
            )
        else:
            cur.execute(
                "UPDATE jobs SET disappeared_at = ? "
                "WHERE source = ? AND company = ? AND disappeared_at IS NULL",
                (now_iso, source, company),
            )
        self._conn.commit()
        return cur.rowcount

    def age_ghosts(self, now: datetime) -> int:
        """first_seen_at older than ghost_stale_after_days sets is_stale=1.
        A stale job never publishes again (unpublished_new() excludes it) --
        record()'s own decision tree already can't hand a job with an
        existing row a NEW verdict, so this is the only enforcement point
        actually needed."""
        _require_utc(now)
        cutoff_iso = _iso(now - timedelta(days=self._ghost_stale_after_days))
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE jobs SET is_stale = 1 WHERE first_seen_at < ? AND is_stale = 0",
            (cutoff_iso,),
        )
        self._conn.commit()
        return cur.rowcount

    # -- source health ---------------------------------------------------------

    def record_success(self, source: str, company: str, job_count: int, now: datetime) -> None:
        # job_count isn't persisted -- there's no column for it (M9's health
        # pruning may add one), and a success by definition can't have
        # returned zero (fetch_raw() raises SourceEmptyError on that, before
        # this is ever called). Accepted here so the caller doesn't need a
        # special case to report a successful fetch.
        _require_utc(now)
        now_iso = _iso(now)
        self._conn.execute(
            """
            INSERT INTO source_health (source, company, last_success_at, consecutive_failures)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(source, company) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                consecutive_failures = 0
            """,
            (source, company, now_iso),
        )
        self._conn.commit()

    def record_failure(self, source: str, company: str, error: str, now: datetime) -> None:
        _require_utc(now)
        now_iso = _iso(now)
        self._conn.execute(
            """
            INSERT INTO source_health
                (source, company, last_failure_at, consecutive_failures, last_error)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(source, company) DO UPDATE SET
                last_failure_at = excluded.last_failure_at,
                consecutive_failures = source_health.consecutive_failures + 1,
                last_error = excluded.last_error
            """,
            (source, company, now_iso, error),
        )
        self._conn.commit()

    def unhealthy_sources(self, threshold: int, now: datetime) -> list[tuple[str, str, int]]:
        # `now` isn't used by threshold-only logic today; kept in the
        # signature (like JobSource.fetch_raw()'s etag) so a future
        # time-based criterion (e.g. "and no success in N days") doesn't
        # need an interface change.
        _require_utc(now)
        rows = self._conn.execute(
            "SELECT source, company, consecutive_failures FROM source_health "
            "WHERE consecutive_failures >= ?",
            (threshold,),
        ).fetchall()
        return [(row["source"], row["company"], row["consecutive_failures"]) for row in rows]

    # -- introspection (M9 B5, D1) -------------------------------------------

    def schema_version(self) -> int:
        """The version this database is actually at right now. Note that
        opening a JobStore already runs initialize(), which migrates an old
        database up to SCHEMA_VERSION as a side effect -- so this mostly
        confirms the store opened and initialized cleanly, not that it was
        already current before this process touched it."""
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return row["version"] if row is not None else 0

    def stats(self) -> StoreStats:
        """A snapshot for `jobbot.run --stats` -- no fetching, no posting,
        just what's already in the database."""

        def count(where: str) -> int:
            return self._conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}").fetchone()[0]

        total_jobs = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        published = count("published_at IS NOT NULL")
        pending = count("publish_pending = 1")
        stale = count("is_stale = 1")
        disappeared = count("disappeared_at IS NOT NULL")

        by_company = dict(
            self._conn.execute(
                "SELECT company, COUNT(*) FROM jobs GROUP BY company ORDER BY company"
            ).fetchall()
        )

        recent_rows = self._conn.execute(
            "SELECT title, company, published_at FROM jobs "
            "WHERE published_at IS NOT NULL ORDER BY published_at DESC LIMIT 10"
        ).fetchall()
        recently_published = [
            (row["title"], row["company"], row["published_at"]) for row in recent_rows
        ]

        return StoreStats(
            total_jobs=total_jobs,
            published=published,
            pending=pending,
            stale=stale,
            disappeared=disappeared,
            by_company=by_company,
            recently_published=recently_published,
        )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        company=row["company"],
        title=row["title"],
        location=row["location"],
        contract_type=row["contract_type"],
        url=row["url"],
        posted_at=_parse_iso(row["source_posted_at"]),
        description=row["description"],
        source=row["source"],
        external_id=row["external_id"],
    )
