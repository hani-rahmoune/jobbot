from __future__ import annotations

import httpx
import pytest
import respx
from conftest import FIXTURES_DIR, TEST_USER_AGENT

from jobbot.sources.base import SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.jsonld import JsonLdSource

JSONLD_FIXTURES_DIR = FIXTURES_DIR / "jsonld"

# robots.txt that allows everything -- the default posture for most sites,
# and the one that lets a properly-mocked test reach the page fetch at all.
ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"
DISALLOW_ALL_ROBOTS = "User-agent: *\nDisallow: /\n"


def _read_fixture(name: str) -> str:
    return (JSONLD_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_source(client: httpx.Client, url: str, company_name: str = "DataCorp Test") -> JsonLdSource:
    return JsonLdSource(url, company_name, client, user_agent=TEST_USER_AGENT)


def _mock_robots_allow(host: str) -> None:
    respx.get(f"https://{host}/robots.txt").mock(
        return_value=httpx.Response(200, text=ALLOW_ALL_ROBOTS)
    )


# --- shape parsing -------------------------------------------------------


def test_single_object_shape_parses_correctly(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        jobs = source.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "dc-2026-001"
    assert job.title == "Data Analyst Intern"
    assert job.location == "Nantes, FR"
    assert job.contract_type == "internship"
    assert str(job.url) == "https://careers.example-datacorp.test/jobs/dc-2026-001"
    assert "Nantes" in job.description
    assert "<p>" not in job.description


def test_array_shape_parses_correctly_and_skips_non_job_postings(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/array"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("array.html")))
        jobs = {job.external_id: job for job in source.fetch()}

    assert len(jobs) == 2  # the Organization entry must be ignored, not error

    apprentice = jobs["dc-2026-002"]
    assert apprentice.contract_type == "apprenticeship"
    assert apprentice.location == "Ile-de-France, FR"

    other = jobs["dc-2026-003"]
    assert other.contract_type == "other"
    assert other.location == "Paris"
    # dc-2026-003 has no "url" field in the fixture -- must fall back to the page URL.
    assert str(other.url) == url


def test_graph_shape_parses_correctly(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/graph"
    source = _make_source(mock_client, url, company_name="Exemple SARL")
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("graph.html")))
        jobs = source.fetch()

    assert len(jobs) == 1  # the WebPage entry in @graph must be ignored
    job = jobs[0]
    assert job.external_id == "ex-004"  # extracted from the PropertyValue object
    assert job.title == "Stage Data Science"
    assert job.contract_type == "internship"  # bare "stage" + French context
    assert job.location == "Nantes, FR"
    assert str(job.url) == "https://exemple-sarl.example/jobs/ex-004"


def test_malformed_block_is_skipped_and_the_valid_block_still_parses(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://careers.example-datacorp.test/malformed"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("malformed.html")))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 1
    assert jobs[0].external_id == "ok-005"
    assert any("malformed JSON-LD block" in record.message for record in caplog.records)


def test_no_job_posting_raises_source_empty_error(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/none"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("none.html")))
        with pytest.raises(SourceEmptyError):
            source.fetch()


# --- identifier validation -------------------------------------------------


@pytest.mark.parametrize(
    "bad_identifier",
    [
        "not-a-url",
        "http://careers.example-datacorp.test/",  # http, not https
        "ftp://careers.example-datacorp.test/",
        "https://",  # scheme with no host
        "acme",  # a bare token, like other adapters take
    ],
)
def test_non_https_url_identifier_raises_value_error(
    mock_client: httpx.Client, bad_identifier: str
) -> None:
    with pytest.raises(ValueError):
        JsonLdSource(bad_identifier, "DataCorp Test", mock_client, user_agent=TEST_USER_AGENT)


# --- robots.txt --------------------------------------------------------


def test_robots_disallow_raises_source_error_and_page_is_never_requested(
    mock_client: httpx.Client,
) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        respx.get("https://careers.example-datacorp.test/robots.txt").mock(
            return_value=httpx.Response(200, text=DISALLOW_ALL_ROBOTS)
        )
        page_route = respx.get(url).mock(
            return_value=httpx.Response(200, text=_read_fixture("single.html"))
        )
        with pytest.raises(SourceError):
            source.fetch()

    assert page_route.call_count == 0


def test_robots_allow_proceeds_normally(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        jobs = source.fetch()

    assert len(jobs) == 1


def test_robots_fetch_failure_is_treated_as_allowed(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        respx.get("https://careers.example-datacorp.test/robots.txt").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        jobs = source.fetch()

    assert len(jobs) == 1


def test_robots_404_is_treated_as_allowed(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        respx.get("https://careers.example-datacorp.test/robots.txt").mock(
            return_value=httpx.Response(404)
        )
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        jobs = source.fetch()

    assert len(jobs) == 1


def test_robots_result_is_cached_per_host_for_the_life_of_the_instance(
    mock_client: httpx.Client,
) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        robots_route = respx.get("https://careers.example-datacorp.test/robots.txt").mock(
            return_value=httpx.Response(200, text=ALLOW_ALL_ROBOTS)
        )
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        source.fetch()
        source.fetch()  # second call must not refetch robots.txt

    assert robots_route.call_count == 1


# --- fetch_raw() / fetch() error handling --------------------------------


def test_fetch_raw_returns_a_two_tuple(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(200, text=_read_fixture("single.html")))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_retries_once_on_500_then_succeeds(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        route = respx.get(url)
        route.side_effect = [
            httpx.Response(500),
            httpx.Response(200, text=_read_fixture("single.html")),
        ]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        route = respx.get(url)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, text=_read_fixture("single.html")),
        ]
        jobs = source.fetch()

    assert len(jobs) == 1
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        route = respx.get(url).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/gone"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        route = respx.get(url).mock(return_value=httpx.Response(410))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/missing"
    source = _make_source(mock_client, url)
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    with respx.mock:
        _mock_robots_allow("careers.example-datacorp.test")
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):  # SourceNotFoundError is a SourceError
            source.fetch()


def test_user_agent_header_matches_what_was_injected(mock_client: httpx.Client) -> None:
    url = "https://careers.example-datacorp.test/single"
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = JsonLdSource(url, "DataCorp Test", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        robots_route = respx.get("https://careers.example-datacorp.test/robots.txt").mock(
            return_value=httpx.Response(200, text=ALLOW_ALL_ROBOTS)
        )
        page_route = respx.get(url).mock(
            return_value=httpx.Response(200, text=_read_fixture("single.html"))
        )
        source.fetch()

    assert page_route.calls.last.request.headers["User-Agent"] == custom_user_agent
    assert robots_route.calls.last.request.headers["User-Agent"] == custom_user_agent
