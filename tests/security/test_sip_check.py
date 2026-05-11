"""Unit tests for sip_check.py.

The parser is tested against every csrutil output form we know
about (fully enabled, fully disabled, custom-configuration with
each subset off). The gate (enforce_or_die) is tested for fail-
closed behaviour on unknown/parse errors.

The live ``csrutil`` binary is never invoked — we drive the parser
via ``LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT`` so the tests work
identically on every CI host regardless of its SIP state.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock


# --- parser -------------------------------------------------------------


# Real outputs captured from macOS 14/15.
_FULLY_ENABLED = "System Integrity Protection status: enabled.\n"
_FULLY_DISABLED = "System Integrity Protection status: disabled.\n"
_CUSTOM_ALL_ON = """\
System Integrity Protection status: enabled (Custom Configuration).

Configuration:
\tApple Internal: disabled
\tKext Signing: enabled
\tFilesystem Protections: enabled
\tDebugging Restrictions: enabled
\tDTrace Restrictions: enabled
\tNVRAM Protections: enabled
\tBaseSystem Verification: enabled

This is an unsupported configuration, likely to break in the future and leave your machine in an unknown state.
"""
_CUSTOM_FS_OFF = """\
System Integrity Protection status: enabled (Custom Configuration).

Configuration:
\tApple Internal: disabled
\tKext Signing: enabled
\tFilesystem Protections: disabled
\tDebugging Restrictions: enabled
\tDTrace Restrictions: enabled
\tNVRAM Protections: enabled
\tBaseSystem Verification: enabled
"""
_CUSTOM_DTRACE_OFF = """\
System Integrity Protection status: enabled (Custom Configuration).

Configuration:
\tApple Internal: disabled
\tKext Signing: enabled
\tFilesystem Protections: enabled
\tDebugging Restrictions: enabled
\tDTrace Restrictions: disabled
\tNVRAM Protections: enabled
\tBaseSystem Verification: enabled
"""
_GARBLED = "Unrecognised output from csrutil\nfoo bar baz\n"


def _import_module():
    from local_scribe.security import sip_check
    return sip_check


class ParserTests(unittest.TestCase):
    def test_fully_enabled(self):
        m = _import_module()
        rep = m._parse(_FULLY_ENABLED)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.FULLY_ENABLED)
        self.assertEqual(rep.raw_top_line, "enabled.")
        self.assertEqual(rep.missing_protections, [])

    def test_fully_disabled(self):
        m = _import_module()
        rep = m._parse(_FULLY_DISABLED)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.DISABLED)
        # When SIP is fully off, every protection counts as missing.
        for name in m.REQUIRED_PROTECTIONS:
            self.assertIn(name, rep.missing_protections)

    def test_custom_config_all_on_is_full(self):
        m = _import_module()
        rep = m._parse(_CUSTOM_ALL_ON)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.FULLY_ENABLED)
        self.assertTrue(rep.protections["Filesystem Protections"])
        self.assertTrue(rep.protections["DTrace Restrictions"])
        # Apple Internal is informational; off here, doesn't matter.
        self.assertFalse(rep.protections.get("Apple Internal", True))

    def test_custom_config_filesystem_off_is_partial(self):
        m = _import_module()
        rep = m._parse(_CUSTOM_FS_OFF)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.PARTIALLY_DISABLED)
        self.assertIn("Filesystem Protections", rep.missing_protections)
        # Other protections still on are NOT listed as missing.
        self.assertNotIn("DTrace Restrictions", rep.missing_protections)

    def test_custom_config_dtrace_off_is_partial(self):
        m = _import_module()
        rep = m._parse(_CUSTOM_DTRACE_OFF)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.PARTIALLY_DISABLED)
        self.assertEqual(rep.missing_protections, ["DTrace Restrictions"])

    def test_garbled_output(self):
        m = _import_module()
        rep = m._parse(_GARBLED)  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.UNKNOWN)
        self.assertIsNotNone(rep.error)

    def test_empty_output(self):
        m = _import_module()
        rep = m._parse("")  # noqa: SLF001
        self.assertEqual(rep.state, m.SIPState.UNKNOWN)


# --- status (env override) ---------------------------------------------


class StatusEnvOverrideTests(unittest.TestCase):
    def _with_override(self, value):
        return mock.patch.dict(
            os.environ, {"LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT": value},
        )

    def test_status_uses_env_override(self):
        m = _import_module()
        with self._with_override(_FULLY_ENABLED):
            self.assertEqual(m.status().state, m.SIPState.FULLY_ENABLED)
        with self._with_override(_FULLY_DISABLED):
            self.assertEqual(m.status().state, m.SIPState.DISABLED)

    def test_is_fully_enabled(self):
        m = _import_module()
        with self._with_override(_FULLY_ENABLED):
            self.assertTrue(m.is_fully_enabled())
        with self._with_override(_FULLY_DISABLED):
            self.assertFalse(m.is_fully_enabled())
        with self._with_override(_CUSTOM_FS_OFF):
            self.assertFalse(m.is_fully_enabled())
        with self._with_override(_GARBLED):
            # UNKNOWN counts as not-enabled (fail closed).
            self.assertFalse(m.is_fully_enabled())


# --- enforce_or_die -----------------------------------------------------


class EnforceTests(unittest.TestCase):
    def setUp(self):
        # Make sure dev mode is OFF for these tests; the default
        # behaviour we're verifying here is the strict fail-closed
        # path. The dev-mode-bypass path has its own class below.
        self._saved_dev = os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)

    def tearDown(self):
        if self._saved_dev is not None:
            os.environ["LOCAL_SCRIBE_DEV_MODE"] = self._saved_dev

    def _with_output(self, value):
        return mock.patch.dict(
            os.environ, {"LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT": value},
        )

    def test_enforces_passes_on_fully_enabled(self):
        m = _import_module()
        with self._with_output(_FULLY_ENABLED):
            rep = m.enforce_or_die()
            self.assertEqual(rep.state, m.SIPState.FULLY_ENABLED)

    def test_enforces_raises_on_disabled(self):
        m = _import_module()
        with self._with_output(_FULLY_DISABLED):
            with self.assertRaises(m.SIPDisabledError) as ctx:
                m.enforce_or_die()
            self.assertIn("DISABLED", str(ctx.exception))

    def test_enforces_raises_on_partial(self):
        m = _import_module()
        with self._with_output(_CUSTOM_FS_OFF):
            with self.assertRaises(m.SIPDisabledError) as ctx:
                m.enforce_or_die()
            self.assertIn("Filesystem Protections",
                          str(ctx.exception))

    def test_enforces_raises_on_unknown(self):
        """Fail closed: if we can't parse, we refuse."""
        m = _import_module()
        with self._with_output(_GARBLED):
            with self.assertRaises(m.SIPDisabledError):
                m.enforce_or_die()


