from __future__ import annotations

import sys
import types
from typing import Self

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.base import SourceError
from jobbot.sources.rendered import RenderedSource, _split_labeled_fields

URL = "https://acme.example/careers"


# --- fake Playwright, for every test that shouldn't need the real package --


class _FakeLink:
    def __init__(self, href: str | None) -> None:
        self._href = href

    def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None


class _FakeElement:
    def __init__(self, text: str, href: str | None = None) -> None:
        self._text = text
        self._href = href

    def inner_text(self) -> str:
        return self._text

    def query_selector(self, _selector: str) -> _FakeLink | None:
        return _FakeLink(self._href) if self._href else None


class _FakePage:
    def __init__(self, html: str, elements: list[_FakeElement] | None = None) -> None:
        self._html = html
        self._elements = elements or []
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.selector_used: str | None = None

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def content(self) -> str:
        return self._html

    def query_selector_all(self, selector: str) -> list[_FakeElement]:
        self.selector_used = selector
        return self._elements


class _FakeMultiPage:
    """M15 Part B: sitemap mode navigates ONE page object to MANY distinct
    URLs in a single fetch_raw() call, unlike every other fake page in this
    file (one URL per test). `pages` maps url -> {selector: text}, or None
    to simulate a page whose selectors match nothing (a load that succeeded
    but landed on a differently-shaped page)."""

    def __init__(self, pages: dict[str, dict[str, str] | None]) -> None:
        self._pages = pages
        self._current_url: str | None = None
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        self._current_url = url

    def query_selector(self, selector: str) -> _FakeElement | None:
        entry = self._pages.get(self._current_url)
        if entry is None:
            return None
        text = entry.get(selector)
        return _FakeElement(text) if text is not None else None


class _RaisingPage(_FakePage):
    def __init__(self, error: Exception) -> None:
        super().__init__("")
        self._error = error

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        raise self._error


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False
        self.new_page_user_agent: str | None = None

    def new_page(self, user_agent: str | None = None) -> _FakePage:
        self.new_page_user_agent = user_agent
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.launch_called = False

    def launch(self) -> _FakeBrowser:
        self.launch_called = True
        return self._browser


class _FakePlaywrightContext:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakePlaywrightError(Exception):
    pass


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch, page: _FakePage
) -> _FakeBrowser:
    browser = _FakeBrowser(page)

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _FakePlaywrightContext(browser)  # type: ignore[attr-defined]
    fake_sync_api.Error = _FakePlaywrightError  # type: ignore[attr-defined]

    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    return browser


def _make_source(
    client: httpx.Client, identifier: str = URL, company_name: str = "Acme"
) -> RenderedSource:
    return RenderedSource(identifier, company_name, client, user_agent=TEST_USER_AGENT)


def _mock_robots_allowed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("https://acme.example/robots.txt").mock(return_value=httpx.Response(404))


# --- identifier validation --------------------------------------------


@pytest.mark.parametrize("bad_identifier", ["not-a-url", "http://acme.example/careers", ""])
def test_invalid_identifier_raises_value_error(mock_client: httpx.Client, bad_identifier: str) -> None:
    with pytest.raises(ValueError):
        _make_source(mock_client, identifier=bad_identifier)


def test_identifier_without_pipe_has_no_selector(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier=URL)
    assert source._selector is None


def test_identifier_with_pipe_parses_the_selector(mock_client: httpx.Client) -> None:
    source = _make_source(mock_client, identifier=f"{URL}|.job-card")
    assert source._url == URL
    assert source._selector == ".job-card"


# --- robots.txt ----------------------------------------------------------


def test_robots_disallow_raises_source_error_and_playwright_is_never_touched(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client)
    browser = _install_fake_playwright(monkeypatch, _FakePage("<html></html>"))
    with respx.mock:
        respx.get("https://acme.example/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /careers")
        )
        with pytest.raises(SourceError):
            source.fetch()

    assert browser.new_page_user_agent is None  # never launched


# --- fetch_raw() / fetch(), JSON-LD path ----------------------------------


