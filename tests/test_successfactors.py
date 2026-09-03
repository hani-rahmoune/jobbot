"""M12 Part C: SAP SuccessFactors Recruiting Marketing (Jobs2Web) adapter
tests, mirroring test_lever.py's structure (parse() first, then fetch_raw()/
fetch() against respx-mocked responses, then the fetch-contract edge cases
every adapter in this codebase shares -- retry-once-on-5xx, timeout retry,
404, robots.txt, User-Agent). The lenient-extraction behavior (Part C scope
change 1) and the redirect-following assertion (Part A) are this adapter's
own additions on top of that shared shape.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from conftest import FIXTURES_DIR, TEST_USER_AGENT

from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.successfactors import SuccessFactorsSource, _city_from_slug, _extract_itemprops

SF_FIXTURES_DIR = FIXTURES_DIR / "successfactors"
BASE_URL = "https://jobs.example.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"


def _read_fixture(name: str) -> str:
    return (SF_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_source(
    client: httpx.Client, identifier: str = SITEMAP_URL, company_name: str = "Example Corp", **kwargs: object
) -> SuccessFactorsSource:
    return SuccessFactorsSource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _mock_robots_allowed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE_URL}/robots.txt").mock(return_value=httpx.Response(404))


# --- __init__ / identifier validation ---------------------------------------


def test_invalid_identifier_raises_value_error(mock_client: httpx.Client) -> None:
    for bad in ["not-a-url", "http://jobs.example.com/sitemap.xml", ""]:
        with pytest.raises(ValueError):
            SuccessFactorsSource(bad, "Example Corp", mock_client, user_agent=TEST_USER_AGENT)


# --- lenient itemprop extraction (Part C scope change 1) -------------------


def test_valid_microdata_item_is_extracted() -> None:
    itemprops = _extract_itemprops(_read_fixture("job_page_microdata.html"))
    assert itemprops["title"] == ["Alternance Data Analyst - Direction Data F/H"]
    assert itemprops["streetaddress"] == ["Paris, FR"]


def test_description_nested_inside_the_same_tag_name_is_not_truncated() -> None:
    """The real bug this exists to prevent: Eramet's own description span
    nests further <span style=...> tags for inline formatting -- a naive
    "stop at the first </span>" regex would cut the content after
    "Contexte : " and lose everything else."""
    itemprops = _extract_itemprops(_read_fixture("job_page_microdata.html"))
    (description,) = itemprops["description"]
    assert "Direction Data" in description
    assert "12 mois" in description


def test_lenient_itemprop_with_no_enclosing_itemscope_is_still_extracted() -> None:
    """Worldline's real shape: itemprop="title"/"description" with no
    itemscope/itemtype wrapper at all -- must not require one."""
    itemprops = _extract_itemprops(_read_fixture("job_page_lenient.html"))
    assert itemprops["title"] == ["Alternance - Data Analyst F/H"]
    assert len(itemprops["description"]) == 3  # three separate sections, all real content
    assert "streetaddress" not in itemprops


def test_multiple_description_spans_all_survive_extraction() -> None:
    itemprops = _extract_itemprops(_read_fixture("job_page_lenient.html"))
    joined = " ".join(itemprops["description"])
    assert "Who we are" in joined
    assert "The opportunity" in joined
    assert "Paris, France" in joined


# --- city-from-slug fallback -------------------------------------------


def test_city_from_slug_takes_the_leading_hyphen_segment() -> None:
    assert _city_from_slug(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/") == "Paris"


def test_city_from_slug_url_decodes_before_splitting() -> None:
    url = f"{BASE_URL}/job/PUTEAUX-La-D%C3%A9fense-Apprenti-Buyer/80435/"
    assert _city_from_slug(url) == "PUTEAUX"


def test_city_from_slug_returns_empty_string_for_an_unrecognizable_shape() -> None:
    assert _city_from_slug(f"{BASE_URL}/") == ""


# --- parse() -------------------------------------------------------


def test_parse_maps_the_microdata_fixture_correctly(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    raw = [{"url": f"{BASE_URL}/job/x/1", "html": _read_fixture("job_page_microdata.html")}]
    (job,) = source.parse(raw)

    assert job.title == "Alternance Data Analyst - Direction Data F/H"
    assert job.location == "Paris, FR"  # from the streetAddress itemprop
    assert job.contract_type == "apprenticeship"
    assert str(job.url) == f"{BASE_URL}/job/x/1"
    assert job.source == "successfactors"
    assert "Direction Data" in job.description


def test_parse_falls_back_to_the_url_slug_for_location_when_no_streetaddress(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    raw = [
        {
            "url": f"{BASE_URL}/job/Paris-Alternance-Data-Analyst/1000000001/",
            "html": _read_fixture("job_page_lenient.html"),
        }
    ]
    (job,) = source.parse(raw)
    assert job.location == "Paris"  # no streetaddress itemprop on this fixture


def test_parse_falls_back_to_the_title_tag_when_no_title_itemprop(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    raw = [{"url": f"{BASE_URL}/job/x/1", "html": _read_fixture("job_page_title_tag_fallback.html")}]
    (job,) = source.parse(raw)

    assert job.title == "Stage Marketing Digital H/F"  # "Job Details | Example Corp" stripped
    assert job.contract_type == "internship"


def test_parse_skips_an_entry_with_no_title_anywhere(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    raw = [{"url": f"{BASE_URL}/job/x/1", "html": _read_fixture("job_page_no_title.html")}]
    with caplog.at_level("WARNING"):
        jobs = source.parse(raw)

    assert jobs == []  # never invent a title
    assert any("no title found" in r.message for r in caplog.records)


def test_parse_multiple_entries_only_the_untitled_one_is_dropped(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    raw = [
        {"url": f"{BASE_URL}/job/1", "html": _read_fixture("job_page_microdata.html")},
        {"url": f"{BASE_URL}/job/2", "html": _read_fixture("job_page_no_title.html")},
        {"url": f"{BASE_URL}/job/3", "html": _read_fixture("job_page_lenient.html")},
    ]
    jobs = source.parse(raw)
    assert len(jobs) == 2
    assert {str(j.url) for j in jobs} == {f"{BASE_URL}/job/1", f"{BASE_URL}/job/3"}


# --- fetch_raw() / fetch() ----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, slug_vocabulary=["alternance", "stage"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=_read_fixture("sitemap.xml")))
        respx.get(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_microdata.html"))
        )
        respx.get(f"{BASE_URL}/job/Nantes-Stage-Marketing-Digital/1000000003/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_lenient.html"))
        )
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_only_reaches_slug_matched_urls_not_the_whole_sitemap(mock_client: httpx.Client) -> None:
    """The non-alternance/stage entry (Lyon-Senior-Software-Engineer) must
    never be fetched at all -- confirms this adapter really does reuse M11
    Part A's narrowing rather than fetching every sitemap URL."""
    source = _make_source(mock_client, slug_vocabulary=["alternance", "stage"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=_read_fixture("sitemap.xml")))
        respx.get(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_microdata.html"))
        )
        respx.get(f"{BASE_URL}/job/Nantes-Stage-Marketing-Digital/1000000003/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_lenient.html"))
        )
        senior_route = respx.get(f"{BASE_URL}/job/Lyon-Senior-Software-Engineer/1000000002/")
        jobs = source.fetch()

    assert senior_route.call_count == 0
    assert len(jobs) == 2


