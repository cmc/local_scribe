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

from local_scribe.egress import firewall


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
    """Smoke-test the argparse layer. ``enable`` / ``disable`` in
    --mode system aren't invoked here because they require sudo;
    --mode process is a no-op and IS exercised below."""

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

    def test_mode_subcommand_runs(self):
        # Smoke-test: the ``mode`` subcommand must print JSON and
        # return 0 regardless of whether system-hosts is active.
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = firewall.main(["mode"])
        self.assertEqual(rc, 0)
        import json as _json
        out = _json.loads(buf.getvalue())
        self.assertEqual(out["default_mode"], "process")
        self.assertIn("process", out["supported_modes"])
        self.assertIn("system", out["supported_modes"])
        self.assertEqual(out["proxy_port"], firewall.PROXY_PORT)

    def test_enable_process_mode_is_noop(self):
        # --mode process must NOT call sudo / osascript / touch
        # /etc/hosts. We confirm by patching the internal hosts-file
        # path to a tempfile that DOES NOT EXIST and asserting it
        # still doesn't get created.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ghost = Path(td) / "etc_hosts_should_not_exist"
            with mock.patch.object(firewall, "DEFAULT_HOSTS_PATH", ghost):
                rc = firewall.main(["enable", "--mode", "process"])
            self.assertEqual(rc, 0)
            self.assertFalse(
                ghost.exists(),
                "process-mode enable must not create /etc/hosts surrogate",
            )

    def test_disable_process_mode_is_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ghost = Path(td) / "etc_hosts_should_not_exist"
            with mock.patch.object(firewall, "DEFAULT_HOSTS_PATH", ghost):
                rc = firewall.main(["disable", "--mode", "process"])
            self.assertEqual(rc, 0)
            self.assertFalse(ghost.exists())


class ModeTests(unittest.TestCase):
    """Sanity-check the Mode enum + default-mode invariant."""

    def test_default_is_process(self):
        # Switching the default away from process would silently
        # break the "no sudo on bootstrap" promise. Pin it.
        self.assertEqual(firewall.DEFAULT_MODE, firewall.Mode.PROCESS_PROXY)

    def test_mode_values_are_str(self):
        # CLI hands us ``args.mode`` as a string; the Mode constructor
        # has to accept those strings. Pin the enum values so a
        # future rename doesn't silently break --mode parsing.
        self.assertEqual(firewall.Mode("process"),
                         firewall.Mode.PROCESS_PROXY)
        self.assertEqual(firewall.Mode("system"),
                         firewall.Mode.SYSTEM_HOSTS)


