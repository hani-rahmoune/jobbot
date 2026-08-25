"""The adapter contract every ATS source implements.

`fetch_raw()` is the only method allowed to touch the network. It accepts
`etag`/`last_modified` and may ignore them for now (always returning `None` as
the new etag) — the signature exists so conditional-request caching can be
added in M9 without changing every adapter's interface. `parse()` turns raw
payloads into `Job`s. `fetch()` is the convenience that chains the two.

Every subclass is auto-registered on definition (see `__init_subclass__`) so
`registered_sources()` can enumerate all of them without relying on adapters
remembering to opt in — that enumeration backs the load-bearing
`test_source_integrity` check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from jobbot.models import Job

_registry: list[type[JobSource]] = []


class SourceError(Exception):
    """Base exception for adapter failures (network, HTTP, bad payload)."""


class SourceEmptyError(SourceError):
    """Raised by fetch_raw() when the source's payload contains zero items."""


class SourceNotFoundError(SourceError):
    """Raised by fetch_raw() on a 404: the identifier/board token doesn't
    resolve. M9 will want to treat a dead board token (renamed, ATS switched,
    typo) differently from a transient failure like a 500 or timeout."""


class JobSource(ABC):
    name: ClassVar[str]
    tier: ClassVar[int]  # 1 = employer ATS, 2 = official gov API
    first_party: ClassVar[bool]

    def __init__(
        self, identifier: str, company_name: str, client: httpx.Client, user_agent: str
    ) -> None:
        self.identifier = identifier
        self.company_name = company_name
        self.client = client
        # Injected, not looked up: adapters must not reach into jobbot.settings
        # themselves (that coupling belongs to whatever constructs the source,
        # e.g. the future M5 orchestrator). Used verbatim in request headers.
        self.user_agent = user_agent

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _registry.append(cls)

    @abstractmethod
    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Fetch the raw listing payload. Returns (raw_items, new_etag).

        Raises SourceEmptyError when the payload contains zero items.
        """

    @abstractmethod
    def parse(self, raw: list[dict]) -> list[Job]:
        """Convert raw source payloads into validated Job models."""

    def fetch(self) -> list[Job]:
        """fetch_raw() then parse(), in one call."""
        raw, _new_etag = self.fetch_raw()
        return self.parse(raw)


def registered_sources() -> list[type[JobSource]]:
    """Every JobSource subclass defined so far. Used by test_source_integrity."""
    return list(_registry)
