"""End-to-end tests for :mod:`egress_proxy`.

These tests spin up the real asyncio proxy on an ephemeral port and
exercise it through socket clients. We avoid making any actual
external connections -- the "upstream" for allow-path tests is a
second asyncio echo server bound to loopback so the proxy bridges
between two in-process endpoints.

Coverage goals
--------------

* :func:`firewall.is_blocked` matches exact + subdomain + case-
  insensitive. (Also covered in ``test_firewall`` but pinned here
  for documentation alongside the proxy that consumes it.)
* CONNECT to a blocked host returns 403 with the JSON deny body and
  records a ``deny`` decision in the audit ring.
* CONNECT to an allowed host returns 200, then bridges raw bytes
  bidirectionally and records an ``allow``.
* CONNECT to an unreachable allowed host returns 502 and records an
  ``error``.
* Plain-HTTP GET through the proxy is rejected when blocked, passed
  through when allowed.
* Unsupported HTTP verbs get 405.
* ``LOCAL_PASS_THROUGH`` (127.0.0.1, localhost, ::1) are always
  allowed regardless of catalog state.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import unittest
from typing import Iterable, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # tests/egress/ -> tests/ -> repo
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from local_scribe.egress import egress_proxy  # noqa: E402
from local_scribe.egress import firewall      # noqa: E402


# ---------- helpers ---------------------------------------------------


async def _start_echo_server() -> Tuple[asyncio.base_events.Server, int]:
    """Bind an echo server on an ephemeral loopback port. Returns
    ``(server, port)``. Echo: every byte received is sent back."""
    async def handle(reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_proxy(
    categories: Iterable[str] = firewall.DEFAULT_ENABLED_CATEGORIES,
) -> Tuple[asyncio.base_events.Server, int]:
    """Start the proxy on an ephemeral port. Returns ``(server, port)``."""
    cats = tuple(categories)
    server = await asyncio.start_server(
        lambda r, w: egress_proxy._handle_client(r, w, categories=cats),
        host="127.0.0.1", port=0, reuse_address=True,
    )
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _send_request(port: int, payload: bytes,
                        *, read_until_close: bool = False,
                        max_bytes: int = 64 * 1024) -> bytes:
    """Open a TCP connection to ``127.0.0.1:port``, send payload,
    read response up to either the first ``\\r\\n\\r\\n`` (default)
    or until EOF if ``read_until_close``."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        if read_until_close:
            data = bytearray()
            while len(data) < max_bytes:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)
        # Read until we see headers terminator.
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < max_bytes:
            chunk = await reader.read(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)
    finally:
        writer.close()


def _run(coro):
    return asyncio.run(coro)


# ---------- tests -----------------------------------------------------


class IsBlockedTests(unittest.TestCase):
    """Pin down the proxy's decision predicate. The proxy is only as
    correct as :func:`firewall.is_blocked`, so cover the matching
    semantics here alongside the proxy that consumes them."""

    def test_exact_match_in_default_categories(self) -> None:
        e = firewall.is_blocked("api.openai.com")
        self.assertIsNotNone(e)
        self.assertEqual(e.category, "providers")

    def test_subdomain_match(self) -> None:
        # Sub-domain of a catalog entry should also be blocked --
        # providers commonly shard onto regional subdomains.
        self.assertIsNotNone(firewall.is_blocked("eastus.api.openai.com"))

    def test_unrelated_subdomain_not_matched(self) -> None:
        # "openai.com.example.org" is NOT a subdomain of openai.com;
        # it's a different domain. We must not match it.
        self.assertIsNone(firewall.is_blocked("openai.com.example.org"))

    def test_case_insensitive(self) -> None:
        self.assertIsNotNone(firewall.is_blocked("API.OpenAI.com"))

    def test_trailing_dot_stripped(self) -> None:
        self.assertIsNotNone(firewall.is_blocked("api.openai.com."))

    def test_char_cloud_off_by_default(self) -> None:
        self.assertIsNone(firewall.is_blocked("api.char.com"))
        self.assertIsNotNone(firewall.is_blocked(
            "api.char.com", categories=firewall.ALL_CATEGORIES,
        ))

    def test_empty_hostname_safe(self) -> None:
        # Defensive: blank or whitespace must not crash the proxy.
        self.assertIsNone(firewall.is_blocked(""))
        self.assertIsNone(firewall.is_blocked("   "))


