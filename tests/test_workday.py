from __future__ import annotations

import json

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

import jobbot.sources.workday as workday_module
from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.workday import PAGE_SIZE, WorkdaySource

BOARD_URL = "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs"


def _make_source(
    client: httpx.Client,
    identifier: str = "sanofi.wd3.SanofiCareers",
    company_name: str = "Sanofi",
    **kwargs: object,
) -> WorkdaySource:
    return WorkdaySource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _make_postings(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "title": f"Role {i}",
            "externalPath": f"/job/Loc/Role-{i}_R{i}",
            "locationsText": "Paris, France",
            "postedOn": "Posted Today",
        }
        for i in range(start, start + n)
    ]


@pytest.fixture
def workday_source(mock_client: httpx.Client) -> WorkdaySource:
    return _make_source(mock_client)


# --- identifier validation --------------------------------------------


def test_valid_identifier_parses_tenant_wd_number_and_site(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="sanofi.wd3.SanofiCareers")
    assert source._tenant == "sanofi"
    assert source._wd_number == "3"
    assert source._site == "SanofiCareers"


@pytest.mark.parametrize(
    "bad_identifier",
    [
        "sanofi",
        "sanofi.SanofiCareers",
        "sanofi.wd3",
        "sanofi.wdX.SanofiCareers",
        "sanofi.wd.SanofiCareers",
        "",
        "acme",  # a bare token, like other adapters take -- not enough here
    ],
)
def test_invalid_identifier_raises_value_error(mock_client: httpx.Client, bad_identifier: str) -> None:
    with pytest.raises(ValueError):
        _make_source(mock_client, identifier=bad_identifier)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(workday_payload, workday_source) -> None:
    jobs = {job.external_id: job for job in workday_source.parse(workday_payload["jobPostings"])}

    apprentice = jobs["/job/Paris/Alternance-Data-Analyst--F-H-_R9000001"]
    assert apprentice.title == "Alternance Data Analyst (F/H)"
    assert apprentice.location == "Paris, France"
    assert apprentice.contract_type == "apprenticeship"
    assert str(apprentice.url) == (
        "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers"
        "/job/Paris/Alternance-Data-Analyst--F-H-_R9000001"
    )

    intern = jobs["/job/Gentilly/Stage-Data-Science---6-mois--F-H-_R9000002"]
    assert intern.contract_type == "internship"  # bare "stage" + "6 mois" French context in title

    other = jobs["/job/Morristown-NJ/Internal-Auditor---VIE-Contract_R2860110"]
    assert other.contract_type == "other"

    assert "/job/Nowhere/Missing-Title_R9000004" not in jobs  # malformed, missing title


def test_missing_location_becomes_empty_string_not_a_crash(workday_payload, workday_source) -> None:
    jobs = {job.external_id: job for job in workday_source.parse(workday_payload["jobPostings"])}
    assert jobs["/job/Remote-Location/Field-Sales-Representative_R9000003"].location == ""


def test_posted_at_is_always_none_and_description_always_empty(
    workday_payload, workday_source
) -> None:
    # Workday's list endpoint carries neither -- see the module docstring.
    jobs = workday_source.parse(workday_payload["jobPostings"])
    assert all(job.posted_at is None for job in jobs)
    assert all(job.description == "" for job in jobs)


def test_malformed_entry_is_skipped_with_a_logged_warning(
    workday_payload, workday_source, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        jobs = workday_source.parse(workday_payload["jobPostings"])

    assert len(jobs) == 6  # 7 fixture entries minus the one malformed (no title)
    assert any("skipping malformed entry" in record.message for record in caplog.records)


# --- fetch_raw() / fetch() -----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(workday_payload, mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=workday_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count_and_stops_at_one_page(
    workday_payload, mock_client: httpx.Client
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=workday_payload))
        jobs = source.fetch()

    assert len(jobs) == 6  # 7 in the fixture, minus the malformed one
    assert route.call_count == 1  # fewer than PAGE_SIZE results: no second page fetched


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure -- see
    # jobbot/run.py's process_source() for where that decision now lives.
    source = _make_source(mock_client)
    with respx.mock:
        respx.post(BOARD_URL).mock(
            return_value=httpx.Response(200, json={"total": 0, "jobPostings": []})
        )
        jobs = source.fetch()

    assert jobs == []


def test_fetch_retries_once_on_500_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"total": 1, "jobPostings": _make_postings(1)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=payload)]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    payload = {"total": 1, "jobPostings": _make_postings(1)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [httpx.TimeoutException("timed out"), httpx.Response(200, json=payload)]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="missing.wd3.MissingCareers", company_name="Missing")
    url = "https://missing.wd3.myworkdayjobs.com/wday/cxs/missing/MissingCareers/jobs"
    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier="missing.wd3.MissingCareers", company_name="Missing")
    url = "https://missing.wd3.myworkdayjobs.com/wday/cxs/missing/MissingCareers/jobs"
    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):  # SourceNotFoundError is a SourceError
            source.fetch()


def test_user_agent_header_matches_what_was_injected(
    workday_payload, mock_client: httpx.Client
) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = WorkdaySource(
        "sanofi.wd3.SanofiCareers", "Sanofi", mock_client, user_agent=custom_user_agent
    )
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=workday_payload))
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent


