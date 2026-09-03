from __future__ import annotations

import copy
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

import jobbot.run as run_module
from jobbot.config import CompanySource
from jobbot.filters import FilterConfig, JobFilter, KeywordFilterConfig, LocationFilterConfig
from jobbot.models import Job
from jobbot.run import (
    _fetch_one_concurrently,
    _HostThrottle,
    _HostThrottledTransport,
    _order_and_cap_companies,
    build_source,
    main,
    process_source,
    run,
    run_check,
)
from jobbot.sources.base import JobSource, SourceError
from jobbot.sources.rendered import MAX_RENDERED_SOURCES_PER_POLL
from jobbot.store import JobStore

BASE = datetime(2024, 1, 1, tzinfo=UTC)
GREENHOUSE_BOARD = "https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs"


@pytest.fixture(autouse=True)
def _fast_httpx_client_in_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """run.py legitimately constructs its own httpx.Client() with the
    default verify=True (as real network safety requires -- this is the one
    module allowed to build a real client), which pays ~0.5-1s for
    SSLContext/CA-bundle setup per construction. This file calls run()/
    main() many times, so left alone that adds up to several seconds.
    respx intercepts at the transport layer regardless of `verify`, so no
    real TLS handshake ever happens in these tests either way -- only the
    suite's speed is at stake here, not the correctness of run.py's actual
    (unmodified) production code path.
    """
    original_init = httpx.Client.__init__

    def _fast_init(self, *args, **kwargs):
        kwargs.setdefault("verify", False)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _fast_init)


def _make_job(**overrides: object) -> Job:
    fields = {
        "company": "Acme Corp",
        "title": "Alternance Data Engineer",
        "location": "Paris, France",
        "contract_type": "apprenticeship",
        "url": "https://example.com/jobs/1",
        "posted_at": None,
        "description": "",
        "source": "greenhouse",
        "external_id": "1",
    }
    fields.update(overrides)
    return Job(**fields)


def _company(identifier: str = "acme", name: str = "Acme Corp") -> CompanySource:
    return CompanySource(name=name, ats="greenhouse", identifier=identifier)


def _permissive_filter() -> JobFilter:
    """Passes any internship/apprenticeship job in one of a few locations,
    with no keyword requirement -- broad enough not to get in the way of
    tests that are about verdicts/orchestration, not filtering itself."""
    return JobFilter(
        FilterConfig(
            locations=LocationFilterConfig(include=["paris", "lyon", "remote", "berlin"]),
            contract_types=["internship", "apprenticeship"],
            keywords=KeywordFilterConfig(include=[]),
        )
    )


class _FakeSource(JobSource):
    """A JobSource test double that skips HTTP entirely: process_source only
    ever calls .fetch(), .name, and (via `company` separately) needs nothing
    else from the source object itself."""

    name = "fake-ats"
    tier = 1
    first_party = True

    def __init__(self, jobs: list[Job] | None = None, error: Exception | None = None) -> None:
        self.company_name = "Acme Corp"
        self._jobs = list(jobs) if jobs is not None else []
        self._error = error

    def fetch_raw(self, etag=None, last_modified=None):
        raise NotImplementedError

    def parse(self, raw):
        raise NotImplementedError

    def fetch(self) -> list[Job]:
        if self._error is not None:
            raise self._error
        return list(self._jobs)


def _write_run_config(
    tmp_path: Path,
    *,
    companies: list[dict],
    state_db_path: str = ":memory:",
    filters_yaml: str | None = None,
    poll_concurrency: int = 2,
) -> dict[str, Path]:
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir(exist_ok=True)
    lines = [
        f"- name: {c['name']}\n  ats: {c['ats']}\n  identifier: {c['identifier']}\n"
        for c in companies
    ]
    (companies_dir / "companies.yaml").write_text("\n".join(lines), encoding="utf-8")

    filters_path = tmp_path / "filters.yaml"
    filters_path.write_text(
        filters_yaml
        or (
            "locations:\n"
            "  include: [paris, lyon, remote, berlin]\n"
            "contract_types: [internship, apprenticeship]\n"
            "keywords:\n"
            "  include: []\n"
        ),
        encoding="utf-8",
    )

    settings_path = tmp_path / "settings.yaml"
    # Single-quoted YAML string: state_db_path may be a Windows path with
    # backslashes, which a double-quoted YAML string would try to escape.
    settings_path.write_text(
        f"user_agent_contact: \"test@example.invalid\"\nstate_db_path: '{state_db_path}'\n"
        f"poll_concurrency: {poll_concurrency}\n",
        encoding="utf-8",
    )

    return {"config_dir": companies_dir, "filters_path": filters_path, "settings_path": settings_path}


def _offset_job_ids(payload: dict, offset: int) -> dict:
    """A deep copy of a greenhouse payload with every job's id shifted, so
    two boards can serve "the same" fixture content without their jobs
    colliding on job_id -- which is what would happen if two different
    companies' postings literally shared a source `id`. Real Greenhouse ids
    are unique per-board; this just keeps the test fixture honest about it.
    """
    new_payload = copy.deepcopy(payload)
    for entry in new_payload["jobs"]:
        if "id" in entry:
            entry["id"] = entry["id"] + offset
    return new_payload


def _minimal_payload(count: int, *, start_id: int = 1) -> dict:
    """A small, predictable Greenhouse-shaped payload for orchestration
    tests that care about counts and control flow, not classification
    diversity (that's already covered thoroughly in test_greenhouse.py).
    Every entry titles itself "Alternance ..." (unambiguous apprenticeship
    vocabulary, no French-context guard to reason about) in Paris (always
    in the standard test filters.yaml include list), so every entry is
    guaranteed to pass the filter and classify predictably.
    """
    return {
        "jobs": [
            {
                "id": start_id + i,
                "title": f"Alternance Role {start_id + i}",
                "updated_at": "2024-01-01T00:00:00+00:00",
                "location": {"name": "Paris, France"},
                "absolute_url": f"https://boards.greenhouse.io/co/jobs/{start_id + i}",
                "content": "<p>Une alternance de douze mois.</p>",
            }
            for i in range(count)
        ]
    }


# --- process_source() -------------------------------------------------


def test_process_source_records_verdicts_and_returns_only_publishable_jobs() -> None:
    new_job = _make_job(external_id="1", title="Alternance Data Engineer 1")
    known_job = _make_job(external_id="2", title="Alternance Data Engineer 2")

    with JobStore(":memory:") as store:
        store.record(known_job, BASE)  # pre-existing, so its next sighting is KNOWN

        source = _FakeSource(jobs=[new_job, known_job])
        publishable, fetched_count, verdict_counts, ok = process_source(
            source, _company(), _permissive_filter(), store, BASE + timedelta(minutes=20)
        )

    assert ok is True
    assert fetched_count == 2
    assert [job.external_id for job, _kw in publishable] == ["1"]
    assert verdict_counts == {"NEW": 1, "KNOWN": 1}


def test_process_source_on_source_error_records_failure_and_returns_empty() -> None:
    with JobStore(":memory:") as store:
        pre_existing = _make_job(external_id="1")
        store.record(pre_existing, BASE)

        source = _FakeSource(error=SourceError("boom"))
        company = _company()

        publishable, fetched_count, verdict_counts, ok = process_source(
            source, company, _permissive_filter(), store, BASE + timedelta(days=1)
        )

        assert ok is False
        assert publishable == []
        assert fetched_count == 0
        assert verdict_counts == {}

        # Critically: mark_absent must NOT have been called. The
        # pre-existing job must not gain disappeared_at just because this
        # particular fetch failed -- we don't know if it's still there.
        row = store._conn.execute(
            "SELECT disappeared_at FROM jobs WHERE job_id = ?", (pre_existing.job_id,)
        ).fetchone()
        assert row["disappeared_at"] is None

        unhealthy = store.unhealthy_sources(threshold=1, now=BASE + timedelta(days=1))
        assert (source.name, company.name, 1) in unhealthy


def test_known_verdict_job_is_not_returned_as_publishable() -> None:
    job = _make_job(external_id="1")
    with JobStore(":memory:") as store:
        store.record(job, BASE)
        source = _FakeSource(jobs=[job])
        publishable, _fetched, verdict_counts, ok = process_source(
            source, _company(), _permissive_filter(), store, BASE + timedelta(days=1)
        )

    assert ok is True
    assert publishable == []
    assert verdict_counts == {"KNOWN": 1}


def test_new_job_failing_filter_is_not_published_and_never_stored() -> None:
    # Berlin isn't in this filter's include list.
    job = _make_job(external_id="1", location="Berlin, Germany")
    restrictive_filter = JobFilter(
        FilterConfig(
            locations=LocationFilterConfig(include=["paris"]),
            contract_types=["internship", "apprenticeship"],
            keywords=KeywordFilterConfig(include=[]),
        )
    )

    with JobStore(":memory:") as store:
        source = _FakeSource(jobs=[job])
        publishable, fetched_count, verdict_counts, ok = process_source(
            source, _company(), restrictive_filter, store, BASE
        )

        assert ok is True
        assert publishable == []
        assert fetched_count == 1
        assert verdict_counts == {}  # store.record() was never called for it

        # B3: filter runs BEFORE store.record(), so a job the user will
        # never see never gets a row at all.
        count = store._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        assert count == 0


# --- M8b: legitimately-empty boards are not failures ------------------