def test_fetch_raw_returns_a_two_tuple(mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source(mock_client)
    page = _FakePage('<script type="application/ld+json">{"@type": "JobPosting", "title": "x"}</script>')
    _install_fake_playwright(monkeypatch, page)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_jsonld_found_on_the_rendered_page_is_parsed(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client)
    html = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Alternance Data Analyst", '
        '"description": "<p>Stage de 6 mois</p>", '
        '"jobLocation": {"address": {"addressLocality": "Paris", "addressCountry": "France"}}}'
        "</script>"
    )
    _install_fake_playwright(monkeypatch, _FakePage(html))
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        jobs = source.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Alternance Data Analyst"
    assert job.contract_type == "apprenticeship"
    assert job.location == "Paris, France"
    assert str(job.url) == URL  # no top-level url on the posting -- falls back to the listing page


def test_user_agent_reaches_the_browser_context(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = RenderedSource(URL, "Acme", mock_client, user_agent="jobbot-test/9.9 (+x@example.invalid)")
    browser = _install_fake_playwright(monkeypatch, _FakePage("<html></html>"))
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        source.fetch()

    assert browser.new_page_user_agent == "jobbot-test/9.9 (+x@example.invalid)"


def test_returns_an_empty_list_rather_than_raising_when_nothing_matches(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M8b: zero results is a valid, non-failing outcome.
    source = _make_source(mock_client)
    _install_fake_playwright(monkeypatch, _FakePage("<html>no postings here</html>"))
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        jobs = source.fetch()

    assert jobs == []


# --- fetch_raw() / fetch(), CSS-selector fallback -------------------------


def test_selector_fallback_used_only_when_no_jsonld_is_found(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client, identifier=f"{URL}|.job-card")
    elements = [
        _FakeElement("Alternance Chef de Projet\nParis, France", href="/jobs/1"),
        _FakeElement("Stage Marketing Digital\n6 mois", href="/jobs/2"),
    ]
    page = _FakePage("<html>no ld+json here</html>", elements=elements)
    _install_fake_playwright(monkeypatch, page)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        jobs = source.fetch()

    assert page.selector_used == ".job-card"
    assert len(jobs) == 2
    assert jobs[0].title == "Alternance Chef de Projet"
    assert jobs[0].contract_type == "apprenticeship"
    assert str(jobs[0].url) == "https://acme.example/jobs/1"
    assert jobs[1].title == "Stage Marketing Digital"
    assert jobs[1].contract_type == "internship"


def test_selector_fallback_not_used_when_jsonld_is_present_even_with_a_selector_configured(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client, identifier=f"{URL}|.job-card")
    html = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Real Posting"}</script>'
    page = _FakePage(html, elements=[_FakeElement("Should never be used")])
    _install_fake_playwright(monkeypatch, page)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        jobs = source.fetch()

    assert page.selector_used is None  # query_selector_all() was never called
    assert len(jobs) == 1
    assert jobs[0].title == "Real Posting"


def test_card_with_no_visible_text_is_skipped_with_a_logged_warning(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = _make_source(mock_client, identifier=f"{URL}|.job-card")
    elements = [_FakeElement("   \n  "), _FakeElement("Real Title Here")]
    _install_fake_playwright(monkeypatch, _FakePage("<html></html>", elements=elements))
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        with caplog.at_level("WARNING"):
            jobs = source.fetch()

    assert len(jobs) == 1
    assert jobs[0].title == "Real Title Here"
    assert any("skipping malformed entry" in r.message for r in caplog.records)


def test_card_with_no_link_falls_back_to_the_listing_url(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client, identifier=f"{URL}|.job-card")
    _install_fake_playwright(
        monkeypatch, _FakePage("<html></html>", elements=[_FakeElement("Some Role")])
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        jobs = source.fetch()

    assert str(jobs[0].url) == URL


# --- failure handling ------------------------------------------------------


def test_playwright_not_installed_raises_a_clear_source_error(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Forces the real ImportError path regardless of whether the optional
    # 'playwright' extra happens to be installed in whatever environment
    # runs this suite (setting a sys.modules entry to None is the standard,
    # documented way to make a subsequent import of that name fail) --
    # confirms the bot degrades to a clean SourceError rather than an
    # unhandled crash when the extra genuinely isn't present.
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    source = _make_source(mock_client)
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        with pytest.raises(SourceError, match="playwright"):
            source.fetch()


def test_a_playwright_error_during_page_load_becomes_a_source_error(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(mock_client)
    browser = _install_fake_playwright(
        monkeypatch, _RaisingPage(_FakePlaywrightError("navigation timeout"))
    )
    with respx.mock:
        _mock_robots_allowed(respx.mock)
        with pytest.raises(SourceError):
            source.fetch()

    assert browser.closed is True  # the browser is still closed even on failure


# --- real Playwright, opt-in ------------------------------------------------


def _playwright_actually_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.slow
@pytest.mark.skipif(not _playwright_actually_installed(), reason="playwright is not installed")
def test_real_playwright_renders_a_real_page(mock_client: httpx.Client) -> None:
    """The one real-browser integration test M9e asks for -- skipped unless
    the optional 'playwright' extra (and its chromium binary) is actually
    present, so the rest of this suite never depends on it.

    The robots.txt check (this adapter's one real use of `self.client`) is
    still mocked via respx, same as every other test in this file and the
    same as CLAUDE.md rule 6 requires everywhere. The browser's own actual
    page traffic is a real, separate Chromium subprocess with its own
    network stack. On Windows, Playwright's SYNC API also opens a local
    loopback socketpair for its own driver-process IPC, which DOES go
    through Python's `socket` module -- confirmed live to need
    conftest.py's `_block_network` guard to specifically permit 127.0.0.1/
    ::1 (M13 Part B, approved by the user) for this test to run at all
    rather than skip; every other host stays blocked by that same guard.
    """
    source = RenderedSource(
        "https://example.com", "Example", mock_client, user_agent=TEST_USER_AGENT
    )
    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        # example.com has no jobs, obviously -- this only proves the real
        # browser launches, navigates, and returns cleanly (an empty list,
        # M8b's valid non-failing outcome), not that any adapter logic
        # beyond that is exercised against real content.
        jobs = source.fetch()

    assert jobs == []


# --- sitemap mode (M15 Part B) -----------------------------------------

SITEMAP_URL = "https://jobs.example.com/careers/sitemap_index.xml"
SM_IDENTIFIER = f"sitemap:{SITEMAP_URL}|h2|main"


def _mock_te_robots_allowed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("https://jobs.example.com/robots.txt").mock(return_value=httpx.Response(404))


def _mock_sitemap(respx_mock: respx.MockRouter, job_urls: list[str]) -> None:
    body = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in job_urls) + "</urlset>"
    respx_mock.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=body))


REAL_CONTENT = """Country
France
Area
69 - Rhône

Workplace location
SOLAIZE-CHEMIN DU CANAL(FRA)
Domain
Research Innovation&Developpt
Type of contract
Apprenticeship
Contract duration
12 Months
Experience
Less than 3 years
Activities

Une alternance de 12 mois au Laboratoire Qualité de l'Air, à partir de
septembre 2026, pour unétudiant en école d'ingénieurs.

Candidate Profile

Cette offre s'adresse aux étudiants de niveau Bac+5.

Additional Information
TotalEnergies values diversity, promotes individual growth and offers
equal opportunity careers.
Apply
"""


@pytest.mark.parametrize(
    "bad_identifier",
    [
        "sitemap:",
        "sitemap:https://jobs.example.com/sitemap.xml",
        "sitemap:https://jobs.example.com/sitemap.xml|h2",
        "sitemap:https://jobs.example.com/sitemap.xml||main",
        "sitemap:not-a-url|h2|main",
        "sitemap:http://jobs.example.com/sitemap.xml|h2|main",  # not https
    ],
)
def test_sitemap_mode_invalid_identifier_raises_value_error(
    mock_client: httpx.Client, bad_identifier: str
) -> None:
    with pytest.raises(ValueError):
        RenderedSource(bad_identifier, "Acme", mock_client, user_agent=TEST_USER_AGENT)


def test_sitemap_mode_identifier_parses_url_and_both_selectors(mock_client: httpx.Client) -> None:
    source = RenderedSource(SM_IDENTIFIER, "Acme", mock_client, user_agent=TEST_USER_AGENT)
    assert source._sitemap_mode is True
    assert source._sitemap_url == SITEMAP_URL
    assert source._title_selector == "h2"
    assert source._content_selector == "main"


def test_single_page_mode_identifier_is_unaffected_by_sitemap_mode(
    mock_client: httpx.Client,
) -> None:
    source = _make_source(mock_client)
    assert source._sitemap_mode is False


def test_sitemap_mode_robots_disallow_raises_source_error_before_any_render(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = RenderedSource(SM_IDENTIFIER, "Acme", mock_client, user_agent=TEST_USER_AGENT)
    browser = _install_fake_playwright(monkeypatch, _FakeMultiPage({}))
    with respx.mock:
        respx.get("https://jobs.example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        with pytest.raises(SourceError):
            source.fetch()

    assert browser.new_page_user_agent is None  # never launched


def test_sitemap_mode_renders_each_selected_url_and_parses_real_content(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_url = "https://jobs.example.com/en_US/careers/JobDetail/ALTERNANCE-X/34295"
    source = RenderedSource(
        SM_IDENTIFIER, "TotalEnergies", mock_client, user_agent=TEST_USER_AGENT,
        search_terms=["alternance"],
    )
    fake_page = _FakeMultiPage({job_url: {"h2": "ALTERNANCE - X", "main": REAL_CONTENT}})
    browser = _install_fake_playwright(monkeypatch, fake_page)

    with respx.mock:
        _mock_te_robots_allowed(respx.mock)
        _mock_sitemap(respx.mock, [job_url])
        jobs = source.fetch()

    assert fake_page.goto_calls == [job_url]
    assert browser.closed is True
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "ALTERNANCE - X"
    assert str(job.url) == job_url
    assert job.location == "SOLAIZE-CHEMIN DU CANAL(FRA), 69 - Rhône, France"
    assert job.contract_type == "apprenticeship"
    assert "Laboratoire Qualité de l'Air" in job.description
    assert "Additional Information" not in job.description
    assert "TotalEnergies values diversity" not in job.description


def test_sitemap_mode_a_page_matching_neither_selector_is_skipped_not_crashed(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_url = "https://jobs.example.com/en_US/careers/JobDetail/ALTERNANCE-X/34295"
    bad_url = "https://jobs.example.com/en_US/careers/JobDetail/ALTERNANCE-Y/43501"
    source = RenderedSource(
        SM_IDENTIFIER, "TotalEnergies", mock_client, user_agent=TEST_USER_AGENT,
        search_terms=["alternance"],
    )
    fake_page = _FakeMultiPage(
        {
            good_url: {"h2": "ALTERNANCE - X", "main": REAL_CONTENT},
            bad_url: None,  # simulates an error page / different shape entirely
        }
    )
    _install_fake_playwright(monkeypatch, fake_page)

    with respx.mock:
        _mock_te_robots_allowed(respx.mock)
        _mock_sitemap(respx.mock, [good_url, bad_url])
        jobs = source.fetch()

    assert len(jobs) == 1
    assert jobs[0].title == "ALTERNANCE - X"


def test_sitemap_mode_a_playwright_error_on_one_page_is_skipped_not_fatal(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike single-page mode (where a Playwright error IS the whole fetch
    # failing), sitemap mode renders many individually-discovered pages --
    # one page timing out is this mode's equivalent of sitemap_jsonld.py's
    # own per-page SourceError catch (_fetch_job_posting): logged and
    # skipped, not a reason to fail the whole batch. M8b: the resulting
    # empty list is a valid, non-failing outcome.
    job_url = "https://jobs.example.com/en_US/careers/JobDetail/ALTERNANCE-X/34295"
    source = RenderedSource(
        SM_IDENTIFIER, "TotalEnergies", mock_client, user_agent=TEST_USER_AGENT,
        search_terms=["alternance"],
    )
    browser = _install_fake_playwright(
        monkeypatch, _RaisingPage(_FakePlaywrightError("navigation timeout"))
    )
    with respx.mock:
        _mock_te_robots_allowed(respx.mock)
        _mock_sitemap(respx.mock, [job_url])
        jobs = source.fetch()

    assert jobs == []
    assert browser.closed is True


def test_sitemap_mode_zero_candidates_returns_empty_list_not_an_error(
    mock_client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = RenderedSource(
        SM_IDENTIFIER, "TotalEnergies", mock_client, user_agent=TEST_USER_AGENT,
        search_terms=["a-term-that-matches-nothing"], sample_size=0,
    )
    _install_fake_playwright(monkeypatch, _FakeMultiPage({}))
    with respx.mock:
        _mock_te_robots_allowed(respx.mock)
        _mock_sitemap(respx.mock, [])
        jobs = source.fetch()

    assert jobs == []


def test_sitemap_mode_locations_kwarg_reaches_the_shared_discovery_instance(
    mock_client: httpx.Client,
) -> None:
    source = RenderedSource(
        SM_IDENTIFIER, "TotalEnergies", mock_client, user_agent=TEST_USER_AGENT,
        locations=["paris", "nantes"],
    )
    assert source._discovery.locations == ["paris", "nantes"]


def test_sitemap_mode_default_page_cap_is_far_lower_than_sitemap_jsonld(
    mock_client: httpx.Client,
) -> None:
    from jobbot.sources.rendered import DEFAULT_RENDERED_SITEMAP_PAGE_CAP
    from jobbot.sources.sitemap_jsonld import DEFAULT_PAGE_CAP as SITEMAP_JSONLD_PAGE_CAP

    assert 0 < DEFAULT_RENDERED_SITEMAP_PAGE_CAP < SITEMAP_JSONLD_PAGE_CAP


# --- _split_labeled_fields (M15 Part B) --------------------------------


def test_split_labeled_fields_extracts_the_real_metadata_header() -> None:
    fields, _ = _split_labeled_fields(REAL_CONTENT)
    assert fields == {
        "Country": "France",
        "Area": "69 - Rhône",
        "Workplace location": "SOLAIZE-CHEMIN DU CANAL(FRA)",
        "Domain": "Research Innovation&Developpt",
        "Type of contract": "Apprenticeship",
        "Contract duration": "12 Months",
        "Experience": "Less than 3 years",
    }


def test_split_labeled_fields_description_excludes_the_activities_header_line() -> None:
    _, description = _split_labeled_fields(REAL_CONTENT)
    assert not description.startswith("Activities")
    assert description.startswith("Une alternance")


def test_split_labeled_fields_description_stops_before_additional_information() -> None:
    _, description = _split_labeled_fields(REAL_CONTENT)
    assert "Additional Information" not in description
    assert "TotalEnergies values diversity" not in description
    assert "Apply" not in description


def test_split_labeled_fields_degrades_safely_on_unrecognized_content() -> None:
    fields, description = _split_labeled_fields("Just some unrelated page text.\nMore text.")
    assert fields == {}
    assert description == "Just some unrelated page text.\nMore text."


def test_split_labeled_fields_a_label_with_no_value_does_not_swallow_the_next_label() -> None:
    # Live-confirmed bug on a real TotalEnergies posting missing "Area":
    # without a fix, "Area" -> "Domain" (the NEXT label's own name, not a
    # real value) leaked into the location. Avature genuinely leaves some
    # label values blank.
    text = (
        "Country\nFrance\nArea\nWorkplace location\nPARIS(FRA)\n"
        "Domain\nHR\nType of contract\nInternship\nActivities\n\nReal text."
    )
    fields, description = _split_labeled_fields(text)
    assert fields == {
        "Country": "France",
        "Workplace location": "PARIS(FRA)",
        "Domain": "HR",
        "Type of contract": "Internship",
    }
    assert "Area" not in fields
    assert description == "Real text."