# --- dev-mode bypass ---------------------------------------------------


class EnforceDevModeBypassTests(unittest.TestCase):
    """``LOCAL_SCRIBE_DEV_MODE=1`` lets ``enforce_or_die`` return on
    every non-fully-enabled state (with a banner emitted to stderr)
    instead of raising. The strict variant
    (``enforce_or_die_strict``) ignores dev mode entirely so callers
    that need fail-closed semantics keep getting them.

    We also assert that the loud-banner side effect actually fires
    once per process — a silent bypass would be the worst possible
    failure mode for this feature."""

    def _with_output_and_dev(self, value):
        return mock.patch.dict(
            os.environ, {
                "LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT": value,
                "LOCAL_SCRIBE_DEV_MODE": "1",
            },
        )

    def setUp(self):
        # Reset the once-per-process banner flag so each test gets
        # a clean slate. (The production code also has a public
        # ``reset_for_tests`` helper for exactly this.)
        from local_scribe.common import dev_mode as _dev
        _dev.reset_for_tests()

    def tearDown(self):
        from local_scribe.common import dev_mode as _dev
        _dev.reset_for_tests()

    def test_dev_mode_returns_report_on_disabled(self):
        m = _import_module()
        buf_err = []
        with self._with_output_and_dev(_FULLY_DISABLED), \
                mock.patch("sys.stderr.write", side_effect=buf_err.append):
            rep = m.enforce_or_die()
        self.assertEqual(rep.state, m.SIPState.DISABLED)
        emitted = "".join(buf_err)
        self.assertIn("DEV MODE ACTIVE", emitted)
        self.assertIn("LOCAL_SCRIBE_DEV_MODE", emitted)

    def test_dev_mode_returns_report_on_partial(self):
        m = _import_module()
        with self._with_output_and_dev(_CUSTOM_FS_OFF), \
                mock.patch("sys.stderr.write"):
            rep = m.enforce_or_die()
        self.assertEqual(rep.state, m.SIPState.PARTIALLY_DISABLED)

    def test_dev_mode_returns_report_on_unknown(self):
        m = _import_module()
        with self._with_output_and_dev(_GARBLED), \
                mock.patch("sys.stderr.write"):
            rep = m.enforce_or_die()
        self.assertEqual(rep.state, m.SIPState.UNKNOWN)

    def test_dev_mode_banner_is_idempotent(self):
        """Second call within the process gets a short one-line
        marker, not the full banner — so an operator scanning logs
        for the loud warning sees it once, not on every gate."""
        m = _import_module()
        buf_err = []
        with self._with_output_and_dev(_FULLY_DISABLED), \
                mock.patch("sys.stderr.write", side_effect=buf_err.append):
            m.enforce_or_die()
            m.enforce_or_die()
        combined = "".join(buf_err)
        # The long banner phrase appears exactly once.
        self.assertEqual(combined.count("DEV MODE ACTIVE"), 1)
        # Each subsequent call still produces the short indicator
        # so the operator can grep for the per-gate bypass marker.
        self.assertIn("[DEV MODE]", combined)
        self.assertIn("sip_check.enforce_or_die: bypassed", combined)

    def test_dev_mode_off_value_does_not_bypass(self):
        """``LOCAL_SCRIBE_DEV_MODE=0`` (and the other documented
        off-values) must NOT enable the bypass — otherwise a
        well-meaning operator who explicitly sets the var to ``0``
        to disable dev mode would get the bypass anyway."""
        m = _import_module()
        for off_val in ("0", "false", "FALSE", "no", "off", ""):
            with self.subTest(off_val=off_val):
                with mock.patch.dict(
                    os.environ, {
                        "LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT": _FULLY_DISABLED,
                        "LOCAL_SCRIBE_DEV_MODE": off_val,
                    },
                ):
                    with self.assertRaises(m.SIPDisabledError):
                        m.enforce_or_die()

    def test_allow_dev_mode_false_overrides_env(self):
        """Even with ``LOCAL_SCRIBE_DEV_MODE=1`` set, an explicit
        ``allow_dev_mode=False`` kwarg must raise. This is the
        kwarg the key-rotation CLI uses for "I really do mean
        strict, no matter what shell I'm running in."""
        m = _import_module()
        with self._with_output_and_dev(_FULLY_DISABLED), \
                mock.patch("sys.stderr.write"):
            with self.assertRaises(m.SIPDisabledError):
                m.enforce_or_die(allow_dev_mode=False)

    def test_strict_variant_ignores_dev_mode(self):
        """``enforce_or_die_strict`` is the pre-dev-mode behaviour;
        it must raise regardless of the env var."""
        m = _import_module()
        with self._with_output_and_dev(_FULLY_DISABLED), \
                mock.patch("sys.stderr.write"):
            with self.assertRaises(m.SIPDisabledError):
                m.enforce_or_die_strict()


