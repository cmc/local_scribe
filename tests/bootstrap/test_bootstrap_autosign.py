"""Static-analysis test: ``cmd_bootstrap`` must auto-invoke
``config sign`` so that ``./run.sh start`` doesn't refuse immediately
after a fresh bootstrap.

Background
----------

The start-time ``pinned_config_gate`` (run.sh:437) verifies an HMAC
signature over ``local_scribe/common/pinned.json`` and
``~/.config/local_scribe/char_baseline.json`` against the operator's
split master key. Without a valid signature, ``start`` refuses with:

    FAIL [pinned] no signature; run `./run.sh config sign`
    FAIL [char_baseline] no signature; run `./run.sh config sign`

That message is correct but it's also a UX dead-end for first-time
operators: they just finished a 10-stage bootstrap, the completion
banner told them to run ``./run.sh start``, and ``start`` refuses on
the very next command. (Observed in the 2026-05-11 conversation
window after the operator hit six prior reorg regressions.)

The fix lives in ``cmd_bootstrap``: between stage 10 and the
"════════ bootstrap complete ════════" banner, we now call
``$VENV_PY -m local_scribe config sign`` so the signatures exist
before the operator types ``./run.sh start``. This costs one
additional Touch ID + YubiKey tap during bootstrap, which is fine
because the operator has just done the same dance for stage 3 (key
init) and stage 9 (configure-char) anyway.

This test asserts that the auto-sign block is present in
``cmd_bootstrap``. It does NOT exercise the signing flow itself
(that would require a real master key + Touch ID + YubiKey, which
this test environment can't provide). It's a thin guard-rail against
a future refactor accidentally deleting or relocating the call.

If the call gets moved or rewritten, update this test alongside the
move — the test failure message points at the exact reason the guard
exists.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"


def _extract_cmd_bootstrap_body(run_sh: Path) -> str:
    """Slice the text of ``cmd_bootstrap()`` out of run.sh.

    Bash function bodies are recognised by ``cmd_bootstrap() {`` open
    and the first matching ``}`` at column 0. We use a simple brace
    counter rather than parsing bash properly, which is acceptable
    for this static check.
    """
    text = run_sh.read_text()
    m = re.search(r"^cmd_bootstrap\(\)\s*\{\s*$", text, flags=re.M)
    if not m:
        raise AssertionError("cmd_bootstrap() not found in run.sh")
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    if depth != 0:
        raise AssertionError("could not find matching '}' for cmd_bootstrap")
    return text[start:i]


class BootstrapAutoSignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = _extract_cmd_bootstrap_body(_RUN_SH)

    def test_cmd_bootstrap_invokes_config_sign(self) -> None:
        """The body must contain a ``-m local_scribe config sign``
        invocation. Without this, ``./run.sh start`` refuses on the
        very next command after bootstrap and the operator has to
        manually run ``./run.sh config sign`` first."""
        self.assertRegex(
            self.body,
            r'"\$VENV_PY"\s+-m\s+local_scribe\s+config\s+sign',
            msg=(
                "cmd_bootstrap is missing the auto-config-sign call. "
                "Without it, ./run.sh start refuses with "
                "'FAIL [pinned] no signature' immediately after "
                "bootstrap completes. See "
                "tests/bootstrap/test_bootstrap_autosign.py docstring."
            ),
        )

    def test_auto_sign_runs_before_completion_banner(self) -> None:
        """The sign call must precede the ════ banner so the
        signatures exist before the 'Start the pipeline:
        ./run.sh start' next-step hint."""
        sign_match = re.search(
            r'"\$VENV_PY"\s+-m\s+local_scribe\s+config\s+sign',
            self.body,
        )
        banner_match = re.search(r"════════ bootstrap complete", self.body)
        self.assertIsNotNone(sign_match, "no config sign invocation found")
        self.assertIsNotNone(banner_match, "no completion banner found")
        assert sign_match is not None and banner_match is not None
        self.assertLess(
            sign_match.start(), banner_match.start(),
            msg=(
                "config sign must run BEFORE the bootstrap-complete "
                "banner so the operator's next-step hint "
                "(./run.sh start) succeeds on first invocation."
            ),
        )

    def test_auto_sign_skips_when_already_signed(self) -> None:
        """Idempotence guard: re-running bootstrap on an already-blessed
        install should NOT prompt for a redundant Touch ID + YubiKey
        tap. We assert the body contains a ``config verify`` call
        gating the sign so repeat bootstraps are quiet."""
        self.assertRegex(
            self.body,
            r'"\$VENV_PY"\s+-m\s+local_scribe\s+config\s+verify',
            msg=(
                "cmd_bootstrap should pre-check `config verify` before "
                "calling `config sign`, so a second bootstrap of an "
                "already-signed install doesn't ask for a redundant tap."
            ),
        )

    def test_auto_sign_failure_is_non_fatal(self) -> None:
        """Bootstrap shouldn't abort 9 successful stages worth of work
        because the operator declined the final Touch ID prompt. The
        sign block must end in a yellow warning, not ``return 1``."""
        # Find the sign block (loose: from the printf preamble to the
        # next blank line). We just verify the WARNING message is
        # present somewhere in the body, since it's specific enough
        # to be unambiguous.
        self.assertIn(
            "./run.sh config sign\\` manually",
            self.body,
            msg=(
                "config sign failure path should fall back to a yellow "
                "warning telling the operator to run "
                "`./run.sh config sign` manually before start, NOT "
                "abort bootstrap."
            ),
        )


if __name__ == "__main__":
    unittest.main()
