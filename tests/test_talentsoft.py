from __future__ import annotations

import httpx
import pytest
import respx
from conftest import FIXTURES_DIR, TEST_USER_AGENT

import jobbot.sources.talentsoft as talentsoft_module
from jobbot.sources.base import SourceError, SourceNotFoundError
from jobbot.sources.talentsoft import MAX_PAGES_PER_SEARCH_TERM, TalentsoftSource

TALENTSOFT_FIXTURES_DIR = FIXTURES_DIR / "talentsoft"
BASE_URL = "https://casa-cacib-recrute.talent-soft.com"
LIST_URL = f"{BASE_URL}/job/list-of-all-jobs.aspx"


def _read_fixture(name: str) -> str:
    return (TALENTSOFT_FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_source(
    client: httpx.Client, identifier: str = BASE_URL, company_name: str = "Credit Agricole CIB", **kwargs: object
) -> TalentsoftSource:
    return TalentsoftSource(identifier, company_name, client, user_agent=TEST_USER_AGENT, **kwargs)


def _mock_robots_allowed(respx_mock: respx.MockRouter, base_url: str = BASE_URL) -> None:
    respx_mock.get(f"{base_url}/robots.txt").mock(return_value=httpx.Response(404))


def _empty_page_html(total: int = 0) -> str:
    return (
        '<div class="ts-ol-pagination__title resultat">'
        f'<span id="x" class="gras">{total} offre(s)</span></div>'
        '<div class="ts-related-offers LayerContainer ListeOffre"></div>'
    )


def _page_html(total: int, n_cards: int, start: int = 0) -> str:
    """A page carrying a real "N offre(s)" total plus `n_cards` synthetic
    ts-offer-card blocks -- used to test pagination math, where the page
    size must be measurable from the cards actually present (see
    TalentsoftSource._fetch_all_pages: real page size varies by tenant
    template, so it's derived from page 1's own count, never assumed)."""
    cards = "".join(
        f'<div class="ts-offer-card Layer"><h3 class="ts-offer-card__title">'
        f'<a class="ts-offer-card__title-link" href="/offre_{i}.aspx" title="{i}">Role {i}</a></h3>'
        '<div class="ts-offer-card-content offerContent"><ul class="ts-offer-card-content__list">'
        "<li>CDI</li></ul></div></div>"
        for i in range(start, start + n_cards)
    )
    return (
        '<div class="ts-ol-pagination__title resultat">'
        f'<span id="x" class="gras">{total} offre(s)</span></div>'
        f'<div class="ts-related-offers LayerContainer ListeOffre">{cards}</div>'
    )


@pytest.fixture
def talentsoft_source(mock_client: httpx.Client) -> TalentsoftSource:
    return _make_source(mock_client)


# --- identifier validation --------------------------------------------


@pytest.mark.parametrize("bad_identifier", ["not-a-url", "http://casa-cacib-recrute.talent-soft.com", ""])
def test_invalid_identifier_raises_value_error(mock_client: httpx.Client, bad_identifier: str) -> None:
    with pytest.raises(ValueError):
        _make_source(mock_client, identifier=bad_identifier)


# --- robots.txt ----------------------------------------------------------


def test_robots_disallow_raises_source_error_and_page_is_never_requested(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(f"{BASE_URL}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /job/")
        )
        list_route = respx.get(LIST_URL)
        with pytest.raises(SourceError):
            source.fetch()

    assert list_route.call_count == 0


# --- fetch_raw() / fetch() -----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("list_page.html"))
        )
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_returns_an_empty_list_rather_than_raising_on_zero_results(
    mock_client: httpx.Client,
) -> None:
    # M8b: zero results is no longer automatically a failure -- see
    # jobbot/run.py's process_source() for where that decision now lives.
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("empty_results.html"))
        )
        jobs = source.fetch()

    assert jobs == []


def test_fetch_retries_once_on_500_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL)
        route.side_effect = [
            httpx.Response(500),
            httpx.Response(200, text=_read_fixture("empty_results.html")),
        ]
        jobs = source.fetch()

    assert jobs == []
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, text=_read_fixture("empty_results.html")),
        ]
        jobs = source.fetch()

    assert jobs == []
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_does_not_retry_on_a_non_404_4xx(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1


def test_user_agent_header_matches_what_was_injected(mock_client: httpx.Client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = TalentsoftSource(BASE_URL, "Credit Agricole CIB", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("empty_results.html"))
        )
        source.fetch()

    assert route.calls.last.request.headers["User-Agent"] == custom_user_agent


def test_lcid_french_is_always_sent(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("empty_results.html"))
        )
        source.fetch()

    assert dict(route.calls.last.request.url.params)["LCID"] == "1036"


# --- parse() / extraction, via the real fixture ---------------------------


def test_parse_maps_every_real_offer_card_correctly(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("list_page.html"))
        )
        jobs = {job.external_id: job for job in source.fetch()}

    other = jobs["111787"]
    assert other.title == "Business Analyst H/F"
    assert other.contract_type == "other"
    assert other.location == "France, Montrouge"
    assert str(other.url) == (
        "https://casa-cacib-recrute.talent-soft.com/offre-de-emploi/"
        "emploi-business-analyst-h-f_111787.aspx"
    )

    apprentice = jobs["110456"]
    assert apprentice.title == "Gestion de projets - Direction Financière H/F"  # entity decoded
    assert apprentice.contract_type == "apprenticeship"  # via "Alternance / Apprentissage" badge

    intern = jobs["115202"]
    assert intern.contract_type == "internship"  # via title "Intern"

    single_badge = jobs["109716"]
    assert single_badge.location == ""  # only one badge (contract type), no location badges

    assert "109999" not in jobs  # the blank-title card must be skipped, not stored


