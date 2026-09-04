"""Eightfold AI adapter.

M14 Part A: Eightfold is a talent/recruiting platform (identifiable by its
own X-EF-* response headers and eightfold.ai/eightfold-gov.ai CSP entries --
see discovery/probe_vendor.py, which checks these alongside the RMK/Jobs2Web
signals, one probe entry point for both rather than two scripts). Found via
Kering's real board (careers.kering.com), previously misidentified in M13 as
a rendered.py candidate -- it isn't; it has a real, public, unauthenticated,
paginated JSON search API, robots.txt-allowed.

Discovered by capturing real browser network traffic against a job detail
page (Playwright, not curl -- the page is a client-rendered SPA, so its own
requests are only visible with real JS execution), not by guessing paths:

- GET /api/pcsx/search?domain={domain}&query={term}&location=&start={offset}
  returns {"data": {"positions": [...], "count": N}} -- a paginated list of
  position summaries (id, displayJobId, name, locations, department, ...),
  10 per page on the one tenant checked, `count` is the real total. `query`
  genuinely narrows server-side (confirmed live on Kering's own board:
  "alternance" -> 6 real French postings, "stage" -> 117, "internship" ->
  29, "apprenticeship" -> 0) -- the same "search_terms narrows server-side"
  contract every other search-capable adapter in this codebase already has.
- GET /api/pcsx/position_details?position_id={id}&domain={domain} returns
  the full posting, including `jobDescription` (rich HTML) and both
  `locations` (human-readable) and `standardizedLocations` (a clean
  "City, Region, CC" string) -- confirmed live, no authentication needed.

Both are under /api/pcsx, which Kering's own robots.txt explicitly Allows
(inside a "Disallow: / then selective Allow:" structure -- see M14's fix to
jobbot/sources/robots.py, without which every one of these paths reads as
disallowed regardless of what the file actually says).
/careerhub/explore/jobs, despite also being Allowed, redirects straight to
a login page and was not used; neither was /api/career_hub (404 on every
path tried).

`domain` is a required, validated parameter distinct from the careers host
itself (confirmed live: omitting it errors with "Missing data for required
field"; a wrong value returns a non-JSON response) -- Eightfold is
multi-tenant infrastructure, and this is how it resolves which employer's
board to search. It is NOT reliably derivable from the careers host (a
"careers." prefix strip happens to work for Kering but isn't guaranteed to
generalize to every tenant's own subdomain convention), so the identifier
carries both pieces explicitly: "{careers_base_url}|{domain}", e.g.
"https://careers.kering.com|kering.com".

Volume control mirrors talentsoft.py's shape closely: with search_terms
configured, one query per term, results deduplicated by position id across
terms; page size is measured from page 1's own result count rather than
assumed a platform-wide constant (confirmed 10 on Kering, not asserted
beyond that -- the same reasoning talentsoft.py's own docstring gives for
the identical choice on that adapter). Without search_terms, an empty
query paginates the whole board, capped like every other adapter's plain-
pagination fallback.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page fetched

MAX_PAGES = 40  # plain full-board crawl, no search_terms
# One narrowed query, still potentially large -- Kering's own "stage" alone
# was 117 results (~12 pages at 10/page).
MAX_PAGES_PER_SEARCH_TERM = 15

_SEARCH_PATH = "/api/pcsx/search"
_DETAILS_PATH = "/api/pcsx/position_details"


class EightfoldSource(JobSource):
    name = "eightfold"
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
        base_url, sep, domain = identifier.partition("|")
        parsed = urlsplit(base_url)
        if not sep or not domain or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"eightfold: identifier must be 'https://{{careers host}}|{{domain}}' "
                f"(e.g. 'https://careers.kering.com|kering.com'), got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._base_url = base_url.rstrip("/")
        self._domain = domain
        self._robots = RobotsCache(client, user_agent)
        self.search_terms = list(search_terms) if search_terms is not None else []

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        search_url = f"{self._base_url}{_SEARCH_PATH}"
        if not self._robots.allowed(search_url):
            raise SourceError(
                f"eightfold: robots.txt disallows fetching {search_url} for {self.company_name}"
            )

        if self.search_terms:
            position_ids = self._search_by_terms()
        else:
            position_ids = self._search_all(max_pages=MAX_PAGES)

        postings: list[dict] = []
        for position_id in position_ids:
            entry = self._fetch_details(position_id)
            if entry is not None:
                postings.append(entry)

        # M8b: zero results is a valid, non-failing outcome -- a genuinely
        # quiet board, or every match failing to fetch its own details, is
        # not itself an error.
        return postings, None

    def _search_by_terms(self) -> list[int]:
        ids_seen: dict[int, None] = {}  # dict, not set: keeps first-seen order
        for term in self.search_terms:
            for position_id in self._search_all(max_pages=MAX_PAGES_PER_SEARCH_TERM, query=term):
                ids_seen.setdefault(position_id, None)
        return list(ids_seen)

    def _search_all(self, max_pages: int, query: str = "") -> list[int]:
        first_page, total = self._fetch_search_page(query=query, start=0)
        ids = [p["id"] for p in first_page if "id" in p]
        if total is None or not first_page:
            return ids

        page_size = len(first_page)
        total_pages = min(max_pages, math.ceil(total / page_size))
        if math.ceil(total / page_size) > max_pages:
            logger.warning(
                "eightfold: %s (%s) query %r hit the %d-page cap, more postings may "
                "exist (total reported: %d)",
                self.company_name, self.identifier, query, max_pages, total,
            )

        for page in range(1, total_pages):
            positions, _ = self._fetch_search_page(query=query, start=page * page_size)
            ids.extend(p["id"] for p in positions if "id" in p)

        return ids

    def _fetch_search_page(self, query: str, start: int) -> tuple[list[dict], int | None]:
        params = {"domain": self._domain, "query": query, "location": "", "start": start}
        response = self._get(f"{self._base_url}{_SEARCH_PATH}", params)
        try:
            payload = response.json()
            positions = payload["data"]["positions"]
            total = payload["data"].get("count")
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "eightfold: %s (%s) returned an unparseable search response for query %r",
                self.company_name, self.identifier, query,
            )
            return [], None
        return positions, total

    def _fetch_details(self, position_id: int) -> dict | None:
        """A malformed/unreachable individual posting must not crash the
        whole batch -- skipped with a logged warning, same spirit as every
        other per-page-fetch adapter in this codebase."""
        params = {"position_id": position_id, "domain": self._domain}
        try:
            response = self._get(f"{self._base_url}{_DETAILS_PATH}", params)
        except SourceError as exc:
            logger.warning(
                "eightfold: skipping unreachable position %s for %s: %s",
                position_id, self.company_name, exc,
            )
            return None
        try:
            return response.json()["data"]
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "eightfold: %s (%s) returned an unparseable details response for position %s",
                self.company_name, self.identifier, position_id,
            )
            return None

    def _get(self, url: str, params: dict[str, object]) -> httpx.Response:
        """One request, with the same retry-once-on-5xx/timeout contract
        every other adapter in this codebase follows."""
        headers = {"User-Agent": self.user_agent}
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
                    "eightfold: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"eightfold: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "eightfold: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"eightfold: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"eightfold: not found for {self.company_name} ({url}): returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"eightfold: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        return response

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "eightfold: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        title = entry["name"]
        url = entry.get("publicUrl") or f"{self._base_url}{entry.get('positionUrl', '')}"
        location = _extract_location(entry)
        description = strip_html(entry.get("jobDescription") or "")
        contract_type = classify_contract_type(title, description, "")
        posted_at = _epoch_seconds_to_utc(entry.get("postedTs") or entry.get("creationTs"))

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=posted_at,
            description=description,
            source=self.name,
            external_id=str(entry.get("displayJobId") or entry["id"]),
        )


def _extract_location(entry: dict) -> str:
    """`standardizedLocations` (a clean "City, Region, CC" string) is
    preferred over `locations` (the same place, written out in full by a
    human), used as a fallback only when the standardized field is
    absent."""
    standardized = entry.get("standardizedLocations")
    if isinstance(standardized, list) and standardized:
        return str(standardized[0])
    locations = entry.get("locations")
    if isinstance(locations, list) and locations:
        return str(locations[0])
    return ""


def _epoch_seconds_to_utc(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC)
