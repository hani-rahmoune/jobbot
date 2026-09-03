from __future__ import annotations

import sys
import types
from typing import Self

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT, NetworkAccessDisabledError

from jobbot.sources.base import SourceError
from jobbot.sources.rendered import RenderedSource

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
    network stack -- but confirmed live (not assumed): on Windows,
    Playwright's SYNC API also opens a local loopback socketpair for its
    own driver-process IPC, which DOES go through Python's `socket` module
    and so IS caught by conftest.py's blanket, load-bearing `_block_network`
    guard (CLAUDE.md: edit only with confirmation, never unilaterally).
    Rather than either weaken that guard or let this one opt-in test hard-
    fail for anyone who installs the optional extra, that specific,
    identified failure mode is treated as a skip, with the real reason
    surfaced -- everything else about this test still runs for real.
    """
    source = RenderedSource(
        "https://example.com", "Example", mock_client, user_agent=TEST_USER_AGENT
    )
    try:
        with respx.mock:
            respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
            # example.com has no jobs, obviously -- this only proves the real
            # browser launches, navigates, and returns cleanly (an empty list,
            # M8b's valid non-failing outcome), not that any adapter logic
            # beyond that is exercised against real content.
            jobs = source.fetch()
    except NetworkAccessDisabledError:
        pytest.skip(
            "Playwright's sync API needs a local loopback socket for its own "
            "driver-process IPC on this platform, which conftest.py's "
            "no-network guard also blocks -- see this test's own docstring."
        )

    assert jobs == []
