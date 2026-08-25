from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobbot.models import Job
from jobbot.store import SCHEMA_VERSION, JobStore, JobVerdict, _row_to_job, is_publishable

BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _make_job(**overrides: object) -> Job:
    fields = {
        "company": "Acme Corp",
        "title": "Ingénieur Logiciel",
        "location": "Paris, France",
        "contract_type": "internship",
        "url": "https://example.com/jobs/1",
        "posted_at": None,
        "description": "",
        "source": "greenhouse",
        "external_id": "1",
    }
    fields.update(overrides)
    return Job(**fields)


# --- schema / lifecycle ------------------------------------------------


def test_schema_creation_is_idempotent() -> None:
    with JobStore(":memory:") as store:
        store.initialize()
        store.initialize()
        assert store.record(_make_job(), BASE) == JobVerdict.NEW


def test_journal_mode_is_not_wal(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() != "wal"


def test_now_must_be_timezone_aware() -> None:
    with JobStore(":memory:") as store, pytest.raises(ValueError):
        naive = datetime(2024, 1, 1)  # noqa: DTZ001 -- deliberately naive, that's the point
        store.record(_make_job(), naive)


def test_now_must_be_utc_not_merely_aware() -> None:
    # A2: aware but +02:00 must be rejected, not just "has a tzinfo".
    with JobStore(":memory:") as store, pytest.raises(ValueError):
        plus_two = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=2)))
        store.record(_make_job(), plus_two)


