from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

import jobbot.discover as discover_module
from jobbot.config import load_companies
from jobbot.discover import (
    DiscoveryResult,
    _derive_name_from_url,
    _ranked_depth2_candidates,
    candidate_careers_urls,
    careers_links,
    detect_ats,
    discover_company,
    load_existing_entries,
    load_input_companies,
    main,
    print_summary,
    result_to_entry,
    run_discovery,
    verify,
    write_output_yaml,
)

ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"


def _mock_robots_allow(host: str) -> None:
    respx.get(f"https://{host}/robots.txt").mock(
        return_value=httpx.Response(200, text=ALLOW_ALL_ROBOTS)
    )


def _mock_no_sitemap(website: str) -> None:
    """M9d's step 4 always tries these two guessed paths once robots.txt's
    own Sitemap: lines come up empty (as ALLOW_ALL_ROBOTS does) -- every
    test whose earlier steps are all meant to fail needs this mocked too,
    or step 4 has an unmocked respx call and the test errors instead of
    reaching its own confidence="none" assertion."""
    respx.get(f"{website}/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get(f"{website}/sitemap.xml").mock(return_value=httpx.Response(404))


# --- detect_ats ------------------------------------------------------------


def test_detect_ats_greenhouse_boards_url() -> None:
    html = '<a href="https://boards.greenhouse.io/acme">Careers</a>'
    assert detect_ats(html, "https://acme.example") == ("greenhouse", "acme")


def test_detect_ats_greenhouse_job_boards_url() -> None:
    html = '<a href="https://job-boards.greenhouse.io/acme">Careers</a>'
    assert detect_ats(html, "https://acme.example") == ("greenhouse", "acme")


def test_detect_ats_greenhouse_api_url() -> None:
    html = 'fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")'
    assert detect_ats(html, "https://acme.example") == ("greenhouse", "acme")


def test_detect_ats_lever_hosted_url() -> None:
    html = '<a href="https://jobs.lever.co/acme">Careers</a>'
    assert detect_ats(html, "https://acme.example") == ("lever", "acme")


def test_detect_ats_lever_api_url() -> None:
    html = 'fetch("https://api.lever.co/v0/postings/acme?mode=json")'
    assert detect_ats(html, "https://acme.example") == ("lever", "acme")


def test_detect_ats_ashby_hosted_url() -> None:
    html = '<a href="https://jobs.ashbyhq.com/acme">Careers</a>'
    assert detect_ats(html, "https://acme.example") == ("ashby", "acme")


def test_detect_ats_ashby_api_url() -> None:
    html = 'fetch("https://api.ashbyhq.com/posting-api/job-board/acme")'
    assert detect_ats(html, "https://acme.example") == ("ashby", "acme")


def test_detect_ats_workday_url_with_locale_prefix() -> None:
    html = '<a href="https://ipsen.wd103.myworkdayjobs.com/en-EN/Ipsen_Careers">Careers</a>'
    assert detect_ats(html, "https://ipsen.example") == ("workday", "ipsen.wd103.Ipsen_Careers")


def test_detect_ats_workday_url_without_locale_prefix() -> None:
    html = '<a href="https://sanofi.wd3.myworkdayjobs.com/SanofiCareers">Careers</a>'
    assert detect_ats(html, "https://sanofi.example") == ("workday", "sanofi.wd3.SanofiCareers")


def test_detect_ats_smartrecruiters_careers_url() -> None:
    html = '<a href="https://careers.smartrecruiters.com/KIABI">Careers</a>'
    assert detect_ats(html, "https://kiabi.example") == ("smartrecruiters", "KIABI")


def test_detect_ats_smartrecruiters_api_url() -> None:
    html = 'fetch("https://api.smartrecruiters.com/v1/companies/KIABI/postings")'
    assert detect_ats(html, "https://kiabi.example") == ("smartrecruiters", "KIABI")


def test_detect_ats_talentsoft_url() -> None:
    html = '<a href="https://casa-cacib-recrute.talent-soft.com/job/list-of-all-jobs.aspx">Jobs</a>'
    assert detect_ats(html, "https://cacib.example") == (
        "talentsoft", "https://casa-cacib-recrute.talent-soft.com",
    )


def test_detect_ats_jibe_via_jibecdn_asset_reference() -> None:
    html = '<script src="https://assets.jibecdn.com/prod/axa/0.0.179/assets/_scripts/header.js"></script>'
    ats, identifier = detect_ats(html, "https://careers.axa.com/global/en")
    assert ats == "jibe"
    assert identifier == "https://careers.axa.com"


def test_detect_ats_ats_signatures_are_checked_before_jibe_and_jsonld() -> None:
    html = (
        '<a href="https://boards.greenhouse.io/acme">Careers</a>'
        '<script src="https://assets.jibecdn.com/prod/acme/header.js"></script>'
    )
    assert detect_ats(html, "https://acme.example") == ("greenhouse", "acme")


def test_detect_ats_jsonld_job_posting() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Data Analyst"}
    </script>
    """
    ats, identifier = detect_ats(html, "https://acme.example/careers")
    assert ats == "jsonld"
    assert identifier == "https://acme.example/careers"


def test_detect_ats_ignores_ld_json_block_without_job_posting() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "Organization", "name": "Acme"}
    </script>
    """
    assert detect_ats(html, "https://acme.example") == (None, None)


def test_detect_ats_prefers_ats_signature_over_jsonld_on_same_page() -> None:
    html = (
        '<a href="https://boards.greenhouse.io/acme">Careers</a>'
        '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    )
    assert detect_ats(html, "https://acme.example") == ("greenhouse", "acme")


def test_detect_ats_two_different_ats_mentioned_first_wins_deterministically() -> None:
    gh_then_lever = (
        '<a href="https://boards.greenhouse.io/acme">GH</a>'
        '<a href="https://jobs.lever.co/other">Lever</a>'
    )
    lever_then_gh = (
        '<a href="https://jobs.lever.co/other">Lever</a>'
        '<a href="https://boards.greenhouse.io/acme">GH</a>'
    )
    # Whichever reference appears first in the raw text must not matter --
    # greenhouse always wins because it's checked first.
    assert detect_ats(gh_then_lever, "https://x.example") == ("greenhouse", "acme")
    assert detect_ats(lever_then_gh, "https://x.example") == ("greenhouse", "acme")


def test_detect_ats_false_positive_prose_does_not_match() -> None:
    html = "<p>We need to pull the right lever to grow the team internationally.</p>"
    assert detect_ats(html, "https://acme.example") == (None, None)


def test_detect_ats_empty_page_returns_none() -> None:
    assert detect_ats("", "https://acme.example") == (None, None)


# --- candidate_careers_urls --------------------------------------------


def test_candidate_careers_urls_default_order_for_non_fr_domain() -> None:
    urls = candidate_careers_urls("https://example.com")
    assert urls == [
        "https://example.com/careers",
        "https://example.com/jobs",
        "https://example.com/carrieres",
        "https://example.com/recrutement",
        "https://example.com/nous-rejoindre",
        "https://example.com/rejoignez-nous",
        "https://example.com/emplois",
        "https://example.com/join-us",
        "https://example.com/careers/jobs",
        "https://example.com/fr/carrieres",
        "https://example.com",
    ]


def test_candidate_careers_urls_french_paths_first_for_fr_domain() -> None:
    urls = candidate_careers_urls("https://example.fr")
    assert urls[:6] == [
        "https://example.fr/carrieres",
        "https://example.fr/recrutement",
        "https://example.fr/nous-rejoindre",
        "https://example.fr/rejoignez-nous",
        "https://example.fr/emplois",
        "https://example.fr/fr/carrieres",
    ]
    assert urls[6:10] == [
        "https://example.fr/careers",
        "https://example.fr/jobs",
        "https://example.fr/join-us",
        "https://example.fr/careers/jobs",
    ]
    assert urls[-1] == "https://example.fr"


def test_candidate_careers_urls_strips_trailing_slash_and_puts_root_last() -> None:
    urls = candidate_careers_urls("https://example.com/")
    assert urls[0] == "https://example.com/careers"
    assert urls[-1] == "https://example.com"


# --- careers_links ---------------------------------------------------------


def test_careers_links_matches_by_english_href() -> None:
    html = '<a href="/careers">Join Us</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/careers"]


def test_careers_links_matches_by_french_href() -> None:
    html = '<a href="/carrieres">A propos</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/carrieres"]


def test_careers_links_matches_by_english_link_text() -> None:
    html = '<a href="/about-us">Careers</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/about-us"]


def test_careers_links_matches_by_french_link_text() -> None:
    html = '<a href="/a-propos">Nous recrutons</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/a-propos"]


def test_careers_links_matches_accented_french_text() -> None:
    # "carrière" (accented) must match the unaccented "carriere" keyword.
    html = '<a href="/x">Nos carrières</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/x"]


def test_careers_links_matches_hiring_phrases() -> None:
    for text in ("We're hiring", "On recrute", "Nous recrutons !"):
        html = f'<a href="/x">{text}</a>'
        assert careers_links(html, "https://acme.example") == ["https://acme.example/x"], text


def test_careers_links_ignores_off_host_links() -> None:
    html = '<a href="https://otherdomain.example/careers">Careers</a>'
    assert careers_links(html, "https://acme.example") == []


def test_careers_links_allows_known_ats_hosts_despite_being_off_host() -> None:
    html = '<a href="https://jobs.ashbyhq.com/acme">Careers</a>'
    assert careers_links(html, "https://acme.example") == ["https://jobs.ashbyhq.com/acme"]


def test_careers_links_ignores_www_prefix_when_comparing_hosts() -> None:
    html = '<a href="https://www.acme.example/careers">Careers</a>'
    assert careers_links(html, "https://acme.example") == ["https://www.acme.example/careers"]


def test_careers_links_ignores_links_with_no_careers_signal() -> None:
    html = '<a href="/about">About us</a><a href="/pricing">Pricing</a>'
    assert careers_links(html, "https://acme.example") == []


def test_careers_links_ignores_non_http_hrefs() -> None:
    html = '<a href="mailto:careers@acme.example">Careers</a><a href="#careers">Careers</a>'
    assert careers_links(html, "https://acme.example") == []


def test_careers_links_deduplicates() -> None:
    html = '<a href="/careers">Careers</a><a href="/careers">Careers (footer)</a>'
    assert careers_links(html, "https://acme.example") == ["https://acme.example/careers"]


def test_careers_links_preserves_document_order() -> None:
    html = '<a href="/jobs">Jobs</a><a href="/careers">Careers</a>'
    assert careers_links(html, "https://acme.example") == [
        "https://acme.example/jobs",
        "https://acme.example/careers",
    ]


def test_careers_links_caps_at_six() -> None:
    html = "".join(f'<a href="/careers-{i}">Careers</a>' for i in range(8))
    links = careers_links(html, "https://acme.example")
    assert len(links) == 6
    assert links == [f"https://acme.example/careers-{i}" for i in range(6)]


# --- discover_company ----------------------------------------------------


def test_discover_company_root_with_ats_href_resolves_at_step_1_with_zero_further_fetches(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        root_route = respx.get(website, path="/").mock(
            return_value=httpx.Response(
                200, text='<a href="https://boards.greenhouse.io/acme">Careers</a>'
            )
        )
        # No other route is registered at all -- respx errors on any request
        # that doesn't match a route, so a second fetch would fail the test.
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "greenhouse"
    assert result.identifier == "acme"
    assert root_route.call_count == 1


def test_discover_company_root_with_careers_nav_link_resolves_at_step_2(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        root_route = respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="/careers">Careers</a>')
        )
        careers_route = respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Apply</a>')
        )
        # None of the guessed paths are registered -- if the resolution
        # order ever fell through to them, respx would error on the request.
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "lever"
    assert result.identifier == "acme"
    assert root_route.call_count == 1
    assert careers_route.call_count == 1


def test_discover_company_falls_through_to_guessed_paths_when_root_has_neither(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "lever"
    assert result.identifier == "acme"


def test_discover_company_gives_up_after_eight_attempts_with_confidence_none(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    # 6 careers-looking nav links on the root -- careers_links() caps at 6,
    # so root (1) + these (6) + one guessed path (1) = 8, the cap.
    root_html = "".join(f'<a href="/link{i}">Careers</a>' for i in range(6))
    with respx.mock:
        _mock_robots_allow("acme.example")
        _mock_no_sitemap(website)
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text=root_html))
        for i in range(6):
            respx.get(f"{website}/link{i}").mock(
                return_value=httpx.Response(200, text="<html>no ats signature here</html>")
            )
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text="<html>no ats signature here either</html>")
        )
        never_route = respx.get(f"{website}/jobs").mock(
            return_value=httpx.Response(200, text="must never be fetched, 9th attempt")
        )

        result = discover_company(website, "Acme", mock_client, TEST_USER_AGENT, sleep=lambda s: None)

    assert result.confidence == "none"
    assert result.ats is None
    assert result.identifier is None
    assert never_route.call_count == 0


