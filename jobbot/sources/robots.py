"""Shared robots.txt compliance, used by any adapter or tool that fetches an
arbitrary employer-owned URL directly (as opposed to a named ATS's API,
which is not a "non-API fetch" per CLAUDE.md's adapter contract).

Extracted from jsonld.py (M7 Part B1) so jobbot/discover.py can honor the
same policy while resolving a company's careers page, without duplicating
the fetch-and-parse logic.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0


class RobotsCache:
    """Fetches and caches robots.txt per host for the life of the instance.

    A robots.txt that returns non-200 (404 included) or fails to fetch at
    all is treated as allowing -- the standard convention: absence of a
    robots.txt is not a site asking to be left alone.
    """

    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        host = parsed.netloc
        if host not in self._cache:
            self._cache[host] = self._fetch(parsed.scheme, host)
        parser = self._cache[host]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _fetch(self, scheme: str, host: str) -> RobotFileParser | None:
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            response = self.client.get(
                robots_url, headers={"User-Agent": self.user_agent}, timeout=TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            logger.info("robots: fetch failed for %s, treating as allowed", host)
            return None

        if response.status_code != 200:
            logger.info(
                "robots: %s returned HTTP %d, treating as allowed",
                robots_url, response.status_code,
            )
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser
