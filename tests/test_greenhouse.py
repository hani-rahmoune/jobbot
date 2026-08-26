from __future__ import annotations

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.base import SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.greenhouse import GreenhouseSource

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _make_source(
    client: httpx.Client, identifier: str = "acme", company_name: str = "Acme Corp"
) -> GreenhouseSource:
    return GreenhouseSource(identifier, company_name, client, user_agent=TEST_USER_AGENT)


# --- parse() -----------------------------------------------------------


def test_parse_classifies_every_fixture_entry_correctly(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}

    assert jobs["1001"].contract_type == "apprenticeship"  # French alternance
    assert jobs["1002"].contract_type == "internship"  # French stage
    assert jobs["1003"].contract_type == "internship"  # English internship
    assert jobs["1004"].contract_type == "other"  # senior full-time
    assert jobs["1005"].contract_type == "apprenticeship"  # apprenti, no location
    assert jobs["1006"].contract_type == "internship"  # stage, HTML entities
    assert jobs["1008"].contract_type == "apprenticeship"  # repost of 1001
    assert "1007" not in jobs  # malformed entry, must be skipped


def test_parse_skips_malformed_entry_and_returns_the_rest(greenhouse_payload, greenhouse_source):
    jobs = greenhouse_source.parse(greenhouse_payload["jobs"])
    assert len(jobs) == 7  # 8 fixture entries minus the one malformed entry


def test_missing_location_becomes_empty_string_not_a_crash(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    assert jobs["1005"].location == ""


def test_html_becomes_readable_plain_text(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    description = jobs["1006"].description

    assert "<p>" not in description
    assert "<div>" not in description
    assert "<li>" not in description
    assert "&amp;" not in description
    assert "&eacute;" not in description

    assert "Missions & responsabilités:" in description
    assert "Analyse de données" in description
    # An entity-encoded "<hebdomadaire>" must survive as literal text, not be
    # eaten by tag-stripping: content is decoded *after* real tags are gone.
    assert "Reporting <hebdomadaire>" in description
    assert "800€/mois" in description


# --- fetch_raw() / fetch() ----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        jobs = source.fetch()

    assert len(jobs) == 7  # 8 fixture entries minus the malformed one


def test_fetch_raises_source_empty_error_on_empty_jobs_array(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
        with pytest.raises(SourceEmptyError):
            source.fetch()


def test_fetch_retries_once_on_500_then_succeeds(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=greenhouse_payload)]
        jobs = source.fetch()

    assert len(jobs) == 7
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, json=greenhouse_payload),
        ]
        jobs = source.fetch()

    assert len(jobs) == 7
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_does_not_retry_on_4xx(mock_client) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/missing-co/jobs"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/missing-co/jobs"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    # SourceNotFoundError is a SourceError, so callers that only catch the
    # base class still see it.
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):
            source.fetch()


def test_user_agent_header_matches_what_was_injected(greenhouse_payload, mock_client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = GreenhouseSource("acme", "Acme Corp", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        source.fetch()

    sent_headers = route.calls.last.request.headers
    assert sent_headers["User-Agent"] == custom_user_agent