def test_first_ever_zero_records_success_and_does_not_fail_the_source() -> None:
    with JobStore(":memory:") as store:
        source = _FakeSource(jobs=[])
        publishable, fetched_count, verdict_counts, ok = process_source(
            source, _company(), _permissive_filter(), store, BASE
        )

        assert ok is True
        assert publishable == []
        assert fetched_count == 0
        assert verdict_counts == {}
        assert store.unhealthy_sources(threshold=1, now=BASE) == []
        assert store.has_seen_postings(source.name, "Acme Corp") is False


def test_zero_after_a_previous_non_zero_success_records_a_failure() -> None:
    job = _make_job(external_id="1")
    with JobStore(":memory:") as store:
        # A real posting was seen once, establishing has_seen_postings.
        non_empty_source = _FakeSource(jobs=[job])
        process_source(non_empty_source, _company(), _permissive_filter(), store, BASE)
        assert store.has_seen_postings(non_empty_source.name, "Acme Corp") is True

        empty_source = _FakeSource(jobs=[])
        publishable, fetched_count, verdict_counts, ok = process_source(
            empty_source, _company(), _permissive_filter(), store, BASE + timedelta(days=1)
        )

        assert ok is False
        assert publishable == []
        assert fetched_count == 0
        assert verdict_counts == {}
        unhealthy = store.unhealthy_sources(threshold=1, now=BASE + timedelta(days=1))
        assert (empty_source.name, "Acme Corp", 1) in unhealthy

        # The previously-seen job must not be marked disappeared over a
        # fetch process_source() doesn't trust.
        row = store._conn.execute(
            "SELECT disappeared_at FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        assert row["disappeared_at"] is None


def test_build_source_raises_on_unknown_ats(mock_client: httpx.Client) -> None:
    # "lever" was this placeholder's original choice, back when only
    # Greenhouse existed; M6 gave it a real registered adapter, so it no
    # longer demonstrates "no adapter registered" -- use a name that will
    # never be one instead.
    company = CompanySource(name="Foo", ats="totally-unregistered-ats", identifier="foo")
    with pytest.raises(ValueError):
        build_source(company, mock_client, "jobbot-test/0.1")


def test_build_source_returns_the_right_adapter(mock_client: httpx.Client) -> None:
    from jobbot.sources.greenhouse import GreenhouseSource

    source = build_source(_company(), mock_client, "jobbot-test/0.1")
    assert isinstance(source, GreenhouseSource)
    assert source.identifier == "acme"
    assert source.company_name == "Acme Corp"


@pytest.mark.parametrize(
    ("ats", "identifier", "expected_cls_path"),
    [
        ("workday", "sanofi.wd3.SanofiCareers", "jobbot.sources.workday.WorkdaySource"),
        ("smartrecruiters", "KIABI", "jobbot.sources.smartrecruiters.SmartRecruitersSource"),
        ("jibe", "https://careers.axa.com", "jobbot.sources.jibe.JibeSource"),
        (
            "talentsoft",
            "https://casa-cacib-recrute.talent-soft.com",
            "jobbot.sources.talentsoft.TalentsoftSource",
        ),
    ],
)
def test_build_source_threads_search_terms_into_every_search_capable_adapter(
    mock_client: httpx.Client, ats: str, identifier: str, expected_cls_path: str
) -> None:
    import importlib

    module_path, _, cls_name = expected_cls_path.rpartition(".")
    expected_cls = getattr(importlib.import_module(module_path), cls_name)

    company = CompanySource(name="Some Corp", ats=ats, identifier=identifier)
    source = build_source(
        company, mock_client, "jobbot-test/0.1",
        search_terms=["alternance", "stage"],
    )

    assert isinstance(source, expected_cls)
    assert source.search_terms == ["alternance", "stage"]


def test_build_source_ignores_search_terms_for_adapters_that_do_not_support_it(
    mock_client: httpx.Client,
) -> None:
    # A non-search-capable adapter has no such concept -- passing the kwarg
    # through must not raise or otherwise leak into an unrelated adapter's
    # construction.
    source = build_source(
        _company(), mock_client, "jobbot-test/0.1",
        search_terms=["alternance"],
    )
    assert not hasattr(source, "search_terms")


# --- run() ---------------------------------------------------------------


def test_run_with_one_source_failing_the_other_still_publishes(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
            {"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"},
        ],
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(3))
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
            return_value=httpx.Response(500)
        )
        respx.post(webhook_url).mock(return_value=httpx.Response(204))

        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
        )

    assert report.sources_attempted == 2
    assert report.sources_failed == 1
    assert len(report.errors) == 1
    assert "Broken Co" in report.errors[0]
    assert report.published == 3  # Acme's 3 NEW jobs still publish


def test_run_with_all_sources_failing(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path, companies=[{"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"}]
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
            return_value=httpx.Response(500)
        )
        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
        )

    assert report.sources_attempted == 1
    # This is exactly the condition main() turns into exit code 1.
    assert report.sources_failed == report.sources_attempted


