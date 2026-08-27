"""Generic schema.org JobPosting (JSON-LD) adapter.

This is the long-tail adapter: most small French companies with a careers
page but no recognizable ATS still mark up individual postings with
schema.org structured markup. Still first-party per CLAUDE.md rule 2 -- it
comes straight off the employer's own domain, just not through a named
ATS's API.

Unlike every other adapter, the identifier here is a full https URL (the
careers/listing page itself), not an opaque board token -- validated in
__init__. Robots.txt is honored before the page is ever fetched, cached per
host for the life of the instance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout

# Deliberately tolerant of the attribute-quoting styles seen in the wild
# (some pages skip quotes on type=application/ld+json entirely).
_LD_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

_ADDRESS_FIELDS = ("addressLocality", "addressRegion", "addressCountry")


class JsonLdSource(JobSource):
    name = "jsonld"
    tier = 1
    first_party = True

    def __init__(
        self, identifier: str, company_name: str, client: httpx.Client, user_agent: str
    ) -> None:
        parsed = urlsplit(identifier)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"jsonld: identifier must be a full https URL, got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._robots = RobotsCache(client, user_agent)

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = self.identifier

        if not self._robots.allowed(url):
            raise SourceError(
                f"jsonld: robots.txt disallows fetching {url} for {self.company_name}"
            )

        headers = {"User-Agent": self.user_agent}
        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "jsonld: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"jsonld: {self.company_name} ({url}) returned HTTP {response.status_code}"
                )
                logger.warning(
                    "jsonld: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"jsonld: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"jsonld: page not found for {self.company_name} ({url}): returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"jsonld: {self.company_name} ({url}) returned HTTP {response.status_code}"
            )

        postings = self._extract_job_postings(response.text)

        if not postings:
            raise SourceEmptyError(
                f"jsonld: {self.company_name} ({url}) contained no JobPosting entries"
            )

        return postings, None

    def _extract_job_postings(self, html: str) -> list[dict]:
        postings: list[dict] = []
        for match in _LD_JSON_BLOCK_RE.finditer(html):
            raw_block = match.group(1).strip()
            try:
                parsed = json.loads(raw_block)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "jsonld: skipping malformed JSON-LD block for %s: %s", self.company_name, exc
                )
                continue
            postings.extend(self._job_postings_from_block(parsed))
        return postings

    @staticmethod
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

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "jsonld: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        title = entry["title"]
        url = entry.get("url") or self.identifier
        raw_identifier = self._extract_identifier_value(entry.get("identifier"))
        external_id = raw_identifier or hashlib.sha256(url.encode("utf-8")).hexdigest()
        location = self._extract_location(entry.get("jobLocation"))
        employment_hint = entry.get("employmentType") or ""
        if isinstance(employment_hint, list):
            employment_hint = " ".join(str(value) for value in employment_hint)
        description = strip_html(entry.get("description") or "")
        contract_type = classify_contract_type(title, description, str(employment_hint))

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=entry.get("datePosted"),
            description=description,
            source=self.name,
            external_id=external_id,
        )

    @staticmethod
    def _extract_identifier_value(value: Any) -> str:
        """schema.org's `identifier` is either a plain string or a
        PropertyValue object ({"@type": "PropertyValue", "value": ...})."""
        if isinstance(value, dict):
            return str(value.get("value") or value.get("name") or "")
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _extract_location(job_location: Any) -> str:
        """jobLocation may be a single Place object or a list of them (take
        the first). Build the display string from whichever of locality/
        region/country are actually present, most specific first."""
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