# --- CLI ---------------------------------------------------------------


class CliTests(unittest.TestCase):
    def _with_output(self, value):
        return mock.patch.dict(
            os.environ, {"LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT": value},
        )

    def test_cli_check_exit_0_on_enabled(self):
        m = _import_module()
        with self._with_output(_FULLY_ENABLED):
            self.assertEqual(m._cli_check([]), 0)  # noqa: SLF001

    def test_cli_check_exit_nonzero_on_disabled(self):
        m = _import_module()
        with self._with_output(_FULLY_DISABLED):
            self.assertEqual(m._cli_check([]), 1)  # noqa: SLF001

    def test_cli_status_emits_json(self):
        m = _import_module()
        # Make sure dev mode is OFF for this test so the
        # ``dev_mode_active`` field is False in the payload.
        with self._with_output(_FULLY_ENABLED), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)
            buf_out = []
            with mock.patch("sys.stdout.write", side_effect=buf_out.append):
                self.assertEqual(m._cli_status([]), 0)  # noqa: SLF001
        payload = json.loads("".join(buf_out))
        self.assertEqual(payload["state"], "fully_enabled")
        # The dev-mode bit is part of the documented JSON contract
        # so the inspector / doctor tile can consume it directly.
        self.assertIn("dev_mode_active", payload)
        self.assertFalse(payload["dev_mode_active"])

    def test_cli_status_reports_dev_mode_when_set(self):
        m = _import_module()
        with self._with_output(_FULLY_ENABLED), \
                mock.patch.dict(os.environ, {"LOCAL_SCRIBE_DEV_MODE": "1"}):
            buf_out = []
            with mock.patch("sys.stdout.write", side_effect=buf_out.append):
                self.assertEqual(m._cli_status([]), 0)  # noqa: SLF001
        payload = json.loads("".join(buf_out))
        self.assertTrue(payload["dev_mode_active"])

    def test_cli_banner_renders_for_partial(self):
        m = _import_module()
        with self._with_output(_CUSTOM_DTRACE_OFF):
            buf_err = []
            with mock.patch("sys.stderr.write", side_effect=buf_err.append):
                rc = m._cli_banner([])  # noqa: SLF001
        self.assertEqual(rc, 1)
        banner = "".join(buf_err)
        self.assertIn("REFUSING TO RUN", banner)
        self.assertIn("DTrace", banner)


# --- subprocess fallback (when override env is unset) ------------------


class SubprocessFallbackTests(unittest.TestCase):
    """The csrutil subprocess path is tested only for fail-closed
    behaviour when the binary is missing — the success path is
    covered by the env-override tests above and exercising the real
    csrutil on every CI host would be flaky."""

    def setUp(self):
        # Ensure the override env var is NOT set.
        self._saved = os.environ.pop("LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT"] = self._saved

    def test_missing_csrutil_is_unknown(self):
        m = _import_module()
        with mock.patch.object(m, "_CSRUTIL_PATH", "/no/such/binary"):
            rep = m.status()
            self.assertEqual(rep.state, m.SIPState.UNKNOWN)
            self.assertIsNotNone(rep.error)

    def test_missing_csrutil_enforce_raises(self):
        m = _import_module()
        with mock.patch.object(m, "_CSRUTIL_PATH", "/no/such/binary"):
            with self.assertRaises(m.SIPDisabledError):
                m.enforce_or_die()


if __name__ == "__main__":
    unittest.main()