class ElevationExplanationTests(unittest.TestCase):
    """Pin down the UX of the privileged-install prompts.

    Two surfaces:
      * the multi-line *stderr banner* that operators in a terminal
        see while sudo is about to ask for their password;
      * the single-line *AppleScript prompt* that goes into the
        ``do shell script ... with prompt "..."`` clause -- this is
        what the macOS auth dialog shows above the password field.

    If either drifts (gets generic, drops the ``/etc/hosts`` mention,
    forgets to surface the backup path, etc.) users start seeing
    "osascript wants to make changes" with no context, which
    conditions them to type their password without understanding
    what's being changed. Pin the invariants here.
    """

    def test_install_banner_mentions_hosts_and_backup(self) -> None:
        banner, _ = firewall._explain_intent(
            "install",
            hosts_path=Path("/etc/hosts"),
            backup_path=Path("/etc/hosts.local_scribe.bak.20260510"),
            n_blocks=27, n_categories=2,
        )
        self.assertIn("/etc/hosts", banner)
        self.assertIn("local_scribe", banner)
        self.assertIn("install", banner.lower())
        self.assertIn("backup", banner.lower())
        self.assertIn("hosts.local_scribe.bak.20260510", banner)
        self.assertIn("undo", banner.lower())

    def test_install_banner_includes_scope_count(self) -> None:
        banner, _ = firewall._explain_intent(
            "install",
            hosts_path=Path("/etc/hosts"),
            backup_path=None,
            n_blocks=42, n_categories=3,
        )
        # Operator should see how many hosts are getting blackholed
        # -- "edits /etc/hosts" with no count tells them nothing
        # about the blast radius.
        self.assertIn("42", banner)
        self.assertIn("3", banner)

    def test_install_prompt_is_one_line_under_cap(self) -> None:
        _, prompt = firewall._explain_intent(
            "install",
            hosts_path=Path("/etc/hosts"),
            backup_path=Path("/etc/hosts.local_scribe.bak.20260510"),
            n_blocks=27, n_categories=2,
        )
        # AppleScript dialog header has a hard truncation at ~255
        # chars; we cap at 250 for safety. Verify the cap holds.
        self.assertLessEqual(len(prompt), firewall._AS_PROMPT_MAX)
        # No newlines -- the dialog renders them inconsistently.
        self.assertNotIn("\n", prompt)
        self.assertNotIn("\r", prompt)
        # Must identify the app + the action + the file. If any of
        # these are missing, the dialog becomes "osascript wants to
        # make changes" which is exactly what we're trying to avoid.
        self.assertIn("local_scribe", prompt)
        self.assertIn("/etc/hosts", prompt)
        self.assertIn("install", prompt.lower())

    def test_remove_banner_and_prompt(self) -> None:
        banner, prompt = firewall._explain_intent(
            "remove",
            hosts_path=Path("/etc/hosts"),
            backup_path=Path("/etc/hosts.local_scribe.bak.20260510"),
        )
        self.assertIn("remove", banner.lower())
        self.assertIn("backup", banner.lower())
        self.assertIn("redo", banner.lower())
        # Prompt invariants identical to install except the verb.
        self.assertLessEqual(len(prompt), firewall._AS_PROMPT_MAX)
        self.assertNotIn("\n", prompt)
        self.assertIn("local_scribe", prompt)
        self.assertIn("/etc/hosts", prompt)
        self.assertIn("remove", prompt.lower())

    def test_applescript_string_escapes(self) -> None:
        # AppleScript needs ``\\`` for backslash, ``\\"`` for quote.
        # Everything else passes through. We also strip CRs so the
        # dialog doesn't render them as glyphs.
        self.assertEqual(firewall._applescript_string("hello"), "hello")
        self.assertEqual(firewall._applescript_string(r"a\b"), r"a\\b")
        self.assertEqual(firewall._applescript_string('a"b'), r'a\"b')
        # Truncation + ellipsis.
        long = "x" * 400
        out = firewall._applescript_string(long)
        self.assertEqual(len(out), firewall._AS_PROMPT_MAX)
        self.assertTrue(out.endswith("…"))

    def test_build_elevation_cmd_includes_with_prompt(self) -> None:
        # The critical invariant: every osascript invocation MUST
        # pass ``with prompt "..."`` so the dialog shows our context
        # rather than the generic "osascript wants to make changes".
        cmd = firewall._build_elevation_cmd(
            "echo ok", elevation="osascript",
            osascript_prompt="hello world",
        )
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        script = cmd[2]
        self.assertIn('with prompt "hello world"', script)
        self.assertIn("with administrator privileges", script)
        # AppleScript parser requires ``with prompt`` BEFORE
        # ``with administrator privileges``. If a refactor swaps
        # them the dialog still works on some macOS versions but
        # not all; pin the order.
        self.assertLess(
            script.index("with prompt"),
            script.index("with administrator privileges"),
            "with prompt must precede with administrator privileges",
        )

    def test_build_elevation_cmd_sudo_path(self) -> None:
        # Sudo path doesn't take a prompt (sudo's own "Password:"
        # line is what the user sees), but the function must still
        # return a runnable argv.
        cmd = firewall._build_elevation_cmd(
            "echo ok", elevation="sudo",
            osascript_prompt="ignored in sudo path",
        )
        self.assertEqual(cmd[0], "sudo")
        self.assertIn("echo ok", cmd[-1])

    def test_build_elevation_cmd_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            firewall._build_elevation_cmd(
                "echo ok", elevation="magic",
                osascript_prompt="x",
            )

    def test_install_calls_with_prompt(self) -> None:
        """End-to-end: drive install() against a tempfile, mock
        subprocess.run, assert the captured argv contains a
        ``with prompt`` clause. This is the regression test for the
        actual user-visible dialog."""
        import tempfile, os as _os
        from unittest import mock as _mock

        with tempfile.TemporaryDirectory() as td:
            hosts = Path(td) / "hosts"
            hosts.write_text("127.0.0.1 localhost\n")
            captured = []

            def fake_run(cmd, **kw):
                captured.append(cmd)
                class R:  # noqa: D401
                    returncode = 0
                    stdout = stderr = ""
                return R()

            with _mock.patch.object(firewall.subprocess, "run",
                                    side_effect=fake_run), \
                 _mock.patch.object(firewall, "flush_dns_cache",
                                    return_value=(True, "")):
                ok, msg = firewall.install(
                    firewall.DEFAULT_ENABLED_CATEGORIES,
                    mode=firewall.Mode.SYSTEM_HOSTS,
                    hosts_path=hosts,
                    elevation="osascript",
                    backup=True,
                )

            self.assertTrue(ok, msg)
            self.assertGreaterEqual(len(captured), 1)
            argv = captured[0]
            self.assertEqual(argv[0], "osascript")
            # The complete AppleScript invocation:
            script = argv[2]
            self.assertIn("with prompt", script)
            self.assertIn("local_scribe", script)
            # Mentions the host file path so the user sees what's
            # actually being edited.
            self.assertIn(str(hosts), script)


