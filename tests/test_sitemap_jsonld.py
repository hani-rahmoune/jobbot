from __future__ import annotations

import httpx
import pytest
import respx
from conftest import FIXTURES_DIR, TEST_USER_AGENT

from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.sitemap_jsonld import (
    DEFAULT_PAGE_CAP,
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SLUG_VOCABULARY,
    SitemapJsonLdSource,
    _evenly_spread_sample,
)

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
    # slug_vocabulary=[] here and below: these tests are about the fetch+
    # parse pipeline handling all 4 real fixture shapes, not about M11's
    # selection layers (which have their own dedicated tests) -- an empty
    # override disables vocabulary-narrowing so every candidate falls
    # through to sampling (M11 A2) and all 4 get fetched, as these tests
    # need to see the full varied set (valid, apprenticeship, no-JobPosting,
    # malformed) in one fetch() call.
    source = _make_source(mock_client, slug_vocabulary=[])
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


def test_returns_an_empty_list_rather_than_raising_when_sampling_finds_nothing(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure. Neither
    # search_terms nor the slug vocabulary matches these slugs, so this
    # exercises the full fall-through to sampling (M11 A2) -- both
    # candidates are genuinely fetched (not short-circuited), and simply
    # carry no JobPosting JSON-LD at all.
    source = _make_source(mock_client, search_terms=["a-term-that-matches-nothing"])
    sitemap = (
        "<urlset>"
        "<url><loc>https://careers.thalesgroup.com/fr/fr/job/1111/reseau-ingenieur</loc></url>"
        "<url><loc>https://careers.thalesgroup.com/fr/fr/job/2222/comptable-senior</loc></url>"
        "</urlset>"
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=sitemap))
        route_1 = respx.get(f"{BASE_URL}/job/1111/reseau-ingenieur").mock(
            return_value=httpx.Response(200, text="<html>no ld+json here</html>")
        )
        route_2 = respx.get(f"{BASE_URL}/job/2222/comptable-senior").mock(
            return_value=httpx.Response(200, text="<html>no ld+json here either</html>")
        )
        jobs = source.fetch()

    assert jobs == []
    assert route_1.call_count == 1  # genuinely fetched, not skipped
    assert route_2.call_count == 1


def test_an_unreachable_job_page_is_skipped_with_a_warning_not_a_crash(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, slug_vocabulary=[])  # see the comment above, same reason
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
    source = _make_source(mock_client, slug_vocabulary=[])  # see the comment above, same reason
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
    source = _make_source(mock_client, slug_vocabulary=[])  # see the comment above, same reason
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


def test_page_cap_is_configurable_per_instance_and_logged(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, search_terms=["alternance"], page_cap=2)
    sitemap = "<urlset>" + "".join(
        f'<url><loc>{BASE_URL}/job/{i}/alternance-poste-{i}</loc></url>' for i in range(5)
    ) + "</urlset>"
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=sitemap))
        for i in range(5):
            respx.get(f"{BASE_URL}/job/{i}/alternance-poste-{i}").mock(
                return_value=httpx.Response(200, text="<html>no ld+json here</html>")
            )
        with caplog.at_level("WARNING"):
            source.fetch()

        fetched_job_pages = [c for c in respx.mock.calls if "/job/" in str(c.request.url)]
        assert len(fetched_job_pages) == 2  # capped, out of 5 real search_terms matches

    assert any(
        "hit the 2-page cap on the 'search_terms' path" in r.message for r in caplog.records
    )


def test_default_page_cap_is_a_sane_positive_bound() -> None:
    assert DEFAULT_PAGE_CAP > 0


def test_default_page_cap_comfortably_exceeds_the_confirmed_accor_candidate_count() -> None:
    # M11 A4: Accor alone had 86 real search_terms-matched candidates.
    assert DEFAULT_PAGE_CAP > 86


# --- slug_vocabulary fallback (M11 Part A) ---------------------------------


def test_no_slug_vocabulary_override_uses_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.slug_vocabulary == list(DEFAULT_SLUG_VOCABULARY)


def test_falls_back_to_slug_vocabulary_when_search_terms_matches_nothing(
    mock_client: httpx.Client,
) -> None:
    # The real Thales fixture's "Alternance-Assistant-Marketing-Digital"
    # slug matches DEFAULT_SLUG_VOCABULARY's "alternance" even though
    # search_terms itself was configured with something that matches none
    # of the fixture's slugs -- this is the exact fallback the bug needed.
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
        matching_route = respx.get(
            f"{BASE_URL}/job/IOS-2026-3001/Alternance-Assistant-Marketing-Digital"
        ).mock(return_value=httpx.Response(200, text=_read_fixture("job_page_apprenticeship.html")))
        jobs = source.fetch()

    assert matching_route.call_count == 1
    assert len(jobs) == 1
    assert jobs[0].external_id == "IOS-2026-3001"


