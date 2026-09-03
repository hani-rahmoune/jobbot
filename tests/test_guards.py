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


# M13 Part B: the guard now permits 127.0.0.1/::1 (Playwright's driver IPC),
# every other host must still be blocked exactly as before -- these two
# assert that narrowly, independent of the pre-existing tests above (which
# already used a non-loopback host, but not ones written specifically to
# guard against the loopback carve-out being accidentally widened).


def test_an_external_numeric_ip_is_still_blocked_after_the_loopback_exemption() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessDisabledError):
            sock.connect(("93.184.216.34", 80))  # example.com's own IP, not loopback
    finally:
        sock.close()


def test_an_external_hostname_via_create_connection_is_still_blocked() -> None:
    with pytest.raises(NetworkAccessDisabledError):
        socket.create_connection(("example.com", 80), timeout=1)


def test_loopback_v4_connect_is_permitted_through_to_the_real_socket_call() -> None:
    """Doesn't assert a full connection succeeds (nothing real is listening
    on this port in CI) -- asserts the guard itself steps aside for
    127.0.0.1 by confirming NetworkAccessDisabledError specifically is never
    raised. Whatever the real socket layer does with the attempt (refuse it,
    since nothing listens on port 1; or occasionally accept it, depending on
    the platform) is that layer's business, not this guard's -- "let the
    real connect() decide" is the whole point of the exemption."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)  # a real OS-level connect attempt, kept fast on purpose
    try:
        sock.connect(("127.0.0.1", 1))  # port 1 -- nothing listens there
    except NetworkAccessDisabledError:
        pytest.fail("the network guard blocked a loopback connect it should have exempted")
    except OSError:
        pass  # connection refused/timed out/similar -- expected, and proves the real socket ran
    finally:
        sock.close()


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
