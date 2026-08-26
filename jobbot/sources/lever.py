"""Lever Postings API adapter.

Lever is an ATS product employers embed on their own careers page; the
public `api.lever.co` endpoint used here serves exactly the postings that
employer has published, first-party per CLAUDE.md rule 2.

GET https://api.lever.co/v0/postings/{identifier}?mode=json returns every
open posting for one company's board as a JSON array directly -- unlike
Greenhouse, there's no wrapping object with a "jobs" key.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html

logger = logging.getLogger(__name__)

BOARD_URL_TEMPLATE = "https://api.lever.co/v0/postings/{identifier}?mode=json"
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout


def _epoch_millis_to_utc(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class LeverSource(JobSource):
    name = "lever"
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
                    "lever: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"lever: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "lever: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"lever: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"lever: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"lever: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        jobs = response.json()  # the response IS the array, no wrapping object

        if not jobs:
            raise SourceEmptyError(
                f"lever: {self.company_name} ({self.identifier}) returned zero jobs"
            )

        return jobs, None

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "lever: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        external_id = entry["id"]
        title = entry["text"]
        url = entry["hostedUrl"]
        categories = entry.get("categories") or {}
        location = categories.get("location") or ""
        employment_hint = categories.get("commitment") or ""
        description_plain = entry.get("descriptionPlain")
        description = (
            description_plain if description_plain else strip_html(entry.get("description") or "")
        )
        contract_type = classify_contract_type(title, description, employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=_epoch_millis_to_utc(entry.get("createdAt")),
            description=description,
            source=self.name,
            external_id=str(external_id),
        )
