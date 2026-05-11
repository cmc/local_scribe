"""Real-subprocess integration tests for ``yubikey_backup.enroll()``.

Why a separate file from ``test_yubikey_backup.py``
---------------------------------------------------

``test_yubikey_backup.py`` is the unit-test file. It uses bash stubs that
exit 0 with no useful output, because the legacy tests only assert against
side effects (e.g. that ``assert_tools()`` doesn't bail).

This file targets the ACTUAL ``subprocess.run`` boundary inside
``enroll()`` — the layer that hit two production bugs in a row on
2026-05-11:

  Bug 1 (commit history): we passed ``--identity-output PATH`` to
        age-plugin-yubikey. That flag has never existed in 0.5.x. The
        plugin errored at rc=2 and bootstrap halted at stage 3.

  Bug 2 (same day, same stage): we called the plugin with
        ``subprocess.run(..., capture_output=True)``. The real plugin
        prompts for the YubiKey PIV PIN on stdin and emits diagnostics
        on stderr. ``capture_output=True`` pipes BOTH — the plugin then
        errored with ``IO error: not a terminal`` and rc=1.

Both bugs are integration-layer issues: the unit tests would never have
caught them because the stub plugin didn't model the real CLI contract.
The fake binary in ``_fake_age_plugin_yubikey.py`` DOES model the real
contract (rejects unrecognized flags exactly like the real plugin,
optionally requires a TTY exactly like the real plugin, emits identity
stubs in the real format) — so these tests catch the next regression
in the same family before a fresh-laptop bootstrap does.

Coverage
--------

  test_enroll_happy_path
        End-to-end: ``enroll()`` invokes the fake plugin and produces
        the expected ~/.config/local_scribe/ artifacts (identity,
        recipient).

  test_identity_file_is_0600
        Defensive: the identity stub is not a secret on its own, but
        we still tighten perms.

  test_recipient_extracted_from_stdout
        The recipient is the ``age1yubikey1...`` line from the plugin's
        stdout, not stderr.

  test_does_not_pass_identity_output_flag
        Bug 1 regression. Reads the fake plugin's argv trace and
        asserts ``--identity-output`` is not among the args.

  test_does_not_capture_stdin_or_stderr
        Bug 2 regression. Monkey-patches ``subprocess.run`` to capture
        the kwargs passed by ``enroll()`` and asserts:

          * ``capture_output`` is not set / is False
          * ``stdin``  is None (= inherit parent's stdin)
          * ``stderr`` is None (= inherit parent's stderr)
          * ``stdout`` is ``subprocess.PIPE``    (we need the identity)

  test_plugin_failure_propagates
        If the plugin exits non-zero, ``enroll()`` raises
        ``YubiKeyError`` with the rc + a pointer to where the operator
        can find the plugin output.

  test_force_re_enroll_overwrites_identity
        ``enroll(force=True)`` should overwrite an existing identity
        file (regression-guard for the in-place atomic write).
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


_FAKE_PLUGIN_SCRIPT = Path(__file__).with_name("_fake_age_plugin_yubikey.py")
_FAKE_AGE = """#!/usr/bin/env bash
exit 0
"""
_FAKE_YKMAN = """#!/usr/bin/env bash
if [[ "${1:-}" == "list" ]]; then
  echo "YubiKey 5C Nano (5.4.3) [OTP+FIDO+CCID] Serial: 16366413"
  exit 0
