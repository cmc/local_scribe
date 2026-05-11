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

  test_age_too_old_triggers_brew_upgrade
        2026-05-11 production failure mode: ``age --version`` exits 0
        and reports ``v1.0.0``, predating plugin-recipient support
        (added in v1.1.0). bootstrap MUST detect this and call ``brew
        upgrade age``, NOT silently advance to a stage-3 failure with
        ``age: error: malformed recipient ... invalid type
        "age1yubikey"``.

  test_age_modern_does_not_trigger_upgrade
        Inverse guard: when age reports a version >= AGE_MIN_VERSION,
        ``ensure_age_tools`` must NOT call ``brew upgrade``.

  test_age_upgrade_reverify_after_brew
        After ``brew upgrade age`` succeeds, the helper must re-check
        the installed version. If it's still too old (e.g. the tap
        pins an old formula), bootstrap fails loud rather than
        falsely claiming success.

  test_version_lt_helper_handles_edge_cases
        Direct exercises of the dotted-triple comparator that the
        version-floor check depends on (boundary, equality, missing
        components, multi-digit).
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

    # ------- age version-floor regression (2026-05-11) -------------------

    def test_age_too_old_triggers_brew_upgrade(self) -> None:
        """Models the stale-install regression: ``age --version`` returns
        ``v1.0.0``, which predates plugin-recipient support added in
        v1.1.0. ``age1yubikey1...`` recipients then fail at encrypt
        time with ``malformed recipient ... invalid type "age1yubikey"``.
        bootstrap must detect this and ``brew upgrade age`` *before*
        the operator reaches stage 3.
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix="ls-age-ver-"))
        version_file = tmp_dir / "age_version.txt"
        version_file.write_text("1.0.0\n")
        brew_trace = tmp_dir / "brew_trace.txt"
        try:
            with FakeBinDir(
                tools=["age", "age-plugin-yubikey", "ykman", "brew"],
            ) as bins:
                r = _invoke(
                    "ensure_age_tools",
                    path=f"{bins.path}:/usr/bin:/bin",
                    extra_env={
                        "LOCAL_SCRIBE_FAKE_AGE_VERSION_FILE": str(version_file),
                        "LOCAL_SCRIBE_FAKE_BREW_TRACE": str(brew_trace),
                        "LOCAL_SCRIBE_FAKE_BREW_UPGRADE_FLIP": "1.3.1",
                    },
                )
            self.assertTrue(
                brew_trace.exists(),
                msg=f"brew never called; stdout:\n{r.stdout}",
            )
            trace_lines = brew_trace.read_text().strip().splitlines()
            self.assertTrue(
                any(line.strip() == "upgrade age" for line in trace_lines),
                msg=f"expected ``brew upgrade age`` in trace:\n"
                    f"  {trace_lines!r}",
            )
            # Sanity: must not be install/reinstall (those wouldn't
            # advance the version pin for an already-installed formula).
            self.assertFalse(
                any(line.strip() == "install age" for line in trace_lines),
                msg=f"install would no-op for too-old age; trace:\n  {trace_lines!r}",
            )
            self.assertFalse(
                any(line.strip() == "reinstall age" for line in trace_lines),
                msg=f"reinstall would rewrite the SAME version; trace:\n  {trace_lines!r}",
            )
            # The operator-facing message should mention the version
            # mismatch so they understand why an upgrade just ran.
            self.assertRegex(
                r.stdout,
                r"installed 1\.0\.0 < required",
                msg=f"upgrade reason must be explicit in stdout:\n{r.stdout}",
            )
            self.assertEqual(_exit_code_from(r.stdout), 0, msg=r.stdout)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_age_modern_does_not_trigger_upgrade(self) -> None:
        """Inverse guard: a modern age must not cause ``brew upgrade``."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="ls-age-ver-"))
        version_file = tmp_dir / "age_version.txt"
        version_file.write_text("1.2.0\n")
        brew_trace = tmp_dir / "brew_trace.txt"
        try:
            with FakeBinDir(
                tools=["age", "age-plugin-yubikey", "ykman", "brew"],
            ) as bins:
                r = _invoke(
                    "ensure_age_tools",
                    path=f"{bins.path}:/usr/bin:/bin",
                    extra_env={
                        "LOCAL_SCRIBE_FAKE_AGE_VERSION_FILE": str(version_file),
                        "LOCAL_SCRIBE_FAKE_BREW_TRACE": str(brew_trace),
                    },
                )
            self.assertEqual(_exit_code_from(r.stdout), 0, msg=r.stdout)
            # brew must not have been called at all.
            if brew_trace.exists():
                trace = brew_trace.read_text()
                self.assertNotIn(
                    "upgrade", trace,
                    msg=f"brew upgrade should not run for modern age; trace:\n{trace}",
                )
                self.assertNotIn("install", trace)
                self.assertNotIn("reinstall", trace)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_age_upgrade_reverify_after_brew(self) -> None:
        """If ``brew upgrade age`` ran but the post-upgrade age is STILL
        too old (e.g. the tap pins an old formula), ``ensure_age_tools``
        must fail loud rather than silently advance to stage 3.
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix="ls-age-ver-"))
        version_file = tmp_dir / "age_version.txt"
        version_file.write_text("1.0.0\n")
        try:
            with FakeBinDir(
                tools=["age", "age-plugin-yubikey", "ykman", "brew"],
            ) as bins:
                r = _invoke(
                    "ensure_age_tools",
                    path=f"{bins.path}:/usr/bin:/bin",
                    extra_env={
                        "LOCAL_SCRIBE_FAKE_AGE_VERSION_FILE": str(version_file),
                        # No LOCAL_SCRIBE_FAKE_BREW_UPGRADE_FLIP — the
                        # fake brew "upgrade age" succeeds but the
                        # version file is unchanged, so the post-upgrade
                        # re-verify sees 1.0.0 still.
                    },
                )
            self.assertNotEqual(_exit_code_from(r.stdout), 0, msg=r.stdout)
            self.assertRegex(
                r.stdout,
                r"age still at 1\.0\.0 after upgrade",
                msg=f"failure message must surface the stuck-version case:\n{r.stdout}",
            )
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_version_lt_helper_handles_edge_cases(self) -> None:
        """Direct exercises of the ``_version_lt`` helper. We can't
        return a value from a bash function across the bash/Python
        boundary trivially, so this test invokes it via a wrapper
        script and asserts on the exit code."""
        for a, b, expect_lt in [
            ("1.0.0", "1.1.0", True),
            ("1.0.9", "1.1.0", True),
            ("1.1.0", "1.1.0", False),
            ("1.1.0", "1.0.0", False),
            ("1.2.0", "1.1.0", False),
            ("2.0.0", "1.99.99", False),
            ("1.10.0", "1.9.0", False),     # multi-digit minor
            ("1.0", "1.1", True),              # missing patch
            ("1", "1.0.1", True),              # missing minor + patch
        ]:
            script = (
                f'source "{_RUN_SH}" >/dev/null 2>&1 || true\n'
                f'if _version_lt "{a}" "{b}"; then echo "__rc=0"; '
                f'else echo "__rc=$?"; fi\n'
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", str(_REPO)),
                    "TERM": "dumb",
                    "LOCAL_SCRIBE_DEV_MODE": "1",
                },
                capture_output=True, text=True, timeout=10,
                cwd=str(_REPO),
            )
            rc = _exit_code_from(proc.stdout)
            got_lt = (rc == 0)
            self.assertEqual(
                got_lt, expect_lt,
                msg=f"_version_lt {a!r} {b!r}: expected lt={expect_lt}, "
                    f"got lt={got_lt}\nstdout:\n{proc.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
