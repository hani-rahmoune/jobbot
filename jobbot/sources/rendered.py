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

M15 Part B: a second identifier shape, "sitemap:{sitemap_url}|{title_selector}
|{content_selector}", for the shape sitemap_jsonld.py exists to solve but
this adapter originally didn't: TotalEnergies (jobs.totalenergies.com) has a
real, robots.txt-allowed sitemap with hundreds of individual job page URLs
(the M13/M14 false negative -- ruled out purely by the robots.txt parser bug
fixed in M14 Part A prep), each one an Avature JobDetail page carrying only
an og:title server-side; the real body only exists once rendered. Reuses
SitemapDiscovery for the exact same candidate-narrowing pipeline every other
sitemap-based adapter uses (search_terms, slug vocabulary, sample, and M14
Part C's location narrowing), then launches ONE browser for the whole fetch,
reusing a single page object across every selected URL (a fresh page per URL
would pay browser-context setup on every navigation for no benefit -- respx-
style test isolation is irrelevant here since nothing but the real Playwright
process is ever touched).

`title_selector` and `content_selector` are deliberately two separate CSS
selectors, not one card selector like the single-page mode above -- a job
DETAIL page is one whole page describing one posting, not a list of many
posting "cards", so the single-page mode's "first line of card text is the
title" heuristic does not apply here at all (confirmed live: an Avature
JobDetail page's own main content region opens with field labels like
"Country"/"Area", not a title). See parse()'s _split_labeled_fields() for
how the content region's own label/value header block (Country, Area,
Workplace location, Domain, Type of contract, Contract duration, Experience
-- Avature's own standard English-locale JobDetail template fields, not a
user search preference, the same CLAUDE.md rule 4 exemption every other
adapter's own structural vocabulary already has) is separated from the free-
text description that follows it.

Volume control for sitemap mode specifically: DEFAULT_RENDERED_SITEMAP_PAGE_CAP
is far lower than sitemap_jsonld's own DEFAULT_PAGE_CAP (150) -- a real
browser page load is roughly two orders of magnitude slower than an httpx
GET (confirmed live: ~3-5s per TotalEnergies job page vs sub-100ms for a
typical httpx fetch), so the same cap would make a single "rendered" source
dominate the entire poll's wall-clock. Location narrowing (M14 Part C) is
the intended way to make the pre-cap candidate set small enough that the cap
rarely binds at all, rather than raising the cap itself.
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
from jobbot.sources.sitemap_discovery import DEFAULT_SAMPLE_SIZE, SitemapDiscovery

logger = logging.getLogger(__name__)

PAGE_LOAD_TIMEOUT_MS = 30_000
MAX_RENDERED_SOURCES_PER_POLL = 10

_LD_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_FIELDS = ("addressLocality", "addressRegion", "addressCountry")

# M15 Part B: sitemap-mode identifiers start with this prefix, distinguishing
# them from the original "{listing_url}[|{selector}]" shape -- an identifier
# never otherwise starts with a scheme-less literal, so there's no ambiguity
# with a real https URL.
_SITEMAP_MODE_PREFIX = "sitemap:"

# M15 Part B: far lower than sitemap_jsonld's DEFAULT_PAGE_CAP (150) -- see
# the module docstring's Volume control section for why.
DEFAULT_RENDERED_SITEMAP_PAGE_CAP = 40

# M15 Part B: Avature's own standard English-locale JobDetail template field
# labels (confirmed live against jobs.totalenergies.com's /en_US/ pages) --
# structural vocabulary this adapter recognizes to separate the page's own
# metadata header from its free-text body, not a user search preference.
# Same CLAUDE.md rule 4 exemption as DEFAULT_JOB_PATH_MARKERS/
# DEFAULT_SLUG_VOCABULARY (sitemap_discovery.py) and classify.py's own
# contract-type vocabulary.
_RENDERED_SITEMAP_LEADING_LABELS = (
    "Country", "Area", "Workplace location", "Domain",
    "Type of contract", "Contract duration", "Experience",
)
# The label that marks the end of the metadata header and the start of the
# free-text description.
_RENDERED_SITEMAP_DESCRIPTION_START = "Activities"
# Labels that mark the end of the free-text description -- generic page
# chrome (share buttons, a repeated company boilerplate blurb) that follows
# every posting on this template and carries no per-job information.
_RENDERED_SITEMAP_DESCRIPTION_END_MARKERS = ("Additional Information",)


