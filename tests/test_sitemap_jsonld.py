from __future__ import annotations

import httpx
import pytest
import respx
from conftest import FIXTURES_DIR, TEST_USER_AGENT

import jobbot.sources.sitemap_jsonld as sitemap_jsonld_module
from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.sitemap_jsonld import MAX_JOB_PAGES, SitemapJsonLdSource

SJ_FIXTURES_DIR = FIXTURES_DIR / "sitemap_jsonld"
BASE_URL = "https://careers.thalesgroup.com/fr/fr"
INDEX_URL = f"{BASE_URL}/sitemap_index.xml"


def _read_fixture(name: str) -> str:
    return (SJ_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_source(
    client: httpx.Client, identifier: str = INDEX_URL, company_name: str = "Thales", **kwargs: object
) -> SitemapJsonLdSource:
    return SitemapJsonLdSource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _mock_robots_allowed(respx_mock: respx.MockRouter, base: str = "https://careers.thalesgroup.com") -> None:
    respx_mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))


def _mock_full_sitemap_tree(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(INDEX_URL).mock(
        return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
    )
    respx_mock.get(f"{BASE_URL}/sitemap1.xml").mock(
        return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
    )
    respx_mock.get(f"{BASE_URL}/sitemap2.xml").mock(
        return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
    )
    respx_mock.get(f"{BASE_URL}/job/R0313776/Ingenieur-Plateforme-DevOps").mock(
        return_value=httpx.Response(200, text=_read_fixture("job_page.html"))
    )
    respx_mock.get(f"{BASE_URL}/job/IOS-2026-3001/Alternance-Assistant-Marketing-Digital").mock(
        return_value=httpx.Response(200, text=_read_fixture("job_page_apprenticeship.html"))
    )
    respx_mock.get(f"{BASE_URL}/job/R0000000/expired").mock(
        return_value=httpx.Response(200, text=_read_fixture("job_page_no_jobposting.html"))
    )
    respx_mock.get(f"{BASE_URL}/job/R0999999/malformed").mock(
        return_value=httpx.Response(200, text=_read_fixture("job_page_malformed.html"))
    )


@pytest.fixture
def source(mock_client: httpx.Client) -> SitemapJsonLdSource:
    return _make_source(mock_client)


# --- identifier validation --------------------------------------------


@pytest.mark.parametrize("bad_identifier", ["not-a-url", "http://careers.thalesgroup.com/sitemap.xml", ""])
def test_invalid_identifier_raises_value_error(mock_client: httpx.Client, bad_identifier: str) -> None:
    with pytest.raises(ValueError):
        _make_source(mock_client, identifier=bad_identifier)


# --- robots.txt ----------------------------------------------------------


def test_robots_disallow_raises_source_error_and_nothing_else_is_fetched(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get("https://careers.thalesgroup.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /fr/")
        )
        index_route = respx.get(INDEX_URL)
        with pytest.raises(SourceError):
            source.fetch()

    assert index_route.call_count == 0


# --- fetch_raw() / full discovery pipeline --------------------------------


def test_fetch_raw_returns_a_two_tuple(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_discovers_through_the_index_and_both_leaf_sitemaps(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        jobs = source.fetch()

    # 4 job URLs across both leaf sitemaps; one has no JobPosting block and
    # one is malformed (missing title) -- both must be skipped, not crash.
    assert len(jobs) == 2


def test_a_relative_loc_entry_is_resolved_rather_than_crashing(mock_client: httpx.Client) -> None:
    # Real-world sitemaps sometimes carry a relative <loc> even though the
    # protocol requires absolute URLs -- confirmed live via
    # discovery/seeds/Batch1.txt, where a relative Sitemap: robots.txt
    # declaration crashed deep inside urllib before this fix.
    source = _make_source(mock_client)
    sitemap_with_relative_loc = '<urlset><url><loc>/job/relative-posting</loc></url></urlset>'
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=sitemap_with_relative_loc))
        # A leading "/" resolves against the domain root, not the sitemap's
        # own directory (standard urljoin/href semantics) -- confirming
        # that's really what happens is the point of this test.
        job_route = respx.get("https://careers.thalesgroup.com/job/relative-posting").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page.html"))
        )
        jobs = source.fetch()

    assert job_route.call_count == 1
    assert len(jobs) == 1