def test_run_posts_a_single_summarized_message_to_the_error_webhook(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Broken One", "ats": "greenhouse", "identifier": "broken-one"},
            {"name": "Broken Two", "ats": "greenhouse", "identifier": "broken-two"},
        ],
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"
    error_webhook_url = "https://discord.com/api/webhooks/2/errors"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="broken-one")).mock(
            return_value=httpx.Response(500)
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="broken-two")).mock(
            return_value=httpx.Response(500)
        )
        error_route = respx.post(error_webhook_url).mock(return_value=httpx.Response(204))

        run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=error_webhook_url,
            now=BASE,
        )

    # Summarized into ONE message, not one post per failure (B3).
    assert error_route.call_count == 1
    payload = json.loads(error_route.calls[0].request.content)
    assert "Broken One" in payload["content"]
    assert "Broken Two" in payload["content"]


def test_run_does_not_post_to_error_webhook_during_dry_run(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path, companies=[{"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"}]
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"
    error_webhook_url = "https://discord.com/api/webhooks/2/errors"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
            return_value=httpx.Response(500)
        )
        error_route = respx.post(error_webhook_url).mock(return_value=httpx.Response(204))

        run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=error_webhook_url,
            now=BASE,
            dry_run=True,
        )

    assert error_route.call_count == 0


def test_run_records_an_error_when_build_source_rejects_an_unregistered_ats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jobbot.run as run_module

    paths = _write_run_config(
        tmp_path, companies=[{"name": "Odd Co", "ats": "greenhouse", "identifier": "odd"}]
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    # Simulate an ats with no registered adapter reaching run() -- normally
    # prevented by config.py's KNOWN_ATS check, but build_source() defends
    # against it independently, and run() must handle that defensively too.
    monkeypatch.setattr(run_module, "registered_sources", list)

    report = run(
        config_dir=paths["config_dir"],
        filters_path=paths["filters_path"],
        settings_path=paths["settings_path"],
        webhook_url=webhook_url,
        error_webhook_url=None,
        now=BASE,
    )

    assert report.sources_attempted == 1
    assert report.sources_failed == 1
    assert len(report.errors) == 1


def test_seed_mode_posts_nothing_even_with_webhook_and_dry_run_false(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path, companies=[{"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"}]
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        post_route = respx.post(webhook_url).mock(return_value=httpx.Response(204))

        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
            dry_run=False,
            seed=True,
        )

    assert post_route.call_count == 0
    assert report.published == 0
    assert report.verdicts.get("SEEDED", 0) > 0


def test_dry_run_makes_zero_requests_but_still_records(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    paths = _write_run_config(
        tmp_path,
        companies=[{"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"}],
        state_db_path=str(db_path),
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        post_route = respx.post(webhook_url).mock(return_value=httpx.Response(204))

        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
            dry_run=True,
            seed=False,
        )

    assert post_route.call_count == 0
    assert report.published == 2  # what WOULD have been published

    with JobStore(db_path) as reopened:
        total = reopened._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert total > 0
        # Recorded, but publish_pending must survive: dry_run never confirmed
        # a send, so mark_published_batch must not have been called.
        pending = reopened._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE publish_pending = 1"
        ).fetchone()[0]
        assert pending == 2


def test_age_ghosts_called_once_per_run_not_once_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinct id ranges: job_id doesn't factor in company name, so two
    # companies both using external_id "1" would collide on job_id.
    first_payload = _minimal_payload(1, start_id=1)
    second_payload = _minimal_payload(1, start_id=1001)
    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
            {"name": "Beta Inc", "ats": "greenhouse", "identifier": "beta"},
        ],
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    calls = {"n": 0}
    original_age_ghosts = JobStore.age_ghosts

    def _counting_age_ghosts(self: JobStore, now: datetime) -> int:
        calls["n"] += 1
        return original_age_ghosts(self, now)

    monkeypatch.setattr(JobStore, "age_ghosts", _counting_age_ghosts)

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=first_payload)
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="beta")).mock(
            return_value=httpx.Response(200, json=second_payload)
        )
        respx.post(webhook_url).mock(return_value=httpx.Response(204))

        run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
        )

    assert calls["n"] == 1


# --- main() ----------------------------------------------------------------


def test_main_exits_2_when_webhook_missing_and_not_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOBBOT_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["jobbot"])
    assert main() == 2


def test_main_exits_2_on_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBBOT_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/test")
    missing_settings = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(
        sys, "argv",
        ["jobbot", "--settings", str(missing_settings), "--config-dir", str(tmp_path)],
    )
    assert main() == 2


