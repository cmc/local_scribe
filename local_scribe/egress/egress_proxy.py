"""Per-Char egress CONNECT proxy.

A tiny HTTP CONNECT proxy that listens on ``127.0.0.1:8889`` and
enforces the :mod:`firewall` catalog as the **per-process** half of
local_scribe's outbound firewall. Companion to :mod:`char_sandbox`,
which is the **containment** half (sandbox-exec restricts Char's
network reach to loopback only, so all egress is forced through
this proxy).

Why a proxy at all
==================

macOS exposes no native "block hostname X for binary Y" primitive.
``pf`` filters per-user but not per-app; the Application Firewall is
inbound-only; Network Extension is gated behind an Apple-granted
entitlement that an open-source project can't ship. The two macOS
primitives we *can* compose are:

  1. ``sandbox-exec`` containment (deny network-outbound except
     loopback) — see :mod:`char_sandbox`.
  2. ``HTTPS_PROXY`` env var honored by Char's HTTP stack
     (Tauri/reqwest, Sentry's Rust SDK, the Tauri auto-updater,
     PostHog's Rust SDK).

Together they give us a bypass-proof per-Char filter without
requiring Developer ID, kernel extensions, root, or any one-off Apple
entitlement.

What the proxy does
===================

* Speaks plain HTTP/1.1 on ``127.0.0.1:8889``.
* Accepts CONNECT (HTTPS tunnels) and the seven plain-HTTP methods
  (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS).
* For every request, looks up the destination hostname in
  :func:`firewall.is_blocked` and:

    - If the hostname is in the catalog, returns ``403 Forbidden``
      with a JSON body explaining which category blocked it.
    - Otherwise, opens a TCP connection to the destination and
      bridges bytes in both directions (CONNECT) or proxies the
      single HTTP/1.1 request (plain HTTP).

* Every decision (allow / deny / error) is logged to a ring buffer
  + the structured log file so the inspector and ``./run.sh doctor``
  can show recent egress decisions.

The proxy is **not** a TLS-terminating MITM. Char never has to trust
a local CA, and we never see TLS plaintext. The block decision is
made on the CONNECT line alone (hostname + port), which is enough to
prevent connections to blocked providers.

Performance
-----------

The proxy is asyncio-based with one task per connection. A modern
M-series Mac handles tens of thousands of simultaneous connections
without breaking a sweat; in practice Char makes single-digit
concurrent connections so headroom is huge. We deliberately don't
add a connection pool, HTTP/2 support, or keep-alives to the
upstream — the goal is "tiny and obviously correct," not "high
throughput."

The proxy auto-starts from ``./run.sh start`` and runs alongside the
ASR + Inspector services in :mod:`run.sh`'s service registry.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import errno
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from local_scribe.egress import firewall


logger = logging.getLogger("local_scribe.egress_proxy")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default upstream-connect timeout (seconds). Char will see this as
#: a slow CONNECT response; in practice cloud APIs answer in <500 ms.
UPSTREAM_TIMEOUT = 10.0

#: Buffer size for the bidirectional bridge. 64 KB matches macOS's
#: default socket buffer (``net.inet.tcp.sendspace`` /
#: ``net.inet.tcp.recvspace``); larger doesn't help, smaller forces
#: extra round-trips through the event loop.
BRIDGE_BUF = 64 * 1024

#: Maximum size of the CONNECT / request-line buffer. RFC 7230
#: doesn't pin a number; we cap at 16 KB to defend against a
#: misbehaving client that never sends ``\r\n\r\n``.
MAX_HEADER_BYTES = 16 * 1024

#: Hostnames the proxy will allow through *unconditionally* — these
#: are local_scribe's own loopback services. Catching them here
#: rather than waiting for ``is_blocked`` to say "no match" gives a
#: faster path and a cleaner log entry.
LOCAL_PASS_THROUGH: frozenset[str] = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Decision:
    """One proxy decision. Captured in :class:`AuditRing` so the
    inspector + doctor can surface recent egress activity."""

    ts: float                # unix epoch seconds
    method: str              # "CONNECT", "GET", ...
    host: str
    port: int
    decision: str            # "allow" | "deny" | "error"
    reason: Optional[str]    # firewall reason for deny; exception
                             # string for error
    category: Optional[str]  # firewall category for deny

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class AuditRing:
    """Bounded ring buffer of recent :class:`Decision`s. Threadsafe-
    ish in the single-threaded asyncio sense (one writer task per
    connection, all touching the same deque via append/iter)."""

    def __init__(self, capacity: int = 512) -> None:
        self._buf: collections.deque[Decision] = collections.deque(
            maxlen=capacity,
        )

    def add(self, d: Decision) -> None:
        self._buf.append(d)

    def recent(self, n: int = 50) -> list[Decision]:
        # The deque is ordered oldest -> newest; tail-slice and
        # reverse so callers see newest-first.
        out = list(self._buf)[-n:]
        out.reverse()
        return out

    def counts(self) -> dict[str, int]:
        c = {"allow": 0, "deny": 0, "error": 0, "total": len(self._buf)}
        for d in self._buf:
            if d.decision in c:
                c[d.decision] += 1
        return c


# Process-wide singleton. The proxy is a single asyncio event loop,
# so a module-level instance is appropriate (no shared-state
# concurrency outside the loop).
audit_ring = AuditRing()


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


def _decide(
    host: str,
    *,
    categories: Iterable[str] = firewall.DEFAULT_ENABLED_CATEGORIES,
) -> tuple[bool, Optional[firewall.Entry]]:
    """Return ``(allowed, blocking_entry_or_None)``. Used by the
    connection handler so log messages can mention which catalog
    entry matched (or that the host wasn't in the catalog)."""
    # Always allow loopback. Char talks to our ASR server +
    # Inspector + LM Studio over 127.0.0.1; we don't want the proxy
    # to ever break that.
    h = host.strip().lower().rstrip(".")
    if h in LOCAL_PASS_THROUGH:
        return True, None
    entry = firewall.is_blocked(h, categories=categories)
    if entry is not None:
        return False, entry
    return True, None


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    """Read until ``\\r\\n\\r\\n`` or :data:`MAX_HEADER_BYTES`.
    Returns the raw bytes including the terminator."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADER_BYTES:
            raise ValueError("request header too large")
        chunk = await reader.read(4096)
        if not chunk:
            raise ConnectionError("client closed before headers complete")
        data.extend(chunk)
    return bytes(data)


def _parse_request_line(raw: bytes) -> tuple[str, str, str]:
    """Return ``(method, target, http_version)``. Raises ValueError
    on malformed input. We're tolerant of LF-only line endings
    because some clients (e.g. curl with ``--http1.0``) emit them."""
    first_line, _, _rest = raw.partition(b"\r\n")
    if not first_line:
        first_line, _, _rest = raw.partition(b"\n")
    parts = first_line.split()
    if len(parts) != 3:
        raise ValueError(f"malformed request line: {first_line!r}")
    method = parts[0].decode("latin-1")
    target = parts[1].decode("latin-1")
    version = parts[2].decode("latin-1")
    return method, target, version


def _parse_host_port(target: str, *, default_port: int) -> tuple[str, int]:
    """Parse ``host:port`` (CONNECT target) or full URL with port
    fallback. Wraps IPv6 addresses in brackets if needed.
    """
    # CONNECT: target is "host:port"
    if "://" not in target:
        if target.startswith("[") and "]" in target:
            # bracketed IPv6
            close = target.find("]")
            host = target[1:close]
            tail = target[close + 1 :]
            if tail.startswith(":"):
                return host, int(tail[1:])
            return host, default_port
        host, _, port_s = target.rpartition(":")
        if host and port_s.isdigit():
            return host, int(port_s)
        # No port in target — keep default.
        return target, default_port
    # Plain HTTP: target is a full URL.
    from urllib.parse import urlparse
    u = urlparse(target)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port


def _http_response(status: int, reason: str, body: dict) -> bytes:
    """Encode a simple HTTP/1.1 response with a JSON body. Used for
    the 403 deny response. ``Connection: close`` so clients don't
    try to keep-alive a proxy that just refused them."""
    payload = json.dumps(body).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n"
        f"X-Local-Scribe-Firewall: deny\r\n"
        f"\r\n"
    ).encode("ascii")
    return headers + payload


async def _bridge(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """Pipe bytes A->B and B->A concurrently until either side EOFs.
    Closes both writers on exit so the kernel reclaims the sockets
    promptly."""

    async def one_way(src: asyncio.StreamReader,
                      dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(BRIDGE_BUF)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            # Peer hung up mid-stream; the symmetric task will see
            # EOF on its read and the bridge will tear down.
            pass
        finally:
            try:
                dst.write_eof()
            except (OSError, RuntimeError):
                pass

    a2b = asyncio.create_task(one_way(a_reader, b_writer))
    b2a = asyncio.create_task(one_way(b_reader, a_writer))
    try:
        await asyncio.gather(a2b, b2a)
    finally:
        for w in (a_writer, b_writer):
            try:
                w.close()
            except (OSError, RuntimeError):
                pass


async def _handle_connect(
    method: str,
    target: str,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    categories: Iterable[str],
) -> None:
    """Handle one CONNECT tunnel."""
    host, port = _parse_host_port(target, default_port=443)
    allowed, entry = _decide(host, categories=categories)
    now = time.time()
    if not allowed:
        body = {
            "error": "egress_blocked",
            "host": host,
            "port": port,
            "category": entry.category if entry else None,
            "reason": entry.reason if entry else None,
            "hint": ("local_scribe firewall (process mode). The "
                     "destination is in the block catalog. To bypass for a "
                     "single host, edit firewall.BLOCK_CATALOG; to "
                     "disable entirely, run `./run.sh stop`."),
        }
        client_writer.write(_http_response(403, "Forbidden", body))
        await client_writer.drain()
        client_writer.close()
        audit_ring.add(Decision(
            ts=now, method=method, host=host, port=port,
            decision="deny",
            reason=entry.reason if entry else None,
            category=entry.category if entry else None,
        ))
        logger.info("DENY %s %s:%d (%s)", method, host, port,
                    entry.reason if entry else "no catalog match")
        return

    # Allowed: open upstream, return 200, bridge.
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=UPSTREAM_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        body = {
            "error": "upstream_unreachable",
            "host": host, "port": port,
            "detail": str(exc),
        }
        client_writer.write(_http_response(502, "Bad Gateway", body))
        await client_writer.drain()
        client_writer.close()
        audit_ring.add(Decision(
            ts=now, method=method, host=host, port=port,
            decision="error", reason=str(exc), category=None,
        ))
        logger.warning("ERROR %s %s:%d (%s)", method, host, port, exc)
        return

    client_writer.write(
        b"HTTP/1.1 200 Connection established\r\n"
        b"Proxy-Agent: local_scribe-egress/1\r\n"
        b"\r\n"
    )
    await client_writer.drain()
    audit_ring.add(Decision(
        ts=now, method=method, host=host, port=port,
        decision="allow", reason=None, category=None,
    ))
    # INFO (not DEBUG) so allow decisions land in the log file too;
    # ``./run.sh char firewall-status`` tails the same file to show
    # recent egress decisions to the operator.
    logger.info("ALLOW %s %s:%d", method, host, port)
    await _bridge(client_reader, client_writer, up_reader, up_writer)


async def _handle_plain_http(
    method: str,
    target: str,
    version: str,
    headers_raw: bytes,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    categories: Iterable[str],
) -> None:
    """Handle a plain HTTP request through the proxy. Char and the
    Tauri stack overwhelmingly use HTTPS, but the Sentry SDK and the
    auto-updater have been seen with plain HTTP fallbacks in the
    wild, so we cover both paths.

    We do NOT rewrite headers — just relay verbatim with the request
    line rewritten to the origin-form path. Connection: close on
    both sides so each request is one-shot (matches what
    ``HTTPS_PROXY`` clients expect from a non-keep-alive proxy)."""
    host, port = _parse_host_port(target, default_port=80)
    allowed, entry = _decide(host, categories=categories)
    now = time.time()
    if not allowed:
        body = {
            "error": "egress_blocked",
            "host": host, "port": port,
            "category": entry.category if entry else None,
            "reason": entry.reason if entry else None,
        }
        client_writer.write(_http_response(403, "Forbidden", body))
        await client_writer.drain()
        client_writer.close()
        audit_ring.add(Decision(
            ts=now, method=method, host=host, port=port,
            decision="deny",
            reason=entry.reason if entry else None,
            category=entry.category if entry else None,
        ))
        logger.info("DENY %s %s://%s:%d", method,
                    "http", host, port)
        return

    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=UPSTREAM_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        body = {
            "error": "upstream_unreachable",
            "host": host, "port": port, "detail": str(exc),
        }
        client_writer.write(_http_response(502, "Bad Gateway", body))
        await client_writer.drain()
        client_writer.close()
        audit_ring.add(Decision(
            ts=now, method=method, host=host, port=port,
            decision="error", reason=str(exc), category=None,
        ))
        return

    # Rewrite the request line to origin-form: "GET /path HTTP/1.1".
    # The ``target`` we got from the client is absolute-form
    # ("GET http://host/path HTTP/1.1") because that's how proxy
    # clients address upstream. Strip the scheme+host portion.
    from urllib.parse import urlparse
    u = urlparse(target)
    origin = u.path or "/"
    if u.query:
        origin += "?" + u.query
    rewritten_line = f"{method} {origin} {version}\r\n".encode("latin-1")
    # Replace the first line of the captured headers.
    first_break = headers_raw.find(b"\r\n")
    if first_break < 0:
        first_break = headers_raw.find(b"\n")
    rest = headers_raw[first_break + 2 :] if first_break >= 0 else b""
    up_writer.write(rewritten_line + rest)
    await up_writer.drain()
    audit_ring.add(Decision(
        ts=now, method=method, host=host, port=port,
        decision="allow", reason=None, category=None,
    ))
    await _bridge(client_reader, client_writer, up_reader, up_writer)


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    categories: Iterable[str],
) -> None:
    peer = client_writer.get_extra_info("peername") or ("?", 0)
    try:
        try:
            headers_raw = await asyncio.wait_for(
                _read_headers(client_reader), timeout=10.0,
            )
        except (asyncio.TimeoutError, ConnectionError, ValueError) as exc:
            logger.debug("connection from %s closed during headers: %s",
                         peer, exc)
            client_writer.close()
            return
        try:
            method, target, version = _parse_request_line(headers_raw)
        except ValueError:
            logger.debug("malformed request line from %s", peer)
            client_writer.close()
            return

        if method.upper() == "CONNECT":
            await _handle_connect(
                method, target, client_reader, client_writer,
                categories=categories,
            )
            return
        if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE",
                              "HEAD", "OPTIONS"):
            await _handle_plain_http(
                method, target, version, headers_raw,
                client_reader, client_writer,
                categories=categories,
            )
            return
        # Unknown verb: 405 and bail.
        client_writer.write(_http_response(
            405, "Method Not Allowed",
            {"error": "unsupported_method", "method": method},
        ))
        await client_writer.drain()
        client_writer.close()
    except Exception as exc:  # noqa: BLE001
        # Defensive: anything that escapes the per-method handlers
        # is a programming error; close the client and log loudly.
        logger.exception("unhandled error serving %s: %s", peer, exc)
        try:
            client_writer.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public server-start API
# ---------------------------------------------------------------------------


async def serve_async(
    *,
    bind: str = firewall.PROXY_BIND,
    port: int = firewall.PROXY_PORT,
    categories: Iterable[str] = firewall.DEFAULT_ENABLED_CATEGORIES,
) -> asyncio.base_events.Server:
    """Start the proxy server and return the :class:`asyncio.Server`.

    The server keeps running until the caller calls
    ``server.close()``; the typical pattern is
    ``async with server: await server.serve_forever()``.
    """
    cats = tuple(categories)
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, categories=cats),
        host=bind,
        port=port,
        reuse_address=True,
    )
    sockets = server.sockets or ()
    bound = ", ".join(str(s.getsockname()) for s in sockets)
    logger.info("egress proxy listening on %s (categories=%s)",
                bound, sorted(cats))
    return server


def serve_blocking(
    *,
    bind: str = firewall.PROXY_BIND,
    port: int = firewall.PROXY_PORT,
    categories: Iterable[str] = firewall.DEFAULT_ENABLED_CATEGORIES,
) -> None:
    """Synchronous wrapper for ``python -m egress_proxy start``. Runs
    the asyncio event loop until SIGINT / SIGTERM. Used by the
    long-running daemon under ``./run.sh start``."""
    async def runner() -> None:
        server = await serve_async(bind=bind, port=port, categories=categories)
        async with server:
            await server.serve_forever()
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("egress proxy: interrupted, shutting down")


# ---------------------------------------------------------------------------
# Reachability probe (used by doctor)
# ---------------------------------------------------------------------------


def is_listening(
    *,
    bind: str = firewall.PROXY_BIND,
    port: int = firewall.PROXY_PORT,
    timeout: float = 0.5,
) -> bool:
    """``True`` if something is accepting on ``bind:port``. Used by
    doctor + the launcher wrapper to assert the proxy is up before
    pointing Char at it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((bind, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ETIMEDOUT):
            return False
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_start(args: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="egress_proxy start")
    p.add_argument("--bind", default=firewall.PROXY_BIND)
    p.add_argument("--port", type=int, default=firewall.PROXY_PORT)
    p.add_argument(
        "--strict", action="store_true",
        help="enable the char_cloud category too (calendar / integrations)",
    )
    ns = p.parse_args(args)
    cats = set(firewall.DEFAULT_ENABLED_CATEGORIES)
    if ns.strict:
        cats.add("char_cloud")
    logging.basicConfig(
        level=os.environ.get("LOCAL_SCRIBE_PROXY_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s egress_proxy %(message)s",
    )
    serve_blocking(bind=ns.bind, port=ns.port, categories=cats)
    return 0


def _cli_status(_args: list[str]) -> int:
    up = is_listening()
    out = {
        "listening": up,
        "bind": firewall.PROXY_BIND,
        "port": firewall.PROXY_PORT,
        "counts": audit_ring.counts(),
    }
    print(json.dumps(out, indent=2))
    return 0


def _cli_recent(args: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="egress_proxy recent")
    p.add_argument("-n", type=int, default=50,
                   help="how many recent decisions to show")
    ns = p.parse_args(args)
    rows = [d.to_dict() for d in audit_ring.recent(ns.n)]
    print(json.dumps(rows, indent=2, default=str))
    return 0


def _cli_verify(args: list[str]) -> int:
    """Round-trip the proxy: open a CONNECT to a known-blocked host
    via the running proxy and assert it returns 403."""
    import argparse
    p = argparse.ArgumentParser(prog="egress_proxy verify")
    p.add_argument("--host", default="api.openai.com",
                   help="hostname to probe (must be in the catalog)")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--timeout", type=float, default=3.0)
    ns = p.parse_args(args)
    if not is_listening():
        print(json.dumps({"ok": False, "error": "proxy not listening"}))
        return 1
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ns.timeout)
    try:
        s.connect((firewall.PROXY_BIND, firewall.PROXY_PORT))
        req = (
            f"CONNECT {ns.host}:{ns.port} HTTP/1.1\r\n"
            f"Host: {ns.host}:{ns.port}\r\n\r\n"
        ).encode("ascii")
        s.sendall(req)
        # Read up to 4 KB of response.
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        first_line, _, _ = buf.partition(b"\r\n")
        status = first_line.decode("latin-1", errors="replace")
        expected_blocked = firewall.is_blocked(ns.host) is not None
        ok = (("403" in status) if expected_blocked
              else ("200" in status))
        print(json.dumps({
            "ok": ok,
            "host": ns.host,
            "port": ns.port,
            "expected_blocked": expected_blocked,
            "status_line": status,
        }, indent=2))
        return 0 if ok else 1
    finally:
        s.close()


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: python -m egress_proxy {start|status|recent|verify} [...]")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "start":
        return _cli_start(rest)
    if cmd == "status":
        return _cli_status(rest)
    if cmd == "recent":
        return _cli_recent(rest)
    if cmd == "verify":
        return _cli_verify(rest)
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