def test_500_on_one_candidate_moves_to_the_next_rather_than_aborting(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(return_value=httpx.Response(500))
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "lever"
    assert result.identifier == "acme"


def test_a_redirected_candidate_url_is_still_followed_and_detected() -> None:
    # Real company sites redirect constantly (www dropped, http upgraded to
    # https, a domain migration) -- a client that doesn't follow redirects
    # would treat every one of these as "not 200" and give up too early.
    client = httpx.Client(verify=False, follow_redirects=True)
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(301, headers={"Location": "https://www.acme.example"})
        )
        _mock_robots_allow("www.acme.example")
        respx.get("https://www.acme.example", path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        result = discover_company(
            website, "Acme", client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "lever"
    assert result.identifier == "acme"


def test_robots_disallow_skips_that_url_without_fetching_it(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    robots_txt = "User-agent: *\nDisallow: /careers\n"
    with respx.mock:
        respx.get("https://acme.example/robots.txt").mock(
            return_value=httpx.Response(200, text=robots_txt)
        )
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        careers_route = respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text="must never be fetched")
        )
        jobs_route = respx.get(f"{website}/jobs").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert careers_route.call_count == 0
    assert jobs_route.call_count == 1
    assert result.ats == "lever"


def test_unresolved_notes_record_every_attempted_url(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        _mock_no_sitemap(website)
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        respx.get(f"{website}/careers").mock(return_value=httpx.Response(404))
        respx.get(f"{website}/jobs").mock(return_value=httpx.Response(500))

        # Force a small candidate list so the test doesn't need to mock all
        # 8 attempts: monkeypatch-free by capping via candidate_careers_urls
        # isn't possible here, so mock the remaining guessed paths as 404s.
        for path in (
            "/carrieres", "/recrutement", "/nous-rejoindre", "/rejoignez-nous",
            "/emplois", "/join-us",
        ):
            respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))

        result = discover_company(website, "Acme", mock_client, TEST_USER_AGENT, sleep=lambda s: None)

    assert result.confidence == "none"
    assert website in result.notes
    assert f"{website}/careers" in result.notes
    assert f"{website}/jobs" in result.notes
    assert "no careers links found on the root page" in result.notes