def test_non_job_urls_in_the_sitemap_are_never_fetched(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        homepage_route = respx.get(BASE_URL)
        events_route = respx.get(f"{BASE_URL}/events")
        source.fetch()

    assert homepage_route.call_count == 0
    assert events_route.call_count == 0


def test_returns_an_empty_list_rather_than_raising_when_no_job_urls_match(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure.
    source = _make_source(mock_client, search_terms=["a-term-that-matches-nothing"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap1.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap2.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
        )
        jobs = source.fetch()

    assert jobs == []


def test_an_unreachable_job_page_is_skipped_with_a_warning_not_a_crash(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap1.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap2.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
        )
        respx.get(f"{BASE_URL}/job/R0313776/Ingenieur-Plateforme-DevOps").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page.html"))
        )
        respx.get(f"{BASE_URL}/job/IOS-2026-3001/Alternance-Assistant-Marketing-Digital").mock(
            return_value=httpx.Response(200, text=_read_fixture("job_page_apprenticeship.html"))
        )
        respx.get(f"{BASE_URL}/job/R0000000/expired").mock(return_value=httpx.Response(500))
        respx.get(f"{BASE_URL}/job/R0999999/malformed").mock(return_value=httpx.Response(500))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2  # the two reachable, valid postings still come through
    assert any("skipping unreachable job page" in r.message for r in caplog.records)


def test_user_agent_header_matches_what_was_injected(mock_client: httpx.Client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = SitemapJsonLdSource(INDEX_URL, "Thales", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        source.fetch()

    for call in respx.mock.calls:
        assert call.request.headers["User-Agent"] == custom_user_agent


def test_fetch_raises_source_error_after_exhausting_retries_on_the_index(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(INDEX_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()


# --- parse() / real-fixture field mapping ---------------------------------


def test_parse_maps_the_real_thales_fixture_correctly(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        jobs = {job.external_id: job for job in source.fetch()}

    other = jobs["R0313776"]
    assert other.title == "Ingénieur Plateforme DevOps"
    assert other.location == "Rennes, Ille-et-Vilaine, France"
    assert other.contract_type == "other"
    # No top-level "url" on the JobPosting itself (only hiringOrganization.url,
    # which is the company's own generic page, never the posting's) -- falls
    # back to the page URL the posting was actually fetched from.
    assert str(other.url) == f"{BASE_URL}/job/R0313776/Ingenieur-Plateforme-DevOps"
    assert other.posted_at is not None and other.posted_at.date().isoformat() == "2026-07-28"
    # description had vendor-side double-escaped HTML ("&lt;p&gt;") -- must
    # come through as clean text, not literal "<p>" tags.
    assert "<" not in other.description
    assert "Construisons ensemble un avenir de confiance" in other.description

    apprentice = jobs["IOS-2026-3001"]
    assert apprentice.contract_type == "apprenticeship"  # title + INTERN-style hint... actually via title
    assert apprentice.location == "Chatillon, Ile-de-France, France"
    assert "<" not in apprentice.description


def test_malformed_entry_missing_title_is_skipped_with_a_logged_warning(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 2  # 4 candidate job URLs: 2 valid, 1 no-JobPosting, 1 malformed
    assert any("skipping malformed entry" in r.message for r in caplog.records)


# --- search_terms as a sitemap-slug pre-filter (M9) ------------------------


def test_no_search_terms_configured_is_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.search_terms == []


def test_search_terms_filter_candidate_urls_before_any_job_page_is_fetched(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap1.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap2.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
        )
        # Only the matching URL's route is registered -- if the adapter
        # fetched anything else, respx would raise for the unmocked call.
        matching_route = respx.get(
            f"{BASE_URL}/job/IOS-2026-3001/Alternance-Assistant-Marketing-Digital"
        ).mock(return_value=httpx.Response(200, text=_read_fixture("job_page_apprenticeship.html")))
        jobs = source.fetch()

    assert matching_route.call_count == 1
    assert len(jobs) == 1
    assert jobs[0].external_id == "IOS-2026-3001"


def test_hits_the_job_page_cap_when_no_search_terms_narrow_a_large_candidate_set(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sitemap_jsonld_module, "MAX_JOB_PAGES", 2)
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        _mock_full_sitemap_tree(respx.mock)
        with caplog.at_level("WARNING"):
            source.fetch()

        fetched_job_pages = [c for c in respx.mock.calls if "/job/" in str(c.request.url)]
        assert len(fetched_job_pages) == 2  # capped, out of 4 real candidates
    assert any("no search_terms configured" in r.message for r in caplog.records)


def test_max_job_pages_constant_is_a_sane_positive_bound() -> None:
    assert MAX_JOB_PAGES > 0


# --- job_path_markers (M9d) -------------------------------------------


def test_default_job_path_markers_recognize_french_url_shapes(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    for path in ["/offre/123", "/offres/123", "/emploi/123", "/poste/123", "/vacancy/123"]:
        assert source._looks_like_a_job_url(f"{BASE_URL}{path}") is True
    assert source._looks_like_a_job_url(f"{BASE_URL}/events") is False


def test_a_purely_numeric_path_segment_is_recognized_even_with_no_keyword(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    assert source._looks_like_a_job_url(f"{BASE_URL}/posting/482913") is True
    assert source._looks_like_a_job_url(f"{BASE_URL}/posting/ab") is False


def test_custom_job_path_markers_override_the_default_list(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, job_path_markers=["/annonce/"])
    assert source._looks_like_a_job_url(f"{BASE_URL}/annonce/analyste") is True
    assert source._looks_like_a_job_url(f"{BASE_URL}/job/analyste") is False  # not in the override


def test_custom_job_path_markers_are_used_to_filter_the_real_sitemap(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, job_path_markers=["/does-not-exist-in-fixture/"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap1.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap2.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
        )
        jobs = source.fetch()

    assert jobs == []  # every real /job/ URL is invisible under this override