def test_fetch_returns_an_empty_list_rather_than_raising_when_the_board_has_no_job_urls_at_all(
    mock_client: httpx.Client,
) -> None:
    # A sitemap with nothing shaped like a job URL: no per-page fetch is
    # even attempted (discover_job_urls' own job-URL-shape filter already
    # returns empty), so this needs no job-page mocks at all.
    source = _make_source(mock_client)
    non_job_sitemap = (
        "<urlset>"
        "<url><loc>https://jobs.example.com/about-us</loc></url>"
        "<url><loc>https://jobs.example.com/privacy-policy</loc></url>"
        "</urlset>"
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=non_job_sitemap))
        jobs = source.fetch()

    assert jobs == []


def test_an_unreachable_job_page_is_skipped_with_a_warning_not_a_crash(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, slug_vocabulary=["alternance", "stage"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=_read_fixture("sitemap.xml")))
        respx.get(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_microdata.html"))
        )
        respx.get(f"{BASE_URL}/job/Nantes-Stage-Marketing-Digital/1000000003/").mock(
            return_value=httpx.Response(500)
        )
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 1  # the reachable one still comes through
    assert any("skipping unreachable job page" in r.message for r in caplog.records)


def test_robots_disallow_raises_source_error_and_nothing_else_is_fetched(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(f"{BASE_URL}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
        sitemap_route = respx.get(SITEMAP_URL)
        with pytest.raises(SourceError):
            source.fetch_raw()

    assert sitemap_route.call_count == 0


def test_fetch_raises_source_error_after_exhausting_retries_on_the_sitemap(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(SITEMAP_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()


def test_user_agent_header_matches_what_was_injected(mock_client: httpx.Client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = SuccessFactorsSource(
        SITEMAP_URL, "Example Corp", mock_client, user_agent=custom_user_agent,
        slug_vocabulary=["alternance"],
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=_read_fixture("sitemap.xml")))
        route = respx.get(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_microdata.html"))
        )
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent


# --- redirect-following (M12 Part A, asserted here rather than assumed) ----


def test_a_job_url_that_redirects_to_a_locale_suffixed_path_still_yields_its_posting() -> None:
    """This vendor's own job URLs commonly 302 to a locale-suffixed path
    (confirmed live on Nexans and Worldline). The shared `mock_client`
    fixture does NOT set follow_redirects (it's a generic test convenience,
    not a stand-in for run.py's real client), so this test builds its own
    client the same way run.py actually builds its fetch-side one --
    follow_redirects=True -- to exercise the real, project-wide fix from
    M12 Part A rather than assuming it holds."""
    client = httpx.Client(verify=False, follow_redirects=True)
    source = _make_source(client, slug_vocabulary=["alternance"])
    redirect_target = f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001-fr_FR/"
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=_read_fixture("sitemap.xml")))
        respx.get(f"{BASE_URL}/job/Paris-Alternance-Data-Analyst-FH/1000000001/").mock(
            return_value=httpx.Response(302, headers={"Location": redirect_target})
        )
        respx.get(redirect_target).mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_microdata.html"))
        )
        jobs = source.fetch()

    assert len(jobs) == 1
    assert jobs[0].title == "Alternance Data Analyst - Direction Data F/H"
