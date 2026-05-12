"""Unit tests for local_scribe.common.touch_prompts.

The touch-prompts module produces the two loud terminal banners that
fire immediately before a Touch ID modal or a YubiKey-tap blocking
wait. Adding this module on 2026-05-11 closed a UX gap that caused
six "is the terminal hung?" moments in a single afternoon — the
banners are how every future operator knows the pipeline is waiting
on a physical action, not silently stuck.

Contract pinned by these tests
------------------------------

  * ``format_touch_id_banner`` and ``format_yubikey_banner`` are pure
    functions of their inputs (no env reads, no stream writes); easy
    to drive in tests.

  * The literal grep-able phrases ``"TOUCH ID PROMPT INCOMING"`` and
    ``"TAP YOUR YUBIKEY NOW"`` are present in every banner output
    whether or not color is enabled, so CI log scrapers can detect
    blocking waits with a string match.

  * The ``reason``/``message`` argument is embedded verbatim into
    the banner so the operator can see WHY they're being prompted.

  * Color escapes are present only when ``color=True`` and absent
    otherwise; non-TTY callers get plain text.

  * ``print_touch_id_imminent`` / ``cli_yubikey_prompt`` are silent
    under ``LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS=1``.

  * Both side-effect helpers default ``color`` to the stream's
    ``isatty()`` so capture-into-StringIO consumers get plain
    text automatically.

  * Off-values for the quiet env var follow the same convention as
    ``LOCAL_SCRIBE_DEV_MODE``: empty / 0 / false / no / off all mean
    "noisy" (banners enabled). Any other value means "quiet".

Integration with ``key_lifecycle.unlock_master_key`` is covered by
``tests/security/test_key_lifecycle_touch_prompts.py``.
"""

from __future__ import annotations

import io
import os
import unittest


class _CleanEnvMixin:
    """Save/restore ``LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS`` so tests
    don't leak env state. Mirrors ``test_dev_mode._CleanEnvMixin``."""

    def setUp(self):  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._saved = os.environ.pop("LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS", None)

    def tearDown(self):  # type: ignore[override]
        if self._saved is not None:
            os.environ["LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS"] = self._saved
        else:
            os.environ.pop("LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS", None)
        super().tearDown()  # type: ignore[misc]


class FormatTouchIdBannerTests(unittest.TestCase):
    def test_contains_grepable_heading(self) -> None:
        from local_scribe.common import touch_prompts as tp
        out = tp.format_touch_id_banner("Unlock vault", color=False)
        self.assertIn("TOUCH ID PROMPT INCOMING", out)

    def test_embeds_reason_verbatim(self) -> None:
        from local_scribe.common import touch_prompts as tp
        reason = "Sign local_scribe pinned config"
        out = tp.format_touch_id_banner(reason, color=False)
        self.assertIn(reason, out)

    def test_no_color_means_no_escape_codes(self) -> None:
        from local_scribe.common import touch_prompts as tp
        out = tp.format_touch_id_banner("x", color=False)
        self.assertNotIn("\033[", out, "ANSI escape leaked when color=False")

    def test_color_true_emits_escape_codes(self) -> None:
        from local_scribe.common import touch_prompts as tp
        out = tp.format_touch_id_banner("x", color=True)
        self.assertIn("\033[", out, "color=True produced no escapes")


class FormatYubikeyBannerTests(unittest.TestCase):
    def test_contains_grepable_heading(self) -> None:
        from local_scribe.common import touch_prompts as tp
        out = tp.format_yubikey_banner("Please touch your YubiKey", color=False)
        self.assertIn("TAP YOUR YUBIKEY NOW", out)

    def test_embeds_message_verbatim(self) -> None:
        from local_scribe.common import touch_prompts as tp
        msg = "Please touch your YubiKey to decrypt yk_half."
        out = tp.format_yubikey_banner(msg, color=False)
        self.assertIn(msg, out)

    def test_mentions_flashing_light(self) -> None:
        """Operator-feedback contract: the banner must name the
        flashing green LED so the operator knows what to look for.
        Removing this string breaks the UX promise; bump the test
        only if you also update the banner to describe the new cue."""
        from local_scribe.common import touch_prompts as tp
        out = tp.format_yubikey_banner("x", color=False)
        self.assertIn("flashing", out.lower())

    def test_mentions_timeout(self) -> None:
        """Operator-feedback contract: the banner must warn about the
        60 s window so a hesitant operator knows they have a deadline."""
        from local_scribe.common import touch_prompts as tp
        out = tp.format_yubikey_banner("x", color=False)
        self.assertIn("60", out)


