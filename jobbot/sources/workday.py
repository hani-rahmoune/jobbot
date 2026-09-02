"""Workday CXS (Candidate Experience Site) adapter.

M8 Part A found Workday is the only enterprise ATS among the 20 large French
corporates investigated with a genuinely public, unauthenticated, stateless
JSON endpoint -- confirmed live against Renault Group, Stellantis, Sanofi,
Michelin, Airbus, and Veolia (see the M8 report). Every other vendor found
(iCIMS, SAP SuccessFactors, Oracle Taleo, Cegid Talentsoft, Phenom People,
Oleeo) either requires OAuth/API-key auth for anything beyond HTML, or its
public career site is a session-bound page rather than a stateless request.

POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
with a JSON body ({"appliedFacets": {}, "limit": N, "offset": M}) returns one
page of postings. The identifier encodes all three of tenant, wd number, and
site (Workday tenants are otherwise ambiguous -- "sanofi" alone doesn't say
whether the site is on wd1, wd3, or wd5, or which of a tenant's several
career sites to use), in the literal form "{tenant}.wd{N}.{site}", e.g.
"sanofi.wd3.SanofiCareers" -- validated in __init__.

Two things the list endpoint does NOT provide, unlike every other adapter:

- No description. Fetching each posting's own detail page individually
  would multiply the request count by the total posting count -- for a
  board the size of Airbus's (2000+ reported postings) that's thousands of
  extra requests per poll, directly against Part C's volume-control goal.
  classify_contract_type() is called with an empty description; title alone
  carries the classification for Workday postings, same authoritative
  weight it always has, and is the dominant real-world signal anyway
  ("Stage Analyste (F/H)", "Alternance ...").
- No usable posted_at. `postedOn` is a relative, human-phrased string
  ("Posted 6 Days Ago", "Posted Today"), not a timestamp -- parsing it into
  an approximate date would be both fragile and pointless, since CLAUDE.md
  rule 5 already never uses a source-reported date for freshness. posted_at
  is left None; the field remains display-only exactly as documented on
  Job.posted_at, just with nothing to display for this vendor.

Pagination (A2/C1): pages of PAGE_SIZE, until a page returns fewer than
PAGE_SIZE (the real last page) or MAX_PAGES is reached (a hard backstop so
one runaway board can't stall a poll) or `max_postings` is reached (default
2000, overridable per instance) -- whichever comes first. Hitting either cap
logs a warning rather than raising; a partial fetch is still a fetch.

PAGE_SIZE=20 is not a stylistic choice, it's a hard Workday platform limit:
`limit` values above 20 return HTTP 400 (confirmed empirically against all
six tenants verified for the M8 report -- 20 succeeds, 21 does not; an
earlier draft of this adapter assumed 100 based on the response *shape* seen
while sampling with a small limit during Part A research, and every real
Workday source failed outright against it until this was caught during the
Part C timing run).

MAX_PAGES=20, exactly as specified, therefore caps a single source at 400
postings (20 pages * 20/page) -- well short of `max_postings`' default of
2000. That is deliberate, not an oversight: the Part C timing run showed
real per-page latency of roughly 1 second against these tenants, so a
source that actually reached 2000 postings (Michelin and Airbus both did,
before this was measured) took 100+ seconds each -- one employer entirely
capable of stalling a poll, which is precisely what Part A's page cap
exists to prevent. At the real page size, the 20-page cap is what actually
bounds worst-case time (~20-25s observed) for a huge board today;
`max_postings` stays as specified as the second, independent safety net
(e.g. if Workday ever raises the per-page limit), even though it isn't the
binding constraint for any of the six sources confirmed for this milestone.
"""

from __future__ import annotations

import logging
import re

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page
PAGE_SIZE = 20  # hard Workday platform cap -- see module docstring
MAX_PAGES = 20  # per Part A; the binding real-world cap at this page size -- see module docstring
DEFAULT_MAX_POSTINGS = 2000

_IDENTIFIER_RE = re.compile(r"^([a-zA-Z0-9-]+)\.wd(\d+)\.([a-zA-Z0-9_-]+)$")


class WorkdaySource(JobSource):
    name = "workday"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        max_postings: int = DEFAULT_MAX_POSTINGS,
    ) -> None:
        match = _IDENTIFIER_RE.match(identifier)
        if not match:
            raise ValueError(
                f"workday: identifier must be '{{tenant}}.wd{{N}}.{{site}}' "
                f"(e.g. 'sanofi.wd3.SanofiCareers'), got {identifier!r}"
            )
        self._tenant, self._wd_number, self._site = match.groups()
        super().__init__(identifier, company_name, client, user_agent)
        self.max_postings = max_postings

    def _board_url(self) -> str:
        return (
            f"https://{self._tenant}.wd{self._wd_number}.myworkdayjobs.com"
            f"/wday/cxs/{self._tenant}/{self._site}/jobs"
        )

    def _base_job_url(self) -> str:
        return f"https://{self._tenant}.wd{self._wd_number}.myworkdayjobs.com/{self._site}"

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = self._board_url()
        headers = {"User-Agent": self.user_agent}
        all_postings: list[dict] = []
        offset = 0
        hit_page_cap = True

        for _page in range(1, MAX_PAGES + 1):
            page_postings = self._fetch_page(url, headers, offset)
            all_postings.extend(page_postings)

            if len(all_postings) >= self.max_postings:
                logger.warning(
                    "workday: %s (%s) hit max_postings=%d, stopping pagination early "
                    "(more postings may exist)",
                    self.company_name, self.identifier, self.max_postings,
                )
                all_postings = all_postings[: self.max_postings]
                hit_page_cap = False
                break

            if len(page_postings) < PAGE_SIZE:
                hit_page_cap = False
                break

            offset += PAGE_SIZE

        if hit_page_cap:
            logger.warning(
                "workday: %s (%s) hit the %d-page cap, more postings may exist",
                self.company_name, self.identifier, MAX_PAGES,
            )

        if not all_postings:
            raise SourceEmptyError(
                f"workday: {self.company_name} ({self.identifier}) returned zero postings"
            )

        return all_postings, None

    def _fetch_page(self, url: str, headers: dict[str, str], offset: int) -> list[dict]:
        """One page, with the same retry-once-on-5xx/timeout contract every
        other adapter follows -- applied per page, since a mid-pagination
        blip shouldn't discard pages already fetched."""
        body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}

        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.post(
                    url, json=body, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "workday: timeout fetching %s (offset %d) for %s (attempt %d/%d)",
                    url, offset, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"workday: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "workday: HTTP %d fetching %s (offset %d) for %s (attempt %d/%d)",
                    response.status_code, url, offset, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"workday: failed to fetch {url} (offset {offset}) for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"workday: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"workday: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        payload = response.json()
        return payload.get("jobPostings", [])

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "workday: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        title = entry["title"]
        external_path = entry["externalPath"]
        if not external_path:
            raise ValueError("externalPath is empty")
        url = self._base_job_url() + external_path
        location = entry.get("locationsText") or ""

        # No description and no reliable employment-type field in the list
        # response (see module docstring) -- title carries classification.
        contract_type = classify_contract_type(title, "", "")

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=None,  # only a relative string is available; see module docstring
            description="",
            source=self.name,
            external_id=external_path,
        )
