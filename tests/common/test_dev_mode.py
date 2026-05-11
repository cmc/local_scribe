"""Unit tests for local_scribe.common.dev_mode.

The dev-mode flag is a deliberately loud, documented escape hatch
for the SIP gate. The contract these tests pin:

  * ``is_enabled()`` returns True iff the env var is set to a
    truthy value (and False for the documented off-values).
  * ``format_banner()`` always contains the literal phrase
    ``DEV MODE ACTIVE`` so CI log scrapers can detect bypass
    conditions by string match.
  * ``emit_banner_once()`` writes the banner exactly once per
    process to the given stream, ``reset_for_tests()`` clears the
    one-shot flag.
  * Truthy/falsy parsing matches the table documented in the
    module docstring and the parser in run.sh.

Integration with ``sip_check.enforce_or_die`` is covered in
``tests/security/test_sip_check.py::EnforceDevModeBypassTests``.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock


class _CleanEnvMixin:
    """Save/restore ``LOCAL_SCRIBE_DEV_MODE`` so tests don't
    leak env state. Use as a mixin before ``unittest.TestCase`` so
    the standard ``setUp`` ordering applies."""

    def setUp(self):  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._saved_env = os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)
        from local_scribe.common import dev_mode as _dev
        _dev.reset_for_tests()

    def tearDown(self):  # type: ignore[override]
        from local_scribe.common import dev_mode as _dev
        _dev.reset_for_tests()
        if self._saved_env is not None:
            os.environ["LOCAL_SCRIBE_DEV_MODE"] = self._saved_env
        else:
            os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)
        super().tearDown()  # type: ignore[misc]


class IsEnabledParsingTests(_CleanEnvMixin, unittest.TestCase):
    """The truthy/falsy table for ``LOCAL_SCRIBE_DEV_MODE``. We
    intentionally match the same off-values the run.sh shim
    recognises so the two implementations agree."""

    def test_unset_is_disabled(self):
        from local_scribe.common import dev_mode
        os.environ.pop("LOCAL_SCRIBE_DEV_MODE", None)
        self.assertFalse(dev_mode.is_enabled())

    def test_off_values_are_disabled(self):
        from local_scribe.common import dev_mode
        for val in ("", "0", "false", "FALSE", "False",
                    "no", "NO", "off", "OFF"):
            with self.subTest(val=val):
                os.environ["LOCAL_SCRIBE_DEV_MODE"] = val
                self.assertFalse(dev_mode.is_enabled(),
                                 f"expected off for {val!r}")

    def test_truthy_values_enable(self):
        from local_scribe.common import dev_mode
        for val in ("1", "true", "True", "TRUE", "yes",
                    "on", "anything-non-empty", "  yes  "):
            with self.subTest(val=val):
                os.environ["LOCAL_SCRIBE_DEV_MODE"] = val
                self.assertTrue(dev_mode.is_enabled(),
                                f"expected on for {val!r}")


class BannerShapeTests(unittest.TestCase):
    """The banner has CI-stable wording — log scrapers and the
    inspector tests grep for ``DEV MODE ACTIVE`` and the env var
    name. Changing those phrases is a breaking change for those
    consumers."""

    def test_banner_contains_required_phrases(self):
        from local_scribe.common import dev_mode
        for color in (False, True):
            with self.subTest(color=color):
                banner = dev_mode.format_banner(color=color)
                self.assertIn("DEV MODE ACTIVE", banner)
                self.assertIn("LOCAL_SCRIBE_DEV_MODE", banner)
                self.assertIn("SIP", banner)
                # Operator-recovery instructions must be present
                # so the banner is actionable, not just scary.
                self.assertIn("./run.sh stop", banner)
                self.assertIn("./run.sh start", banner)

    def test_banner_color_false_has_no_ansi(self):
        from local_scribe.common import dev_mode
        banner = dev_mode.format_banner(color=False)
        # Two common ANSI escape introducers we should not see.
        self.assertNotIn("\033[", banner)
        self.assertNotIn("\x1b[", banner)

    def test_banner_color_true_has_ansi(self):
        from local_scribe.common import dev_mode
        banner = dev_mode.format_banner(color=True)
        self.assertIn("\033[", banner)


class EmitBannerOnceTests(_CleanEnvMixin, unittest.TestCase):
    """``emit_banner_once`` is the once-per-process side-effecting
    helper that the SIP-gate path calls. We pin:

      * First call writes; returns True.
      * Subsequent calls don't write; return False.
      * ``reset_for_tests`` undoes the one-shot flag.
      * ``stream=None`` defaults to stderr.
    """

    def test_first_call_writes_and_returns_true(self):
        from local_scribe.common import dev_mode
        buf = io.StringIO()
        emitted = dev_mode.emit_banner_once(buf, color=False)
        self.assertTrue(emitted)
        self.assertIn("DEV MODE ACTIVE", buf.getvalue())

    def test_second_call_is_silent_and_returns_false(self):
        from local_scribe.common import dev_mode
        buf1 = io.StringIO()
        buf2 = io.StringIO()
        self.assertTrue(dev_mode.emit_banner_once(buf1, color=False))
        self.assertFalse(dev_mode.emit_banner_once(buf2, color=False))
        self.assertEqual(buf2.getvalue(), "")

    def test_reset_for_tests_clears_one_shot(self):
        from local_scribe.common import dev_mode
        buf1 = io.StringIO()
        buf2 = io.StringIO()
        dev_mode.emit_banner_once(buf1, color=False)
        dev_mode.reset_for_tests()
        # After reset, the next call must actually write again.
        self.assertTrue(dev_mode.emit_banner_once(buf2, color=False))
        self.assertIn("DEV MODE ACTIVE", buf2.getvalue())

    def test_default_stream_is_stderr(self):
        """When ``stream`` is None we go to ``sys.stderr`` — the
        same channel ``sip_check.format_banner`` uses."""
        from local_scribe.common import dev_mode
        captured: list[str] = []
        with mock.patch("sys.stderr") as fake_stderr:
            fake_stderr.write.side_effect = captured.append
            fake_stderr.isatty.return_value = False
            dev_mode.emit_banner_once()
        combined = "".join(captured)
        self.assertIn("DEV MODE ACTIVE", combined)


class ShortIndicatorTests(unittest.TestCase):
    """``short_indicator`` is the per-gate marker used after the
    long banner has already fired. It does NOT consult or update
    the one-shot flag, so callers can use it freely without
    invalidating ``emit_banner_once``'s contract."""

    def test_returns_marker_in_both_color_modes(self):
        from local_scribe.common import dev_mode
        self.assertIn("[DEV MODE]", dev_mode.short_indicator(color=False))
        self.assertIn("[DEV MODE]", dev_mode.short_indicator(color=True))

    def test_does_not_touch_one_shot_flag(self):
        from local_scribe.common import dev_mode
        dev_mode.reset_for_tests()
        _ = dev_mode.short_indicator(color=False)
        # The one-shot flag should still be False; calling
        # emit_banner_once now should succeed (return True).
        buf = io.StringIO()
        self.assertTrue(dev_mode.emit_banner_once(buf, color=False))


class EnvVarConstantTests(unittest.TestCase):
    """The env var name is part of the public contract — the
    inspector front-end and the run.sh shim hard-code it. We pin
    the value so a typo can't sneak past review."""

    def test_env_var_constant(self):
        from local_scribe.common import dev_mode
        self.assertEqual(dev_mode.ENV_VAR, "LOCAL_SCRIBE_DEV_MODE")


if __name__ == "__main__":
    unittest.main()
