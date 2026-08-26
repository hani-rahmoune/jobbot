"""Ashby Job Board API adapter.

Ashby is an ATS product employers embed on their own careers page; the
public `api.ashbyhq.com` posting-api endpoint used here serves exactly the
postings that employer has published, first-party per CLAUDE.md rule 2.

GET https://api.ashbyhq.com/posting-api/job-board/{identifier}?includeCompensation=false
returns every open posting for one company's board in a single response, as
an object with a "jobs" array (like Greenhouse; unlike Lever's bare array).
"""

from __future__ import annotations

import logging

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html

logger = logging.getLogger(__name__)

BOARD_URL_TEMPLATE = (
    "https://api.ashbyhq.com/posting-api/job-board/{identifier}?includeCompensation=false"
)
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout


class AshbySource(JobSource):
    name = "ashby"
    tier = 1
    first_party = True

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = BOARD_URL_TEMPLATE.format(identifier=self.identifier)
        headers = {"User-Agent": self.user_agent}

        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "ashby: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"ashby: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "ashby: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"ashby: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"ashby: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"ashby: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        payload = response.json()
        jobs = payload.get("jobs", [])

        if not jobs:
            raise SourceEmptyError(
                f"ashby: {self.company_name} ({self.identifier}) returned zero jobs"
            )

        return jobs, None

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "ashby: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        external_id = entry["id"]
        title = entry["title"]
        url = entry["jobUrl"]
        location = entry.get("location") or ""
        employment_hint = entry.get("employmentType") or ""
        description_plain = entry.get("descriptionPlain")
        description = (
            description_plain
            if description_plain
            else strip_html(entry.get("descriptionHtml") or "")
        )
        contract_type = classify_contract_type(title, description, employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=entry.get("publishedAt"),
            description=description,
            source=self.name,
            external_id=str(external_id),
        )
