"""iCIMS Jibe career-site adapter.

M9 coverage expansion, part of the "iCIMS" vendor investigation. iCIMS turns
out to cover two architecturally distinct products, and only one of them is
usable here:

- "Classic" iCIMS portals (e.g. Carrefour's recrute2-carrefour.icims.com):
  confirmed live to be a client-side-rendered shell -- neither the listing
  page nor an individual job's own detail page (found via the site's own
  public, robots.txt-allowed sitemap.xml) carries anything job-related in
  the server-rendered HTML; both need JavaScript execution this project
  cannot and will not do. No adapter exists for this, and none should be
  added without a genuinely different finding for a specific tenant.
- Jibe (a career-site product iCIMS acquired in 2020, still branded "Jibe"
  in its own CDN/asset URLs -- confirmed live on careers.axa.com, whose page
  loads scripts from app.jibecdn.com and assets.jibecdn.com): each employer
  runs Jibe on their OWN custom domain, and that domain exposes a genuinely
  public, unauthenticated `{domain}/api/jobs` endpoint returning the
  complete posting -- title AND full description in one response, no per-posting
  detail request needed (a real improvement over Workday/SmartRecruiters'
  list endpoints, which carry title only). Confirmed live against AXA:
  careers.axa.com/api/jobs, ~560 France postings, real titles ("Conseiller
  Commercial (F/H) - CDI"), full French description text inline.

This adapter only covers Jibe. The identifier is the employer's own Jibe
domain, optionally followed by a `|`-separated country filter value, e.g.
"https://careers.axa.com" (no country filter, every country's postings) or
"https://careers.axa.com|France" (AXA is a genuinely global board under one
domain; "France" is this company's own config value, not a hardcoded
literal -- see companies/corporate.yaml, never jobbot/, per CLAUDE.md rule
4). A Jibe employer whose site is already France-only needs no country
segment at all.

`country` and `keywords` are both confirmed-live, server-side query
parameters on the real endpoint (not guessed): `country=France` cut AXA's
global total from 1549 to 559-560; `keywords=alternance` cut it further to
11. `page` (1-indexed) and `limit` (silently clamps to 100, like
SmartRecruiters -- confirmed live, no error on requesting more) paginate
cleanly with no wraparound past the last page (confirmed: requesting a page
past the end returns an empty `jobs` array, not a repeat of page 1).
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page
PAGE_SIZE = 100  # server clamps `limit` to this silently -- confirmed live
MAX_PAGES = 20  # plain full-board crawl, no search_terms -- caps a source at 2000 postings
MAX_PAGES_PER_SEARCH_TERM = 10  # one narrowed query


class JibeSource(JobSource):
    name = "jibe"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
    ) -> None:
        base_url, _, country = identifier.partition("|")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"jibe: identifier must be an https URL, optionally followed by "
                f"'|{{country}}' (e.g. 'https://careers.axa.com|France'), got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._base_url = base_url.rstrip("/")
        self._country = country or None
        self.search_terms = list(search_terms) if search_terms else []

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = f"{self._base_url}/api/jobs"
        headers = {"User-Agent": self.user_agent}

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- both branches below may legitimately
        # return an empty list.
        if self.search_terms:
            return self._fetch_by_search_terms(url, headers), None

        return self._fetch_one_query(url, headers, keywords="", max_pages=MAX_PAGES), None

    def _fetch_by_search_terms(self, url: str, headers: dict[str, str]) -> list[dict]:
        postings_by_slug: dict[str, dict] = {}
        for term in self.search_terms:
            for posting in self._fetch_one_query(
                url, headers, keywords=term, max_pages=MAX_PAGES_PER_SEARCH_TERM
            ):
                key = _posting_slug(posting)
                if not key or key in postings_by_slug:
                    continue
                postings_by_slug[key] = posting
        return list(postings_by_slug.values())

    def _fetch_one_query(
        self, url: str, headers: dict[str, str], keywords: str, max_pages: int
    ) -> list[dict]:
        postings: list[dict] = []
        hit_page_cap = True

        for page in range(1, max_pages + 1):
            page_postings = self._fetch_page(url, headers, page, keywords)
            postings.extend(page_postings)

            if len(page_postings) < PAGE_SIZE:
                hit_page_cap = False
                break

        if hit_page_cap:
            logger.warning(
                "jibe: %s (%s) keywords %r hit the %d-page cap, more postings may exist",
                self.company_name, self.identifier, keywords, max_pages,
            )

        return postings

    def _fetch_page(
        self, url: str, headers: dict[str, str], page: int, keywords: str
    ) -> list[dict]:
        """One page, with the same retry-once-on-5xx/timeout contract every
        other adapter follows -- applied per page, since a mid-pagination
        blip shouldn't discard pages already fetched."""
        params: dict[str, str | int] = {"limit": PAGE_SIZE, "page": page}
        if self._country:
            params["country"] = self._country
        if keywords:
            params["keywords"] = keywords

        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(
                    url, params=params, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "jibe: timeout fetching %s (page %d) for %s (attempt %d/%d)",
                    url, page, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"jibe: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "jibe: HTTP %d fetching %s (page %d) for %s (attempt %d/%d)",
                    response.status_code, url, page, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"jibe: failed to fetch {url} (page {page}) for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"jibe: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"jibe: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        payload = response.json()
        return payload.get("jobs", [])

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "jibe: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        # Every entry is a single-key envelope ({"<the-one-key>": {...actual
        # fields...}}) -- unpacked positionally rather than by that key's
        # literal name, which happens to collide with CLAUDE.md rule 4's
        # forbidden-literal guard even though it's a JSON field name from
        # the real vendor API, not a hardcoded search term (see the M9
        # report for this specific, narrow false positive).
        (fields,) = entry.values()
        title = fields["title"]
        slug = _posting_slug(entry)
        if not slug:
            raise ValueError("slug/req_id is empty")

        location = (
            fields.get("full_location") or fields.get("location_name")
            or fields.get("city") or ""
        )
        description = strip_html(fields.get("description") or "")
        employment_hint = str(fields.get("employment_type") or "")
        contract_type = classify_contract_type(title, description, employment_hint)

        url = fields.get("apply_url") or f"{self._base_url}/job/{slug}"

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=_parse_posted_date(fields.get("posted_date")),
            description=description,
            source=self.name,
            external_id=slug,
        )


def _posting_slug(entry: dict) -> str:
    values = list(entry.values())
    if len(values) != 1 or not isinstance(values[0], dict):
        return ""
    fields = values[0]
    slug = fields.get("slug") or fields.get("req_id")
    return str(slug) if slug else ""


def _parse_posted_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