class _ProxyTestBase(unittest.TestCase):
    """Per-test isolation: fresh audit ring + per-test proxy on its
    own port. We restore the module-level audit_ring afterwards so
    test order doesn't matter."""

    def setUp(self) -> None:
        self._saved_ring = egress_proxy.audit_ring
        egress_proxy.audit_ring = egress_proxy.AuditRing(capacity=64)

    def tearDown(self) -> None:
        egress_proxy.audit_ring = self._saved_ring


class ConnectDenyTests(_ProxyTestBase):
    def test_blocked_host_returns_403(self) -> None:
        async def go() -> Tuple[bytes, dict]:
            server, port = await _start_proxy()
            try:
                resp = await _send_request(
                    port,
                    b"CONNECT api.openai.com:443 HTTP/1.1\r\n"
                    b"Host: api.openai.com:443\r\n\r\n",
                    read_until_close=True,
                )
            finally:
                server.close()
                await server.wait_closed()
            return resp, egress_proxy.audit_ring.counts()

        resp, counts = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"),
                        f"unexpected response: {resp[:200]!r}")
        # Body should mention the category + the host.
        self.assertIn(b'"category": "providers"', resp)
        self.assertIn(b'"host": "api.openai.com"', resp)
        # Audit ring recorded one deny.
        self.assertEqual(counts["deny"], 1)
        self.assertEqual(counts["allow"], 0)

    def test_subdomain_of_blocked_host_returns_403(self) -> None:
        async def go() -> bytes:
            server, port = await _start_proxy()
            try:
                return await _send_request(
                    port,
                    b"CONNECT eu.us.api.openai.com:443 HTTP/1.1\r\n\r\n",
                    read_until_close=True,
                )
            finally:
                server.close()
                await server.wait_closed()

        resp = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"),
                        f"unexpected response: {resp[:200]!r}")


class ConnectAllowTests(_ProxyTestBase):
    def test_allowed_host_bridges_bytes(self) -> None:
        """CONNECT to an in-process echo server, then push bytes and
        verify the proxy bridged them end-to-end. ``127.0.0.1`` is in
        LOCAL_PASS_THROUGH so it's allowed regardless of catalog."""

        async def go() -> Tuple[bytes, bytes, dict]:
            echo, echo_port = await _start_echo_server()
            proxy, proxy_port = await _start_proxy()
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy_port,
                )
                # Send CONNECT.
                writer.write(
                    f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{echo_port}\r\n\r\n".encode("ascii"),
                )
                await writer.drain()
                # Read 200 response.
                resp = bytearray()
                while b"\r\n\r\n" not in resp:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    resp.extend(chunk)
                # Now the tunnel is open. Push a payload and read
                # back the echoed bytes.
                writer.write(b"ping-pong-1234")
                await writer.drain()
                echoed = bytearray()
                while len(echoed) < len(b"ping-pong-1234"):
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    echoed.extend(chunk)
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                return bytes(resp), bytes(echoed), egress_proxy.audit_ring.counts()
            finally:
                proxy.close()
                echo.close()
                await proxy.wait_closed()
                await echo.wait_closed()

        resp, echoed, counts = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"),
                        f"unexpected CONNECT response: {resp[:200]!r}")
        self.assertEqual(echoed, b"ping-pong-1234")
        self.assertEqual(counts["allow"], 1)
        self.assertEqual(counts["deny"], 0)