# --- depth-2 (M9 Part A) ---------------------------------------------------


def test_ranked_depth2_candidates_prefers_listings_suggesting_links() -> None:
    # "/careers" appears first in the document but carries no listings
    # signal; "/nos-offres" appears second but must rank first (A3).
    html = '<a href="/careers">Careers</a><a href="/nos-offres">Nos offres</a>'
    ranked = _ranked_depth2_candidates(html, "https://acme.example")
    assert ranked == [
        "https://acme.example/nos-offres",
        "https://acme.example/careers",
    ]


def test_discover_company_depth2_page_carrying_ats_resolves_and_reports_the_full_path(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="/careers">Careers</a>')
        )
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<a href="/careers/team">Our culture</a>'
                    '<a href="/careers/offres">Voir les offres</a>'
                ),
            )
        )
        respx.get(f"{website}/careers/offres").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Postuler</a>')
        )
        # The listings-suggesting link is tried first (A3) and resolves
        # before the generic "team" link is ever fetched.
        team_route = respx.get(f"{website}/careers/team").mock(
            return_value=httpx.Response(200, text="<html>culture page</html>")
        )
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "lever"
    assert result.identifier == "acme"
    # careers_url reports the full path resolved to: the depth-2 URL, not
    # the root or the depth-1 page that led to it.
    assert result.careers_url == f"{website}/careers/offres"
    assert team_route.call_count == 0


