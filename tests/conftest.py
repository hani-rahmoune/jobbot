"""Shared fixtures, plus the hard network guard required by CLAUDE.md rule 6.

The guard patches the socket layer itself (not just httpx) so a test that
forgets to mock an HTTP call fails loudly with NetworkAccessDisabledError
instead of silently reaching the real network. respx works fine underneath
it: respx replaces httpx's transport before a request ever reaches a socket,
so a properly-mocked test never trips this guard (see test_guards.py).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Injected into every test-built JobSource: adapters take user_agent as a
# constructor argument (not jobbot.settings) so sources/*.py stays free of
# any settings dependency. See test_user_agent_header_matches_what_was_injected
# in test_greenhouse.py for a test that actually checks this value made it
# onto the wire.
TEST_USER_AGENT = "jobbot-test/0.1 (+test@example.invalid)"


class NetworkAccessDisabledError(RuntimeError):
    """Raised when a test tries to open a real socket connection."""


def _blocked(*args: object, **kwargs: object) -> None:
    raise NetworkAccessDisabledError(
        "Real network access is disabled in tests. Mock HTTP calls with respx."
    )


# M13 Part B, APPROVED BY THE USER: the guard permits 127.0.0.1 and ::1 only
# -- Playwright's sync API drives its browser over a local loopback
# socketpair on Windows, which this guard was blocking outright, forcing
# test_real_playwright_renders_a_real_page to skip instead of run. Every
# other host stays blocked, with the exact same error message as before --
# this is a narrow carve-out, not a change to what the guard means for any
# real (non-loopback) address.
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _address_host(address: object) -> object:
    """The host is address[0] for both an AF_INET (host, port) tuple and an
    AF_INET6 (host, port, flowinfo, scopeid) one -- indexing rather than
    unpacking so a non-tuple address (unexpected here, but not this guard's
    business to validate) doesn't itself raise before `_blocked` can."""
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _guarded_connect(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
    if _address_host(address) in _ALLOWED_LOOPBACK_HOSTS:
        return _real_connect(self, address, *args, **kwargs)
    return _blocked()


def _guarded_connect_ex(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
    if _address_host(address) in _ALLOWED_LOOPBACK_HOSTS:
        return _real_connect_ex(self, address, *args, **kwargs)
    return _blocked()


def _guarded_create_connection(address: object, *args: object, **kwargs: object) -> object:
    if _address_host(address) in _ALLOWED_LOOPBACK_HOSTS:
        return _real_create_connection(address, *args, **kwargs)
    return _blocked()


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)


@pytest.fixture
def greenhouse_payload() -> dict[str, Any]:
    """The full mocked API response: {"jobs": [...], "meta": {...}}."""
    return json.loads((FIXTURES_DIR / "greenhouse_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def lever_payload() -> list[dict[str, Any]]:
    """The full mocked API response: a JSON array directly (Lever's own
    shape, not wrapped in an object)."""
    return json.loads((FIXTURES_DIR / "lever_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def ashby_payload() -> dict[str, Any]:
    """The full mocked API response: {"jobs": [...]}."""
    return json.loads((FIXTURES_DIR / "ashby_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def workday_payload() -> dict[str, Any]:
    """One page's worth of the real response shape: {"total": N, "jobPostings": [...]}."""
    return json.loads((FIXTURES_DIR / "workday_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def smartrecruiters_payload() -> dict[str, Any]:
    """One page's worth of the real response shape:
    {"offset": N, "limit": N, "totalFound": N, "content": [...]}."""
    return json.loads((FIXTURES_DIR / "smartrecruiters_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def jibe_payload() -> dict[str, Any]:
    """One page's worth of the real response shape: {"totalCount": N, "jobs": [{"data": {...}}]}."""
    return json.loads((FIXTURES_DIR / "jibe_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def mock_client() -> httpx.Client:
    """One httpx.Client shared by the whole test session. respx replaces the
    transport per-test (see the guard note above and test_guards.py), so
    reusing a single client is safe -- and skips paying httpx's default
    SSLContext/CA-bundle setup (~0.5-1s on this machine, per Client()
    constructed with verify=True) once per test instead of once, total.
    verify=False on top of that: nothing here ever does a real TLS handshake
    anyway. Never close this client in a test (no `with mock_client:`) --
    it's shared, and closing it breaks every test after it.
    """
    return httpx.Client(verify=False)


@pytest.fixture
def greenhouse_source(mock_client: httpx.Client):
    from jobbot.sources.greenhouse import GreenhouseSource

    return GreenhouseSource("acme", "Acme Corp", mock_client, user_agent=TEST_USER_AGENT)
