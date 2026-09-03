"""Headless-browser adapter, for the genuinely JavaScript-only, opt-in.

M9e: every other adapter in this codebase either calls a real API, scrapes
server-rendered HTML, or discovers individual pages via a sitemap -- all of
them fetch with the injected httpx.Client and never execute JavaScript. This
adapter is the deliberate, narrow exception: for an employer whose listing
page has no API, no sitemap, and no JobPosting JSON-LD anywhere (confirmed
by exhausting every other option first -- see the M9e report for exactly
which employers and why), a real headless browser is the only thing left
that reads the page the way a person would.

This means fetch_raw() cannot use `self.client` the way every other
adapter's does -- Playwright drives its own browser process with its own
networking stack, incompatible with handing it an httpx.Client. `self.client`
is still used for the one thing it can be here: the robots.txt check, kept
identical to jsonld.py's and sitemap_jsonld.py's, so this adapter obeys
robots.txt through the exact same mechanism as every other one. The
browser's own requests carry `self.user_agent` (set on the browser context),
so the honest User-Agent-with-contact-URL requirement holds for the real
page load too, even though it isn't literally the injected client making it.

Playwright is an optional dependency (pyproject.toml's `playwright` extra) --
imported lazily, inside fetch_raw() only, never at module import time, so
`import jobbot.sources.rendered` (and everything that transitively imports
it, e.g. jobbot/run.py registering every adapter) succeeds with Playwright
never installed. Only calling fetch_raw() on a "rendered" source without it
installed raises SourceError, with a message that says so plainly.

Identifier is "{listing_url}" or "{listing_url}|{css_selector}" -- the
selector is the fallback path, used only when no JobPosting JSON-LD is
found anywhere on the rendered page (see parse()'s docstring for why the
selector-based extraction is necessarily far less structured than every
other adapter's).

Volume control (M9e, Part C-equivalent for this adapter): a real browser
launch is drastically heavier than an HTTP request -- run.py caps the
number of "rendered" sources actually fetched in one poll at
MAX_RENDERED_SOURCES_PER_POLL and schedules every one of them after every
non-rendered source, so a handful of slow, heavy browser launches never
delay the cheap sources that make up the vast majority of a real poll.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

PAGE_LOAD_TIMEOUT_MS = 30_000
MAX_RENDERED_SOURCES_PER_POLL = 10

_LD_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_FIELDS = ("addressLocality", "addressRegion", "addressCountry")


class RenderedSource(JobSource):
    name = "rendered"
    tier = 1
    first_party = True

    def __init__(
        self, identifier: str, company_name: str, client: httpx.Client, user_agent: str
    ) -> None:
        url, _, selector = identifier.partition("|")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"rendered: identifier must be an https URL, optionally followed by "
                f"'|{{css selector}}' (e.g. 'https://example.com/jobs|.job-card'), "
                f"got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._url = url
        self._selector = selector or None
        self._robots = RobotsCache(client, user_agent)

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        if not self._robots.allowed(self._url):
            raise SourceError(
                f"rendered: robots.txt disallows fetching {self._url} for {self.company_name}"
            )

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceError(
                "rendered: the 'playwright' package is not installed -- install the "
                "optional 'playwright' extra (see pyproject.toml) and run "
                "`playwright install chromium` to use this source"
            ) from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(user_agent=self.user_agent)
                    page.goto(
                        self._url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS
                    )
                    html = page.content()

                    raw_items = _extract_jsonld_postings(html)
                    if not raw_items and self._selector:
                        raw_items = _extract_via_selector(page, self._selector, self._url)
                finally:
                    browser.close()
        except PlaywrightError as exc:
            raise SourceError(
                f"rendered: failed to render {self._url} for {self.company_name}: {exc}"
            ) from exc

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- a genuinely empty board, or a page that
        # rendered fine but matched nothing, is not itself an error.
        return raw_items, None

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "rendered: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        if entry.get("kind") == "jsonld":
            return self._parse_jsonld_entry(entry["posting"])
        return self._parse_card_entry(entry)

    def _parse_jsonld_entry(self, posting: dict) -> Job:
        """Same schema.org JobPosting field mapping every other JSON-LD-
        capable adapter in this codebase uses (jsonld.py, sitemap_jsonld.py)."""
        title = posting["title"]
        url = posting.get("url") or self._url
        location = _extract_location(posting.get("jobLocation"))
        employment_hint = posting.get("employmentType") or ""
        if isinstance(employment_hint, list):
            employment_hint = " ".join(str(value) for value in employment_hint)
        description = strip_html(posting.get("description") or "")
        contract_type = classify_contract_type(title, description, str(employment_hint))

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=posting.get("datePosted"),
            description=description,
            source=self.name,
            external_id=str(posting.get("identifier") or url),
        )

    def _parse_card_entry(self, entry: dict) -> Job:
        """The CSS-selector fallback (used only when the page carries no
        JobPosting JSON-LD at all): far less structured than every other
        adapter, deliberately -- a generic "matched element" has no known
        internal layout to rely on the way a named ATS's own markup does.
        The card's own first line of text is the title (a real, if
        imperfect, proxy: nearly every job-listing card design leads with
        the job title), the full card text is the description (so
        classify_contract_type() still has real prose to work with even
        though no field boundaries are known), and the card's own link
        (or, failing that, the listing page itself) is the URL.
        """
        text = entry.get("text") or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else ""
        if not title:
            raise ValueError("card has no visible text to use as a title")

        url = entry.get("url") or self._url
        description = "\n".join(lines[1:])
        contract_type = classify_contract_type(title, description, "")

        return Job(
            company=self.company_name,
            title=title,
            location="",
            contract_type=contract_type,
            url=url,
            posted_at=None,
            description=description,
            source=self.name,
            external_id=entry.get("url") or f"{self._url}#{hash(text)}",
        )


def _extract_jsonld_postings(html: str) -> list[dict]:
    postings: list[dict] = []
    for match in _LD_JSON_BLOCK_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        for entry in _job_postings_from_block(parsed):
            postings.append({"kind": "jsonld", "posting": entry})
    return postings


def _job_postings_from_block(parsed: Any) -> list[dict]:
    if isinstance(parsed, dict) and "@graph" in parsed:
        candidates = parsed["@graph"]
    elif isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = [parsed]

    result = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "JobPosting" in types:
            result.append(item)
    return result


def _extract_via_selector(page: Any, selector: str, base_url: str) -> list[dict]:
    """Uses the LIVE page's own real CSS engine (Playwright's
    query_selector_all), while the browser is still open -- this is the one
    place in this adapter that isn't parse()'s job, precisely because it
    needs the live page, not just already-fetched HTML text. Everything
    this returns is a plain dict; the browser is never touched again once
    fetch_raw() returns."""
    cards = []
    for element in page.query_selector_all(selector):
        text = element.inner_text()
        link = element.query_selector("a")
        href = link.get_attribute("href") if link else None
        url = _absolute_url(base_url, href) if href else None
        cards.append({"kind": "card", "text": text, "url": url})
    return cards


def _absolute_url(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href)


def _extract_location(job_location: Any) -> str:
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if not isinstance(job_location, dict):
        return ""

    address = job_location.get("address")
    if isinstance(address, str):
        return address
    if not isinstance(address, dict):
        return ""

    parts = [address.get(field) for field in _ADDRESS_FIELDS]
    return ", ".join(part for part in parts if part)