def test_main_exits_0_on_a_successful_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Zero configured companies: main()'s exit-code mapping only cares that
    # sources_failed != sources_attempted when attempted > 0, which is
    # trivially true here without needing any HTTP fetch at all -- the
    # "dry_run genuinely processes and reports jobs" behavior is already
    # covered thoroughly at the run() level above.
    monkeypatch.delenv("JOBBOT_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("JOBBOT_DISCORD_ERROR_WEBHOOK_URL", raising=False)
    paths = _write_run_config(tmp_path, companies=[])
    monkeypatch.setattr(
        sys, "argv",
        [
            "jobbot", "--dry-run",
            "--config-dir", str(paths["config_dir"]),
            "--filters", str(paths["filters_path"]),
            "--settings", str(paths["settings_path"]),
        ],
    )

    assert main() == 0


def test_main_exits_0_on_seed_with_no_webhook_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # --seed forces dry_run internally and can never post regardless of the
    # environment -- docs/DEPLOY.md's deploy sequence explicitly seeds
    # *before* the webhook secret is ever added, so requiring the webhook
    # here would block that documented sequence.
    monkeypatch.delenv("JOBBOT_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("JOBBOT_DISCORD_ERROR_WEBHOOK_URL", raising=False)
    paths = _write_run_config(tmp_path, companies=[])
    monkeypatch.setattr(
        sys, "argv",
        [
            "jobbot", "--seed",
            "--config-dir", str(paths["config_dir"]),
            "--filters", str(paths["filters_path"]),
            "--settings", str(paths["settings_path"]),
        ],
    )

    assert main() == 0


def test_main_exits_1_when_all_sources_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JOBBOT_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/test")
    paths = _write_run_config(
        tmp_path, companies=[{"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"}]
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "jobbot",
            "--config-dir", str(paths["config_dir"]),
            "--filters", str(paths["filters_path"]),
            "--settings", str(paths["settings_path"]),
        ],
    )

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
            return_value=httpx.Response(500)
        )
        exit_code = main()

    assert exit_code == 1


def test_run_does_not_count_a_legitimately_empty_source_as_failed(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path, companies=[{"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"}]
    )

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )
        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url="https://discord.com/api/webhooks/1/test",
            error_webhook_url=None,
            now=BASE,
            dry_run=True,
        )

    assert report.sources_attempted == 1
    assert report.sources_failed == 0
    assert report.errors == []
    assert report.jobs_fetched == 0


# --- M9c: concurrent polling ------------------------------------------


