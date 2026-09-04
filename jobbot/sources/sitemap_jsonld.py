"""Sitemap-discovered, per-page JSON-LD adapter.

M9 Part B3/D3: Phenom People (Orange, Thales) was rejected because its
career-site search widget (`POST {domain}/widgets/search-jobs`) is
genuinely session- and CSRF-token-bound -- confirmed by replicating the
handshake (GET the page, extract the token from the resulting cookie,
resend it as a header) and still getting redirected. That widget stays
unusable. But the search page isn't the only thing the site serves: every
Phenom career site checked publishes a real, robots.txt-listed sitemap
index (`Sitemap:` lines in robots.txt), and every individual job page it
points to is genuinely server-rendered with a complete schema.org
`JobPosting` JSON-LD block -- confirmed live against both Thales
(careers.thalesgroup.com/fr/fr) and Orange (orange.jobs/fr/fr), ~500 real
job URLs each in just the first of several per-locale sub-sitemaps.

This is deliberately a SEPARATE adapter from jsonld.py, not a mode of it:
jsonld.py's contract is one identifier = one listing page containing
however many JobPosting blocks that one page holds, fetched once.
Here there is no listing page with embedded postings at all -- the sitemap
lists hundreds of individual job page URLs, each carrying exactly one
JobPosting. Reusing jsonld.py's single-URL contract wouldn't reach more
than one posting; this adapter's job is specifically the discover-many-
URLs-then-fetch-each-one step jsonld.py was never built for.

M12 Part C: the sitemap-traversal and candidate-narrowing machinery (M11
Part A's three-layer fallback -- search_terms, then a slug vocabulary, then
an evenly-spread sample, each a fallback for when the one before finds
nothing) moved to sitemap_discovery.py, shared with successfactors.py, which
discovers its job pages the identical way. This module still owns
everything downstream of "here is the URL list to fetch": the per-page
fetch, JSON-LD extraction, and field mapping, which is where the two
adapters actually differ (JSON-LD here, lenient itemprop parsing there).
See sitemap_discovery.py's own module docstring for the full three-layer
narrative and the Geodis/Manitou bug it exists to fix -- not repeated here.

The identifier is the employer's sitemap index URL (from robots.txt's own
`Sitemap:` line), e.g. "https://careers.thalesgroup.com/fr/fr/sitemap_index.xml".
"""

from __future__ import annotations

import html
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
from jobbot.sources.sitemap_discovery import (
    DEFAULT_JOB_PATH_MARKERS,
    DEFAULT_PAGE_CAP,
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SLUG_VOCABULARY,
    SitemapDiscovery,
    looks_like_a_job_url,
)
from jobbot.sources.sitemap_discovery import (
    evenly_spread_sample as _evenly_spread_sample,
)

logger = logging.getLogger(__name__)

# Re-exported for backward compatibility -- these constants used to live
# here and existing tests/config still import them from this module.
__all__ = [
    "DEFAULT_JOB_PATH_MARKERS",
    "DEFAULT_PAGE_CAP",
    "DEFAULT_SAMPLE_SIZE",
    "DEFAULT_SLUG_VOCABULARY",
    "SitemapJsonLdSource",
    "_evenly_spread_sample",
]

_LD_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_FIELDS = ("addressLocality", "addressRegion", "addressCountry")


