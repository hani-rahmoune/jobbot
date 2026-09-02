from __future__ import annotations

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

import jobbot.sources.jibe as jibe_module
from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.jibe import PAGE_SIZE, JibeSource

BOARD_URL = "https://careers.axa.com/api/jobs"


def _make_source(
    client: httpx.Client,
    identifier: str = "https://careers.axa.com|France",
    company_name: str = "AXA",
    **kwargs: object,
) -> JibeSource:
    return JibeSource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _make_postings(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "data": {
                "slug": f"synthetic-{i}",
                "title": f"Role {i}",
                "full_location": "PARIS, France",
                "employment_type": "REGULAR",
            }
        }
        for i in range(start, start + n)
    ]


@pytest.fixture
def jibe_source(mock_client: httpx.Client) -> JibeSource:
    return _make_source(mock_client)


# --- identifier validation --------------------------------------------


def test_identifier_without_country_segment_has_no_country_filter(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="https://careers.axa.com")
    assert source._country is None
    assert source._base_url == "https://careers.axa.com"


def test_identifier_with_country_segment_parses_both_parts(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="https://careers.axa.com|France")
    assert source._base_url == "https://careers.axa.com"
    assert source._country == "France"


@pytest.mark.parametrize("bad_identifier", ["not-a-url", "http://careers.axa.com", ""])
def test_invalid_identifier_raises_value_error(mock_client: httpx.Client, bad_identifier: str) -> None:
    with pytest.raises(ValueError):
        _make_source(mock_client, identifier=bad_identifier)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(jibe_payload, jibe_source) -> None:
    jobs = {job.external_id: job for job in jibe_source.parse(jibe_payload["jobs"])}

    apprentice = jobs["24583"]
    assert apprentice.title == "Alternance - Assistant Audiovisuel"
    assert apprentice.location == "PARIS, France"
    assert apprentice.contract_type == "apprenticeship"
    assert str(apprentice.url) == "https://careers-fr-axa.icims.com/jobs/24583/login"
    assert "régies" in apprentice.description  # HTML entities decoded, tags stripped

    intern = jobs["22723"]
    assert intern.contract_type == "internship"  # bare "stage" + French context in title

    other = jobs["24601"]
    assert other.contract_type == "other"

    fallback_location = jobs["24610"]
    assert fallback_location.location == "Wroclaw"  # no full_location, falls back to city

    assert "" not in jobs  # the missing-slug entry must be skipped, not stored under ""


def test_malformed_entry_is_skipped_with_a_logged_warning(
    jibe_payload, jibe_source, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        jobs = jibe_source.parse(jibe_payload["jobs"])

    assert len(jobs) == 4  # 5 fixture entries minus the one missing a slug/req_id
    assert any("skipping malformed entry" in record.message for record in caplog.records)


# --- fetch_raw() / fetch() -----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(jibe_payload, mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=jibe_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count_and_stops_at_one_page(
    jibe_payload, mock_client: httpx.Client
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=jibe_payload))
        jobs = source.fetch()

    assert len(jobs) == 4  # 5 in the fixture, minus the malformed one
    assert route.call_count == 1  # fewer than PAGE_SIZE results: no second page fetched


def test_fetch_sends_the_configured_country_filter(jibe_payload, mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="https://careers.axa.com|France")
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=jibe_payload))
        source.fetch()

    assert dict(route.calls.last.request.url.params)["country"] == "France"


def test_fetch_sends_no_country_param_when_identifier_has_none(
    jibe_payload, mock_client: httpx.Client
) -> None:
    source = _make_source(mock_client, identifier="https://careers.axa.com")
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=jibe_payload))
        source.fetch()

    assert "country" not in dict(route.calls.last.request.url.params)


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure -- see
    # jobbot/run.py's process_source() for where that decision now lives.
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"totalCount": 0, "jobs": []}))
        jobs = source.fetch()

    assert jobs == []


def test_fetch_retries_once_on_500_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"totalCount": 1, "jobs": _make_postings(1)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=payload)]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"totalCount": 1, "jobs": _make_postings(1)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.TimeoutException("timed out"), httpx.Response(200, json=payload)]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="https://careers.missing.example")
    url = "https://careers.missing.example/api/jobs"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="https://careers.missing.example")
    url = "https://careers.missing.example/api/jobs"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1


def test_user_agent_header_matches_what_was_injected(jibe_payload, mock_client: httpx.Client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = JibeSource(
        "https://careers.axa.com", "AXA", mock_client, user_agent=custom_user_agent
    )
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=jibe_payload))
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent


# --- pagination -----------------------------------------------------------


def test_fetch_paginates_until_a_page_returns_fewer_than_page_size(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    partial = PAGE_SIZE // 2
    page1 = {"totalCount": PAGE_SIZE + partial, "jobs": _make_postings(PAGE_SIZE, start=0)}
    page2 = {"totalCount": PAGE_SIZE + partial, "jobs": _make_postings(partial, start=PAGE_SIZE)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        jobs = source.fetch()

    assert len(jobs) == PAGE_SIZE + partial
    assert route.call_count == 2


def test_fetch_stops_at_the_page_cap_and_logs_a_warning(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(jibe_module, "MAX_PAGES", 2)
    source = _make_source(mock_client)
    full_page = {"totalCount": 999, "jobs": _make_postings(PAGE_SIZE)}
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=full_page))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2 * PAGE_SIZE
    assert route.call_count == 2
    assert any("page cap" in record.message for record in caplog.records)


# --- search_terms (M9) -----------------------------------------------------


def test_no_search_terms_configured_is_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.search_terms == []


def test_search_terms_sends_one_query_per_term_with_matching_keywords_param(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    alternance_page = {"totalCount": 2, "jobs": _make_postings(2, start=0)}
    stage_page = {"totalCount": 1, "jobs": _make_postings(1, start=100)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    assert route.call_count == 2
    sent_keywords = [dict(call.request.url.params)["keywords"] for call in route.calls]
    assert sent_keywords == ["alternance", "stage"]
    assert len(jobs) == 3


def test_search_terms_deduplicates_postings_across_terms_by_slug(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    alternance_page = {"totalCount": 2, "jobs": _make_postings(2, start=0)}
    stage_page = {"totalCount": 2, "jobs": _make_postings(2, start=1)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    external_ids = {job.external_id for job in jobs}
    assert len(jobs) == 3  # synthetic-0, synthetic-1 (deduped), synthetic-2
    assert external_ids == {"synthetic-0", "synthetic-1", "synthetic-2"}
