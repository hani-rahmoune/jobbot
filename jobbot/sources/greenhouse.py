"""Greenhouse Job Board API adapter.

Greenhouse is an ATS product employers embed on their own careers page; the
public `boards-api.greenhouse.io` endpoint used here serves exactly the
postings that employer has published, first-party per CLAUDE.md rule 2.

GET https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs?content=true
returns every open posting for one company's board in a single response —
`identifier` is that company's board token (see companies/hot.yaml for how to
find one from a careers page URL).
"""

from __future__ import annotations

import logging

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html

logger = logging.getLogger(__name__)

BOARD_URL_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs?content=true"
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout


class GreenhouseSource(JobSource):
    name = "greenhouse"
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
                    "greenhouse: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"greenhouse: {self.company_name} ({url}) returned "
                    f"HTTP {response.status_code}"
                )
                logger.warning(
                    "greenhouse: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"greenhouse: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception (e.g. HTTPStatusError from
        # raise_for_status()) escape this method -- every failure mode here
        # is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"greenhouse: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"greenhouse: {self.company_name} ({url}) returned "
                f"HTTP {response.status_code}"
            )

        payload = response.json()
        jobs = payload.get("jobs", [])

        # M8b: zero results used to raise SourceEmptyError here, but that
        # can't tell "this board is broken" from "this small company simply
        # has no open roles right now" -- only the store's history can, so
        # that decision now happens in run.process_source() instead. An
        # empty list is a perfectly valid, non-failing return value.
        return jobs, None

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "greenhouse: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        external_id = entry["id"]
        title = entry["title"]
        url = entry["absolute_url"]
        location = ((entry.get("location") or {}).get("name")) or ""
        description = strip_html(entry.get("content") or "")
        contract_type = classify_contract_type(title, description)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=entry.get("updated_at"),
            description=description,
            source=self.name,
            external_id=str(external_id),
        )
