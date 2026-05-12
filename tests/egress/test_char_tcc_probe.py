"""Unit tests for the macOS TCC attribution probe.

The probe queries ``/usr/bin/log show`` for ``tccd`` events
referring to Char.app's bundle and decides whether macOS's TCC
attribution chain has Char itself as the "responsible" bundle (the
healthy state) or some other bundle -- specifically a terminal
emulator -- which is the May 2026 regression signature where
system audio capture was silently denied.

These tests cover three layers, in increasing levels of integration:

1. Pure classification (``is_terminal_identifier`` + ``classify_chain``)
   against representative AttributionChain inputs. CI-safe; doesn't
   touch ``log show`` at all.

2. ``parse_attribution_chain`` against an ndjson sample of a real
   ``eventMessage`` produced by ``tccd`` on macOS 24 (Sequoia). Pinned
   to detect a future macOS log-format change that would silently
   regress the probe to "unknown".

3. ``probe()`` end-to-end with ``_run_log_show`` mocked to return
   canned ndjson. Verifies the state machine transitions
   (ok / terminal / no_events / no_logs) and the JSON shape consumed
   by ``./run.sh char firewall-status``.

The ``terminal`` case is the regression net for the bug -- if a
future change to ``cmd_char_launch`` re-introduces sandbox-exec or
otherwise puts the terminal back at the head of the launch chain,
the live probe will report ``state=terminal`` and the test below
asserts that path is wired through correctly.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from local_scribe.egress import char_tcc_probe  # noqa: E402


# ===========================================================================
# Fixtures
# ===========================================================================


# An AttributionChain produced by tccd on macOS 24 when Char.app was
# launched via ``open -a Char.app`` (the healthy path after the May
# 2026 fix). The responsible bundle is Char.app itself.
CHAIN_HEALTHY = [
    {
        "identifier": "com.hyprnote.stable",
        "pid": "12345",
        "auid": "501",
        "euid": "501",
        "binary_path": "/Applications/Char.app/Contents/MacOS/hyprnote",
        "reason": "requesting",
    },
    {
        "identifier": "com.hyprnote.stable",
        "pid": "12345",
        "auid": "501",
        "euid": "501",
        "binary_path": "/Applications/Char.app/Contents/MacOS/hyprnote",
        "reason": "responsible",
    },
]


# An AttributionChain produced by tccd when Char was launched via
# ``sandbox-exec ... hyprnote`` from an iTerm2 session -- the literal
# bug we're guarding against. The "responsible" slot is iTerm2.
CHAIN_REGRESSION = [
    {
        "identifier": "com.hyprnote.stable",
        "pid": "42861",
        "auid": "501",
        "euid": "501",
        "binary_path": "/Applications/Char.app/Contents/MacOS/hyprnote",
        "reason": "requesting",
    },
    {
        "identifier": "com.hyprnote.stable",
        "pid": "42861",
        "auid": "501",
        "euid": "501",
        "binary_path": "/Applications/Char.app/Contents/MacOS/hyprnote",
        "reason": "instigating",
    },
    {
        "identifier": "com.googlecode.iterm2",
        "pid": "2741",
        "auid": "501",
        "euid": "501",
        "binary_path": "/Applications/iTerm.app/Contents/MacOS/iTerm2",
        "reason": "responsible",
    },
]


def _ndjson_event(message: str) -> str:
    """Return a single-line ndjson event the way ``log show`` emits
    them; only the fields :mod:`char_tcc_probe` reads are populated."""
    return json.dumps({
        "timestamp": "2026-05-12 12:16:43.290000-0700",
        "process": "tccd",
        "eventMessage": message,
    })


# A real-shape eventMessage carrying the regression AttributionChain
# inline. Whitespace and newline placement match what macOS 24's
# ``log show --style ndjson`` actually produces.
EVENT_MESSAGE_REGRESSION = (
    "REQUEST: msgID=1234.567/8 function=<TCCDAccessRequestCheck>, "
    "service=kTCCServiceAudioCapture, "
    "AttributionChain={"
    "[identifier=com.hyprnote.stable, pid=42861, auid=501, euid=501, "
    "binary_path=/Applications/Char.app/Contents/MacOS/hyprnote, "
    "reason=requesting]"
    "[identifier=com.hyprnote.stable, pid=42861, auid=501, euid=501, "
    "binary_path=/Applications/Char.app/Contents/MacOS/hyprnote, "
    "reason=instigating]"
    "[identifier=com.googlecode.iterm2, pid=2741, auid=501, euid=501, "
    "binary_path=/Applications/iTerm.app/Contents/MacOS/iTerm2, "
    "reason=responsible]"
    "}"
)


EVENT_MESSAGE_HEALTHY = (
    "REQUEST: msgID=1234.999/9 function=<TCCDAccessRequestCheck>, "
    "service=kTCCServiceAudioCapture, "
    "AttributionChain={"
    "[identifier=com.hyprnote.stable, pid=12345, auid=501, euid=501, "
    "binary_path=/Applications/Char.app/Contents/MacOS/hyprnote, "
    "reason=requesting]"
    "[identifier=com.hyprnote.stable, pid=12345, auid=501, euid=501, "
    "binary_path=/Applications/Char.app/Contents/MacOS/hyprnote, "
    "reason=responsible]"
    "}"
)


# ===========================================================================
# 1. Pure classification
# ===========================================================================


class IsTerminalIdentifierTests(unittest.TestCase):
    def test_known_terminal_bundle_ids_match(self):
        # The exact set we've seen in the wild as of May 2026.
        for ident in (
            "com.googlecode.iterm2",
            "com.apple.Terminal",
            "dev.warp.Warp-Stable",
            "io.alacritty",
            "net.kovidgoyal.kitty",
            "com.github.wez.wezterm",
            "com.tabby.Tabby",
        ):
            self.assertTrue(
                char_tcc_probe.is_terminal_identifier(ident),
                f"{ident!r} should be classified as a terminal",
            )

    def test_char_is_not_a_terminal(self):
        # Critical: if this ever returns True the probe will
        # mis-report Char's healthy launches as the regression.
        self.assertFalse(
            char_tcc_probe.is_terminal_identifier("com.hyprnote.stable"),
            "Char.app must not be flagged as a terminal emulator",
        )

    def test_falsy_inputs_return_false(self):
        self.assertFalse(char_tcc_probe.is_terminal_identifier(None))
        self.assertFalse(char_tcc_probe.is_terminal_identifier(""))

    def test_unknown_terminal_caught_by_loose_name_match(self):
        # Operators on niche terminals not in TERMINAL_BUNDLE_IDS hit
        # the same TCC bug; the loose-regex fallback must still
        # classify them. Make sure the substring match isn't too
        # narrow.
        self.assertTrue(
            char_tcc_probe.is_terminal_identifier(
                "com.example.MyNewTerminal"
            ),
            "'terminal' in the bundle id is a strong signal that the "
            "loose match must catch",
        )

    def test_false_positive_guards(self):
        # The loose regex must NOT flag bundles that merely contain a
        # substring related to a terminal-shaped word. These should
        # stay 'unknown' so we don't crywolf -- but per the current
        # design, we deliberately accept some false positives in
        # exchange for catching unknown niche terminals. Document
        # exactly which strings are accepted.
        self.assertFalse(
            char_tcc_probe.is_terminal_identifier("com.apple.SystemUIServer"),
            "SystemUIServer is not a terminal",
        )
        self.assertFalse(
            char_tcc_probe.is_terminal_identifier("com.apple.spotlight"),
            "Spotlight is not a terminal",
        )


class ClassifyChainTests(unittest.TestCase):
    def test_healthy_chain_returns_ok(self):
        state, responsible = char_tcc_probe.classify_chain(CHAIN_HEALTHY)
        self.assertEqual(state, "ok")
        self.assertEqual(responsible, "com.hyprnote.stable")

    def test_terminal_responsible_returns_terminal_state(self):
        # The May 2026 regression. This is the assertion path
        # ``./run.sh char firewall-status`` ultimately reads to surface
        # the red ✗ + "quit Char and relaunch" hint.
        state, responsible = char_tcc_probe.classify_chain(CHAIN_REGRESSION)
        self.assertEqual(state, "terminal")
        self.assertEqual(responsible, "com.googlecode.iterm2")

    def test_empty_chain_is_unknown(self):
        state, responsible = char_tcc_probe.classify_chain([])
        self.assertEqual(state, "unknown")
        self.assertEqual(responsible, "-")

    def test_chain_without_responsible_entry_is_unknown(self):
        # Some macOS releases emit chains where 'reason=responsible'
        # isn't materialised yet (e.g. during a system service
        # warm-up). Must degrade to 'unknown' rather than throw.
        partial = [
            {"identifier": "com.hyprnote.stable", "reason": "requesting"},
            {"identifier": "com.hyprnote.stable", "reason": "instigating"},
        ]
        state, responsible = char_tcc_probe.classify_chain(partial)
        self.assertEqual(state, "unknown")
        self.assertEqual(responsible, "-")

    def test_malformed_chain_does_not_raise(self):
        # Defensive: anything wonky in the chain elements must NOT
        # crash the probe. firewall-status calls this from a one-shot
        # python -c invocation that doesn't tolerate exceptions.
        weird = [
            "not-a-dict",
            None,
            {"reason": "responsible"},                 # missing identifier
            {"identifier": "x"},                       # missing reason
            {"identifier": "com.x.y", "reason": "responsible"},
        ]
        state, responsible = char_tcc_probe.classify_chain(weird)  # type: ignore[arg-type]
        # The first valid 'responsible' entry is com.x.y, which is
        # neither Char nor a known terminal.
        self.assertEqual(state, "unknown")
        self.assertEqual(responsible, "com.x.y")

    def test_unknown_responsible_bundle_does_not_false_alarm(self):
        # E.g. SystemUIServer-mediated requests. We don't want a red ✗
        # for "responsible is some Apple helper we haven't catalogued";
        # firewall-status renders unknown as a neutral '•'.
        chain = [
            {"identifier": "com.hyprnote.stable", "reason": "instigating"},
            {"identifier": "com.apple.SystemUIServer",
             "reason": "responsible"},
        ]
        state, responsible = char_tcc_probe.classify_chain(chain)
        self.assertEqual(state, "unknown")
        self.assertEqual(responsible, "com.apple.SystemUIServer")


# ===========================================================================
# 2. Event-message parser (the macOS-format-dependent piece)
# ===========================================================================


class ParseAttributionChainTests(unittest.TestCase):
    def test_extracts_three_entries_from_regression_event(self):
        chain = char_tcc_probe.parse_attribution_chain(
            EVENT_MESSAGE_REGRESSION
        )
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[0]["identifier"], "com.hyprnote.stable")
        self.assertEqual(chain[0]["reason"], "requesting")
        self.assertEqual(chain[2]["identifier"], "com.googlecode.iterm2")
        self.assertEqual(chain[2]["reason"], "responsible")

    def test_extracts_two_entries_from_healthy_event(self):
        chain = char_tcc_probe.parse_attribution_chain(
            EVENT_MESSAGE_HEALTHY
        )
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1]["reason"], "responsible")
        self.assertEqual(chain[1]["identifier"], "com.hyprnote.stable")

    def test_message_without_attribution_chain_returns_empty(self):
        chain = char_tcc_probe.parse_attribution_chain(
            "REPLY: <TCCDAccessReplyForRequest>, status=Denied"
        )
        self.assertEqual(chain, [])

    def test_end_to_end_classification(self):
        # The actual call chain firewall-status uses:
        #   raw message -> parse -> classify
        chain = char_tcc_probe.parse_attribution_chain(
            EVENT_MESSAGE_REGRESSION
        )
        state, responsible = char_tcc_probe.classify_chain(chain)
        self.assertEqual(state, "terminal")
        self.assertEqual(responsible, "com.googlecode.iterm2")


# ===========================================================================
# 3. probe() integration (mocks _run_log_show, no real /usr/bin/log call)
# ===========================================================================


class ProbeIntegrationTests(unittest.TestCase):
    """Drives :func:`probe` with synthetic ndjson via mocking. No
    real ``log show`` invocation -- CI-safe on Linux runners + macOS
    minimal images."""

    def _patch_log_show(self, ndjson_lines: list[str] | None):
        out = ("\n".join(ndjson_lines) + "\n") if ndjson_lines else None
        return mock.patch.object(
            char_tcc_probe, "_run_log_show",
            return_value=out,
        )

    def test_state_ok_for_healthy_chain(self):
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_HEALTHY),
        ]):
            res = char_tcc_probe.probe()
        self.assertEqual(res.state, "ok")
        self.assertEqual(res.responsible, "com.hyprnote.stable")
        self.assertGreaterEqual(res.events_seen, 1)

    def test_state_terminal_for_regression_chain(self):
        # THE regression net. If this test ever fails, either the
        # classifier broke or the parser broke; either way operators
        # would lose the audio-capture signal.
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_REGRESSION),
        ]):
            res = char_tcc_probe.probe()
        self.assertEqual(res.state, "terminal")
        self.assertEqual(res.responsible, "com.googlecode.iterm2")

    def test_latest_chain_wins_when_both_are_present(self):
        # Operator-during-debugging case: they ran the bad launch
        # first, then the good one. The chronologically latest event
        # must win so firewall-status reflects the CURRENT state.
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_REGRESSION),   # earlier
            _ndjson_event(EVENT_MESSAGE_HEALTHY),      # later
        ]):
            res = char_tcc_probe.probe()
        self.assertEqual(res.state, "ok")
        self.assertEqual(res.events_seen, 2)

    def test_state_no_events_when_no_char_events(self):
        # log show ran fine, just didn't see anything mentioning
        # com.hyprnote.stable in the window. Common right after a
        # cold boot before the operator opens Char.
        with self._patch_log_show([
            _ndjson_event("unrelated TCC traffic for com.apple.Safari"),
        ]):
            res = char_tcc_probe.probe()
        self.assertEqual(res.state, "no_events")
        self.assertEqual(res.events_seen, 0)
        self.assertEqual(res.responsible, "-")

    def test_state_no_logs_when_log_show_returns_none(self):
        # /usr/bin/log absent, returned non-zero, or timed out.
        with self._patch_log_show(None):
            res = char_tcc_probe.probe()
        self.assertEqual(res.state, "no_logs")

    def test_to_dict_shape_matches_run_sh_contract(self):
        # run.sh's cmd_char_firewall_status reads the JSON and pulls
        # exactly two keys: 'state' and 'responsible'. Pin the shape so
        # a future field rename in this module can't break the shell
        # consumer silently.
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_REGRESSION),
        ]):
            res = char_tcc_probe.probe()
        d = res.to_dict()
        self.assertIn("state", d)
        self.assertIn("responsible", d)
        self.assertEqual(d["state"], "terminal")
        self.assertEqual(d["responsible"], "com.googlecode.iterm2")

    def test_cli_exit_status_is_1_on_terminal_state(self):
        # `python -m local_scribe.egress.char_tcc_probe` must exit
        # non-zero specifically when the regression is detected so
        # CI / operators can treat it as a hard failure.
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_REGRESSION),
        ]), mock.patch("sys.stdout"):
            rc = char_tcc_probe.main([])
        self.assertEqual(rc, 1)

    def test_cli_exit_status_is_0_on_ok_state(self):
        with self._patch_log_show([
            _ndjson_event(EVENT_MESSAGE_HEALTHY),
        ]), mock.patch("sys.stdout"):
            rc = char_tcc_probe.main([])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