class PrintTouchIdImminentTests(_CleanEnvMixin, unittest.TestCase):
    def test_writes_to_provided_stream(self) -> None:
        from local_scribe.common import touch_prompts as tp
        buf = io.StringIO()
        tp.print_touch_id_imminent("Unlock vault", stream=buf, color=False)
        self.assertIn("TOUCH ID PROMPT INCOMING", buf.getvalue())
        self.assertIn("Unlock vault", buf.getvalue())

    def test_silent_when_quiet_env_set(self) -> None:
        from local_scribe.common import touch_prompts as tp
        os.environ["LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS"] = "1"
        buf = io.StringIO()
        tp.print_touch_id_imminent("Unlock vault", stream=buf, color=False)
        self.assertEqual(buf.getvalue(), "")

    def test_off_values_keep_banner_noisy(self) -> None:
        """The off-values table from the docstring."""
        from local_scribe.common import touch_prompts as tp
        for v in ("", "0", "false", "no", "off", "False", "OFF"):
            os.environ["LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS"] = v
            buf = io.StringIO()
            tp.print_touch_id_imminent("x", stream=buf, color=False)
            self.assertIn(
                "TOUCH ID", buf.getvalue(),
                f"off-value {v!r} unexpectedly silenced the banner",
            )

    def test_color_default_follows_isatty(self) -> None:
        """Non-TTY StringIO should default to color=False
        (no ANSI escapes leak into captured logs)."""
        from local_scribe.common import touch_prompts as tp
        buf = io.StringIO()
        tp.print_touch_id_imminent("x", stream=buf)
        self.assertNotIn("\033[", buf.getvalue())

    def test_does_not_raise_on_broken_stream(self) -> None:
        """Streams whose ``flush()`` raises (e.g. closed file
        handles) shouldn't propagate the exception — the unlock
        flow must continue regardless of banner-write hiccups."""
        from local_scribe.common import touch_prompts as tp

        class BrokenStream:
            def write(self, s): pass

            def flush(self): raise IOError("nope")

            def isatty(self): return False

        tp.print_touch_id_imminent("x", stream=BrokenStream())


class CliYubikeyPromptTests(_CleanEnvMixin, unittest.TestCase):
    def test_default_callback_writes_banner(self) -> None:
        """``cli_yubikey_prompt`` is wired as the default
        ``on_touch_prompt`` callback inside ``unlock_master_key``;
        it must write to stderr (we test via the ``stream=`` seam)."""
        from local_scribe.common import touch_prompts as tp
        buf = io.StringIO()
        tp.cli_yubikey_prompt(
            "Please touch your YubiKey to decrypt yk_half.",
            stream=buf, color=False,
        )
        self.assertIn("TAP YOUR YUBIKEY", buf.getvalue())
        self.assertIn("yk_half", buf.getvalue())

    def test_callback_signature_accepts_single_string(self) -> None:
        """Contract: ``yubikey_backup._age_decrypt`` calls
        ``on_touch_prompt(message)`` with one positional argument.
        This test pins that calling convention so a future refactor
        can't silently break the integration."""
        from local_scribe.common import touch_prompts as tp
        # Should not raise.
        tp.cli_yubikey_prompt("single positional arg",
                              stream=io.StringIO(), color=False)

    def test_silent_when_quiet_env_set(self) -> None:
        from local_scribe.common import touch_prompts as tp
        os.environ["LOCAL_SCRIBE_QUIET_TOUCH_PROMPTS"] = "1"
        buf = io.StringIO()
        tp.cli_yubikey_prompt("x", stream=buf, color=False)
        self.assertEqual(buf.getvalue(), "")


class BannerOrderTests(unittest.TestCase):
    """The two banners are designed to fire in a strict sequence —
    Touch ID first (Keychain read), YubiKey second (age decrypt).
    The ORDER comes from ``unlock_master_key``'s implementation, not
    from this module, but we pin the visual contract here so a future
    edit can't accidentally cross-wire them."""

    def test_touch_id_banner_color_is_yellow(self) -> None:
        """Yellow = "heads up, less destructive than YubiKey"."""
        from local_scribe.common import touch_prompts as tp
        out = tp.format_touch_id_banner("x", color=True)
        self.assertIn("\033[33m", out, "Touch ID banner should be yellow")

    def test_yubikey_banner_color_is_red(self) -> None:
        """Red = "physical action required NOW; loud"."""
        from local_scribe.common import touch_prompts as tp
        out = tp.format_yubikey_banner("x", color=True)
        self.assertIn("\033[31m", out, "YubiKey banner should be red")


if __name__ == "__main__":
    unittest.main()
