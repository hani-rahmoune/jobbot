from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import CompanySource
from jobbot.filters import FilterConfig, JobFilter, KeywordFilterConfig, LocationFilterConfig
from jobbot.models import Job
from jobbot.run import build_source, main, process_source, run
from jobbot.sources.base import JobSource, SourceError
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
        f"user_agent_contact: \"test@example.invalid\"\nstate_db_path: '{state_db_path}'\n",
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
        publishable, fetched_count, verdict_counts = process_source(
            source, _company(), _permissive_filter(), store, BASE + timedelta(minutes=20)
        )

    assert fetched_count == 2
    assert [job.external_id for job, _kw in publishable] == ["1"]
    assert verdict_counts == {"NEW": 1, "KNOWN": 1}


def test_process_source_on_source_error_records_failure_and_returns_empty() -> None:
    with JobStore(":memory:") as store:
        pre_existing = _make_job(external_id="1")
        store.record(pre_existing, BASE)

        source = _FakeSource(error=SourceError("boom"))
        company = _company()

        publishable, fetched_count, verdict_counts = process_source(
            source, company, _permissive_filter(), store, BASE + timedelta(days=1)
        )

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
        publishable, _fetched, verdict_counts = process_source(
            source, _company(), _permissive_filter(), store, BASE + timedelta(days=1)
        )

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
        publishable, fetched_count, verdict_counts = process_source(
            source, _company(), restrictive_filter, store, BASE
        )

        assert publishable == []
        assert fetched_count == 1
        assert verdict_counts == {}  # store.record() was never called for it

        # B3: filter runs BEFORE store.record(), so a job the user will
        # never see never gets a row at all.
        count = store._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        assert count == 0


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
