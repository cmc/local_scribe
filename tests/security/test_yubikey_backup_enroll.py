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

  test_recipient_extracted_with_variable_whitespace
        Bug 5 (2026-05-11): the real plugin's stub uses ``#    Recipient:``
        with VARIABLE internal whitespace for column alignment. Our
        original prefix-based parser only matched ``# Recipient:`` (one
        space) and failed on the real format, producing the
        ``couldn't determine age recipient`` error AFTER the slot had
        already been written. This test confirms the regex-based
        extractor handles every whitespace combination.

  test_recovers_existing_slot_when_generate_fails
        Bug 6 (2026-05-11): when ``--generate`` errors with "Slot N is
        not empty" (e.g. because a previous Bug 5 enrollment populated
        the slot before raising), ``enroll()`` must fall back to
        ``--identity --slot N`` and adopt the existing identity rather
        than refusing to proceed.

  test_force_overrides_slot_collision
        With ``force=True``, the plugin DOES overwrite the slot and we
        do NOT take the recovery path.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

_RECIPIENT_RE = re.compile(r"age1yubikey1[a-z0-9]+")


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
        # Real 0.5.x emits ``#    Recipient: age1yubikey1...`` with
        # column-aligned whitespace; don't pin on a specific spacing.
        self.assertRegex(identity_text, r"Recipient:\s+age1yubikey1")
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
        passes. The --generate call reads the PIV PIN from stdin and
        emits prompts on stderr; we MUST inherit both so the operator
        can answer. The follow-up --identity call is non-interactive
        and is allowed to pipe.
        """
        recorded: list[tuple[list[str], dict]] = []
        real_run = subprocess.run

        def recording_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and "age-plugin-yubikey" in cmd[0]:
                recorded.append((list(cmd), dict(kwargs)))
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
        gen_calls = [(c, kw) for c, kw in recorded if "--generate" in c]
        self.assertTrue(gen_calls, "expected at least one --generate call")
        _, kwargs = gen_calls[0]

        self.assertFalse(
            kwargs.get("capture_output", False),
            "--generate must NOT use capture_output=True (pipes stdin too) "
            "— regression of the 2026-05-11 'not a terminal' bug",
        )
        self.assertIsNone(
            kwargs.get("stdin"),
            "--generate must inherit stdin (=None), not pipe it",
        )
        self.assertIsNone(
            kwargs.get("stderr"),
            "--generate must inherit stderr (=None), not pipe it",
        )
        self.assertEqual(
            kwargs.get("stdout"),
            subprocess.PIPE,
            "--generate must capture stdout (where the identity stub lives)",
        )

    # ---- failure-mode coverage -------------------------------------------

    def test_plugin_failure_propagates(self) -> None:
        """If BOTH ``--generate`` and the ``--identity`` recovery probe
        fail, ``enroll()`` must surface a ``YubiKeyError`` with the
        return codes so the operator knows where to look."""
        # ``enroll()`` invokes the plugin by the bare name
        # ``age-plugin-yubikey`` so PATH lookup wins. Overwrite the
        # on-PATH binary directly with a failing version that
        # rejects every invocation.
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
        self.assertIn("age-plugin-yubikey", msg)
        # The error must mention both return codes so the operator
        # can distinguish a --generate-only failure from a total
        # plugin breakage.
        self.assertIn("--generate rc=2", msg)
        self.assertIn("--identity rc=2", msg)

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

    # ---- BUG-5 regression: recipient parser must tolerate whitespace ------

    def test_recipient_extracted_with_variable_whitespace(self) -> None:
        """The real plugin formats the recipient line as
        ``#    Recipient: age1yubikey1...`` (column-aligned, variable
        internal whitespace). Our original parser only matched the
        exact ``# Recipient:`` (single-space) prefix and failed in
        production on 2026-05-11.

        This test feeds the parser every whitespace combination we've
        observed in the wild — failure indicates a regression in the
        regex-based extractor.
        """
        # Exact format captured 2026-05-11 from a real YubiKey via
        # ``age-plugin-yubikey --identity --slot 1``. Note: bare
        # ``Recipient:`` summary line at top, then the indented
        # ``#    Recipient:`` inside the stub.
        canonical_stub = (
            "Recipient: age1yubikey1qgvkpum4aujll3usmthge3u940v6aglejcujjewhqjdlskvkgtyujfkvtap\n"
            "#       Serial: 16366413, Slot: 1\n"
            "#         Name: local_scribe\n"
            "#      Created: Mon, 11 May 2026 22:24:55 +0000\n"
            "#   PIN policy: Never  (A PIN is NOT required to decrypt)\n"
            "# Touch policy: Always (A physical touch is required for every decryption)\n"
            "#    Recipient: age1yubikey1qgvkpum4aujll3usmthge3u940v6aglejcujjewhqjdlskvkgtyujfkvtap\n"
            "AGE-PLUGIN-YUBIKEY-1FKALJQYZVLYJ4JCGWZLHT\n"
        )
        extracted = self.yk._extract_recipient(canonical_stub)
        self.assertEqual(
            extracted,
            "age1yubikey1qgvkpum4aujll3usmthge3u940v6aglejcujjewhqjdlskvkgtyujfkvtap",
            "_extract_recipient must find the token regardless of the "
            "comment-prefix whitespace alignment (regression of the "
            "2026-05-11 'couldn't determine age recipient' bug)",
        )

        # Spot-check pathological whitespace combinations that aren't
        # in the canonical stub but could appear in a future plugin
        # revision.
        for variant in [
            "#Recipient:age1yubikey1abc123",
            "#  Recipient: age1yubikey1abc123",
            "    age1yubikey1abc123\n",
            "Recipient: age1yubikey1abc123",
            "noise\n#\tRecipient:\tage1yubikey1abc123\nnoise\n",
        ]:
            self.assertIsNotNone(
                self.yk._extract_recipient(variant),
                f"failed to extract from variant: {variant!r}",
            )

        # And the same regex must work when reading from disk.
        identity_path = self.cfg_dir / "stub.txt"
        identity_path.write_text(canonical_stub)
        recipient = self.yk._read_recipient_from_identity(identity_path)
        self.assertEqual(
            recipient,
            "age1yubikey1qgvkpum4aujll3usmthge3u940v6aglejcujjewhqjdlskvkgtyujfkvtap",
        )

    # ---- BUG-6 regression: recover existing slot via --identity fallback --

    def test_recovers_existing_slot_when_generate_fails(self) -> None:
        """Simulate the 2026-05-11 partial-enroll state: a previous
        ``enroll()`` populated slot 1 but then raised because we
        couldn't parse the recipient. On retry, ``--generate`` errors
        with "Slot N is not empty" (rc=1), and ``enroll()`` must fall
        back to ``--identity --slot N`` to adopt the existing identity
        instead of failing — otherwise the operator is stuck and must
        manually pass ``--force`` to burn the slot.
        """
        # Turn on slot-state tracking in the fake plugin. The first
        # --generate writes slot 1; subsequent --generate without
        # --force errors exactly like the real plugin.
        state_dir = self.cfg_dir / "fake-plugin-state"
        state_dir.mkdir()
        os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR"] = str(state_dir)
        recovered_recipient = (
            "age1yubikey1zrecover9zrecover9zrecover9zrecover9zrecover9zrec"
        )
        os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT"] = recovered_recipient
        try:
            # First enroll: populates slot 1 successfully.
            first = self.yk.enroll()
            self.assertEqual(first.recipient, recovered_recipient)
            # Wipe our local config-dir artifacts so the next enroll()
            # has nothing to read back — only the YubiKey slot is
            # populated. This mirrors the production state on
            # 2026-05-11 where the slot had been written but our
            # config dir was empty because we raised before persisting.
            for p in (self.yk.IDENTITY_PATH, self.yk.RECIPIENT_PATH):
                if p.exists():
                    p.unlink()
            # Clear the trace so the next assertions are unambiguous.
            self.trace_path.write_text("")

            # Second enroll: --generate now errors with slot-not-empty.
            # We must recover via --identity and re-persist artifacts.
            second = self.yk.enroll()
        finally:
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR", None)
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", None)

        self.assertEqual(
            second.recipient,
            recovered_recipient,
            "enroll() must recover the existing slot's recipient",
        )
        self.assertTrue(self.yk.IDENTITY_PATH.exists())
        self.assertTrue(self.yk.RECIPIENT_PATH.exists())
        # Verify the recovery path actually called --identity.
        trace = self.trace_path.read_text()
        self.assertIn("--generate", trace)
        self.assertIn("--identity", trace)

    def test_force_overrides_slot_collision(self) -> None:
        """``enroll(force=True)`` must overwrite even a populated slot
        rather than taking the --identity recovery path. The fake
        plugin's --force flag mirrors the real plugin's behaviour.
        """
        state_dir = self.cfg_dir / "fake-plugin-state"
        state_dir.mkdir()
        os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR"] = str(state_dir)
        first_recipient = (
            "age1yubikey1zfirst9zfirst9zfirst9zfirst9zfirst9zfirst9zfirst9z"
        )
        second_recipient = (
            "age1yubikey1zsecnd9zsecnd9zsecnd9zsecnd9zsecnd9zsecnd9zsecnd9z"
        )
        try:
            os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT"] = first_recipient
            first = self.yk.enroll()
            self.assertEqual(first.recipient, first_recipient)

            os.environ["LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT"] = second_recipient
            second = self.yk.enroll(force=True)
        finally:
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_STATE_DIR", None)
            os.environ.pop("LOCAL_SCRIBE_FAKE_AGE_PLUGIN_RECIPIENT", None)

        # With --force, the slot is overwritten with the new identity.
        self.assertEqual(second.recipient, second_recipient)


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
        self.assertRegex(r.stdout, r"Recipient:\s+age1yubikey1")
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
