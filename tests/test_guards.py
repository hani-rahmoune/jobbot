"""Load-bearing per CLAUDE.md: proves the conftest network guard actually
blocks a real connection attempt, rather than merely relying on every test
remembering to mock. If this stops raising, "tests never touch the network"
is no longer true regardless of what the other suites show.
"""

from __future__ import annotations

import socket

import httpx
import pytest
import respx
from conftest import NetworkAccessDisabledError


def test_socket_connect_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessDisabledError):
            sock.connect(("example.com", 80))
    finally:
        sock.close()


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessDisabledError):
        socket.create_connection(("example.com", 80), timeout=1)


def test_respx_mocked_httpx_still_works_under_the_guard(mock_client: httpx.Client) -> None:
    """The guard blocks raw sockets, not respx-mocked httpx calls: respx
    replaces the transport before a request ever reaches a socket."""
    with respx.mock:
        respx.get("https://example.invalid/ping").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        # mock_client is session-scoped and shared: do NOT wrap it in
        # `with mock_client:` here, that would close it for every later test.
        response = mock_client.get("https://example.invalid/ping")
    assert response.json() == {"ok": True}
