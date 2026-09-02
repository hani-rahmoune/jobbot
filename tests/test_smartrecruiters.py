from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

import jobbot.sources.smartrecruiters as smartrecruiters_module
from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.smartrecruiters import PAGE_SIZE, SmartRecruitersSource

BOARD_URL = "https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings"


def _make_source(
    client: httpx.Client,
    identifier: str = "Ubisoft2",
    company_name: str = "Ubisoft",
    **kwargs: object,
) -> SmartRecruitersSource:
    return SmartRecruitersSource(
        identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs
    )


def _make_postings(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "id": f"synthetic-{i}",
            "name": f"Role {i}",
            "location": {"fullLocation": "Paris, IDF, France"},
            "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        }
        for i in range(start, start + n)
    ]


@pytest.fixture
def smartrecruiters_source(mock_client: httpx.Client) -> SmartRecruitersSource:
    return _make_source(mock_client)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(
    smartrecruiters_payload, smartrecruiters_source
) -> None:
    jobs = {
        job.external_id: job
        for job in smartrecruiters_source.parse(smartrecruiters_payload["content"])
    }

    other = jobs["744000147037200"]
    assert other.title == "Executive Assistant (F/H/NB)"
    assert other.location == "Paris, IDF, France"
    assert other.contract_type == "other"
    assert str(other.url) == "https://jobs.smartrecruiters.com/Ubisoft2/744000147037200"
    assert other.posted_at == datetime(2026, 9, 2, 16, 9, 51, 107000, tzinfo=UTC)

    apprentice = jobs["744000147024510"]
    assert apprentice.contract_type == "apprenticeship"  # title + Intern/Internship hint

    intern = jobs["744000147031370"]
    assert intern.contract_type == "internship"  # bare "stage" + "6 mois" French context

    missing_location = jobs["synthetic-0004"]
    assert missing_location.location == ""  # location.country present, but no fullLocation
    assert missing_location.posted_at is None  # no releasedDate on this entry

    assert "synthetic-0005" not in jobs  # malformed: missing "name"


def test_malformed_entry_is_skipped_with_a_logged_warning(
    smartrecruiters_payload, smartrecruiters_source, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        jobs = smartrecruiters_source.parse(smartrecruiters_payload["content"])

    assert len(jobs) == 4  # 5 fixture entries minus the one malformed (no name)
    assert any("skipping malformed entry" in record.message for record in caplog.records)


# --- fetch_raw() / fetch() -----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(smartrecruiters_payload, mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=smartrecruiters_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count_and_stops_at_one_page(
    smartrecruiters_payload, mock_client: httpx.Client
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(
            return_value=httpx.Response(200, json=smartrecruiters_payload)
        )
        jobs = source.fetch()

    assert len(jobs) == 4  # 5 in the fixture, minus the malformed one
    assert route.call_count == 1  # fewer than PAGE_SIZE results: no second page fetched


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure -- see
    # jobbot/run.py's process_source() for where that decision now lives.
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(
            return_value=httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []})
        )
        jobs = source.fetch()

    assert jobs == []


def test_fetch_retries_once_on_500_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"offset": 0, "limit": 100, "totalFound": 1, "content": _make_postings(1)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=payload)]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"offset": 0, "limit": 100, "totalFound": 1, "content": _make_postings(1)}
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
    source = _make_source(mock_client, identifier="Missing", company_name="Missing")
    url = "https://api.smartrecruiters.com/v1/companies/Missing/postings"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="Missing", company_name="Missing")
    url = "https://api.smartrecruiters.com/v1/companies/Missing/postings"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):  # SourceNotFoundError is a SourceError
            source.fetch()


def test_user_agent_header_matches_what_was_injected(
    smartrecruiters_payload, mock_client: httpx.Client
) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = SmartRecruitersSource(
        "Ubisoft2", "Ubisoft", mock_client, user_agent=custom_user_agent
    )
    with respx.mock:
        route = respx.get(BOARD_URL).mock(
            return_value=httpx.Response(200, json=smartrecruiters_payload)
        )
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent


# --- pagination -----------------------------------------------------------


def test_fetch_paginates_until_a_page_returns_fewer_than_page_size(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    partial = PAGE_SIZE // 2
    page1 = {
        "offset": 0, "limit": PAGE_SIZE, "totalFound": PAGE_SIZE + partial,
        "content": _make_postings(PAGE_SIZE, start=0),
    }
    page2 = {
        "offset": PAGE_SIZE, "limit": PAGE_SIZE, "totalFound": PAGE_SIZE + partial,
        "content": _make_postings(partial, start=PAGE_SIZE),
    }
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        jobs = source.fetch()

    assert len(jobs) == PAGE_SIZE + partial
    assert route.call_count == 2


def test_fetch_stops_at_the_page_cap_and_logs_a_warning(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(smartrecruiters_module, "MAX_PAGES", 2)
    source = _make_source(mock_client)
    page1 = {"offset": 0, "limit": PAGE_SIZE, "totalFound": 999, "content": _make_postings(PAGE_SIZE, start=0)}
    page2 = {"offset": PAGE_SIZE, "limit": PAGE_SIZE, "totalFound": 999, "content": _make_postings(PAGE_SIZE, start=PAGE_SIZE)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2 * PAGE_SIZE
    assert route.call_count == 2
    assert any("page cap" in record.message for record in caplog.records)


# --- search_terms (M9) -----------------------------------------------------


def test_no_search_terms_configured_is_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.search_terms == []


def test_search_terms_sends_one_query_per_term_with_matching_q_param(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    alternance_page = {"offset": 0, "limit": PAGE_SIZE, "totalFound": 2, "content": _make_postings(2, start=0)}
    stage_page = {"offset": 0, "limit": PAGE_SIZE, "totalFound": 1, "content": _make_postings(1, start=100)}
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    assert route.call_count == 2
    sent_terms = [dict(call.request.url.params)["q"] for call in route.calls]
    assert sent_terms == ["alternance", "stage"]
    assert len(jobs) == 3


def test_search_terms_deduplicates_postings_across_terms_by_id(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    alternance_page = {"offset": 0, "limit": PAGE_SIZE, "totalFound": 2, "content": _make_postings(2, start=0)}
    stage_page = {"offset": 0, "limit": PAGE_SIZE, "totalFound": 2, "content": _make_postings(2, start=1)}
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