class IsBlockedTests(unittest.TestCase):
    """``is_blocked`` is the predicate :mod:`egress_proxy` consumes.
    Cover the matching semantics here so a regression that affected
    only the proxy gets caught at the firewall layer too."""

    def test_exact_match(self):
        e = firewall.is_blocked("api.openai.com")
        self.assertIsNotNone(e)
        self.assertEqual(e.category, "providers")

    def test_subdomain_match(self):
        # Regional / sharded subdomains must inherit the parent's
        # block. Providers commonly shard via $region.api.* names.
        self.assertIsNotNone(firewall.is_blocked("eu.api.openai.com"))
        self.assertIsNotNone(firewall.is_blocked("a.b.c.api.openai.com"))

    def test_not_a_subdomain(self):
        # ``openai.com.attacker.tld`` is NOT a subdomain of
        # openai.com. We must not match it.
        self.assertIsNone(firewall.is_blocked("openai.com.attacker.tld"))
        # And the entry's own hostname must match itself but not the
        # sibling.
        self.assertIsNone(firewall.is_blocked("openaipi.com"))

    def test_case_insensitive(self):
        self.assertIsNotNone(firewall.is_blocked("API.OpenAI.com"))

    def test_trailing_dot_stripped(self):
        self.assertIsNotNone(firewall.is_blocked("api.openai.com."))

    def test_blank_input_safe(self):
        self.assertIsNone(firewall.is_blocked(""))
        self.assertIsNone(firewall.is_blocked("   "))
        self.assertIsNone(firewall.is_blocked("."))

    def test_char_cloud_category_off_by_default(self):
        self.assertIsNone(firewall.is_blocked("api.char.com"))
        self.assertIsNotNone(firewall.is_blocked(
            "api.char.com", categories=firewall.ALL_CATEGORIES,
        ))

    def test_category_filter(self):
        # Restrict to one category that does NOT include the entry.
        self.assertIsNone(firewall.is_blocked(
            "api.openai.com", categories=["telemetry"],
        ))
        # ... and one that does.
        self.assertIsNotNone(firewall.is_blocked(
            "api.openai.com", categories=["providers"],
        ))


if __name__ == "__main__":
    unittest.main()