class RenderedSource(JobSource):
    name = "rendered"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
        locations: list[str] | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        page_cap: int = DEFAULT_RENDERED_SITEMAP_PAGE_CAP,
    ) -> None:
        self._sitemap_mode = identifier.startswith(_SITEMAP_MODE_PREFIX)

        if self._sitemap_mode:
            rest = identifier[len(_SITEMAP_MODE_PREFIX) :]
            parts = rest.split("|")
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    f"rendered: sitemap-mode identifier must be "
                    f"'{_SITEMAP_MODE_PREFIX}{{sitemap_url}}|{{title_selector}}|"
                    f"{{content_selector}}' (e.g. "
                    f"'{_SITEMAP_MODE_PREFIX}https://example.com/sitemap.xml|h2|main'), "
                    f"got {identifier!r}"
                )
            sitemap_url, title_selector, content_selector = parts
            parsed = urlsplit(sitemap_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(
                    f"rendered: sitemap-mode identifier's sitemap_url must be a full "
                    f"https URL, got {sitemap_url!r}"
                )
            super().__init__(identifier, company_name, client, user_agent)
            self._sitemap_url = sitemap_url
            self._title_selector = title_selector
            self._content_selector = content_selector
            self._url = None
            self._selector = None
            self._robots = RobotsCache(client, user_agent)
            self._discovery = SitemapDiscovery(
                client,
                user_agent,
                company_name,
                source_name=self.name,
                search_terms=search_terms,
                locations=locations,
                sample_size=sample_size,
                page_cap=page_cap,
            )
            return

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
        if self._sitemap_mode:
            return self._fetch_raw_sitemap_mode()
        return self._fetch_raw_single_page()

    def _fetch_raw_single_page(self) -> tuple[list[dict], str | None]:
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

    def _fetch_raw_sitemap_mode(self) -> tuple[list[dict], str | None]:
        """M15 Part B. Only the sitemap index URL itself is robots-checked --
        every individual job page URL comes from that same already-allowed
        sitemap and matches the same path pattern (confirmed live for
        TotalEnergies: /en_US/careers/JobDetail/... falls under the explicit
        `Allow: /*/careers` rule), the identical precedent sitemap_jsonld.py
        already established for its own per-page fetches."""
        if not self._robots.allowed(self._sitemap_url):
            raise SourceError(
                f"rendered: robots.txt disallows fetching {self._sitemap_url} "
                f"for {self.company_name}"
            )

        urls_to_fetch = self._discovery.discover_job_urls(self._sitemap_url)

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceError(
                "rendered: the 'playwright' package is not installed -- install the "
                "optional 'playwright' extra (see pyproject.toml) and run "
                "`playwright install chromium` to use this source"
            ) from exc

        raw_items: list[dict] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    # One page object, reused across every URL -- a fresh
                    # page per navigation would pay browser-context setup
                    # over and over for no benefit, on top of an already
                    # slow per-page cost (see the module docstring).
                    page = browser.new_page(user_agent=self.user_agent)
                    for job_url in urls_to_fetch:
                        raw_items.extend(self._render_one_job_page(page, job_url))
                finally:
                    browser.close()
        except PlaywrightError as exc:
            raise SourceError(
                f"rendered: failed to render {self._sitemap_url} for "
                f"{self.company_name}: {exc}"
            ) from exc

        # M8b: zero results is a valid, non-failing outcome -- see above.
        return raw_items, None

    def _render_one_job_page(self, page: Any, job_url: str) -> list[dict]:
        """A malformed/unreachable individual job page must not crash the
        whole batch -- skipped with a logged warning, same spirit as
        sitemap_jsonld.py's own per-page fetch handling."""
        from playwright.sync_api import Error as PlaywrightError

        try:
            page.goto(job_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
            title_element = page.query_selector(self._title_selector)
            content_element = page.query_selector(self._content_selector)
        except PlaywrightError as exc:
            logger.warning(
                "rendered: skipping unreachable job page for %s (%s): %s",
                self.company_name, job_url, exc,
            )
            return []

        if title_element is None or content_element is None:
            logger.warning(
                "rendered: skipping %s for %s: title or content selector matched nothing",
                job_url, self.company_name,
            )
            return []

        return [
            {
                "kind": "rendered_sitemap_page",
                "url": job_url,
                "title": title_element.inner_text(),
                "content": content_element.inner_text(),
            }
        ]

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
        kind = entry.get("kind")
        if kind == "jsonld":
            return self._parse_jsonld_entry(entry["posting"])
        if kind == "rendered_sitemap_page":
            return self._parse_rendered_sitemap_entry(entry)
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

    def _parse_rendered_sitemap_entry(self, entry: dict) -> Job:
        """M15 Part B. Unlike _parse_card_entry above, this page shape is
        known and stable enough (one Avature template, confirmed live) to
        extract real structured fields, not just a title-plus-free-text
        guess -- see _split_labeled_fields()."""
        title = (entry.get("title") or "").strip()
        if not title:
            raise ValueError("rendered sitemap page has no title")

        url = entry["url"]
        content = entry.get("content") or ""
        fields, description = _split_labeled_fields(content)
        location = ", ".join(
            part
            for part in (
                fields.get("Workplace location"),
                fields.get("Area"),
                fields.get("Country"),
            )
            if part
        )
        employment_hint = fields.get("Type of contract", "")
        contract_type = classify_contract_type(title, description, employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=None,
            description=description,
            source=self.name,
            external_id=url,
        )


def _split_labeled_fields(text: str) -> tuple[dict[str, str], str]:
    """Splits a rendered Avature JobDetail page's main content into its
    leading label/value metadata header (Country, Area, Workplace location,
    ...) and the free-text description that follows -- see the module
    docstring's Part B section for why this template's fields are safe to
    recognize by name.

    Walks the text line by line: any line that's a known label consumes the
    next non-blank line as its value; the walk stops the moment it reaches
    _RENDERED_SITEMAP_DESCRIPTION_START ("Activities"), and everything from
    there up to (excluding) the first description-end marker becomes the
    description. A page that never mentions any of these labels at all (a
    different template, or a load failure that returned something else)
    degrades safely: fields stays empty and the whole text becomes the
    description, rather than raising.
    """
    lines = [line.strip() for line in text.splitlines()]
    fields: dict[str, str] = {}
    i = 0
    n = len(lines)
    found_description_start = False
    while i < n:
        line = lines[i]
        if line == _RENDERED_SITEMAP_DESCRIPTION_START:
            found_description_start = True
            i += 1  # the marker line itself is a section header, not content
            break
        if line in _RENDERED_SITEMAP_LEADING_LABELS:
            j = i + 1
            while j < n and not lines[j]:
                j += 1
            # A label with no real value (Avature leaves some blank) must
            # not swallow the NEXT label's own name as if it were a value --
            # confirmed live on a real TotalEnergies posting missing "Area":
            # without this check, "Area" -> "Domain" (a label name, not a
            # real value) leaked into the location. If the next non-blank
            # line is itself a recognized label or the description marker,
            # this label simply has no value; `i` advances by one only, so
            # the very next loop iteration re-examines that line properly.
            if j < n and lines[j] not in _RENDERED_SITEMAP_LEADING_LABELS and (
                lines[j] != _RENDERED_SITEMAP_DESCRIPTION_START
            ):
                fields[line] = lines[j]
                i = j + 1
                continue
        i += 1

    # "Activities" never appeared at all -- a differently-shaped page (a
    # different template, or a load that landed somewhere unexpected). Fall
    # back to treating the WHOLE text as the description rather than the
    # empty tail the walk above would otherwise leave: the walk still
    # consumed `i` all the way to `n` looking for a marker that wasn't there.
    start = i if found_description_start else 0

    end = n
    for marker in _RENDERED_SITEMAP_DESCRIPTION_END_MARKERS:
        try:
            end = min(end, lines.index(marker, start))
        except ValueError:
            pass

    description = "\n".join(line for line in lines[start:end] if line).strip()
    return fields, description


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