def test_visited_url_is_never_refetched_even_when_reachable_from_two_paths(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        _mock_no_sitemap(website)
        respx.get(website, path="/").mock(
            return_value=httpx.Response(
                200, text='<a href="/careers">Careers</a><a href="/jobs">Jobs</a>'
            )
        )
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text='<a href="/offres">Voir les offres</a>')
        )
        respx.get(f"{website}/jobs").mock(
            return_value=httpx.Response(200, text='<a href="/offres">Voir les offres</a>')
        )
        # Reachable as a depth-2 candidate from both /careers and /jobs --
        # must only ever be fetched once.
        shared_route = respx.get(f"{website}/offres").mock(
            return_value=httpx.Response(200, text="<html>no ats here either</html>")
        )
        # Nothing above finds an ATS, so discover_company falls through to
        # the guessed-path list -- mock the rest so that fallback (which
        # this test isn't about) doesn't hit an unmocked route.
        for path in (
            "/carrieres", "/recrutement", "/nous-rejoindre", "/rejoignez-nous",
            "/emplois", "/join-us", "/careers/jobs", "/fr/carrieres",
        ):
            respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))

        discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert shared_route.call_count == 1


def test_eight_attempt_cap_holds_across_both_depths(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        _mock_no_sitemap(website)
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="/nav-careers">Careers</a>')
        )
        respx.get(f"{website}/nav-careers").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<a href="/nav-careers/d1">Voir les offres</a>'
                    '<a href="/nav-careers/d2">Voir les offres</a>'
                ),
            )
        )
        respx.get(f"{website}/nav-careers/d1").mock(
            return_value=httpx.Response(200, text="<html>no ats</html>")
        )
        respx.get(f"{website}/nav-careers/d2").mock(
            return_value=httpx.Response(200, text="<html>no ats</html>")
        )
        # root(1) + nav-careers(2) + d1(3) + d2(4) + these four(5-8) = 8, the cap.
        for path in ("/careers", "/jobs", "/carrieres", "/recrutement"):
            respx.get(f"{website}{path}").mock(
                return_value=httpx.Response(200, text="<html>no ats</html>")
            )
        never_route = respx.get(f"{website}/nous-rejoindre").mock(
            return_value=httpx.Response(200, text="must never be fetched, 9th attempt")
        )

        result = discover_company(website, "Acme", mock_client, TEST_USER_AGENT, sleep=lambda s: None)

    assert result.confidence == "none"
    assert never_route.call_count == 0