fi
exit 0
"""


class _FakeBinDir:
    """Tmp directory holding fake binaries; PATH-prepended for the test."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="ls-yk-enroll-bin-"))
        # The fake age-plugin-yubikey is a Python script; symlink it in
        # under the expected name so $PATH lookup finds it.
        self.plugin = self.path / "age-plugin-yubikey"
        self.plugin.symlink_to(_FAKE_PLUGIN_SCRIPT)
        self.age = self.path / "age"
        self.age.write_text(_FAKE_AGE)
        self.age.chmod(0o755)
        self.ykman = self.path / "ykman"
        self.ykman.write_text(_FAKE_YKMAN)
        self.ykman.chmod(0o755)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class EnrollIntegrationTests(unittest.TestCase):
    """Drive ``yubikey_backup.enroll()`` against a real fake binary."""

    def setUp(self) -> None:
        self.cfg_dir = Path(tempfile.mkdtemp(prefix="ls-yk-enroll-cfg-"))
        self.bins = _FakeBinDir()
        self.trace_path = Path(tempfile.mkdtemp(prefix="ls-yk-enroll-trace-")) / "trace.txt"

        # Save + override env. Restored in tearDown.
        self._old_env: dict[str, Optional[str]] = {}
        env_overrides = {
            "PATH": f"{self.bins.path}{os.pathsep}{os.environ.get('PATH', '')}",
            "LOCAL_SCRIBE_CONFIG_DIR": str(self.cfg_dir),
            "LOCAL_SCRIBE_AGE_BIN": str(self.bins.age),
            "LOCAL_SCRIBE_AGE_PLUGIN_BIN": str(self.bins.plugin),
            "LOCAL_SCRIBE_YKMAN_BIN": str(self.bins.ykman),
            "LOCAL_SCRIBE_FAKE_AGE_PLUGIN_TRACE": str(self.trace_path),
        }
        for k, v in env_overrides.items():
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v

        # Force re-import so module-level CONFIG_DIR / IDENTITY_PATH /
        # RECIPIENT_PATH pick up the tmp cfg dir.
        if "local_scribe.security.yubikey_backup" in sys.modules:
            self.yk = importlib.reload(
                sys.modules["local_scribe.security.yubikey_backup"]
            )
        else:
            import local_scribe.security.yubikey_backup as yk
            self.yk = yk

    def tearDown(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Reload once more so subsequent test modules see a clean state.
        if "local_scribe.security.yubikey_backup" in sys.modules:
            importlib.reload(sys.modules["local_scribe.security.yubikey_backup"])
        shutil.rmtree(self.cfg_dir, ignore_errors=True)
        shutil.rmtree(self.trace_path.parent, ignore_errors=True)
        self.bins.cleanup()

    # ---- happy-path coverage ---------------------------------------------

    def test_enroll_happy_path(self) -> None:
        info = self.yk.enroll()
        self.assertIsNotNone(info)
        self.assertTrue(self.yk.IDENTITY_PATH.exists())
        self.assertTrue(self.yk.RECIPIENT_PATH.exists())
        identity_text = self.yk.IDENTITY_PATH.read_text()
        self.assertIn("# Recipient: age1yubikey1", identity_text)
        self.assertIn("AGE-PLUGIN-YUBIKEY-1", identity_text)
        recipient_text = self.yk.RECIPIENT_PATH.read_text().strip()
        self.assertTrue(
            recipient_text.startswith("age1yubikey1"),
            f"recipient looks wrong: {recipient_text!r}",
        )

    def test_identity_file_is_0600(self) -> None:
        self.yk.enroll()
        mode = self.yk.IDENTITY_PATH.stat().st_mode & 0o777
        self.assertEqual(
            mode,
            0o600,
            "identity stub must be 0600 (created via os.open with mode arg)",
        )

    def test_recipient_extracted_from_stdout(self) -> None:
        custom = "age1yubikey1ztest123ztest123ztest123ztest123ztest123ztest123zte"
        os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT"] = custom
        try:
            info = self.yk.enroll()
        finally:
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", None)
        self.assertIsNotNone(info)
        self.assertEqual(info.recipient, custom)

    # ---- BUG-1 regression: don't pass --identity-output ------------------

    def test_does_not_pass_identity_output_flag(self) -> None:
        """The argv-trace from the fake plugin must not include
        ``--identity-output``. age-plugin-yubikey 0.5.x has never had
        such a flag; passing it errors at rc=2 and breaks bootstrap.
        """
        self.yk.enroll()
        trace = self.trace_path.read_text()
        self.assertIn("--generate", trace)
        self.assertNotIn(
            "--identity-output",
            trace,
            "enroll() must not pass --identity-output to age-plugin-yubikey "
            "(it's not in the 0.5.x CLI; regression of the 2026-05-11 bug)",
        )

    # ---- BUG-2 regression: don't pipe stdin/stderr -----------------------

    def test_does_not_capture_stdin_or_stderr(self) -> None:
        """Monkey-patch ``subprocess.run`` to record the kwargs enroll()
        passes. The plugin reads the PIV PIN from stdin and emits prompts
        on stderr; we MUST inherit both so the operator can answer.
        """
        recorded: list[dict] = []
        real_run = subprocess.run

        def recording_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and "age-plugin-yubikey" in cmd[0]:
                recorded.append(dict(kwargs))
            return real_run(*args, **kwargs)

        subprocess.run = recording_run  # type: ignore[assignment]
        try:
            self.yk.enroll()
        finally:
            subprocess.run = real_run  # type: ignore[assignment]

        self.assertTrue(
            recorded,
            "no age-plugin-yubikey subprocess invocation captured",
        )
        gen_calls = [c for c in recorded if c.get("timeout") is not None]
        self.assertTrue(gen_calls, "expected --generate call with timeout")
        kwargs = gen_calls[0]

        self.assertFalse(
            kwargs.get("capture_output", False),
            "enroll() must NOT use capture_output=True (pipes stdin too) "
            "— regression of the 2026-05-11 'not a terminal' bug",
        )
        self.assertIsNone(
            kwargs.get("stdin"),
            "enroll() must inherit stdin (=None), not pipe it",
        )
        self.assertIsNone(
            kwargs.get("stderr"),
            "enroll() must inherit stderr (=None), not pipe it",
        )
        self.assertEqual(
            kwargs.get("stdout"),
            subprocess.PIPE,
            "enroll() must capture stdout (where the identity stub lives)",
        )

    # ---- failure-mode coverage -------------------------------------------

    def test_plugin_failure_propagates(self) -> None:
        """If the plugin errors (e.g. by being passed an unrecognized
        flag), ``enroll()`` must surface a ``YubiKeyError`` with the
        return code so the operator knows where to look."""
        # ``enroll()`` invokes the plugin by the bare name
        # ``age-plugin-yubikey`` so PATH lookup wins. Overwrite the
        # on-PATH binary directly with a failing version.
        target = self.bins.plugin
        target.unlink()
        target.write_text(
            "#!/usr/bin/env bash\n"
            ">&2 echo 'age-plugin-yubikey: simulated failure'\n"
            "exit 2\n"
        )
        target.chmod(0o755)
        with self.assertRaises(self.yk.YubiKeyError) as ctx:
            self.yk.enroll()
        msg = str(ctx.exception)
        self.assertIn("rc=2", msg)
        self.assertIn("age-plugin-yubikey", msg)

    # ---- idempotency / force ---------------------------------------------

    def test_force_re_enroll_overwrites_identity(self) -> None:
        """``enroll(force=True)`` must overwrite an existing identity
        even if previous artifacts are present."""
        first = self.yk.enroll()
        first_text = self.yk.IDENTITY_PATH.read_text()
        # Re-enroll with a different recipient to prove overwrite.
        os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT"] = (
            "age1yubikey1zzzz123zzzz123zzzz123zzzz123zzzz123zzzz123zzzz12"
        )
        try:
            second = self.yk.enroll(force=True)
        finally:
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", None)
        second_text = self.yk.IDENTITY_PATH.read_text()
        self.assertNotEqual(first_text, second_text)
        self.assertNotEqual(first.recipient, second.recipient)


