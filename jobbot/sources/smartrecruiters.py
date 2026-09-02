"""SmartRecruiters Posting API adapter.

M9 coverage expansion: an earlier milestone rejected SmartRecruiters after
seeing `totalFound: 0` for two named test companies (Societe Generale,
Decathlon). That zero was real, not a broken endpoint -- both companies'
public SmartRecruiters boards genuinely have zero open postings right now
(confirmed by fetching their own careers.smartrecruiters.com pages, which
say so themselves). The API itself is exactly what it looks like: SmartRecruiters'
own official, documented, public Posting API
(https://developers.smartrecruiters.com/docs/posting-api), unauthenticated,
meant for exactly this kind of third-party read access -- confirmed live
against three large French employers with real open postings: KIABI (267
postings), Boulanger (209), Ubisoft2 (284).

GET https://api.smartrecruiters.com/v1/companies/{identifier}/postings
returns {"offset": N, "limit": N, "totalFound": N, "content": [...]}. The
identifier is SmartRecruiters' own "company identifier" (the token in a
company's careers.smartrecruiters.com/{identifier} URL, e.g. "KIABI" --
verify it live before adding a company, exactly as required for every other
adapter's identifier).

Two things the list endpoint does NOT provide, same shape of limitation as
Workday's and iCIMS/Jibe's list endpoints:

- No description. The full job description only lives on the per-posting
  detail endpoint (`/postings/{id}`), which would multiply the request
  count by the posting count -- avoided for the same volume-control reason
  Workday's module docstring explains. Title (plus the structured
  typeOfEmployment/experienceLevel hint, see _parse_entry) carries
  classification, same as Workday.
- No canonical browser URL field in the LIST response (`ref` is only the
  API detail-endpoint URL, not a browsable one) -- but a bare
  `https://jobs.smartrecruiters.com/{identifier}/{id}` (no slug needed)
  was confirmed live to resolve to the real posting page, so the URL is
  constructed directly from fields already in hand, no extra request needed.

Pagination: `limit` silently clamps to 100 server-side (confirmed live:
requesting 101 or 200 both come back reporting limit=100) -- there is no
error to catch, so PAGE_SIZE=100 is used directly rather than discovered by
a failed request the way Workday's PAGE_SIZE=20 was. Offset-paginate until
a page returns fewer than PAGE_SIZE (the real last page) or MAX_PAGES is
reached.

Server-side search (M9's search_terms): the `q` parameter is SmartRecruiters'
own documented full-text search ("based on a job title, location"),
confirmed live to narrow real result counts (KIABI: 267 -> 8 for
"alternance"; Boulanger: 209 -> 13; Ubisoft2: 284 -> 4). Same optional,
config-driven, one-query-per-term, dedup-by-id design as Workday's
search_terms -- see jobbot/run.py's build_source() and settings.yaml's
`search_terms`. Unlike Workday, per-company boards here are small (a few
hundred postings, not thousands), so search_terms is a nice-to-have for
consistency and lower per-poll volume, not a hard necessity the way it is
for Workday's largest boards.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type

logger = logging.getLogger(__name__)

BOARD_URL_TEMPLATE = "https://api.smartrecruiters.com/v1/companies/{identifier}/postings"
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page
PAGE_SIZE = 100  # server clamps `limit` to this silently -- see module docstring
MAX_PAGES = 20  # plain full-board crawl, no search_terms -- caps a source at 2000 postings
MAX_PAGES_PER_SEARCH_TERM = 10  # one narrowed query -- these boards are small, see docstring


class SmartRecruitersSource(JobSource):
    name = "smartrecruiters"
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
        super().__init__(identifier, company_name, client, user_agent)
        # Config, not code (CLAUDE.md rule 4) -- see settings.yaml's
        # search_terms. Empty means no narrowing: a plain full-board crawl.
        self.search_terms = list(search_terms) if search_terms else []

    def _board_url(self) -> str:
        return BOARD_URL_TEMPLATE.format(identifier=self.identifier)

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = self._board_url()
        headers = {"User-Agent": self.user_agent}

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- both branches below may legitimately
        # return an empty list.
        if self.search_terms:
            return self._fetch_by_search_terms(url, headers), None

        return self._fetch_one_query(url, headers, query="", max_pages=MAX_PAGES), None

    def _fetch_by_search_terms(self, url: str, headers: dict[str, str]) -> list[dict]:
        postings_by_id: dict[str, dict] = {}
        for term in self.search_terms:
            for posting in self._fetch_one_query(
                url, headers, query=term, max_pages=MAX_PAGES_PER_SEARCH_TERM
            ):
                key = posting.get("id")
                if key is None or key in postings_by_id:
                    continue
                postings_by_id[key] = posting
        return list(postings_by_id.values())

    def _fetch_one_query(
        self, url: str, headers: dict[str, str], query: str, max_pages: int
    ) -> list[dict]:
        postings: list[dict] = []
        offset = 0
        hit_page_cap = True

        for _page in range(1, max_pages + 1):
            page_postings = self._fetch_page(url, headers, offset, query)
            postings.extend(page_postings)

            if len(page_postings) < PAGE_SIZE:
                hit_page_cap = False
                break

            offset += PAGE_SIZE

        if hit_page_cap:
            logger.warning(
                "smartrecruiters: %s (%s) query %r hit the %d-page cap, "
                "more postings may exist",
                self.company_name, self.identifier, query, max_pages,
            )

        return postings

    def _fetch_page(
        self, url: str, headers: dict[str, str], offset: int, query: str
    ) -> list[dict]:
        """One page, with the same retry-once-on-5xx/timeout contract every
        other adapter follows -- applied per page, since a mid-pagination
        blip shouldn't discard pages already fetched."""
        params = {"limit": PAGE_SIZE, "offset": offset}
        if query:
            params["q"] = query

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
                    "smartrecruiters: timeout fetching %s (offset %d) for %s (attempt %d/%d)",
                    url, offset, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"smartrecruiters: {self.company_name} ({url}) "
                    f"returned HTTP {response.status_code}"
                )
                logger.warning(
                    "smartrecruiters: HTTP %d fetching %s (offset %d) for %s (attempt %d/%d)",
                    response.status_code, url, offset, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"smartrecruiters: failed to fetch {url} (offset {offset}) for "
                f"{self.company_name} after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"smartrecruiters: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"smartrecruiters: {self.company_name} ({url}) "
                f"returned HTTP {response.status_code}"
            )

        payload = response.json()
        return payload.get("content", [])

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "smartrecruiters: skipping malformed entry for %s: %s",
                    self.company_name, exc,
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        external_id = str(entry["id"])
        title = entry["name"]
        location = (entry.get("location") or {}).get("fullLocation") or ""

        # Structured hints SmartRecruiters exposes (M6 A3 style): a
        # controlled-vocabulary field the employer picked from a fixed set,
        # not free prose -- same authority as title. No description is
        # available in the list response (see module docstring), so this is
        # the only secondary signal beyond the title itself.
        type_of_employment = (entry.get("typeOfEmployment") or {}).get("label") or ""
        experience_level = (entry.get("experienceLevel") or {}).get("label") or ""
        employment_hint = f"{type_of_employment} {experience_level}".strip()

        contract_type = classify_contract_type(title, "", employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=f"https://jobs.smartrecruiters.com/{self.identifier}/{external_id}",
            posted_at=_parse_released_date(entry.get("releasedDate")),
            description="",
            source=self.name,
            external_id=external_id,
        )


def _parse_released_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