# --- sitemap_jsonld resolution, step 4 (M9d) --------------------------------


def _mock_every_guessing_step_fails(website: str, host: str) -> None:
    """Steps 1-3 all fail cleanly (no ATS signature anywhere, no careers
    links on the root, every guessed path 404s) so a test can focus purely
    on whether step 4 (the sitemap route) resolves something -- mirrors
    test_unresolved_entries_never_appear_in_the_yaml's own setup."""
    _mock_robots_allow(host)
    respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
    for path in (
        "/careers", "/jobs", "/carrieres", "/recrutement", "/nous-rejoindre",
        "/rejoignez-nous", "/emplois", "/join-us", "/careers/jobs", "/fr/carrieres",
    ):
        respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))


def test_discover_company_resolves_via_sitemap_jsonld_when_everything_else_fails(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_every_guessing_step_fails(website, "acme.example")
        respx.get(f"{website}/sitemap_index.xml").mock(return_value=httpx.Response(404))
        respx.get(f"{website}/sitemap.xml").mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<urlset><url><loc>https://acme.example/job/123</loc></url>"
                    "<url><loc>https://acme.example/about</loc></url></urlset>"
                ),
            )
        )
        about_route = respx.get(f"{website}/about").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        respx.get(f"{website}/job/123").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<script type="application/ld+json">'
                    '{"@type": "JobPosting", "title": "Alternance Data Analyst"}'
                    "</script>"
                ),
            )
        )

        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

        # The non-job-looking /about URL from the same sitemap must never be
        # fetched -- only URLs that look like job pages are sampled.
        assert about_route.call_count == 0

    assert result.ats == "sitemap_jsonld"
    assert result.identifier == f"{website}/sitemap.xml"


def test_discover_company_finds_sitemap_via_robots_txt_declaration(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        respx.get(f"{website}/robots.txt").mock(
            return_value=httpx.Response(
                200,
                text=f"User-agent: *\nAllow: /\nSitemap: {website}/custom-sitemap.xml\n",
            )
        )
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        for path in (
            "/careers", "/jobs", "/carrieres", "/recrutement", "/nous-rejoindre",
            "/rejoignez-nous", "/emplois", "/join-us", "/careers/jobs", "/fr/carrieres",
        ):
            respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))
        respx.get(f"{website}/custom-sitemap.xml").mock(
            return_value=httpx.Response(
                200, text="<urlset><url><loc>https://acme.example/offre/55</loc></url></urlset>"
            )
        )
        respx.get(f"{website}/offre/55").mock(
            return_value=httpx.Response(
                200,
                text='<script type="application/ld+json">{"@type": "JobPosting"}</script>',
            )
        )

        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "sitemap_jsonld"
    assert result.identifier == f"{website}/custom-sitemap.xml"


def test_discover_company_resolves_a_relative_sitemap_declaration_rather_than_crashing(
    mock_client: httpx.Client,
) -> None:
    # Real, observed behavior (confirmed live against discovery/seeds/
    # Batch1.txt): a robots.txt Sitemap: line that's a relative path, not
    # the absolute URL the sitemap protocol requires. Handing this straight
    # to httpx/RobotFileParser as if it were absolute raised deep inside
    # urllib rather than failing cleanly -- this is that regression test.
    website = "https://acme.example"
    with respx.mock:
        respx.get(f"{website}/robots.txt").mock(
            return_value=httpx.Response(
                200, text="User-agent: *\nAllow: /\nSitemap: /relative-sitemap.xml\n"
            )
        )
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        for path in (
            "/careers", "/jobs", "/carrieres", "/recrutement", "/nous-rejoindre",
            "/rejoignez-nous", "/emplois", "/join-us", "/careers/jobs", "/fr/carrieres",
        ):
            respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))
        respx.get(f"{website}/relative-sitemap.xml").mock(
            return_value=httpx.Response(
                200, text="<urlset><url><loc>https://acme.example/offre/9</loc></url></urlset>"
            )
        )
        respx.get(f"{website}/offre/9").mock(
            return_value=httpx.Response(
                200, text='<script type="application/ld+json">{"@type": "JobPosting"}</script>'
            )
        )

        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.ats == "sitemap_jsonld"
    assert result.identifier == f"{website}/relative-sitemap.xml"


