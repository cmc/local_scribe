"""Tests for ``run.sh``'s ``ensure_age_tools`` helper.

Strategy
--------

``ensure_age_tools`` is a bash function. We drive it from Python by:

  1. Building a tmp PATH with the fake tools we want exposed (or
     deliberately omitting them to model "missing").
  2. Sourcing ``run.sh`` in a subshell, suppressing the dispatcher at
     the bottom by overriding $1 to an empty string.
  3. Calling ``ensure_age_tools``.
  4. Asserting exit code + (where useful) brew trace + stdout/stderr.

The fake brew refuses to actually install anything (it's a no-op), so
the tests just confirm that ``ensure_age_tools`` correctly decided
whether to call brew at all and whether it asked for ``install`` vs
``reinstall``.

Coverage
--------

  test_all_tools_present_returns_zero
        Happy path: every tool on PATH, every tool runs.

  test_missing_tool_triggers_brew_install
        ``age-plugin-yubikey`` not on PATH → brew install age-plugin-yubikey.

  test_broken_tool_triggers_brew_reinstall
        Tool ON PATH but ``--version`` errors → brew reinstall (the
        2026-05-11 production failure mode: brew install is a no-op
        when Homebrew thinks the formula is installed).

  test_post_install_verification_fails_loud
        If brew "succeeds" but the tool STILL doesn't run, return
        non-zero rather than silently advancing to stage 3.

  test_no_brew_present_errors_clearly
        Brew not installed AND a tool is missing → return non-zero
        with a clear error pointing at https://brew.sh.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ._fake_bins import FakeBinDir


_REPO = Path(__file__).resolve().parents[2]
_RUN_SH = _REPO / "run.sh"


def _invoke(fn: str, *, path: str, extra_env: dict | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Source run.sh in a controlled bash subshell, then call ``fn``."""
    env = {
        "PATH": path,
        "HOME": os.environ.get("HOME", str(_REPO)),
        "TERM": "dumb",   # disable colour codes in test output
        "LOCAL_SCRIBE_DEV_MODE": "1",   # bypass SIP gate
    }
    if extra_env:
        env.update(extra_env)
    # We invoke run.sh as a subshell argument so the dispatcher does
    # nothing (no command passed — and the dispatcher is now guarded
    # behind a ``[[ "${BASH_SOURCE[0]}" == "${0}" ]]`` check anyway).
    # Then call the function we care about. ``set -e`` is on in
    # run.sh; we use ``if`` to suppress its effect so we still print
    # the __rc marker on non-zero exit (otherwise the subshell dies
    # silently and the test can't see the actual return code).
    script = (
        f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
        f'if {fn} 2>&1; then rc=0; else rc=$?; fi\n'
        f'echo "__rc=$rc"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(cwd or _REPO),
    )


def _exit_code_from(output: str) -> int:
    for line in reversed(output.splitlines()):
        if line.startswith("__rc="):
            return int(line[len("__rc="):])
    raise AssertionError(f"no __rc=N marker in output:\n{output}")