# ---------------------------------------------------------------------------
# Standalone smoke tests for the fake binary itself. If THESE fail the
# fake doesn't model 0.5.x correctly and every integration test above
# is unreliable.

class FakePluginContractTests(unittest.TestCase):
    """The fake plugin must behave exactly like age-plugin-yubikey 0.5.x
    for the CLI surface we depend on. These tests pin its contract."""

    def _run(self, *args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(_FAKE_PLUGIN_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=15,
            env=full_env,
        )

    def test_version_flag_works(self) -> None:
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("age-plugin-yubikey", r.stdout)

    def test_generate_emits_recipient_and_stub(self) -> None:
        r = self._run("--generate", "--slot", "1", "--name", "test")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("# Recipient: age1yubikey1", r.stdout)
        self.assertIn("AGE-PLUGIN-YUBIKEY-1", r.stdout)
        self.assertIn("Generating key", r.stderr)

    def test_rejects_identity_output_flag(self) -> None:
        r = self._run("--generate", "--identity-output", "/tmp/x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--identity-output", r.stderr)
        self.assertIn("unrecognized option", r.stderr)

    def test_require_tty_mode_errors_without_tty(self) -> None:
        r = self._run(
            "--generate",
            env={"LOCAL_SCRIBE_FAKE_AGE_PLUGIN_REQUIRE_TTY": "1"},
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a terminal", r.stderr)


if __name__ == "__main__":
    unittest.main()