def test_discover_company_sitemap_route_gives_up_cleanly_with_no_job_urls(
    mock_client: httpx.Client,
) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_every_guessing_step_fails(website, "acme.example")
        respx.get(f"{website}/sitemap_index.xml").mock(return_value=httpx.Response(404))
        respx.get(f"{website}/sitemap.xml").mock(
            return_value=httpx.Response(
                200, text="<urlset><url><loc>https://acme.example/about</loc></url></urlset>"
            )
        )

        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.confidence == "none"


# --- verify() / confidence promotion --------------------------------------


def test_verify_returns_postings_count_and_note(mock_client: httpx.Client) -> None:
    with respx.mock:
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "1",
                        "text": "Data Intern",
                        "hostedUrl": "https://jobs.lever.co/acme/1",
                        "createdAt": 1700000000000,
                    }
                ],
            )
        )
        count, note = verify("lever", "acme", mock_client, TEST_USER_AGENT)

    assert count == 1
    assert note


def test_verify_returns_zero_and_a_reason_on_source_error_rather_than_raising(
    mock_client: httpx.Client,
) -> None:
    with respx.mock:
        respx.get("https://api.lever.co/v0/postings/missing?mode=json").mock(
            return_value=httpx.Response(404)
        )
        count, note = verify("lever", "missing", mock_client, TEST_USER_AGENT)

    assert count == 0
    assert note


def test_verify_workday_with_a_real_shaped_identifier(mock_client: httpx.Client) -> None:
    url = "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs"
    with respx.mock:
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Alternance Data Analyst",
                            "externalPath": "/job/Paris/Alternance_R1",
                            "locationsText": "Paris",
                        }
                    ],
                },
            )
        )
        count, note = verify("workday", "sanofi.wd3.SanofiCareers", mock_client, TEST_USER_AGENT)

    assert count == 1
    assert note


def test_verify_sitemap_jsonld_with_the_sitemap_index_url_as_identifier(
    mock_client: httpx.Client,
) -> None:
    identifier = "https://careers.example.com/sitemap_index.xml"
    with respx.mock:
        respx.get("https://careers.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(identifier).mock(
            return_value=httpx.Response(
                200, text="<urlset><url><loc>https://careers.example.com/job/1</loc></url></urlset>"
            )
        )
        respx.get("https://careers.example.com/job/1").mock(
            return_value=httpx.Response(
                200,
                text='<script type="application/ld+json">{"@type": "JobPosting", "title": "Stage"}</script>',
            )
        )
        count, note = verify("sitemap_jsonld", identifier, mock_client, TEST_USER_AGENT)

    assert count == 1
    assert note


def test_discover_company_confirmed_when_verify_finds_postings(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "1",
                        "text": "Data Intern",
                        "hostedUrl": "https://jobs.lever.co/acme/1",
                        "createdAt": 1700000000000,
                    }
                ],
            )
        )
        result = discover_company(website, "Acme", mock_client, TEST_USER_AGENT, sleep=lambda s: None)

    assert result.confidence == "confirmed"
    assert result.postings_found == 1


def test_discover_company_probable_when_verify_finds_nothing(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(404)
        )
        result = discover_company(website, "Acme", mock_client, TEST_USER_AGENT, sleep=lambda s: None)

    assert result.confidence == "probable"
    assert result.postings_found == 0


def test_discover_company_probable_when_no_verify_flag_set(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        result = discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=lambda s: None, verify_result=False,
        )

    assert result.confidence == "probable"
    assert result.postings_found == 0


# --- sleep / delay ---------------------------------------------------------


def test_sleep_called_between_requests_with_the_configured_delay(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    sleep_calls: list[float] = []
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="nothing here"))
        respx.get(f"{website}/careers").mock(
            return_value=httpx.Response(200, text='<a href="https://boards.greenhouse.io/acme">C</a>')
        )
        discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=sleep_calls.append, delay=3.5, verify_result=False,
        )

    assert sleep_calls == [3.5]  # once, between the root fetch and the /careers fallback


def test_sleep_not_called_when_root_succeeds(mock_client: httpx.Client) -> None:
    website = "https://acme.example"
    sleep_calls: list[float] = []
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://boards.greenhouse.io/acme">C</a>')
        )
        discover_company(
            website, "Acme", mock_client, TEST_USER_AGENT,
            sleep=sleep_calls.append, delay=3.5, verify_result=False,
        )

    assert sleep_calls == []