class EnsureAgeToolsTests(unittest.TestCase):
    def test_all_tools_present_returns_zero(self) -> None:
        with FakeBinDir(tools=["age", "age-plugin-yubikey", "ykman", "brew"]) as bins:
            r = _invoke("ensure_age_tools", path=bins.path_for_env())
        self.assertEqual(_exit_code_from(r.stdout), 0, msg=r.stdout)
        self.assertIn("present + executable", r.stdout)
        # Brew must NOT have been called.
        self.assertNotIn("brew install", r.stdout)
        self.assertNotIn("brew reinstall", r.stdout)

    def test_missing_tool_triggers_brew_install(self) -> None:
        trace = Path(tempfile.mkdtemp(prefix="ls-brew-trace-")) / "trace.txt"
        try:
            # Build a PATH that has age + ykman + brew but NOT
            # age-plugin-yubikey. Use a STRICT PATH (no real
            # /opt/homebrew/bin) so the real plugin doesn't get
            # picked up.
            with FakeBinDir(tools=["age", "ykman", "brew"]) as bins:
                r = _invoke(
                    "ensure_age_tools",
                    path=f"{bins.path}:/usr/bin:/bin",
                    extra_env={"LOCAL_SCRIBE_FAKE_BREW_TRACE": str(trace)},
                )
            # ``ensure_age_tools`` runs a post-install re-verify; the
            # fake brew doesn't actually install anything, so this
            # will end non-zero. The important assertions are:
            #   (a) brew was asked to ``install age-plugin-yubikey``;
            #   (b) the failure happened in the *verify* phase, not
            #       the "Homebrew not installed" path.
            self.assertTrue(trace.exists(),
                            msg=f"trace not created; stdout:\n{r.stdout}\n"
                                f"stderr:\n{r.stderr}")
            trace_lines = trace.read_text().strip().splitlines()
            self.assertTrue(
                any(line.strip() == "install age-plugin-yubikey"
                    for line in trace_lines),
                msg=f"expected ``brew install age-plugin-yubikey`` in trace:\n"
                    f"  {trace_lines!r}",
            )
            self.assertFalse(
                any("reinstall" in line for line in trace_lines),
                msg=f"reinstall shouldn't have fired (tool was absent, "
                    f"not broken); trace:\n  {trace_lines!r}",
            )
        finally:
            if trace.parent.exists():
                import shutil
                shutil.rmtree(trace.parent, ignore_errors=True)

    def test_broken_tool_triggers_brew_reinstall(self) -> None:
        """Models 2026-05-11: ykman is on PATH but its libexec python is
        a 0-byte file, so ``ykman --version`` exits 126 with
        'exec format error'. ``ensure_age_tools`` must use ``brew
        reinstall``, NOT ``brew install`` (which is a no-op for
        already-installed formulae).
        """
        trace = Path(tempfile.mkdtemp(prefix="ls-brew-trace-")) / "trace.txt"
        try:
            with FakeBinDir(
                tools=["age", "age-plugin-yubikey", "ykman", "brew"],
                ykman_broken=True,
            ) as bins:
                r = _invoke(
                    "ensure_age_tools",
                    path=f"{bins.path}:/usr/bin:/bin",
                    extra_env={"LOCAL_SCRIBE_FAKE_BREW_TRACE": str(trace)},
                )
            self.assertTrue(trace.exists(),
                            msg=f"trace not created; stdout:\n{r.stdout}")
            trace_lines = trace.read_text().strip().splitlines()
            self.assertTrue(
                any(line.strip() == "reinstall ykman" for line in trace_lines),
                msg=f"expected ``brew reinstall ykman`` in trace:\n"
                    f"  {trace_lines!r}",
            )
            # Plain ``brew install ykman`` would be a no-op for an
            # already-installed formula — make sure we didn't try it.
            self.assertFalse(
                any(line.strip() == "install ykman" for line in trace_lines),
                msg=f"must use reinstall not install:\n  {trace_lines!r}",
            )
        finally:
            if trace.parent.exists():
                import shutil
                shutil.rmtree(trace.parent, ignore_errors=True)

    def test_post_install_verification_fails_loud(self) -> None:
        """Brew 'succeeded' but the tool still doesn't run → non-zero.

        We model this by giving the fake brew a no-op (the default)
        while the underlying ykman remains broken. ``ensure_age_tools``
        must NOT return 0.
        """
        with FakeBinDir(
            tools=["age", "age-plugin-yubikey", "ykman", "brew"],
            ykman_broken=True,
        ) as bins:
            r = _invoke("ensure_age_tools", path=bins.path_for_env())
        self.assertNotEqual(
            _exit_code_from(r.stdout),
            0,
            msg=f"ensure_age_tools must fail when ykman still broken; got:\n{r.stdout}",
        )
        self.assertIn(
            "ykman", r.stdout,
            msg="error message should name the still-broken tool",
        )

    def test_no_brew_present_errors_clearly(self) -> None:
        """Brew not installed AND a tool is missing → clear error.

        We achieve "brew not installed" by simply omitting brew from
        the FakeBinDir and using a minimal PATH that contains no real
        brew either. (Hosts that run pytest in a developer shell will
        often have /opt/homebrew/bin in PATH; we strip it.)
        """
        with FakeBinDir(tools=["age", "ykman"]) as bins:
            # Build a strict PATH with no brew anywhere.
            r = _invoke(
                "ensure_age_tools",
                path=f"{bins.path}:/usr/bin:/bin",
            )
        rc = _exit_code_from(r.stdout)
        self.assertNotEqual(rc, 0, msg=r.stdout)
        self.assertIn(
            "Homebrew not installed", r.stdout,
            msg="error must mention Homebrew so the operator knows what to install",
        )


if __name__ == "__main__":
    unittest.main()