_V1_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE jobs (
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
    last_verdict TEXT NOT NULL
);
CREATE TABLE fingerprints (
    content_fingerprint TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    origin_job_id TEXT NOT NULL
);
CREATE TABLE source_health (
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (source, company)
);
"""


def test_opening_a_v1_database_migrates_it_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.commit()
    conn.close()

    with JobStore(path) as store:
        assert SCHEMA_VERSION == 2
        row = store._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == 2

        # publish_pending exists and defaults to 0 -- no error, no NULLs.
        store.record(_make_job(), BASE)
        job_row = store._conn.execute(
            "SELECT publish_pending FROM jobs LIMIT 1"
        ).fetchone()
        assert job_row["publish_pending"] == 1


# --- round-trip ----------------------------------------------------------


def test_job_round_trips_losslessly_including_none_fields_and_accents() -> None:
    job = _make_job(
        title="Développeur Généraliste",
        location="Île-de-France",
        description="Résumé: café, déjà-vu, naïve.",
        external_id=None,
        posted_at=None,
        url="https://example.com/jobs/accent-test",
    )
    with JobStore(":memory:") as store:
        store.record(job, BASE)
        row = store._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        rebuilt = _row_to_job(row)

    assert rebuilt.company == job.company
    assert rebuilt.title == job.title
    assert rebuilt.location == job.location
    assert rebuilt.contract_type == job.contract_type
    assert str(rebuilt.url) == str(job.url)
    assert rebuilt.posted_at == job.posted_at
    assert rebuilt.description == job.description
    assert rebuilt.source == job.source
    assert rebuilt.external_id == job.external_id
    assert rebuilt.job_id == job.job_id
    assert rebuilt.content_fingerprint == job.content_fingerprint


def test_job_round_trips_losslessly_with_external_id_and_posted_at() -> None:
    job = _make_job(external_id="42", posted_at=BASE - timedelta(days=5))
    with JobStore(":memory:") as store:
        store.record(job, BASE)
        row = store._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        rebuilt = _row_to_job(row)

    assert rebuilt.external_id == job.external_id
    assert rebuilt.posted_at == job.posted_at
    assert rebuilt.job_id == job.job_id


# --- verdicts --------------------------------------------------------------


def test_first_record_returns_new() -> None:
    with JobStore(":memory:") as store:
        assert store.record(_make_job(), BASE) == JobVerdict.NEW


def test_recording_same_job_again_returns_known_and_advances_last_seen_at() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        later = BASE + timedelta(days=1)

        assert store.record(job, later) == JobVerdict.KNOWN

        row = store._conn.execute(
            "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        assert row["first_seen_at"] == BASE.isoformat()
        assert row["last_seen_at"] == later.isoformat()


def test_same_job_id_changed_source_date_identical_fingerprint_returns_bump() -> None:
    with JobStore(":memory:") as store:
        job1 = _make_job(external_id="42", posted_at=BASE)
        store.record(job1, BASE)

        job2 = _make_job(external_id="42", posted_at=BASE + timedelta(days=2))
        assert job1.job_id == job2.job_id
        assert job1.content_fingerprint == job2.content_fingerprint

        assert store.record(job2, BASE + timedelta(days=2)) == JobVerdict.BUMP


def test_new_job_id_with_fingerprint_seen_30_days_ago_returns_repost() -> None:
    with JobStore(":memory:") as store:
        original = _make_job(external_id="100", url="https://example.com/jobs/100")
        store.record(original, BASE)

        repost = _make_job(external_id="200", url="https://example.com/jobs/200")
        assert repost.job_id != original.job_id
        assert repost.content_fingerprint == original.content_fingerprint

        assert store.record(repost, BASE + timedelta(days=30)) == JobVerdict.REPOST


def test_same_case_at_200_days_beyond_repost_window_returns_new() -> None:
    with JobStore(":memory:") as store:
        original = _make_job(external_id="100", url="https://example.com/jobs/100")
        store.record(original, BASE)

        repost = _make_job(external_id="300", url="https://example.com/jobs/300")
        assert store.record(repost, BASE + timedelta(days=200)) == JobVerdict.NEW


def test_job_absent_3_days_then_returning_is_known_not_resurrection() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        assert store.record(job, BASE + timedelta(days=3)) == JobVerdict.KNOWN


def test_job_absent_30_days_then_returning_returns_resurrection() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        assert store.record(job, BASE + timedelta(days=30)) == JobVerdict.RESURRECTION


def test_resurrection_clears_disappeared_at() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        store.mark_absent("greenhouse", "Acme Corp", set(), BASE + timedelta(days=1))

        row = store._conn.execute(
            "SELECT disappeared_at FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        assert row["disappeared_at"] is not None

        store.record(job, BASE + timedelta(days=30))
        row = store._conn.execute(
            "SELECT disappeared_at FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        assert row["disappeared_at"] is None


def test_seed_mode_returns_seeded_and_leaves_unpublished_new_empty() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        assert store.record(job, BASE, seed_mode=True) == JobVerdict.SEEDED
        assert store.unpublished_new() == []


def test_is_publishable_true_only_for_new() -> None:
    for verdict in JobVerdict:
        assert is_publishable(verdict) == (verdict is JobVerdict.NEW)


def test_mark_published_removes_job_from_unpublished_new() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        assert len(store.unpublished_new()) == 1

        store.mark_published(job.job_id, BASE + timedelta(hours=1))

        assert store.unpublished_new() == []
        row = store._conn.execute(
            "SELECT published_at FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        assert row["published_at"] == (BASE + timedelta(hours=1)).isoformat()


# --- A1, load bearing: a NEW job must survive an unpublished poll cycle ----


def test_a_new_job_survives_an_unpublished_poll_cycle() -> None:
    """Load bearing. Trace this failing without the fix: record() returns
    NEW, the publisher then fails to send it, the next poll re-records the
    same unchanged job as KNOWN, and unpublished_new() must still return it
    -- publish_pending, not last_verdict, is what unpublished_new() trusts.
    """
    with JobStore(":memory:") as store:
        job = _make_job()
        assert store.record(job, BASE) == JobVerdict.NEW

        # Next poll cycle, publisher never got to send it: unchanged re-fetch.
        assert store.record(job, BASE + timedelta(minutes=20)) == JobVerdict.KNOWN

        unpublished = store.unpublished_new()
        assert len(unpublished) == 1
        assert unpublished[0].job_id == job.job_id


def test_new_job_survives_a_bump_in_between() -> None:
    with JobStore(":memory:") as store:
        job = _make_job(posted_at=BASE)
        assert store.record(job, BASE) == JobVerdict.NEW

        bumped = _make_job(posted_at=BASE + timedelta(days=1))
        assert job.job_id == bumped.job_id
        assert store.record(bumped, BASE + timedelta(days=1)) == JobVerdict.BUMP

        unpublished = store.unpublished_new()
        assert len(unpublished) == 1
        assert unpublished[0].job_id == job.job_id


def test_new_job_survives_five_consecutive_known_polls() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        assert store.record(job, BASE) == JobVerdict.NEW

        for i in range(1, 6):
            assert store.record(job, BASE + timedelta(minutes=20 * i)) == JobVerdict.KNOWN

        unpublished = store.unpublished_new()
        assert len(unpublished) == 1
        assert unpublished[0].job_id == job.job_id


def test_mark_published_clears_pending_and_it_does_not_return() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        store.mark_published(job.job_id, BASE + timedelta(minutes=1))
        assert store.unpublished_new() == []

        # Next poll cycle: still gone, publish_pending stays cleared.
        store.record(job, BASE + timedelta(minutes=20))
        assert store.unpublished_new() == []


# --- A3: unpublished_new() excludes disappeared jobs -----------------------


def test_unpublished_new_excludes_jobs_with_disappeared_at_set() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        assert len(store.unpublished_new()) == 1

        store.mark_absent("greenhouse", "Acme Corp", set(), BASE + timedelta(hours=1))
        assert store.unpublished_new() == []


# --- presence / staleness ------------------------------------------------


def test_mark_absent_sets_disappeared_at_only_for_missing_jobs_and_returns_count() -> None:
    with JobStore(":memory:") as store:
        jobs = [
            _make_job(external_id=str(i), url=f"https://example.com/jobs/{i}")
            for i in range(3)
        ]
        for job in jobs:
            store.record(job, BASE)

        seen = {jobs[0].job_id, jobs[1].job_id}
        count = store.mark_absent("greenhouse", "Acme Corp", seen, BASE + timedelta(days=1))
        assert count == 1

        rows = {
            row["job_id"]: row["disappeared_at"]
            for row in store._conn.execute("SELECT job_id, disappeared_at FROM jobs").fetchall()
        }
        assert rows[jobs[0].job_id] is None
        assert rows[jobs[1].job_id] is None
        assert rows[jobs[2].job_id] is not None

        # never deletes
        total = store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert total == 3


def test_mark_absent_does_not_recount_an_already_absent_job() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        assert store.mark_absent("greenhouse", "Acme Corp", set(), BASE + timedelta(days=1)) == 1
        assert store.mark_absent("greenhouse", "Acme Corp", set(), BASE + timedelta(days=2)) == 0


def test_mark_absent_with_all_jobs_still_seen_marks_nothing() -> None:
    with JobStore(":memory:") as store:
        job = _make_job()
        store.record(job, BASE)
        count = store.mark_absent("greenhouse", "Acme Corp", {job.job_id}, BASE + timedelta(days=1))
        assert count == 0


def test_age_ghosts_flags_old_jobs_and_excludes_them_from_unpublished_new() -> None:
    with JobStore(":memory:", ghost_stale_after_days=90) as store:
        job = _make_job()
        store.record(job, BASE)

        unpublished = store.unpublished_new()
        assert len(unpublished) == 1
        assert unpublished[0].job_id == job.job_id

        count = store.age_ghosts(BASE + timedelta(days=91))
        assert count == 1
        assert store.unpublished_new() == []

        # idempotent: already-stale rows aren't re-counted
        assert store.age_ghosts(BASE + timedelta(days=92)) == 0


# --- atomicity -------------------------------------------------------------


def test_record_batch_atomic_rolls_back_completely_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with JobStore(":memory:") as store:
        good_job = _make_job(external_id="1")
        bad_job = _make_job(external_id="2")
        original_record_one = store._record_one

        def _boom(cur, job, now, seed_mode):
            if job.external_id == "2":
                raise RuntimeError("simulated failure")
            return original_record_one(cur, job, now, seed_mode)

        monkeypatch.setattr(store, "_record_one", _boom)

        with pytest.raises(RuntimeError):
            store.record_batch([good_job, bad_job], BASE)

        assert store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_record_batch_keyed_by_job_id() -> None:
    with JobStore(":memory:") as store:
        # Distinct titles, not just distinct external_id: content_fingerprint
        # doesn't include external_id, so same-title jobs would collide and
        # classify as REPOST/NEW unpredictably instead of all being NEW.
        jobs = [_make_job(external_id=str(i), title=f"Role {i}") for i in range(3)]
        results = store.record_batch(jobs, BASE)
        assert set(results.keys()) == {j.job_id for j in jobs}
        assert all(v == JobVerdict.NEW for v in results.values())


# --- source health -----------------------------------------------------


def test_source_health_three_failures_then_success_resets_and_respects_threshold() -> None:
    with JobStore(":memory:") as store:
        for i in range(3):
            store.record_failure(
                "greenhouse", "Acme Corp", f"error {i}", BASE + timedelta(days=i)
            )

        unhealthy = store.unhealthy_sources(threshold=3, now=BASE + timedelta(days=3))
        assert ("greenhouse", "Acme Corp", 3) in unhealthy
        assert store.unhealthy_sources(threshold=4, now=BASE + timedelta(days=3)) == []

        store.record_success(
            "greenhouse", "Acme Corp", job_count=10, now=BASE + timedelta(days=4)
        )
        assert store.unhealthy_sources(threshold=1, now=BASE + timedelta(days=4)) == []


# --- THE FLOOD TEST, load bearing --------------------------------------


def test_seed_mode_publishes_nothing_on_a_realistic_first_run() -> None:
    with JobStore(":memory:") as store:
        # Genuinely distinct postings (unique titles), not just unique
        # external_ids: content_fingerprint ignores external_id/url, so
        # same-title jobs would collapse onto one fingerprint and the
        # "3 new jobs" step below would see REPOST instead of NEW.
        jobs = [
            _make_job(
                external_id=str(i), title=f"Role {i}", url=f"https://example.com/jobs/{i}"
            )
            for i in range(200)
        ]

        results = store.record_batch(jobs, BASE, seed_mode=True)
        assert len(results) == 200
        assert all(v == JobVerdict.SEEDED for v in results.values())
        assert store.unpublished_new() == []

        next_day = BASE + timedelta(days=1)
        results_again = store.record_batch(jobs, next_day, seed_mode=False)
        assert all(v == JobVerdict.KNOWN for v in results_again.values())
        assert store.unpublished_new() == []

        new_jobs = [
            _make_job(
                external_id=str(200 + i),
                title=f"Role {200 + i}",
                url=f"https://example.com/jobs/{200 + i}",
            )
            for i in range(3)
        ]
        results_new = store.record_batch(new_jobs, next_day, seed_mode=False)
        assert all(v == JobVerdict.NEW for v in results_new.values())

        unpublished = store.unpublished_new()
        assert {j.external_id for j in unpublished} == {str(200 + i) for i in range(3)}
