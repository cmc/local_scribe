"""Tests for firewall.py — render / parse / diff round-trip.

We don't touch the real ``/etc/hosts`` here; every test operates on a
string buffer or a tempfile so the suite is hermetic. The actual
elevation paths (``sudo`` + ``osascript``) are covered by the live
end-to-end run documented in SECURITY.md and not by unit tests
(neither can run unattended).
"""

from __future__ import annotations

import socket
import unittest
from pathlib import Path
from unittest import mock

import firewall


SAMPLE_HOSTS = """\
##
# Host Database
#
# localhost is used to configure the loopback interface
# when the system is booting.  Do not change this entry.
##
127.0.0.1\tlocalhost
255.255.255.255\tbroadcasthost
::1             localhost

# user added these manually
1.2.3.4         devbox.local
"""


class RenderBlockTests(unittest.TestCase):
    """``render_block`` produces deterministic, marker-delimited output."""

    def test_default_categories_render_includes_v4_and_v6_sinks(self):
        block = firewall.render_block()
        self.assertIn(firewall.BEGIN_MARKER, block)
        self.assertIn(firewall.END_MARKER, block)
        # Every host gets both a v4 and a v6 sink line.
        self.assertIn("0.0.0.0 us.i.posthog.com", block)
        self.assertIn("::      us.i.posthog.com", block)
        # char_cloud is *not* in the default set.
        self.assertNotIn("0.0.0.0 api.char.com", block)

    def test_strict_mode_includes_char_cloud(self):
        block = firewall.render_block(
            list(firewall.DEFAULT_ENABLED_CATEGORIES) + ["char_cloud"],
        )
        self.assertIn("0.0.0.0 api.char.com", block)

    def test_render_is_deterministic_across_calls(self):
        a = firewall.render_block()
        b = firewall.render_block()
        self.assertEqual(a, b, "render_block must be byte-stable for diff-clean re-applies")

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            firewall.render_block(["bogus"])

    def test_render_includes_self_documenting_comments(self):
        """A user `cat /etc/hosts` should see why each entry is there."""
        block = firewall.render_block()
        # Reason comment for the Sentry DSN.
        self.assertIn("Sentry DSN", block)
        # Pointer to the management command.
        self.assertIn("./run.sh firewall", block)

    def test_empty_categories_emits_markers_only(self):
        """Edge case: caller passes [] — we still emit markers so
        ``status`` can distinguish 'block installed but empty' from
        'block never installed'."""
        block = firewall.render_block([])
        self.assertIn(firewall.BEGIN_MARKER, block)
        self.assertIn(firewall.END_MARKER, block)
        self.assertNotIn("0.0.0.0", block)


class UpsertBlockTests(unittest.TestCase):
    """Round-trip: upsert, parse, remove, upsert-with-new-content."""

    def test_upsert_into_clean_hosts(self):
        out = firewall.upsert_block(SAMPLE_HOSTS)
        self.assertIn(firewall.BEGIN_MARKER, out)
        # User's existing entries survive verbatim.
        self.assertIn("1.2.3.4         devbox.local", out)

    def test_upsert_is_idempotent(self):
        once  = firewall.upsert_block(SAMPLE_HOSTS)
        twice = firewall.upsert_block(once)
        self.assertEqual(once, twice,
                         "re-running enable on an up-to-date file must be a no-op write")

    def test_upsert_replaces_existing_block(self):
        # First install with telemetry only; then upgrade to telemetry+providers.
        v1 = firewall.upsert_block(SAMPLE_HOSTS, ["telemetry"])
        self.assertIn("0.0.0.0 us.i.posthog.com", v1)
        self.assertNotIn("0.0.0.0 api.openai.com", v1)
        v2 = firewall.upsert_block(v1, ["telemetry", "providers"])
        self.assertIn("0.0.0.0 us.i.posthog.com", v2)
        self.assertIn("0.0.0.0 api.openai.com", v2)
        # Only one BEGIN marker still present (not duplicated).
        self.assertEqual(v2.count(firewall.BEGIN_MARKER), 1)

    def test_remove_block_returns_user_content_intact(self):
        installed = firewall.upsert_block(SAMPLE_HOSTS)
        removed = firewall.remove_block(installed)
        # Round-trip back to (roughly) the original; whitespace
        # tolerance is fine but content must match.
        self.assertNotIn(firewall.BEGIN_MARKER, removed)
        self.assertIn("1.2.3.4         devbox.local", removed)
        self.assertIn("127.0.0.1\tlocalhost", removed)

    def test_remove_block_on_clean_file_is_noop(self):
        self.assertEqual(firewall.remove_block(SAMPLE_HOSTS), SAMPLE_HOSTS)

    def test_partial_block_marker_only_begin_is_replaced(self):
        """Defensive: someone hand-deleted the END marker. We still
        recover by treating begin-to-EOF as the stale block."""
        broken = SAMPLE_HOSTS + "\n" + firewall.BEGIN_MARKER + "\n0.0.0.0 stale.example\n"
        fixed = firewall.upsert_block(broken)
        self.assertEqual(fixed.count(firewall.BEGIN_MARKER), 1)
        self.assertEqual(fixed.count(firewall.END_MARKER), 1)
        self.assertNotIn("stale.example", fixed)