# --- pagination (A2, C1) --------------------------------------------------


def test_fetch_paginates_until_a_page_returns_fewer_than_page_size(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    partial = PAGE_SIZE // 2
    page1 = {"total": PAGE_SIZE + partial, "jobPostings": _make_postings(PAGE_SIZE, start=0)}
    page2 = {"total": PAGE_SIZE + partial, "jobPostings": _make_postings(partial, start=PAGE_SIZE)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        jobs = source.fetch()

    assert len(jobs) == PAGE_SIZE + partial
    assert route.call_count == 2


def test_fetch_stops_at_the_page_cap_and_logs_a_warning(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A lower page cap for this test only -- proves the same cap-enforcement
    # behavior as the real MAX_PAGES=20 without paying for 20 real requests
    # and 2000 fake postings just to exercise "the cap was hit".
    monkeypatch.setattr(workday_module, "MAX_PAGES", 3)
    # max_postings set far above what 3 full pages would ever reach, so the
    # page cap -- not max_postings -- is what actually stops this fetch.
    source = _make_source(mock_client, max_postings=1_000_000)
    full_page = {"total": 999_999, "jobPostings": _make_postings(PAGE_SIZE)}
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=full_page))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 3 * PAGE_SIZE
    assert route.call_count == 3
    assert any("page cap" in record.message for record in caplog.records)


def test_fetch_stops_at_max_postings_and_logs_a_warning(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, max_postings=150)
    full_page = {"total": 999_999, "jobPostings": _make_postings(PAGE_SIZE)}
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=full_page))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 150  # truncated mid-page, from 8 pages * 20/page fetched
    assert route.call_count == 8
    assert any("max_postings=150" in record.message for record in caplog.records)


# --- search_terms (M8b) --------------------------------------------------


def test_no_search_terms_configured_is_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.search_terms == []


def test_search_terms_sends_one_query_per_term_with_matching_search_text(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    alternance_page = {"total": 2, "jobPostings": _make_postings(2, start=0)}
    stage_page = {"total": 1, "jobPostings": _make_postings(1, start=100)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    assert route.call_count == 2
    sent_terms = [json.loads(call.request.content)["searchText"] for call in route.calls]
    assert sent_terms == ["alternance", "stage"]
    assert len(jobs) == 3  # 2 + 1, no overlap


def test_search_terms_deduplicates_postings_across_terms_by_external_path(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    # Role-1_R1 is returned by both terms -- the same posting, found twice.
    alternance_page = {"total": 2, "jobPostings": _make_postings(2, start=0)}
    stage_page = {"total": 2, "jobPostings": _make_postings(2, start=1)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    external_ids = {job.external_id for job in jobs}
    assert len(jobs) == 3  # Role-0, Role-1 (deduped across both terms), Role-2
    assert external_ids == {
        "/job/Loc/Role-0_R0",
        "/job/Loc/Role-1_R1",
        "/job/Loc/Role-2_R2",
    }


def test_search_terms_paginates_each_term_independently(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    partial = PAGE_SIZE // 2
    alternance_page1 = {
        "total": PAGE_SIZE + partial, "jobPostings": _make_postings(PAGE_SIZE, start=0)
    }
    alternance_page2 = {
        "total": PAGE_SIZE + partial, "jobPostings": _make_postings(partial, start=PAGE_SIZE),
    }
    stage_page = {"total": 1, "jobPostings": _make_postings(1, start=10_000)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [
            httpx.Response(200, json=alternance_page1),
            httpx.Response(200, json=alternance_page2),
            httpx.Response(200, json=stage_page),
        ]
        jobs = source.fetch()

    assert route.call_count == 3  # 2 pages for "alternance", 1 (short) page for "stage"
    assert len(jobs) == PAGE_SIZE + partial + 1


def test_search_terms_stops_at_its_own_per_term_page_cap_and_logs_a_warning(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workday_module, "MAX_PAGES_PER_SEARCH_TERM", 2)
    source = _make_source(mock_client, search_terms=["alternance"], max_postings=1_000_000)
    page1 = {"total": 999_999, "jobPostings": _make_postings(PAGE_SIZE, start=0)}
    page2 = {"total": 999_999, "jobPostings": _make_postings(PAGE_SIZE, start=PAGE_SIZE)}
    with respx.mock:
        route = respx.post(BOARD_URL)
        route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2 * PAGE_SIZE
    assert route.call_count == 2  # MAX_PAGES_PER_SEARCH_TERM=2, both pages full
    assert any("page cap" in record.message for record in caplog.records)


def test_search_terms_stops_across_terms_once_max_postings_reached(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(
        mock_client, search_terms=["alternance", "stage", "internship"], max_postings=2
    )
    alternance_page = {"total": 2, "jobPostings": _make_postings(2, start=0)}
    with respx.mock:
        route = respx.post(BOARD_URL).mock(return_value=httpx.Response(200, json=alternance_page))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2
    # Stopped after the first term already reached max_postings=2 -- "stage"
    # and "internship" are never queried.
    assert route.call_count == 1
    assert any("max_postings=2" in record.message for record in caplog.records)