# --- YAML output / config round-trip --------------------------------------


def test_yaml_output_loads_cleanly_through_load_companies(tmp_path: Path) -> None:
    output_path = tmp_path / "discovered.yaml"
    result = DiscoveryResult(
        company_name="Acme", website="https://acme.example", ats="lever",
        identifier="acme", careers_url="https://acme.example/careers",
        postings_found=5, confidence="confirmed", notes="5 postings found",
    )
    write_output_yaml(
        [result_to_entry(result)], output_path, "seeds.txt", datetime(2026, 8, 26, tzinfo=UTC)
    )

    loaded = load_companies(output_path)
    assert len(loaded) == 1
    assert loaded[0].name == "Acme"
    assert loaded[0].ats == "lever"
    assert loaded[0].identifier == "acme"
    assert loaded[0].tier == "cold"
    assert loaded[0].enabled is True


def test_output_header_records_discovery_date_and_source_file(tmp_path: Path) -> None:
    output_path = tmp_path / "discovered.yaml"
    write_output_yaml([], output_path, "discovery/seeds/example.txt", datetime(2026, 8, 26, tzinfo=UTC))
    text = output_path.read_text(encoding="utf-8")
    assert "2026-08-26" in text
    assert "discovery/seeds/example.txt" in text


# --- run_discovery: append / unresolved -----------------------------------


