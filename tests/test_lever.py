from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.lever import LeverSource

BOARD_URL = "https://api.lever.co/v0/postings/qonto"


def _make_source(
    client: httpx.Client, identifier: str = "qonto", company_name: str = "Qonto"
) -> LeverSource:
    return LeverSource(identifier, company_name, client, user_agent=TEST_USER_AGENT)


@pytest.fixture
def lever_source(mock_client: httpx.Client) -> LeverSource:
    return _make_source(mock_client)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(lever_payload, lever_source) -> None:
    jobs = {job.external_id: job for job in lever_source.parse(lever_payload)}

    apprentice = jobs["e1c3f84d-0ddd-40a6-bd13-b03285db8296"]
    assert apprentice.title == "Internal Control Apprentice (Tech & Product)"
    assert apprentice.location == "Paris"
    assert apprentice.contract_type == "apprenticeship"  # via categories.commitment
    assert str(apprentice.url) == "https://jobs.lever.co/qonto/e1c3f84d-0ddd-40a6-bd13-b03285db8296"

    intern = jobs["ba688ab4-66b1-4d6a-9aa7-537e191ea392"]
    assert intern.contract_type == "internship"  # via categories.commitment

    intern_no_hint = jobs["354fa50e-2741-4936-82f5-0d1ce5fb8be3"]  # commitment key absent
    assert intern_no_hint.contract_type == "internship"  # via title alone: "Intern"

    other = jobs["f84c6c54-b8da-4651-afc5-6b77a4fe34e8"]
    assert other.contract_type == "other"  # Full-time, no vocabulary anywhere

    assert "synthetic-0002" not in jobs  # malformed (missing "text"), must be skipped


def test_malformed_entry_is_skipped_with_a_logged_warning(
    lever_payload, lever_source, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        jobs = lever_source.parse(lever_payload)

    assert len(jobs) == 5  # 6 fixture entries minus the one malformed entry
    assert any("skipping malformed entry" in record.message for record in caplog.records)


def test_missing_location_becomes_empty_string_not_a_crash(lever_payload, lever_source) -> None:
    jobs = {job.external_id: job for job in lever_source.parse(lever_payload)}
    assert jobs["synthetic-0001"].location == ""


def test_epoch_millis_created_at_converted_to_utc_datetime(lever_payload, lever_source) -> None:
    jobs = {job.external_id: job for job in lever_source.parse(lever_payload)}
    job = jobs["f84c6c54-b8da-4651-afc5-6b77a4fe34e8"]  # createdAt: 1647366301010
    assert job.posted_at == datetime(2022, 3, 15, 17, 45, 1, 10000, tzinfo=UTC)


def test_missing_created_at_becomes_none_not_a_crash(lever_payload, lever_source) -> None:
    jobs = {job.external_id: job for job in lever_source.parse(lever_payload)}
    assert jobs["synthetic-0001"].posted_at is None  # no createdAt key on that fixture entry


# --- fetch_raw() / fetch() ----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(lever_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=lever_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count(lever_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=lever_payload))
        jobs = source.fetch()

    assert len(jobs) == 5


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(mock_client) -> None:
    # M8b: zero results is no longer automatically a failure -- see
    # jobbot/run.py's process_source() for where that decision now lives.
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=[]))
        jobs = source.fetch()

    assert jobs == []


def test_fetch_retries_once_on_500_then_succeeds(lever_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=lever_payload)]
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


def test_fetch_retries_once_on_timeout_then_succeeds(lever_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, json=lever_payload),
        ]
        jobs = source.fetch()

    assert len(jobs) == 5
    assert route.call_count == 2


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client) -> None:
    url = "https://api.lever.co/v0/postings/missing-co"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client) -> None:
    url = "https://api.lever.co/v0/postings/missing-co"
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


def test_user_agent_header_matches_what_was_injected(lever_payload, mock_client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = LeverSource("qonto", "Qonto", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=lever_payload))
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent
