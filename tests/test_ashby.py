from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import SourceEmptyError, SourceError, SourceNotFoundError

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/alan"


def _make_source(
    client: httpx.Client, identifier: str = "alan", company_name: str = "Alan"
) -> AshbySource:
    return AshbySource(identifier, company_name, client, user_agent=TEST_USER_AGENT)


@pytest.fixture
def ashby_source(mock_client: httpx.Client) -> AshbySource:
    return _make_source(mock_client)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(ashby_payload, ashby_source) -> None:
    jobs = {job.external_id: job for job in ashby_source.parse(ashby_payload["jobs"])}

    cto_intern = jobs["d457e0f1-2418-4759-b4ed-e41fdff50bf0"]
    assert cto_intern.title == "CTO Founder Associate - internship"
    assert cto_intern.location == "Paris, France"
    assert cto_intern.contract_type == "internship"  # via employmentType AND title
    assert str(cto_intern.url) == "https://jobs.ashbyhq.com/alan/d457e0f1-2418-4759-b4ed-e41fdff50bf0"

    swe_intern = jobs["de4c30ae-698f-43e9-84d1-f458955fd671"]
    assert swe_intern.location == "Paris"
    assert swe_intern.contract_type == "internship"

    full_stack = jobs["118f5ad8-5db5-48ec-940e-1a880b148f25"]
    assert full_stack.contract_type == "other"  # FullTime, no vocabulary anywhere

    assert "synthetic-0003" not in jobs  # malformed (missing "title"), must be skipped


def test_malformed_entry_is_skipped_with_a_logged_warning(
    ashby_payload, ashby_source, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        jobs = ashby_source.parse(ashby_payload["jobs"])

    assert len(jobs) == 5  # 6 fixture entries minus the one malformed entry
    assert any("skipping malformed entry" in record.message for record in caplog.records)


def test_missing_location_becomes_empty_string_not_a_crash(ashby_payload, ashby_source) -> None:
    jobs = {job.external_id: job for job in ashby_source.parse(ashby_payload["jobs"])}
    assert jobs["synthetic-0001"].location == ""


def test_description_html_used_when_description_plain_absent(ashby_payload, ashby_source) -> None:
    jobs = {job.external_id: job for job in ashby_source.parse(ashby_payload["jobs"])}
    job = jobs["synthetic-0002"]
    assert "only descriptionHtml is present" in job.description
    assert "<p>" not in job.description  # tags stripped


def test_published_at_iso8601_string_converted_to_utc_datetime(ashby_payload, ashby_source) -> None:
    jobs = {job.external_id: job for job in ashby_source.parse(ashby_payload["jobs"])}
    job = jobs["118f5ad8-5db5-48ec-940e-1a880b148f25"]  # publishedAt: 2026-06-16T16:55:05.029+00:00
    assert job.posted_at == datetime(2026, 6, 16, 16, 55, 5, 29000, tzinfo=UTC)


# --- fetch_raw() / fetch() ----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(ashby_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=ashby_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count(ashby_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=ashby_payload))
        jobs = source.fetch()

    assert len(jobs) == 5


def test_fetch_raises_source_empty_error_on_empty_array(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
        with pytest.raises(SourceEmptyError):
            source.fetch()


def test_fetch_retries_once_on_500_then_succeeds(ashby_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=ashby_payload)]
        jobs = source.fetch()

    assert len(jobs) == 5
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_retries_once_on_timeout_then_succeeds(ashby_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, json=ashby_payload),
        ]
        jobs = source.fetch()

    assert len(jobs) == 5
    assert route.call_count == 2


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client) -> None:
    url = "https://api.ashbyhq.com/posting-api/job-board/missing-co"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client) -> None:
    url = "https://api.ashbyhq.com/posting-api/job-board/missing-co"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):  # SourceNotFoundError is a SourceError
            source.fetch()


def test_user_agent_header_matches_what_was_injected(ashby_payload, mock_client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = AshbySource("alan", "Alan", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=ashby_payload))
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent
