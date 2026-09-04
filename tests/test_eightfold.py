"""M14 Part A: Eightfold AI adapter tests, mirroring test_lever.py's
structure (parse() first, then fetch_raw()/fetch() against respx-mocked
responses, then the fetch-contract edge cases every adapter in this
codebase shares -- retry-once-on-5xx, timeout retry, 404, robots.txt,
User-Agent). Search-term pagination/dedup (mirroring talentsoft.py's own
shape) is this adapter's own addition on top of that shared contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.eightfold import EightfoldSource

BASE_URL = "https://careers.example.com"
DOMAIN = "example.com"
IDENTIFIER = f"{BASE_URL}|{DOMAIN}"
SEARCH_URL = f"{BASE_URL}/api/pcsx/search"
DETAILS_URL = f"{BASE_URL}/api/pcsx/position_details"


def _make_source(
    client: httpx.Client, identifier: str = IDENTIFIER, company_name: str = "Example Corp", **kwargs: object
) -> EightfoldSource:
    return EightfoldSource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _mock_robots_allowed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/robots.txt").mock(return_value=httpx.Response(404))


# --- __init__ / identifier validation ---------------------------------------


def test_invalid_identifier_raises_value_error(mock_client: httpx.Client) -> None:
    for bad in ["not-a-url", "http://careers.example.com|example.com", "https://careers.example.com", "https://careers.example.com|"]:
        with pytest.raises(ValueError):
            EightfoldSource(bad, "Example Corp", mock_client, user_agent=TEST_USER_AGENT)


# --- parse() -----------------------------------------------------------


def test_parse_maps_every_fixture_entry_correctly(eightfold_details_payload, mock_client) -> None:
    source = _make_source(mock_client)
    jobs = {job.external_id: job for job in source.parse(eightfold_details_payload)}

    stage = jobs["R168914"]
    assert stage.title == "SAINT LAURENT Stage Assistant(e) Développement Jersey"
    assert stage.location == "Paris, IDF, FR"  # standardizedLocations preferred
    assert stage.contract_type == "internship"
    assert str(stage.url) == "https://careers.kering.com/careers/job/563705891147682"
    assert stage.posted_at == datetime(2026, 7, 21, tzinfo=UTC)  # postedTs, not creationTs

    manager = jobs["R170443"]
    assert manager.location == "Vienna, Vienna, AT"
    assert manager.contract_type == "other"


def test_parse_falls_back_to_locations_when_no_standardized_locations(mock_client) -> None:
    source = _make_source(mock_client)
    entry = {
        "id": 1,
        "displayJobId": "R1",
        "name": "Some Role",
        "locations": ["Somewhere, Nowhere"],
        "standardizedLocations": [],
        "publicUrl": "https://careers.example.com/careers/job/1",
        "jobDescription": "<p>Text.</p>",
    }
    (job,) = source.parse([entry])
    assert job.location == "Somewhere, Nowhere"


def test_parse_falls_back_to_creationts_when_no_postedts(mock_client) -> None:
    source = _make_source(mock_client)
    entry = {
        "id": 1,
        "displayJobId": "R1",
        "name": "Some Role",
        "creationTs": 1700000000,
        "publicUrl": "https://careers.example.com/careers/job/1",
        "jobDescription": "",
    }
    (job,) = source.parse([entry])
    assert job.posted_at == datetime.fromtimestamp(1700000000, tz=UTC)


def test_parse_constructs_url_from_position_url_when_no_public_url(mock_client) -> None:
    source = _make_source(mock_client)
    entry = {
        "id": 1,
        "displayJobId": "R1",
        "name": "Some Role",
        "positionUrl": "/careers/job/1",
        "jobDescription": "",
    }
    (job,) = source.parse([entry])
    assert str(job.url) == f"{BASE_URL}/careers/job/1"


def test_parse_falls_back_to_id_when_no_display_job_id(mock_client) -> None:
    source = _make_source(mock_client)
    entry = {"id": 42, "name": "Some Role", "publicUrl": f"{BASE_URL}/careers/job/42", "jobDescription": ""}
    (job,) = source.parse([entry])
    assert job.external_id == "42"


def test_malformed_entry_missing_name_is_skipped_with_a_logged_warning(
    mock_client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    raw = [{"id": 1, "publicUrl": f"{BASE_URL}/careers/job/1", "jobDescription": ""}]
    with caplog.at_level("WARNING"):
        jobs = source.parse(raw)

    assert jobs == []
    assert any("skipping malformed entry" in r.message for r in caplog.records)


def test_html_description_is_stripped_and_unescaped(mock_client) -> None:
    source = _make_source(mock_client)
    entry = {
        "id": 1,
        "displayJobId": "R1",
        "name": "Some Role",
        "publicUrl": f"{BASE_URL}/careers/job/1",
        "jobDescription": "<p>Line one.</p><p>Line &amp; two.</p>",
    }
    (job,) = source.parse([entry])
    assert "Line one." in job.description
    assert "Line & two." in job.description
    assert "<p>" not in job.description


# --- fetch_raw() / fetch(), no search_terms (plain pagination) ------------


def test_fetch_raw_returns_a_two_tuple(eightfold_search_payload, eightfold_details_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=eightfold_search_payload))
        for detail in eightfold_details_payload:
            respx.get(DETAILS_URL, params={"position_id": str(detail["id"])}).mock(
                return_value=httpx.Response(200, json={"data": detail})
            )
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_with_no_search_terms_fetches_every_position_details(
    eightfold_search_payload, eightfold_details_payload, mock_client
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=eightfold_search_payload))
        for detail in eightfold_details_payload:
            respx.get(DETAILS_URL, params={"position_id": str(detail["id"])}).mock(
                return_value=httpx.Response(200, json={"data": detail})
            )
        jobs = source.fetch()

    assert len(jobs) == 2


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"data": {"positions": [], "count": 0}})
        )
        jobs = source.fetch()

    assert jobs == []


# --- search_terms: one query per term, dedup, pagination -------------------


def test_search_terms_query_the_server_and_dedup_across_terms(mock_client) -> None:
    source = _make_source(mock_client, search_terms=["stage", "alternance"])
    detail = {
        "id": 1, "displayJobId": "R1", "name": "Shared Role",
        "publicUrl": f"{BASE_URL}/careers/job/1", "jobDescription": "",
    }
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        # The SAME position id matches both search terms -- must be
        # fetched (and appear in the result) only once.
        respx.get(SEARCH_URL, params={"query": "stage"}).mock(
            return_value=httpx.Response(200, json={"data": {"positions": [{"id": 1}], "count": 1}})
        )
        respx.get(SEARCH_URL, params={"query": "alternance"}).mock(
            return_value=httpx.Response(200, json={"data": {"positions": [{"id": 1}], "count": 1}})
        )
        details_route = respx.get(DETAILS_URL, params={"position_id": "1"}).mock(
            return_value=httpx.Response(200, json={"data": detail})
        )
        jobs = source.fetch()

    assert len(jobs) == 1
    assert details_route.call_count == 1  # fetched once despite matching twice


def test_search_pagination_follows_the_reported_count(mock_client) -> None:
    """Page size is measured from page 1's own result count (2 here, not
    assumed to be 10 the way Kering's own tenant happened to page) -- with
    count=5 and a page size of 2, three pages are needed."""
    source = _make_source(mock_client, search_terms=["stage"])
    page1 = {"data": {"positions": [{"id": 1}, {"id": 2}], "count": 5}}
    page2 = {"data": {"positions": [{"id": 3}, {"id": 4}], "count": 5}}
    page3 = {"data": {"positions": [{"id": 5}], "count": 5}}
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL, params={"start": "0"}).mock(return_value=httpx.Response(200, json=page1))
        respx.get(SEARCH_URL, params={"start": "2"}).mock(return_value=httpx.Response(200, json=page2))
        respx.get(SEARCH_URL, params={"start": "4"}).mock(return_value=httpx.Response(200, json=page3))
        for i in range(1, 6):
            respx.get(DETAILS_URL, params={"position_id": str(i)}).mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"id": i, "name": f"Role {i}", "publicUrl": f"{BASE_URL}/careers/job/{i}", "jobDescription": ""}},
                )
            )
        jobs = source.fetch()

    assert len(jobs) == 5


def test_search_hits_the_page_cap_and_logs(mock_client, caplog: pytest.LogCaptureFixture) -> None:
    from jobbot.sources import eightfold as eightfold_module

    source = _make_source(mock_client, search_terms=["stage"])
    with respx.mock, pytest.MonkeyPatch.context() as mp:
        mp.setattr(eightfold_module, "MAX_PAGES_PER_SEARCH_TERM", 1)
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL, params={"start": "0"}).mock(
            return_value=httpx.Response(200, json={"data": {"positions": [{"id": 1}], "count": 100}})
        )
        respx.get(DETAILS_URL, params={"position_id": "1"}).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"id": 1, "name": "Role", "publicUrl": f"{BASE_URL}/careers/job/1", "jobDescription": ""}},
            )
        )
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 1
    assert any("hit the 1-page cap" in r.message for r in caplog.records)


# --- fetch-contract edge cases (shared shape with every other adapter) ----


def test_an_unreachable_position_is_skipped_with_a_warning_not_a_crash(
    eightfold_search_payload, mock_client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    good_id = eightfold_search_payload["data"]["positions"][0]["id"]
    bad_id = eightfold_search_payload["data"]["positions"][1]["id"]
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=eightfold_search_payload))
        respx.get(DETAILS_URL, params={"position_id": str(good_id)}).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"id": good_id, "name": "Reachable", "publicUrl": BASE_URL, "jobDescription": ""}},
            )
        )
        respx.get(DETAILS_URL, params={"position_id": str(bad_id)}).mock(return_value=httpx.Response(500))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 1
    assert any("skipping unreachable position" in r.message for r in caplog.records)


def test_robots_disallow_raises_source_error_and_nothing_else_is_fetched(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(f"{BASE_URL}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
        search_route = respx.get(SEARCH_URL)
        with pytest.raises(SourceError):
            source.fetch_raw()

    assert search_route.call_count == 0


def test_fetch_raises_source_error_after_exhausting_retries_on_the_search(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_retries_once_on_timeout_then_succeeds(eightfold_search_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(SEARCH_URL)
        route.side_effect = [httpx.TimeoutException("timed out"), httpx.Response(200, json=eightfold_search_payload)]
        for pos in eightfold_search_payload["data"]["positions"]:
            respx.get(DETAILS_URL, params={"position_id": str(pos["id"])}).mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"id": pos["id"], "name": pos["name"], "publicUrl": BASE_URL, "jobDescription": ""}},
                )
            )
        jobs = source.fetch()

    assert len(jobs) == 2
    assert route.call_count == 2


def test_fetch_raises_source_not_found_error_on_404(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()


def test_an_unparseable_search_response_is_treated_as_no_results(
    mock_client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="not json"))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert jobs == []
    assert any("unparseable search response" in r.message for r in caplog.records)


def test_user_agent_header_matches_what_was_injected(eightfold_search_payload, mock_client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = EightfoldSource(IDENTIFIER, "Example Corp", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=eightfold_search_payload))
        for pos in eightfold_search_payload["data"]["positions"]:
            respx.get(DETAILS_URL, params={"position_id": str(pos["id"])}).mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"id": pos["id"], "name": pos["name"], "publicUrl": BASE_URL, "jobDescription": ""}},
                )
            )
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent
