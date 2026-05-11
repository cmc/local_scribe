"""Unit + integration tests for :mod:`char_sandbox`.

The unit tests are pure-string assertions on
:func:`char_sandbox.render_profile`. The integration tests shell out
to ``/usr/bin/sandbox-exec`` to verify that our profile actually
parses, applies, and enforces the documented invariants (deny
external egress, allow loopback). These integration tests are
macOS-only; they auto-skip on non-Darwin and on a hypothetical
future macOS that has removed the loader binary.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # tests/egress/ -> tests/ -> repo
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from local_scribe.egress import char_sandbox  # noqa: E402


_HAS_SANDBOX_EXEC = char_sandbox.is_available()


class RenderProfileTests(unittest.TestCase):
    def test_includes_version_form(self) -> None:
        prof = char_sandbox.render_profile()
        # ``(version 1)`` is required by SBPL; without it
        # sandbox-exec refuses to load the file.
        self.assertIn("(version 1)", prof)

    def test_denies_outbound_network(self) -> None:
        prof = char_sandbox.render_profile()
        self.assertIn("(deny network-outbound)", prof)
        self.assertIn("(deny network-bind)", prof)

    def test_reallows_loopback(self) -> None:
        # The net effect is "deny everything, except loopback". If
        # either of these rules disappears we'd silently lock Char
        # out of our own ASR / Inspector / LM Studio services.
        prof = char_sandbox.render_profile()
        self.assertIn('(allow network-outbound (remote ip "localhost:*"))',
                      prof)
        self.assertIn('(allow network-bind     (local  ip "localhost:*"))',
                      prof)
        self.assertIn("(allow network-outbound (remote unix-socket))",
                      prof)

    def test_starts_permissive(self) -> None:
        # Char is a full GUI Tauri app; locking down everything
        # would brick it. We must keep `(allow default)` as the
        # baseline.
        prof = char_sandbox.render_profile()
        self.assertIn("(allow default)", prof)

    def test_deterministic(self) -> None:
        a = char_sandbox.render_profile()
        b = char_sandbox.render_profile()
        self.assertEqual(a, b)

    def test_proxy_port_parameter_threads_through(self) -> None:
        # The profile is parameterised but the docstring is what
        # changes when the proxy port moves; the actual allow rule
        # is ``localhost:*`` (not port-specific) so all loopback
        # services keep working. Pin both invariants down.
        prof = char_sandbox.render_profile(proxy_port=9999)
        self.assertIn("9999", prof)
        self.assertIn('(allow network-outbound (remote ip "localhost:*"))',
                      prof)


class WriteProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved_env = os.environ.get("LOCAL_SCRIBE_CHAR_SANDBOX_PATH")
        self.profile_dest = Path(self.tmp.name) / "char.sb"
        os.environ["LOCAL_SCRIBE_CHAR_SANDBOX_PATH"] = str(self.profile_dest)

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("LOCAL_SCRIBE_CHAR_SANDBOX_PATH", None)
        else:
            os.environ["LOCAL_SCRIBE_CHAR_SANDBOX_PATH"] = self._saved_env

    def test_writes_profile_to_path(self) -> None:
        p = char_sandbox.write_profile()
        self.assertTrue(p.is_file())
        self.assertEqual(p, self.profile_dest)
        self.assertIn("(version 1)", p.read_text())

    def test_idempotent_no_mtime_bump_when_unchanged(self) -> None:
        p1 = char_sandbox.write_profile()
        mtime1 = p1.stat().st_mtime
        # Sleep a beat so a write would visibly bump mtime; we want
        # to assert it does NOT bump because the contents are equal.
        import time
        time.sleep(0.05)
        p2 = char_sandbox.write_profile()
        self.assertEqual(p1, p2)
        self.assertEqual(p1.stat().st_mtime, mtime1)

    def test_permissions_0600(self) -> None:
        p = char_sandbox.write_profile()
        mode = p.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


@unittest.skipUnless(_HAS_SANDBOX_EXEC,
                     "sandbox-exec not present on this host")
class SandboxIntegrationTests(unittest.TestCase):
    """End-to-end: invoke ``sandbox-exec -f profile`` against a
    trivial child and assert the policy enforced by the kernel
    matches what we render."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "char.sb"
        self.profile.write_text(char_sandbox.render_profile())

    def test_validate_profile_passes(self) -> None:
        ok, msg = char_sandbox.validate_profile(self.profile)
        self.assertTrue(ok, msg)

    def test_external_egress_denied(self) -> None:
        # nc -v against an external host should fail with "Operation
        # not permitted" (sandbox-denied connectx). We use
        # ``example.com:443`` which is universally reachable, so a
        # non-sandboxed test would succeed; under our profile we
        # expect EPERM. ``-v`` is required because nc is silent on
        # failure without it; ``-G 2`` caps the connect timeout at
        # 2s so an unrelated DNS hiccup doesn't hang the test.
        rc = subprocess.run(
            [char_sandbox.SANDBOX_EXEC, "-f", str(self.profile),
             "/usr/bin/nc", "-G", "2", "-v", "-z", "example.com", "443"],
            capture_output=True, text=True, timeout=10,
        )
        combined = (rc.stderr + rc.stdout).lower()
        self.assertNotEqual(rc.returncode, 0,
                            f"expected denial, got rc=0; output={combined!r}")
        # macOS surfaces sandbox-denied connectx as "Operation not
        # permitted" (EPERM). Distinguish from ECONNREFUSED /
        # ETIMEDOUT which would indicate the connection reached the
        # network stack -- only EPERM proves the kernel denied it
        # at the policy layer.
        self.assertIn("operation not permitted", combined,
                      f"expected EPERM, got: {combined!r}")

    def test_loopback_egress_allowed(self) -> None:
        # Bind a socket on a free loopback port, then connect from
        # inside the sandbox. ECONNREFUSED would mean nothing is
        # listening, but the connectx itself should NOT be policy-
        # denied. We use an actually-listening port so we can
        # distinguish "policy denied" from "nothing listening".
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            rc = subprocess.run(
                [char_sandbox.SANDBOX_EXEC, "-f", str(self.profile),
                 "/usr/bin/nc", "-G", "2", "-v", "-z",
                 "127.0.0.1", str(port)],
                capture_output=True, text=True, timeout=5,
            )
        finally:
            listener.close()
        self.assertEqual(
            rc.returncode, 0,
            f"loopback nc should have succeeded under sandbox; "
            f"stderr={rc.stderr!r} stdout={rc.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()