def test_english_locale_slugs_are_recognized_by_the_default_vocabulary(
    mock_client: httpx.Client,
) -> None:
    # The real M10 bug: Geodis and Manitou's sitemaps use English-locale
    # slugs ("Intern", "Graduate", "Trainee") that the old search_terms-only
    # filter never matched. DEFAULT_SLUG_VOCABULARY must catch these.
    source = _make_source(mock_client)
    for slug, expected in [
        ("Supply-Chain-Intern-2026", True),
        ("Graduate-Program-Finance", True),
        ("Engineering-Trainee-Program", True),
        ("Apprentice-Electrician", True),
        ("Senior-Network-Engineer", False),
        ("Business-Analyst-Permanent", False),
    ]:
        url = f"{BASE_URL}/job/1234/{slug}"
        matched = any(term in url.lower() for term in source.slug_vocabulary)
        assert matched is expected, f"{slug!r} expected match={expected}, got {matched}"


def test_custom_slug_vocabulary_overrides_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, slug_vocabulary=["ausbildung"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap_index.xml"))
        )
        respx.get(f"{BASE_URL}/sitemap1.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap1.xml"))
        )
        # No sitemap2.xml route registered on purpose: with the override,
        # NEITHER real fixture slug matches "ausbildung", so this must fall
        # through to sampling (which fetches from the full candidate set,
        # including sitemap2's URLs) rather than matching via vocabulary.
        respx.get(f"{BASE_URL}/sitemap2.xml").mock(
            return_value=httpx.Response(200, text=_read_fixture("sitemap2.xml"))
        )
        for path in [
            "/job/R0313776/Ingenieur-Plateforme-DevOps",
            "/job/IOS-2026-3001/Alternance-Assistant-Marketing-Digital",
            "/job/R0000000/expired",
            "/job/R0999999/malformed",
        ]:
            respx.get(f"{BASE_URL}{path}").mock(
                return_value=httpx.Response(200, text=_read_fixture("job_page.html"))
            )
        jobs = source.fetch()

    # Sampling (all 4, since 4 <= DEFAULT_SAMPLE_SIZE) rather than the one
    # real "alternance" slug -- proves the override genuinely replaced the
    # default vocabulary rather than being merged with it.
    assert len(jobs) == 4


# --- evenly-spread sampling (M11 A2) ----------------------------------------


def test_evenly_spread_sample_returns_everything_when_under_the_sample_size() -> None:
    urls = ["a", "b", "c"]
    assert _evenly_spread_sample(urls, 40) == urls


def test_evenly_spread_sample_picks_across_the_whole_list_not_just_the_front() -> None:
    urls = [f"url{i}" for i in range(200)]
    sample = _evenly_spread_sample(urls, 20)

    assert len(sample) == 20
    assert len(set(sample)) == 20  # no duplicates
    indices = sorted(urls.index(u) for u in sample)
    # Spread across the whole 0-199 range, not clustered in the first 20.
    assert indices[0] < 10
    assert indices[-1] > 180


def test_default_sample_size_is_a_sane_positive_bound() -> None:
    assert DEFAULT_SAMPLE_SIZE > 0


def test_no_search_terms_and_no_vocabulary_match_samples_evenly_not_head_biased(
    mock_client: httpx.Client,
) -> None:
    # A candidate set bigger than the sample size, where nothing matches
    # search_terms or the vocabulary -- the ones actually fetched must be
    # spread across the set, not just the first `sample_size`.
    source = _make_source(mock_client, sample_size=5)
    urls = [f"{BASE_URL}/job/{i}/role-{i}" for i in range(50)]
    sitemap = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=sitemap))
        for u in urls:
            respx.get(u).mock(return_value=httpx.Response(200, text="<html></html>"))
        source.fetch()

        fetched_urls = {str(c.request.url) for c in respx.mock.calls if "/job/" in str(c.request.url)}

    assert len(fetched_urls) == 5
    fetched_indices = sorted(int(u.split("/job/")[1].split("/")[0]) for u in fetched_urls)
    assert fetched_indices[0] < 10
    assert fetched_indices[-1] > 39  # not clustered at the front of the 0-49 range


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
