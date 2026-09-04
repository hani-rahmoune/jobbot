"""M12 Part C: direct tests for the sitemap-traversal/narrowing logic shared
between sitemap_jsonld.py and successfactors.py. Every behavior here is also
exercised indirectly through test_sitemap_jsonld.py's 32 tests (which is what
actually caught any regression during the extraction), but this module is
now its own reusable unit with its own public API, so it gets its own direct
coverage too -- especially since successfactors.py depends on it working
correctly on its own, not just as sitemap_jsonld's implementation detail.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.sitemap_discovery import (
    DEFAULT_JOB_PATH_MARKERS,
    SitemapDiscovery,
    _dedupe_by_trailing_numeric_id,
    evenly_spread_sample,
    extract_locs,
    looks_like_a_job_url,
)

BASE_URL = "https://careers.example.com"
INDEX_URL = f"{BASE_URL}/sitemap_index.xml"


def _make_discovery(client: httpx.Client, **kwargs: object) -> SitemapDiscovery:
    return SitemapDiscovery(
        client, TEST_USER_AGENT, "Example Corp", source_name="successfactors", **kwargs
    )


# --- looks_like_a_job_url ---------------------------------------------------


def test_default_markers_recognize_common_job_path_shapes() -> None:
    for path in ["/job/123", "/jobs/123", "/offre/123", "/emploi/123"]:
        assert looks_like_a_job_url(f"{BASE_URL}{path}", list(DEFAULT_JOB_PATH_MARKERS)) is True


def test_a_purely_numeric_path_segment_is_recognized_even_with_no_keyword() -> None:
    assert looks_like_a_job_url(f"{BASE_URL}/posting/482913", list(DEFAULT_JOB_PATH_MARKERS)) is True


def test_a_non_job_url_is_not_recognized() -> None:
    assert looks_like_a_job_url(f"{BASE_URL}/about-us", list(DEFAULT_JOB_PATH_MARKERS)) is False


def test_custom_markers_override_the_default_list() -> None:
    assert looks_like_a_job_url(f"{BASE_URL}/annonce/x", ["/annonce/"]) is True
    assert looks_like_a_job_url(f"{BASE_URL}/job/x", ["/annonce/"]) is False


def test_a_blog_url_is_excluded_even_when_it_also_matches_a_job_marker() -> None:
    # M14 Part C: the exact Accor shape -- a marketing post whose slug
    # contains "apprenticeship" still lives under /blogs/ and must not be
    # treated as a job candidate. The deny-list applies by default, without
    # the caller having to opt in.
    url = f"{BASE_URL}/blogs/why-apprenticeship-matters"
    assert looks_like_a_job_url(url, list(DEFAULT_JOB_PATH_MARKERS)) is False


def test_the_deny_list_is_overridable_like_the_allow_list() -> None:
    url = f"{BASE_URL}/blogs/job/1"
    assert looks_like_a_job_url(url, ["/job/"], non_job_path_markers=[]) is True


# --- _dedupe_by_trailing_numeric_id (M15 Part B) -----------------------


def test_dedupe_keeps_the_first_occurrence_of_a_repeated_trailing_id() -> None:
    urls = [
        f"{BASE_URL}/en_US/careers/JobDetail/Some-Role/77566",
        f"{BASE_URL}/fr_FR/careers/JobDetail/Some-Role/77566",
        f"{BASE_URL}/de_DE/careers/JobDetail/Some-Role/77566",
    ]
    assert _dedupe_by_trailing_numeric_id(urls) == [urls[0]]


def test_dedupe_keeps_distinct_ids() -> None:
    urls = [
        f"{BASE_URL}/en_US/careers/JobDetail/Role-A/1001",
        f"{BASE_URL}/en_US/careers/JobDetail/Role-B/1002",
    ]
    assert _dedupe_by_trailing_numeric_id(urls) == urls


def test_dedupe_never_touches_a_url_with_no_trailing_numeric_id() -> None:
    # Thales' own ID shape: the numeric-looking segment comes BEFORE the
    # title, not at the very end, so it must never be treated as a
    # deduplication key at all.
    urls = [
        f"{BASE_URL}/job/R0313776/Ingenieur-Plateforme-DevOps",
        f"{BASE_URL}/job/R0313776/Ingenieur-Plateforme-DevOps",  # a literal exact repeat
    ]
    assert _dedupe_by_trailing_numeric_id(urls) == urls  # both kept -- neither ends in digits


def test_discover_job_urls_dedupes_the_same_posting_across_locale_mirrors(
    mock_client: httpx.Client,
) -> None:
    # The exact TotalEnergies shape: the same requisition ID listed once per
    # locale-prefixed sub-sitemap.
    discovery = _make_discovery(mock_client, search_terms=["alternance"])
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<sitemapindex>"
                    f"<sitemap><loc>{BASE_URL}/en_US/sitemap.xml</loc></sitemap>"
                    f"<sitemap><loc>{BASE_URL}/fr_FR/sitemap.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
            )
        )
        respx.get(f"{BASE_URL}/en_US/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/en_US/careers/JobDetail/Alternance-X/77566</loc></url>"
                    "</urlset>"
                ),
            )
        )
        respx.get(f"{BASE_URL}/fr_FR/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/fr_FR/careers/JobDetail/Alternance-X/77566</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [f"{BASE_URL}/en_US/careers/JobDetail/Alternance-X/77566"]


# --- evenly_spread_sample ----------------------------------------------------


def test_evenly_spread_sample_returns_everything_when_under_the_sample_size() -> None:
    urls = [f"{BASE_URL}/job/{i}" for i in range(5)]
    assert evenly_spread_sample(urls, 10) == urls


def test_evenly_spread_sample_spans_the_whole_list_not_just_the_front() -> None:
    urls = [f"{BASE_URL}/job/{i}" for i in range(100)]
    sampled = evenly_spread_sample(urls, 10)
    indices = [urls.index(u) for u in sampled]
    assert min(indices) < 20
    assert max(indices) > 80


# --- extract_locs -------------------------------------------------------


def test_extract_locs_resolves_a_relative_loc_against_the_base_url() -> None:
    xml = "<urlset><url><loc>/job/relative-1</loc></url></urlset>"
    assert extract_locs(xml, BASE_URL) == [f"{BASE_URL}/job/relative-1"]


def test_extract_locs_keeps_an_already_absolute_loc_unchanged() -> None:
    xml = "<urlset><url><loc>https://other.example.com/job/1</loc></url></urlset>"
    assert extract_locs(xml, BASE_URL) == ["https://other.example.com/job/1"]


def test_extract_locs_drops_a_loc_that_is_not_a_fetchable_url() -> None:
    xml = "<urlset><url><loc>mailto:someone@example.com</loc></url></urlset>"
    assert extract_locs(xml, BASE_URL) == []


# --- SitemapDiscovery.discover_job_urls -------------------------------------

# M14 Part D: this file used to define its own function-scoped mock_client
# fixture (`return httpx.Client()`), shadowing conftest.py's session-scoped
# one and re-paying httpx's ~0.3s default SSLContext/CA-bundle setup cost on
# every single test in this file instead of once for the whole session --
# see conftest.py's own mock_client docstring. Removed; every test below now
# resolves to conftest.py's shared fixture instead.


def test_discover_job_urls_traverses_index_then_leaf_sitemaps(mock_client: httpx.Client) -> None:
    discovery = _make_discovery(mock_client, slug_vocabulary=["alternance"])
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=f"<sitemapindex><sitemap><loc>{BASE_URL}/leaf.xml</loc></sitemap></sitemapindex>",
            )
        )
        respx.get(f"{BASE_URL}/leaf.xml").mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/alternance-data-analyst/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/senior-manager/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [f"{BASE_URL}/job/alternance-data-analyst/1"]


def test_discover_job_urls_falls_back_to_sampling_when_nothing_matches(
    mock_client: httpx.Client,
) -> None:
    discovery = _make_discovery(mock_client, slug_vocabulary=[])
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/warehouse-operator/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/night-shift-lead/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert len(urls) == 2  # neither slug matched anything -- sampling never rejects, see M11 A2


def test_a_blog_post_is_excluded_from_discovery_even_if_its_slug_matches(
    mock_client: httpx.Client,
) -> None:
    # The exact Accor bug: a /blogs/ marketing post whose title contains
    # "apprenticeship" must never reach the candidate set, regardless of
    # which narrowing layer would otherwise have matched it.
    discovery = _make_discovery(mock_client, slug_vocabulary=["apprenticeship"])
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/blogs/why-apprenticeship-matters</loc></url>"
                    f"<url><loc>{BASE_URL}/job/apprenticeship-role/1</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [f"{BASE_URL}/job/apprenticeship-role/1"]


def test_page_cap_truncates_and_logs(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    discovery = _make_discovery(mock_client, slug_vocabulary=["alternance"], page_cap=2)
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text="<urlset>"
                + "".join(
                    f"<url><loc>{BASE_URL}/job/alternance-role-{i}/{i}</loc></url>" for i in range(5)
                )
                + "</urlset>",
            )
        )
        with caplog.at_level("WARNING"):
            urls = discovery.discover_job_urls(INDEX_URL)

    assert len(urls) == 2
    assert any("hit the 2-page cap on the 'slug_vocabulary' path" in r.message for r in caplog.records)


# --- SitemapDiscovery location narrowing (M14 Part C) -----------------------


def test_location_narrowing_selects_french_slugs_and_skips_foreign_ones(
    mock_client: httpx.Client,
) -> None:
    discovery = _make_discovery(
        mock_client, search_terms=["alternance"], locations=["paris", "nantes"]
    )
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/alternance-paris/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/alternance-berlin/2</loc></url>"
                    f"<url><loc>{BASE_URL}/job/alternance-nantes/3</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [
        f"{BASE_URL}/job/alternance-paris/1",
        f"{BASE_URL}/job/alternance-nantes/3",
    ]


def test_zero_location_matches_falls_through_to_the_unrefined_set(
    mock_client: httpx.Client,
) -> None:
    # Some employers (Thales, confirmed live) never put a city in the slug at
    # all -- location narrowing must never turn "no slug-level location
    # signal" into "no postings", only skip the refinement.
    discovery = _make_discovery(
        mock_client, search_terms=["alternance"], locations=["paris", "nantes"]
    )
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/alternance-data-analyst/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/alternance-marketing/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert len(urls) == 2


def test_no_locations_configured_leaves_existing_behavior_unchanged(
    mock_client: httpx.Client,
) -> None:
    discovery = _make_discovery(mock_client, search_terms=["alternance"])
    assert discovery.locations == []
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/alternance-paris/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/alternance-berlin/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert len(urls) == 2


def test_location_narrowing_also_applies_to_the_slug_vocabulary_layer(
    mock_client: httpx.Client,
) -> None:
    discovery = _make_discovery(
        mock_client, slug_vocabulary=["intern"], locations=["paris"]
    )
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/intern-paris/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/intern-london/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [f"{BASE_URL}/job/intern-paris/1"]


def test_search_terms_take_priority_over_slug_vocabulary(mock_client: httpx.Client) -> None:
    discovery = _make_discovery(
        mock_client, search_terms=["stage"], slug_vocabulary=["alternance"]
    )
    with respx.mock:
        respx.get(INDEX_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset>"
                    f"<url><loc>{BASE_URL}/job/stage-marketing/1</loc></url>"
                    f"<url><loc>{BASE_URL}/job/alternance-vente/2</loc></url>"
                    "</urlset>"
                ),
            )
        )
        urls = discovery.discover_job_urls(INDEX_URL)

    assert urls == [f"{BASE_URL}/job/stage-marketing/1"]


# --- SitemapDiscovery.fetch_text ---------------------------------------


def test_fetch_text_retries_once_on_a_5xx_then_succeeds(mock_client: httpx.Client) -> None:
    discovery = _make_discovery(mock_client)
    url = f"{BASE_URL}/job/1"
    with respx.mock:
        route = respx.get(url)
        route.side_effect = [httpx.Response(503), httpx.Response(200, text="ok")]
        assert discovery.fetch_text(url) == "ok"
        assert route.call_count == 2


def test_fetch_text_raises_source_error_after_exhausting_retries(mock_client: httpx.Client) -> None:
    discovery = _make_discovery(mock_client)
    url = f"{BASE_URL}/job/1"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(503))
        with pytest.raises(SourceError):
            discovery.fetch_text(url)


def test_fetch_text_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    discovery = _make_discovery(mock_client)
    url = f"{BASE_URL}/job/1"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            discovery.fetch_text(url)