def test_extract_offer_cards_handles_a_top_offer_s_nested_icon_markup() -> None:
    # M16 Part B: live-confirmed on BRGM's own tenant -- a "top offer"
    # posting wraps a decorative, empty icon <div> INSIDE the title link,
    # before the actual title text. The old text-only title pattern
    # ([^<]+?) couldn't match past that nested tag at all, silently
    # dropping every "top offer" posting card.
    html_snippet = """
        <a class="ts-offer-list-item__title-link "
           href="/offre-de-emploi/emploi-technicien-superieur_4094.aspx"
           title="Technicien superieur mesures physiques F/H (Ref. : 2026-4094)">
            <div class="ts-offer-list-item__top-offer-picto topOfferPicto">
                <div class="square"></div>
            </div>
            Technicien sup&#233;rieur mesures physiques F/H
        </a>
        <ul class="ts-offer-list-item__badges">
            <li>CDI</li><li>FREMING-MERLEBACH</li>
        </ul>
    """
    cards = talentsoft_module._extract_offer_cards(html_snippet, "https://example.talent-soft.com")
    assert len(cards) == 1
    assert cards[0]["title"] == "Technicien supérieur mesures physiques F/H"
    assert cards[0]["badges"] == ["CDI", "FREMING-MERLEBACH"]


def test_malformed_entry_is_skipped_with_a_logged_warning(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_read_fixture("list_page.html"))
        )
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 4  # 5 real offer cards minus the one with a blank title
    assert any("skipping malformed entry" in record.message for record in caplog.records)


# --- pagination, driven by the real total-count text ----------------------


def test_fetch_requests_exactly_ceil_total_over_measured_page_size_pages(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    # Page 1 carries 25 real cards and a "250 offre(s)" total -> page size is
    # measured as 25 (not assumed), so ceil(250/25) == 10 pages.
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_page_html(total=250, n_cards=25))
        )
        source.fetch()

    assert route.call_count == 10
    pages_requested = sorted(int(dict(c.request.url.params)["page"]) for c in route.calls)
    assert pages_requested == list(range(1, 11))


def test_fetch_does_not_paginate_past_the_page_cap_even_with_a_higher_total(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A lower page cap for this test only -- proves the same cap-enforcement
    # behavior as the real MAX_PAGES=40 without paying for 40 mocked round
    # trips just to exercise "the cap was hit" (see test_workday.py's
    # equivalent test for the same convention).
    monkeypatch.setattr(talentsoft_module, "MAX_PAGES", 3)
    source = _make_source(mock_client)
    # 10 cards/page, 5000 total would need 500 pages -- far more than the cap.
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_page_html(total=5000, n_cards=10))
        )
        with caplog.at_level("WARNING"):
            source.fetch()

    assert route.call_count == 3
    assert any("page cap" in record.message for record in caplog.records)


def test_fetch_returns_page_one_only_when_it_has_zero_cards_despite_a_nonzero_total(
    mock_client: httpx.Client,
) -> None:
    # Can't measure a page size from zero cards -- rather than divide by
    # zero or guess, treat page 1 (empty) as the whole result.
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_empty_page_html(total=250))
        )
        jobs = source.fetch()

    assert route.call_count == 1
    assert jobs == []


def test_fetch_falls_back_to_page_one_only_when_total_count_cannot_be_found(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client)
    page_html = (
        '<div class="ts-related-offers LayerContainer ListeOffre">'
        '<div class="ts-offer-card Layer"><h3 class="ts-offer-card__title">'
        '<a class="ts-offer-card__title-link" href="/offre_1.aspx" title="1">Role One</a></h3>'
        '<div class="ts-offer-card-content offerContent"><ul class="ts-offer-card-content__list">'
        "<li>CDI</li></ul></div></div></div>"
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, text=page_html))
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert route.call_count == 1  # no total found -- do not risk wrapping around
    assert len(jobs) == 1
    assert any("total-results count" in record.message for record in caplog.records)


# --- search_terms (M9) -----------------------------------------------------


def test_no_search_terms_configured_is_the_default(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client)
    assert source.search_terms == []


def test_search_terms_sends_one_query_per_term_with_matching_keywords_param(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL)
        route.side_effect = [
            httpx.Response(200, text=_empty_page_html(0)),
            httpx.Response(200, text=_empty_page_html(0)),
        ]
        source.fetch()

    assert route.call_count == 2
    sent_keywords = [dict(c.request.url.params)["Keywords"] for c in route.calls]
    assert sent_keywords == ["alternance", "stage"]


def test_search_terms_deduplicates_postings_across_terms_by_id(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, search_terms=["alternance", "stage"])
    card_html = (
        '<div class="ts-ol-pagination__title resultat"><span id="x" class="gras">1 offre(s)</span></div>'
        '<div class="ts-related-offers LayerContainer ListeOffre">'
        '<div class="ts-offer-card Layer"><h3 class="ts-offer-card__title">'
        '<a class="ts-offer-card__title-link" href="/offre_42.aspx" title="42">Same Role</a></h3>'
        '<div class="ts-offer-card-content offerContent"><ul class="ts-offer-card-content__list">'
        "<li>CDI</li></ul></div></div></div>"
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, text=card_html))
        jobs = source.fetch()

    assert route.call_count == 2  # both terms queried
    assert len(jobs) == 1  # same posting (id "42") found by both terms, deduped


def test_search_terms_use_their_own_smaller_page_cap(
    mock_client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, search_terms=["alternance"])
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        route = respx.get(LIST_URL).mock(
            return_value=httpx.Response(200, text=_page_html(total=5000, n_cards=10))
        )
        with caplog.at_level("WARNING"):
            source.fetch()

    assert route.call_count == MAX_PAGES_PER_SEARCH_TERM