class ParseManagedHostsTests(unittest.TestCase):
    def test_parses_one_hostname_per_managed_line(self):
        installed = firewall.upsert_block(SAMPLE_HOSTS)
        parsed = firewall.parse_managed_hosts(installed)
        # The default catalog has 19 hostnames (telemetry + providers).
        expected = [e.hostname for e in firewall.BLOCK_CATALOG
                    if e.category in firewall.DEFAULT_ENABLED_CATEGORIES]
        self.assertEqual(sorted(parsed), sorted(expected))

    def test_returns_empty_when_no_block_installed(self):
        self.assertEqual(firewall.parse_managed_hosts(SAMPLE_HOSTS), [])


class StatusTests(unittest.TestCase):
    def test_status_reports_full_coverage_after_install(self):
        installed = firewall.upsert_block(SAMPLE_HOSTS)
        s = firewall.status(installed)
        self.assertTrue(s.installed)
        for cat, c in s.coverage_by_category.items():
            self.assertEqual(c["blocked"], c["expected"],
                             f"{cat} expected to be fully covered")
        self.assertEqual(s.missing_by_category, {})

    def test_status_reports_drift_for_new_hosts_post_install(self):
        """If we add a new host to the catalog later, an existing
        install will read as 'drift' until they re-run enable."""
        # Synthesize an install that's missing one host the catalog
        # expects.
        block = firewall.render_block()
        stale = block.replace("0.0.0.0 api.openai.com\n", "")
        stale = stale.replace("::      api.openai.com\n", "")
        s = firewall.status(stale)
        self.assertTrue(s.installed)
        self.assertIn("providers", s.missing_by_category)
        self.assertIn("api.openai.com", s.missing_by_category["providers"])


class HostsFileTests(unittest.TestCase):
    """File-level round-trip against a real tempfile (no sudo, no real
    /etc/hosts)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".hosts", delete=False)
        self.tmp.write(SAMPLE_HOSTS)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def test_status_reads_from_disk(self):
        # Inject our block manually so the test doesn't need sudo.
        self.path.write_text(firewall.upsert_block(self.path.read_text()))
        s = firewall.status(hosts_path=self.path)
        self.assertTrue(s.installed)
        self.assertGreater(len(s.blocked_hostnames), 0)

    def test_status_handles_missing_file_gracefully(self):
        self.path.unlink()
        s = firewall.status(hosts_path=self.path)
        self.assertFalse(s.installed)
        self.assertEqual(s.blocked_hostnames, [])


class ResolveTests(unittest.TestCase):
    """``_resolve`` interprets sink addresses as 'blocked'."""

    def test_sink_v4_only_means_blocked(self):
        with mock.patch.object(socket, "getaddrinfo") as gai:
            gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("0.0.0.0", 0)),
            ]
            r = firewall._resolve("blocked.example")
        self.assertTrue(r.blocked)

    def test_sink_v6_only_means_blocked(self):
        with mock.patch.object(socket, "getaddrinfo") as gai:
            gai.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::", 0, 0, 0)),
            ]
            r = firewall._resolve("blocked6.example")
        self.assertTrue(r.blocked)

    def test_real_address_means_not_blocked(self):
        with mock.patch.object(socket, "getaddrinfo") as gai:
            gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("142.250.72.46", 0)),
            ]
            r = firewall._resolve("google.com")
        self.assertFalse(r.blocked)

    def test_oserror_is_treated_as_blocked(self):
        with mock.patch.object(socket, "getaddrinfo",
                               side_effect=OSError("not found")) as _:
            r = firewall._resolve("nowhere.invalid")
        self.assertTrue(r.blocked)
        self.assertIn("not found", r.error or "")

    def test_mixed_sink_and_real_means_not_blocked(self):
        with mock.patch.object(socket, "getaddrinfo") as gai:
            gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("0.0.0.0", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0)),
            ]
            r = firewall._resolve("leaky.example")
        self.assertFalse(r.blocked,
                         "any real address means the block isn't comprehensive")


class CLITests(unittest.TestCase):
    """Smoke-test the argparse layer. ``enable`` / ``disable`` aren't
    invoked here because they require sudo."""

    def test_status_subcommand_runs(self):
        with mock.patch.object(firewall, "status",
                               return_value=firewall.Status(
                                   installed=False,
                                   blocked_hostnames=[],
                                   coverage_by_category={},
                                   missing_by_category={})):
            rc = firewall.main(["status"])
        self.assertEqual(rc, 0)

    def test_list_subcommand_runs(self):
        self.assertEqual(firewall.main(["list"]), 0)
        self.assertEqual(firewall.main(["list", "--strict"]), 0)

    def test_verify_returns_1_if_anything_resolves(self):
        with mock.patch.object(firewall, "verify",
                               return_value=[firewall.ProbeResult(
                                   hostname="leaky.example",
                                   blocked=False, addresses=["1.2.3.4"])]):
            self.assertEqual(firewall.main(["verify"]), 1)

    def test_verify_returns_0_if_all_blocked(self):
        with mock.patch.object(firewall, "verify",
                               return_value=[firewall.ProbeResult(
                                   hostname="blocked.example",
                                   blocked=True, addresses=["0.0.0.0"])]):
            self.assertEqual(firewall.main(["verify"]), 0)


if __name__ == "__main__":
    unittest.main()