class ConnectErrorTests(_ProxyTestBase):
    def test_unreachable_upstream_returns_502(self) -> None:
        """``example.invalid`` is RFC-2606-reserved and won't resolve.
        The proxy should treat it as 'allowed by policy but
        unreachable' -> 502."""
        async def go() -> Tuple[bytes, dict]:
            server, port = await _start_proxy()
            try:
                resp = await _send_request(
                    port,
                    b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n",
                    read_until_close=True,
                )
            finally:
                server.close()
                await server.wait_closed()
            return resp, egress_proxy.audit_ring.counts()

        resp, counts = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 502"),
                        f"unexpected response: {resp[:200]!r}")
        self.assertEqual(counts["error"], 1)


class PlainHttpTests(_ProxyTestBase):
    def test_plain_http_to_blocked_host(self) -> None:
        async def go() -> bytes:
            server, port = await _start_proxy()
            try:
                return await _send_request(
                    port,
                    b"GET http://api.openai.com/v1/models HTTP/1.1\r\n"
                    b"Host: api.openai.com\r\n"
                    b"Accept: */*\r\n\r\n",
                    read_until_close=True,
                )
            finally:
                server.close()
                await server.wait_closed()

        resp = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"),
                        f"unexpected response: {resp[:200]!r}")

    def test_unsupported_verb_returns_405(self) -> None:
        async def go() -> bytes:
            server, port = await _start_proxy()
            try:
                return await _send_request(
                    port,
                    b"WACKY / HTTP/1.1\r\nHost: x\r\n\r\n",
                    read_until_close=True,
                )
            finally:
                server.close()
                await server.wait_closed()

        resp = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 405"),
                        f"unexpected response: {resp[:200]!r}")


class LoopbackAlwaysAllowed(_ProxyTestBase):
    """Even if a future change added 127.0.0.1 to the catalog (which
    would be deeply wrong), the proxy must still let loopback traffic
    through -- Char talks to our ASR / Inspector / LM Studio on
    loopback constantly."""

    def test_localhost_passthrough(self) -> None:
        async def go() -> bytes:
            echo, echo_port = await _start_echo_server()
            proxy, proxy_port = await _start_proxy()
            try:
                return await _send_request(
                    proxy_port,
                    f"CONNECT localhost:{echo_port} HTTP/1.1\r\n\r\n".encode("ascii"),
                )
            finally:
                proxy.close()
                echo.close()
                await proxy.wait_closed()
                await echo.wait_closed()

        resp = _run(go())
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"),
                        f"unexpected response: {resp[:200]!r}")


class AuditRingTests(unittest.TestCase):
    """Direct unit tests for the ring (independent of network IO)."""

    def test_ring_caps_capacity(self) -> None:
        ring = egress_proxy.AuditRing(capacity=3)
        for i in range(10):
            ring.add(egress_proxy.Decision(
                ts=float(i), method="X", host="h",
                port=1, decision="allow", reason=None, category=None,
            ))
        recent = ring.recent(10)
        self.assertEqual(len(recent), 3)
        # Newest first.
        self.assertEqual([d.ts for d in recent], [9.0, 8.0, 7.0])

    def test_counts_classify_by_decision(self) -> None:
        ring = egress_proxy.AuditRing()
        for d in ("allow", "allow", "deny", "error", "deny"):
            ring.add(egress_proxy.Decision(
                ts=0.0, method="X", host="h", port=1,
                decision=d, reason=None, category=None,
            ))
        self.assertEqual(ring.counts(),
                         {"allow": 2, "deny": 2, "error": 1, "total": 5})


class IsListeningTests(unittest.TestCase):
    def test_returns_false_when_nothing_bound(self) -> None:
        # Use a port in the high range that's almost certainly free.
        # We can't pick "any free port" deterministically, but
        # binding+closing to discover one is fine.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(egress_proxy.is_listening(port=port, timeout=0.1))


if __name__ == "__main__":
    unittest.main()