def test_append_does_not_duplicate_an_entry_already_present(
    mock_client: httpx.Client, tmp_path: Path
) -> None:
    output_path = tmp_path / "discovered.yaml"
    unresolved_path = tmp_path / "unresolved.txt"
    write_output_yaml(
        [{"name": "Acme", "ats": "lever", "identifier": "acme", "enabled": True, "tier": "cold", "tags": []}],
        output_path, "seed1.txt", datetime(2026, 8, 1, tzinfo=UTC),
    )

    website = "https://acme.example"
    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get(website, path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        run_discovery(
            [("Acme", website)], mock_client, TEST_USER_AGENT,
            output_path=output_path, unresolved_path=unresolved_path,
            source_input="seed2.txt", append=True, verify_result=False,
            sleep=lambda s: None,
        )

    assert len(load_existing_entries(output_path)) == 1


def test_unresolved_entries_never_appear_in_the_yaml(mock_client: httpx.Client, tmp_path: Path) -> None:
    output_path = tmp_path / "discovered.yaml"
    unresolved_path = tmp_path / "unresolved.txt"
    website = "https://nomatch.example"
    with respx.mock:
        _mock_robots_allow("nomatch.example")
        _mock_no_sitemap(website)
        respx.get(website, path="/").mock(return_value=httpx.Response(200, text="<html>nothing here</html>"))
        for path in (
            "/careers", "/jobs", "/carrieres", "/recrutement", "/nous-rejoindre",
            "/rejoignez-nous", "/emplois",
        ):
            respx.get(f"{website}{path}").mock(return_value=httpx.Response(404))

        results = run_discovery(
            [("NoMatch", website)], mock_client, TEST_USER_AGENT,
            output_path=output_path, unresolved_path=unresolved_path,
            source_input="seeds.txt", sleep=lambda s: None,
        )

    assert results[0].confidence == "none"
    assert load_existing_entries(output_path) == []
    assert "NoMatch" in unresolved_path.read_text(encoding="utf-8")


def test_run_discovery_writes_incrementally_after_each_company(
    mock_client: httpx.Client, tmp_path: Path
) -> None:
    """A crash/Ctrl-C after company 1 must not lose company 1's result."""
    output_path = tmp_path / "discovered.yaml"
    unresolved_path = tmp_path / "unresolved.txt"
    with respx.mock:
        _mock_robots_allow("first.example")
        _mock_robots_allow("second.example")
        respx.get("https://first.example", path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/first">C</a>')
        )
        # No ATS signature and no careers links here -- forces
        # discover_company to fall through to the guessed-path list for
        # "Second", which is where sleep() (and thus the interruption)
        # happens, since the root fetch itself never sleeps.
        respx.get("https://second.example", path="/").mock(
            return_value=httpx.Response(200, text="<html>no signature here</html>")
        )

        class StopAfterFirst(Exception):
            pass

        def sleep_and_raise_on_second(_delay: float) -> None:
            raise StopAfterFirst

        with pytest.raises(StopAfterFirst):
            run_discovery(
                [("First", "https://first.example"), ("Second", "https://second.example")],
                mock_client, TEST_USER_AGENT,
                output_path=output_path, unresolved_path=unresolved_path,
                source_input="seeds.txt", verify_result=False,
                sleep=sleep_and_raise_on_second,
            )

    # First company's result was flushed to disk before the second company's
    # first sleep() call ever raised.
    entries = load_existing_entries(output_path)
    assert len(entries) == 1
    assert entries[0]["name"] == "First"


# --- load-bearing: never becomes an aggregator client ---------------------


_FORBIDDEN_AGGREGATOR_DOMAINS = (
    "indeed", "linkedin", "glassdoor", "jobteaser", "welcometothejungle", "monster.com", "wttj",
)


def test_discovery_never_fetches_listings_from_a_directory() -> None:
    source = Path(discover_module.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    violations = [d for d in _FORBIDDEN_AGGREGATOR_DOMAINS if d in lowered]
    assert not violations, f"discover.py references forbidden aggregator domain(s): {violations}"


# --- CLI plumbing ----------------------------------------------------------


def test_load_input_companies_parses_bare_url_csv_and_bare_domain(tmp_path: Path) -> None:
    input_path = tmp_path / "seeds.txt"
    input_path.write_text(
        "# a comment, ignored\n"
        "\n"
        "https://example.com\n"
        "Acme Corp, https://acme.example\n"
        "bare-domain.fr\n",
        encoding="utf-8",
    )
    companies = load_input_companies(input_path)
    assert companies == [
        ("Example", "https://example.com"),
        ("Acme Corp", "https://acme.example"),
        ("Bare Domain", "https://bare-domain.fr"),
    ]


def test_derive_name_from_url_strips_www_and_title_cases() -> None:
    assert _derive_name_from_url("https://www.illuin.tech") == "Illuin"
    assert _derive_name_from_url("https://clever-cloud.com") == "Clever Cloud"


def test_print_summary_reports_confirmed_probable_unresolved_and_postings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        DiscoveryResult("A", "https://a.example", "lever", "a", "https://a.example/careers", 5, "confirmed", "5 postings found"),
        DiscoveryResult("B", "https://b.example", "ashby", "b", "https://b.example/careers", 0, "probable", "not verified"),
        DiscoveryResult("C", "https://c.example", None, None, None, 0, "none", "no ATS signature found"),
    ]
    print_summary(results)
    out = capsys.readouterr().out
    assert "Companies attempted: 3" in out
    assert "lever: 1" in out
    assert "Probable: 1" in out
    assert "Unresolved: 1" in out
    assert "Total postings found: 5" in out


def test_main_end_to_end_writes_output_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("user_agent_contact: test@example.invalid\n", encoding="utf-8")
    input_path = tmp_path / "seeds.txt"
    input_path.write_text("https://acme.example\n", encoding="utf-8")
    output_path = tmp_path / "discovered.yaml"

    # main() builds a real (verify=True) httpx.Client, same as run.py's own
    # main() -- correct for real usage, but its SSLContext/CA-bundle setup
    # is the same ~0.5-1s cost the mock_client fixture exists to dodge (see
    # conftest.py). respx.mock intercepts the requests either way, so
    # swapping in verify=False here changes nothing about what's being
    # tested, only how expensive the real Client() construction itself is.
    real_client_cls = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real_client_cls(*a, verify=False, **kw))

    with respx.mock:
        _mock_robots_allow("acme.example")
        respx.get("https://acme.example", path="/").mock(
            return_value=httpx.Response(200, text='<a href="https://jobs.lever.co/acme">Careers</a>')
        )
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "1", "text": "Data Intern",
                        "hostedUrl": "https://jobs.lever.co/acme/1",
                        "createdAt": 1700000000000,
                    }
                ],
            )
        )
        exit_code = main(
            [
                "--input", str(input_path),
                "--output", str(output_path),
                "--settings", str(settings_path),
                "--delay", "0",
            ]
        )

    assert exit_code == 0
    loaded = load_companies(output_path)
    assert len(loaded) == 1
    assert loaded[0].ats == "lever"
    out = capsys.readouterr().out
    assert "Companies attempted: 1" in out


def test_main_exits_2_on_missing_settings_file(tmp_path: Path) -> None:
    input_path = tmp_path / "seeds.txt"
    input_path.write_text("https://acme.example\n", encoding="utf-8")
    missing_settings = tmp_path / "does-not-exist.yaml"

    exit_code = main(["--input", str(input_path), "--settings", str(missing_settings)])

    assert exit_code == 2


def test_main_exits_2_on_missing_input_file(tmp_path: Path) -> None:
    missing_input = tmp_path / "does-not-exist.txt"
    exit_code = main(["--input", str(missing_input)])
    assert exit_code == 2
