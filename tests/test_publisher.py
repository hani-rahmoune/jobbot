from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

import jobbot.publisher
from jobbot.models import Job
from jobbot.publisher import (
    EMBED_DESCRIPTION_MAX,
    DiscordPublisher,
    PublishError,
    RateLimitedError,
    _retry_after_seconds,
    _truncate,
    build_embed,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/fake-token-for-tests"
USER_AGENT = "jobbot-test/0.1 (+test@example.invalid)"

_COUNTER = {"n": 0}


def _make_job(**overrides: object) -> Job:
    _COUNTER["n"] += 1
    fields = {
        "company": "Acme Corp",
        "title": "Ingénieur Logiciel",
        "location": "Paris, France",
        "contract_type": "internship",
        "url": f"https://example.com/jobs/{_COUNTER['n']}",
        "posted_at": None,
        "description": "A great role at a great company.",
        "source": "greenhouse",
        "external_id": str(_COUNTER["n"]),
    }
    fields.update(overrides)
    return Job(**fields)


def _make_publisher(client: httpx.Client, sleep=None) -> DiscordPublisher:
    kwargs = {} if sleep is None else {"sleep": sleep}
    return DiscordPublisher(client, USER_AGENT, **kwargs)


class _FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# --- build_embed() ---------------------------------------------------------


def test_build_embed_is_pure_and_deterministic() -> None:
    job = _make_job()
    assert build_embed(job, ["python", "sql"]) == build_embed(job, ["python", "sql"])


def test_truncate_with_a_non_positive_limit_returns_empty_string() -> None:
    assert _truncate("anything", 0) == ""


def test_retry_after_falls_back_to_one_second_on_unparseable_body() -> None:
    response = httpx.Response(429, content=b"not json")
    assert _retry_after_seconds(response) == 1.0


def test_title_longer_than_256_is_truncated_to_256_including_ellipsis() -> None:
    job = _make_job(title="A" * 300)
    embed = build_embed(job, [])
    assert len(embed["title"]) == 256
    assert embed["title"].endswith("…")


def test_description_longer_than_4096_is_truncated() -> None:
    job = _make_job(description="B" * 5000)
    embed = build_embed(job, [])
    assert len(embed["description"]) <= EMBED_DESCRIPTION_MAX


def test_matched_keywords_empty_means_no_keywords_field() -> None:
    embed = build_embed(_make_job(), [])
    assert not any(f["name"] == "Keywords" for f in embed["fields"])


def test_matched_keywords_non_empty_means_keywords_field_appears() -> None:
    embed = build_embed(_make_job(), ["python", "sql"])
    keyword_fields = [f for f in embed["fields"] if f["name"] == "Keywords"]
    assert len(keyword_fields) == 1
    assert keyword_fields[0]["value"] == "python, sql"


def test_footer_labels_source_date_as_reported_by_the_company() -> None:
    job = _make_job(posted_at=datetime(2024, 1, 10, tzinfo=UTC))
    embed = build_embed(job, [])
    assert "reported by the company" in embed["footer"]["text"]
    assert "2024-01-10" in embed["footer"]["text"]


def test_footer_has_no_date_when_posted_at_is_none() -> None:
    job = _make_job(posted_at=None)
    embed = build_embed(job, [])
    assert "reported by the company" not in embed["footer"]["text"]
    assert job.source in embed["footer"]["text"]


def test_internship_and_apprenticeship_produce_different_colors() -> None:
    internship = build_embed(_make_job(contract_type="internship"), [])
    apprenticeship = build_embed(_make_job(contract_type="apprenticeship"), [])
    assert internship["color"] != apprenticeship["color"]


def test_missing_location_gets_a_placeholder_not_an_empty_field() -> None:
    embed = build_embed(_make_job(location=""), [])
    location_field = next(f for f in embed["fields"] if f["name"] == "Location")
    assert location_field["value"] == "Not specified"


# --- chunking ----------------------------------------------------------


def test_25_jobs_produce_exactly_3_requests_of_10_10_and_5(mock_client) -> None:
    jobs = [(_make_job(title=f"Role {i}"), []) for i in range(25)]
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        result = publisher.publish(WEBHOOK_URL, jobs)

    assert route.call_count == 3
    embed_counts = [len(json.loads(call.request.content)["embeds"]) for call in route.calls]
    assert embed_counts == [10, 10, 5]
    assert result.sent == 25
    assert result.requests_made == 3
    assert result.failed == []


def test_long_descriptions_chunk_by_char_limit_not_only_count(mock_client) -> None:
    # Maxed-out titles plus a long, truncated-to-1024 Keywords field push
    # each embed to ~1.6-1.7k chars, forcing the 6000-char rule to split
    # messages well before the 10-embed count limit would.
    many_keywords = [f"keyword-{i}" for i in range(80)]
    jobs = [
        (_make_job(title="A" * 256, description="B" * 2000), many_keywords)
        for _ in range(20)
    ]
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        result = publisher.publish(WEBHOOK_URL, jobs)

    count_only_would_produce = -(-20 // 10)  # ceil(20 / 10) = 2
    assert route.call_count > count_only_would_produce
    assert result.sent == 20
    for call in route.calls:
        payload = json.loads(call.request.content)
        total_chars = sum(
            len(e.get("title", "")) + len(e.get("description", ""))
            + len(e.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))
            for e in payload["embeds"]
        )
        assert total_chars <= 6000


def test_dry_run_makes_zero_requests_and_reports_correct_count(mock_client) -> None:
    jobs = [(_make_job(title=f"Role {i}"), []) for i in range(15)]
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        result = publisher.publish(WEBHOOK_URL, jobs, dry_run=True)

    assert route.call_count == 0
    assert result.sent == 15
    assert result.requests_made == 0
    assert result.failed == []


# --- retries, at the _post() level --------------------------------------


def test_429_with_retry_after_sleeps_then_retries_and_succeeds(mock_client) -> None:
    fake_sleep = _FakeSleep()
    publisher = _make_publisher(mock_client, sleep=fake_sleep)

    with respx.mock:
        route = respx.post(WEBHOOK_URL)
        route.side_effect = [
            httpx.Response(429, json={"retry_after": 1.5, "message": "rate limited"}),
            httpx.Response(204),
        ]
        attempts = publisher._post(WEBHOOK_URL, {"embeds": []})

    assert attempts == 2
    assert route.call_count == 2
    assert fake_sleep.calls == [1.5]


def test_three_consecutive_429s_raise_rate_limited_error(mock_client) -> None:
    fake_sleep = _FakeSleep()
    publisher = _make_publisher(mock_client, sleep=fake_sleep)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(
            return_value=httpx.Response(429, json={"retry_after": 0.1})
        )
        with pytest.raises(RateLimitedError):
            publisher._post(WEBHOOK_URL, {"embeds": []})

    assert route.call_count == 3
    assert fake_sleep.calls == [0.1, 0.1]


def test_500_retries_once_then_succeeds(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(204)]
        attempts = publisher._post(WEBHOOK_URL, {"embeds": []})

    assert attempts == 2
    assert route.call_count == 2


def test_500_twice_raises_publish_error_after_one_retry(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(PublishError):
            publisher._post(WEBHOOK_URL, {"embeds": []})

    assert route.call_count == 2


def test_400_raises_publish_error_with_no_retry(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(
            return_value=httpx.Response(400, json={"message": "bad request"})
        )
        with pytest.raises(PublishError):
            publisher._post(WEBHOOK_URL, {"embeds": []})

    assert route.call_count == 1


# --- publish() aggregation -----------------------------------------------


def test_partial_failure_reports_right_job_ids_and_does_not_claim_sent(mock_client) -> None:
    good_jobs = [(_make_job(title=f"Good {i}"), []) for i in range(10)]
    bad_jobs = [(_make_job(title=f"Bad {i}"), []) for i in range(5)]
    all_jobs = good_jobs + bad_jobs  # chunked as [10 good][5 bad]

    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL)
        route.side_effect = [
            httpx.Response(204),  # first chunk (10 good) succeeds
            httpx.Response(400, json={"message": "bad"}),  # second chunk (5 bad) fails
        ]
        result = publisher.publish(WEBHOOK_URL, all_jobs)

    assert result.sent == 10
    assert sorted(result.failed) == sorted(job.job_id for job, _ in bad_jobs)
    assert route.call_count == 2


# --- publish_error() -----------------------------------------------------


def test_publish_error_posts_a_single_message(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        publisher.publish_error(WEBHOOK_URL, "Something broke")

    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["content"] == "Something broke"


def test_publish_error_never_raises_even_on_500(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        publisher.publish_error(WEBHOOK_URL, "Something broke")  # must not raise


def test_publish_error_never_raises_on_a_connection_level_exception(mock_client) -> None:
    publisher = _make_publisher(mock_client)

    with respx.mock:
        respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("boom"))
        publisher.publish_error(WEBHOOK_URL, "Something broke")  # must not raise


# --- B1: secrets never live in yaml --------------------------------------


def test_no_discord_webhook_url_hardcoded_in_config_files() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    targets = [repo_root / "settings.yaml", repo_root / "filters.yaml"]
    companies_dir = repo_root / "companies"
    if companies_dir.is_dir():
        targets.extend(sorted(companies_dir.glob("*.yaml")))

    violations = [
        str(path)
        for path in targets
        if path.exists() and "discord.com/api/webhooks" in path.read_text(encoding="utf-8")
    ]
    assert not violations, f"Discord webhook URL hardcoded in: {violations}"


# --- THE NO-DOUBLE-POST TEST, load bearing --------------------------------


def test_publisher_never_marks_published_on_a_failed_send() -> None:
    """publish() must never call into the store -- it has no store reference
    at all. Marking published is the orchestrator's job (M5), and only after
    a confirmed 2xx. This is what prevents a job being marked published when
    the send actually failed."""
    sig = inspect.signature(DiscordPublisher.__init__)
    assert "store" not in sig.parameters

    source = Path(jobbot.publisher.__file__).read_text(encoding="utf-8")
    assert "jobbot.store" not in source
    assert "import store" not in source