class SitemapJsonLdSource(JobSource):
    name = "sitemap_jsonld"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
        job_path_markers: list[str] | None = None,
        slug_vocabulary: list[str] | None = None,
        locations: list[str] | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        page_cap: int = DEFAULT_PAGE_CAP,
    ) -> None:
        parsed = urlsplit(identifier)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"sitemap_jsonld: identifier must be a full https URL to a sitemap "
                f"(index or plain), got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._robots = RobotsCache(client, user_agent)
        self._discovery = SitemapDiscovery(
            client,
            user_agent,
            company_name,
            source_name=self.name,
            search_terms=search_terms,
            job_path_markers=job_path_markers,
            slug_vocabulary=slug_vocabulary,
            locations=locations,
            sample_size=sample_size,
            page_cap=page_cap,
        )

    # Thin passthroughs -- tests and callers access these as attributes of
    # the adapter itself (established before the M12 extraction), so they
    # stay here rather than forcing every call site to reach into
    # `self._discovery`. One source of truth (the SitemapDiscovery instance)
    # either way.
    @property
    def search_terms(self) -> list[str]:
        return self._discovery.search_terms

    @property
    def job_path_markers(self) -> list[str]:
        return self._discovery.job_path_markers

    @property
    def slug_vocabulary(self) -> list[str]:
        return self._discovery.slug_vocabulary

    @property
    def locations(self) -> list[str]:
        return self._discovery.locations

    @property
    def sample_size(self) -> int:
        return self._discovery.sample_size

    @property
    def page_cap(self) -> int:
        return self._discovery.page_cap

    def _looks_like_a_job_url(self, url: str) -> bool:
        return looks_like_a_job_url(url, self.job_path_markers)

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        if not self._robots.allowed(self.identifier):
            raise SourceError(
                f"sitemap_jsonld: robots.txt disallows fetching {self.identifier} "
                f"for {self.company_name}"
            )

        urls_to_fetch = self._discovery.discover_job_urls(self.identifier)

        postings: list[dict] = []
        for job_url in urls_to_fetch:
            postings.extend(self._fetch_job_posting(job_url))

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- a genuinely quiet board, or every
        # candidate URL's page having no JobPosting block, is not an error.
        return postings, None

    def _fetch_job_posting(self, job_url: str) -> list[dict]:
        """A malformed/unreachable individual job page must not crash the
        whole batch -- skipped with a logged warning, same spirit as
        parse()'s per-entry error handling, just one step earlier since the
        failure here is a fetch, not a field-mapping problem."""
        try:
            html_text = self._discovery.fetch_text(job_url)
        except SourceError as exc:
            logger.warning(
                "sitemap_jsonld: skipping unreachable job page for %s: %s",
                self.company_name, exc,
            )
            return []

        postings = []
        for match in _LD_JSON_BLOCK_RE.finditer(html_text):
            try:
                parsed = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
            for entry in _job_postings_from_block(parsed):
                postings.append({"url": job_url, "posting": entry})
        return postings

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "sitemap_jsonld: skipping malformed entry for %s: %s",
                    self.company_name, exc,
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        page_url = entry["url"]
        posting = entry["posting"]

        title = posting["title"]
        url = posting.get("url") or page_url
        raw_identifier = _extract_identifier_value(posting.get("identifier"))
        external_id = raw_identifier or page_url
        location = _extract_location(posting.get("jobLocation"))
        employment_hint = posting.get("employmentType") or ""
        if isinstance(employment_hint, list):
            employment_hint = " ".join(str(value) for value in employment_hint)
        # Confirmed live on both Thales and Orange: this vendor's own
        # JobPosting JSON-LD carries `description` as HTML that's ALREADY
        # entity-escaped ("&lt;p&gt;" as literal text, not a real tag) --
        # unlike jsonld.py's fixtures, which see real "<p>" tags directly.
        # strip_html() only unescapes entities AFTER stripping tags (so a
        # genuinely-escaped "&lt;script&gt;" in prose text can't be revived
        # into a live tag mid-strip), so without unescaping first here, this
        # vendor's real tags would never be recognized as tags at all and
        # would leak into the stored description as literal "<p>" text.
        description = strip_html(html.unescape(posting.get("description") or ""))
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
            external_id=external_id,
        )


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


def _extract_identifier_value(value: Any) -> str:
    """schema.org's `identifier` is either a plain string or a
    PropertyValue object ({"@type": "PropertyValue", "value": ...})."""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "")
    if value is None:
        return ""
    return str(value)


def _extract_location(job_location: Any) -> str:
    """jobLocation may be a single Place object or a list of them (take the
    first). Build the display string from whichever of locality/region/
    country are actually present, most specific first."""
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