class _ThreadCheckingStore(JobStore):
    """A real JobStore (same sqlite3 mechanics, nothing mocked) that also
    records which thread every store-writing method was called from --
    monkeypatched in for `jobbot.run.JobStore` so run()'s own, real
    construction of it picks this up transparently."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.calls_from_non_main_thread: list[str] = []
        self._main_thread = threading.current_thread()

    def _note(self, method_name: str) -> None:
        if threading.current_thread() is not self._main_thread:
            self.calls_from_non_main_thread.append(method_name)

    def record(self, *args: object, **kwargs: object):
        self._note("record")
        return super().record(*args, **kwargs)

    def record_success(self, *args: object, **kwargs: object) -> None:
        self._note("record_success")
        super().record_success(*args, **kwargs)

    def record_failure(self, *args: object, **kwargs: object) -> None:
        self._note("record_failure")
        super().record_failure(*args, **kwargs)

    def mark_absent(self, *args: object, **kwargs: object) -> None:
        self._note("mark_absent")
        super().mark_absent(*args, **kwargs)

    def has_seen_postings(self, *args: object, **kwargs: object) -> bool:
        self._note("has_seen_postings")
        return super().has_seen_postings(*args, **kwargs)

    def age_ghosts(self, *args: object, **kwargs: object) -> None:
        self._note("age_ghosts")
        super().age_ghosts(*args, **kwargs)


def test_store_is_only_ever_touched_from_the_main_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_stores: list[_ThreadCheckingStore] = []

    def _capturing_store(*args: object, **kwargs: object) -> _ThreadCheckingStore:
        store = _ThreadCheckingStore(*args, **kwargs)
        captured_stores.append(store)
        return store

    monkeypatch.setattr(run_module, "JobStore", _capturing_store)

    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
            {"name": "Beta Inc", "ats": "greenhouse", "identifier": "beta"},
            {"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"},
        ],
        poll_concurrency=6,
    )

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="beta")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
            return_value=httpx.Response(500)
        )
        run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url="https://discord.com/api/webhooks/1/test",
            error_webhook_url=None,
            now=BASE,
            dry_run=True,
        )

    assert len(captured_stores) == 1
    assert captured_stores[0].calls_from_non_main_thread == []


def test_run_produces_the_same_report_regardless_of_poll_concurrency(tmp_path: Path) -> None:
    companies = [
        {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
        {"name": "Beta Inc", "ats": "greenhouse", "identifier": "beta"},
        {"name": "Broken Co", "ats": "greenhouse", "identifier": "broken"},
    ]

    def _run_with(concurrency: int, tmp_subdir: Path):
        tmp_subdir.mkdir(parents=True, exist_ok=True)
        paths = _write_run_config(tmp_subdir, companies=companies, poll_concurrency=concurrency)
        with respx.mock:
            respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
                return_value=httpx.Response(200, json=_minimal_payload(3))
            )
            respx.get(GREENHOUSE_BOARD.format(identifier="beta")).mock(
                return_value=httpx.Response(200, json=_minimal_payload(2))
            )
            respx.get(GREENHOUSE_BOARD.format(identifier="broken")).mock(
                return_value=httpx.Response(500)
            )
            return run(
                config_dir=paths["config_dir"],
                filters_path=paths["filters_path"],
                settings_path=paths["settings_path"],
                webhook_url="https://discord.com/api/webhooks/1/test",
                error_webhook_url=None,
                now=BASE,
                dry_run=True,
            )

    sequential = _run_with(1, tmp_path / "seq")
    concurrent = _run_with(6, tmp_path / "conc")

    assert concurrent.sources_attempted == sequential.sources_attempted
    assert concurrent.sources_failed == sequential.sources_failed
    assert concurrent.jobs_fetched == sequential.jobs_fetched
    assert concurrent.jobs_passing_filter == sequential.jobs_passing_filter
    assert concurrent.verdicts == sequential.verdicts
    assert len(concurrent.errors) == len(sequential.errors)


def test_a_worker_failure_does_not_affect_other_workers(tmp_path: Path) -> None:
    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
            {"name": "Broken One", "ats": "greenhouse", "identifier": "broken-one"},
            {"name": "Beta Inc", "ats": "greenhouse", "identifier": "beta"},
            {"name": "Broken Two", "ats": "greenhouse", "identifier": "broken-two"},
        ],
        poll_concurrency=4,
    )

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="broken-one")).mock(
            return_value=httpx.Response(500)
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="beta")).mock(
            return_value=httpx.Response(200, json=_minimal_payload(2))
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="broken-two")).mock(
            return_value=httpx.Response(500)
        )
        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url="https://discord.com/api/webhooks/1/test",
            error_webhook_url=None,
            now=BASE,
            dry_run=True,
        )

    assert report.sources_attempted == 4
    assert report.sources_failed == 2
    assert report.jobs_fetched == 4  # acme's 2 + beta's 2, unaffected by the two failures
    assert len(report.errors) == 2


def test_host_throttled_transport_never_exceeds_the_per_host_limit() -> None:
    throttle = _HostThrottle(per_host_limit=2)
    lock = threading.Lock()
    in_flight = 0
    peak_in_flight = 0

    class _SlowFakeTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak_in_flight
            with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return httpx.Response(200, request=request)

    transport = _HostThrottledTransport(_SlowFakeTransport(), throttle)

    def _fire() -> None:
        transport.handle_request(httpx.Request("GET", "https://example.invalid/x"))

    threads = [threading.Thread(target=_fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak_in_flight <= 2


def test_host_throttled_transport_releases_the_semaphore_even_on_exception() -> None:
    throttle = _HostThrottle(per_host_limit=1)

    class _RaisingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

    transport = _HostThrottledTransport(_RaisingTransport(), throttle)
    request = httpx.Request("GET", "https://example.invalid/x")

    with pytest.raises(httpx.ConnectError):
        transport.handle_request(request)

    # If the failed request had never released the semaphore, this
    # non-blocking acquire would find it already exhausted.
    semaphore = throttle.semaphore_for("example.invalid")
    assert semaphore.acquire(blocking=False) is True
    semaphore.release()


def test_different_hosts_get_independent_semaphores() -> None:
    throttle = _HostThrottle(per_host_limit=1)
    a = throttle.semaphore_for("a.example.invalid")
    b = throttle.semaphore_for("b.example.invalid")
    assert a is not b


def test_a_redirecting_job_url_still_yields_its_posting() -> None:
    """M12 Part A: the per-worker client now sets follow_redirects=True.
    Without it, a job URL that 302s (locale-suffixed paths, canonicalization,
    a trailing-slash normalization -- all real-world, not hypothetical) gets
    the redirect response's own body back instead of the real page, and the
    adapter's own parsing silently reads that as "no postings here" rather
    than as a fetch failure -- there's no exception to surface, just an empty
    result. Confirmed live cost before this fix: Nexans's entire board, 302s
    on every job URL, never once reached. This test exercises the exact
    function whose httpx.Client construction changed (_fetch_one_concurrently),
    not the full run() pipeline, to isolate the fix from filter/store/webhook
    machinery."""
    company = _company(identifier="acme")
    throttle = _HostThrottle(per_host_limit=2)
    redirect_target = "https://boards-api.greenhouse.io/v1/boards/acme/jobs-fr"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(302, headers={"Location": redirect_target})
        )
        respx.get(redirect_target).mock(return_value=httpx.Response(200, json=_minimal_payload(1)))

        outcome = _fetch_one_concurrently(company, "jobbot-test/1.0", None, throttle)

    assert outcome.build_error is None
    assert outcome.fetch_error is None
    assert outcome.jobs is not None
    assert len(outcome.jobs) == 1


# --- M9e: rendered-source ordering and per-poll cap ------------------------


def _rendered_company(name: str) -> CompanySource:
    return CompanySource(name=name, ats="rendered", identifier=f"https://{name.lower()}.example")


def test_rendered_companies_are_moved_after_every_non_rendered_one() -> None:
    companies = [
        _rendered_company("R1"),
        _company("acme", "Acme"),
        _rendered_company("R2"),
        _company("beta", "Beta"),
    ]
    ordered, skipped = _order_and_cap_companies(companies)

    assert [c.name for c in ordered] == ["Acme", "Beta", "R1", "R2"]
    assert skipped == []


def test_relative_order_is_preserved_within_each_group() -> None:
    companies = [
        _rendered_company("R2"),
        _rendered_company("R1"),
        _company("beta", "Beta"),
        _company("acme", "Acme"),
    ]
    ordered, _skipped = _order_and_cap_companies(companies)

    assert [c.name for c in ordered] == ["Beta", "Acme", "R2", "R1"]


def test_rendered_companies_beyond_the_cap_are_excluded_and_reported_separately() -> None:
    rendered = [_rendered_company(f"R{i}") for i in range(MAX_RENDERED_SOURCES_PER_POLL + 3)]
    ordered, skipped = _order_and_cap_companies(rendered)

    assert len(ordered) == MAX_RENDERED_SOURCES_PER_POLL
    assert len(skipped) == 3
    # The FIRST N (in original order) are kept, not an arbitrary subset.
    assert [c.name for c in ordered] == [f"R{i}" for i in range(MAX_RENDERED_SOURCES_PER_POLL)]
    assert [c.name for c in skipped] == [
        f"R{i}" for i in range(MAX_RENDERED_SOURCES_PER_POLL, MAX_RENDERED_SOURCES_PER_POLL + 3)
    ]


def test_a_skipped_rendered_source_is_never_attempted_this_poll(tmp_path: Path) -> None:
    companies_dir = tmp_path / "companies"
    companies_dir.mkdir()
    lines = [
        f"- name: R{i}\n  ats: rendered\n  identifier: https://r{i}.example/careers\n"
        for i in range(MAX_RENDERED_SOURCES_PER_POLL + 1)
    ]
    (companies_dir / "companies.yaml").write_text("\n".join(lines), encoding="utf-8")
    filters_path = tmp_path / "filters.yaml"
    filters_path.write_text(
        "locations:\n  include: [paris]\ncontract_types: [internship]\nkeywords:\n  include: []\n",
        encoding="utf-8",
    )
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        'user_agent_contact: "test@example.invalid"\nstate_db_path: \':memory:\'\n'
        "poll_concurrency: 2\n",
        encoding="utf-8",
    )

    with respx.mock:
        # robots.txt disallowed for every one of the kept rendered sources,
        # so each fails fast and cleanly without needing a real browser --
        # this test is only about the cap, not about rendered.py itself.
        for i in range(MAX_RENDERED_SOURCES_PER_POLL):
            respx.get(f"https://r{i}.example/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
            )
        never_route = respx.get(
            f"https://r{MAX_RENDERED_SOURCES_PER_POLL}.example/robots.txt"
        ).mock(return_value=httpx.Response(404))

        report = run(
            config_dir=companies_dir,
            filters_path=filters_path,
            settings_path=settings_path,
            webhook_url="https://discord.com/api/webhooks/1/test",
            error_webhook_url=None,
            now=BASE,
            dry_run=True,
        )

    assert report.sources_attempted == MAX_RENDERED_SOURCES_PER_POLL
    assert never_route.call_count == 0


# --- THE END TO END TEST, load bearing ------------------------------------


def test_full_cycle_from_fixture_to_discord_payload(
    tmp_path: Path, greenhouse_payload: dict
) -> None:
    """Two mocked Greenhouse boards serving the real fixture. A full cycle
    against an empty store, then a second, identical cycle that must
    publish exactly zero -- that second run is the whole point of the
    project (CLAUDE.md rule 5, the flood-protection promise)."""
    beta_payload = _offset_job_ids(greenhouse_payload, 5000)

    db_path = tmp_path / "state.db"
    paths = _write_run_config(
        tmp_path,
        companies=[
            {"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"},
            {"name": "Beta Inc", "ats": "greenhouse", "identifier": "beta"},
        ],
        state_db_path=str(db_path),
    )
    webhook_url = "https://discord.com/api/webhooks/1/test"

    with respx.mock:
        respx.get(GREENHOUSE_BOARD.format(identifier="acme")).mock(
            return_value=httpx.Response(200, json=greenhouse_payload)
        )
        respx.get(GREENHOUSE_BOARD.format(identifier="beta")).mock(
            return_value=httpx.Response(200, json=beta_payload)
        )
        post_route = respx.post(webhook_url).mock(return_value=httpx.Response(204))

        report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE,
            dry_run=False,
            seed=False,
        )

        # Per company: 1001/1002/1003/1005 pass the filter and are genuinely
        # NEW (1004 is "other", 1006 is Marseille (not in include), 1007 is
        # malformed and never parses, 1008 is a same-fingerprint repost).
        assert report.sources_attempted == 2
        assert report.sources_failed == 0
        assert report.published == 8
        assert post_route.call_count == 1

        sent_payload = json.loads(post_route.calls[0].request.content)
        embeds = sent_payload["embeds"]
        assert len(embeds) == 8

        for embed in embeds:
            assert len(embed["title"]) <= 256
            assert len(embed["description"]) <= 4096
            for f in embed["fields"]:
                assert len(f["value"]) <= 1024
        total_chars = sum(
            len(e.get("title", "")) + len(e.get("description", ""))
            + len(e.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))
            for e in embeds
        )
        assert total_chars <= 6000

        # --- second, identical cycle ---
        second_report = run(
            config_dir=paths["config_dir"],
            filters_path=paths["filters_path"],
            settings_path=paths["settings_path"],
            webhook_url=webhook_url,
            error_webhook_url=None,
            now=BASE + timedelta(minutes=20),
            dry_run=False,
            seed=False,
        )

    assert second_report.published == 0
    assert post_route.call_count == 1  # no additional POST beyond the first run


# --- M9 B5: --stats ----------------------------------------------------


def test_stats_flag_prints_summary_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"
    with JobStore(db_path) as store:
        job = Job(
            company="Acme", title="Alternance Data Analyst", location="Paris",
            contract_type="apprenticeship", url="https://acme.example/1",
            source="greenhouse", external_id="1",
        )
        store.record(job, BASE)
        store.mark_published(job.job_id, BASE)

    paths = _write_run_config(tmp_path, companies=[], state_db_path=str(db_path))
    monkeypatch.setattr(
        sys, "argv", ["jobbot", "--stats", "--settings", str(paths["settings_path"])]
    )

    assert main() == 0

    out = capsys.readouterr().out
    assert "Total jobs:  1" in out
    assert "Published:   1" in out
    assert "Acme: 1" in out
    assert "Alternance Data Analyst" in out


def test_stats_flag_exits_2_on_settings_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_settings = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(
        sys, "argv", ["jobbot", "--stats", "--settings", str(missing_settings)]
    )

    assert main() == 2


# --- M9 D1: --check ------------------------------------------------------


def test_check_flag_all_pass_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JOBBOT_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/sometoken")
    paths = _write_run_config(
        tmp_path,
        companies=[{"name": "Acme Corp", "ats": "greenhouse", "identifier": "acme"}],
        state_db_path=str(tmp_path / "state.db"),
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "jobbot", "--check",
            "--config-dir", str(paths["config_dir"]),
            "--filters", str(paths["filters_path"]),
            "--settings", str(paths["settings_path"]),
        ],
    )

    assert main() == 0

    out = capsys.readouterr().out
    # config parses, filters parse, settings parse, adapter registered,
    # webhook looks right, state database opens -- six checks total.
    assert out.count("PASS:") == 6
    assert "FAIL:" not in out


def test_check_flag_fails_and_exits_2_when_webhook_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JOBBOT_DISCORD_WEBHOOK_URL", raising=False)
    paths = _write_run_config(
        tmp_path, companies=[], state_db_path=str(tmp_path / "state.db")
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "jobbot", "--check",
            "--config-dir", str(paths["config_dir"]),
            "--filters", str(paths["filters_path"]),
            "--settings", str(paths["settings_path"]),
        ],
    )

    assert main() == 2

    out = capsys.readouterr().out
    assert "FAIL: JOBBOT_DISCORD_WEBHOOK_URL is set" in out


def test_check_flag_never_prints_the_webhook_url_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_url = "https://discord.com/api/webhooks/999999/super-secret-token-value"
    monkeypatch.setenv("JOBBOT_DISCORD_WEBHOOK_URL", secret_url)
    paths = _write_run_config(tmp_path, companies=[], state_db_path=str(tmp_path / "state.db"))

    run_check(paths["config_dir"], paths["filters_path"], paths["settings_path"])

    out = capsys.readouterr().out
    assert secret_url not in out
    assert "super-secret-token-value" not in out


def test_check_flag_rejects_a_url_that_does_not_look_like_a_discord_webhook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JOBBOT_DISCORD_WEBHOOK_URL", "https://example.com/not-a-webhook")
    paths = _write_run_config(tmp_path, companies=[], state_db_path=str(tmp_path / "state.db"))

    passed = run_check(paths["config_dir"], paths["filters_path"], paths["settings_path"])

    assert passed is False
    out = capsys.readouterr().out
    assert "FAIL: JOBBOT_DISCORD_WEBHOOK_URL looks like a Discord webhook URL" in out
