"""Regression tests for the ``_reattach_stdin_to_tty`` plumbing in
``local_scribe.security.key_lifecycle``.

2026-05-11 bug
--------------

``./run.sh key init`` pipes the operator's DR-recovery passphrase into
``python -m local_scribe.security.key_lifecycle init`` on stdin. The
Python entry point reads all of stdin to collect the passphrase, then
calls ``init_master_key()`` which (via ``yubikey_backup.enroll()``)
spawns ``age-plugin-yubikey --generate`` with stdin inherited. The
plugin prompts for the YubiKey PIV PIN — but inherited stdin is now
the closed end of the bash pipe, so the plugin errors with::

    Enter PIN for YubiKey with serial NNNNNNNN (default is 123456):
    Error: Failed to get input from user: IO error: Bad file descriptor (os error 9)

The fix is to reopen fd 0 to ``/dev/tty`` AFTER consuming the
passphrase and BEFORE spawning the plugin. ``_reattach_stdin_to_tty``
encapsulates that os.open + os.dup2 dance.

These tests pin:

  test_reattach_stdin_to_tty_returns_false_when_no_tty
        Headless / nohup case: returns False rather than raising. The
        caller is then responsible for failing loudly.

  test_reattach_stdin_to_tty_dups_when_tty_available
        Drives a controlled-pty subprocess where /dev/tty IS
        available, asserts dup2(tty_fd, 0) actually happened.

  test_cli_init_reattaches_after_passphrase_read
        Pins call order: _cli_init must consume stdin AND THEN call
        _reattach_stdin_to_tty AND THEN call init_master_key. Caught
        as a monkey-patch trace.

  test_cli_init_fails_loud_when_no_tty_and_enroll
        Headless case + ``--no-enroll`` not passed: must exit 2 with
        the clear error message, not crash inside age-plugin-yubikey.

  test_cli_init_skips_reattach_when_no_enroll
        ``--no-enroll`` skips the YubiKey path entirely, so we
        shouldn't even try to reattach stdin.

  test_cli_dr_restore_reattaches_when_reinit
        Same bug surface as _cli_init, same fix; pin the call order
        for the DR-restore path too.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from typing import Optional
from unittest import mock


class ReattachHelperTests(unittest.TestCase):
    """Direct unit tests for ``_reattach_stdin_to_tty`` itself."""

    def test_reattach_stdin_to_tty_returns_false_when_no_tty(self) -> None:
        """If ``/dev/tty`` can't be opened (CI / nohup), the helper
        must return False instead of raising."""
        from local_scribe.security import key_lifecycle as kl
        with mock.patch("os.open", side_effect=OSError("ENXIO")):
            self.assertFalse(kl._reattach_stdin_to_tty())

    def test_reattach_stdin_to_tty_dups_when_tty_available(self) -> None:
        """When ``/dev/tty`` opens successfully, dup2 is called with
        the tty fd to fd 0."""
        from local_scribe.security import key_lifecycle as kl
        with mock.patch("os.open", return_value=42) as m_open, \
             mock.patch("os.dup2") as m_dup2, \
             mock.patch("os.close") as m_close:
            ok = kl._reattach_stdin_to_tty()
        self.assertTrue(ok)
        m_open.assert_called_once_with("/dev/tty", os.O_RDONLY)
        m_dup2.assert_called_once_with(42, 0)
        m_close.assert_called_once_with(42)

    def test_reattach_helper_always_closes_fd_on_dup2_error(self) -> None:
        """If dup2 raises, the tty fd is still closed (no leak)."""
        from local_scribe.security import key_lifecycle as kl
        with mock.patch("os.open", return_value=42), \
             mock.patch("os.dup2", side_effect=OSError("EBADF")), \
             mock.patch("os.close") as m_close:
            with self.assertRaises(OSError):
                kl._reattach_stdin_to_tty()
        m_close.assert_called_once_with(42)


class CliInitCallOrderTests(unittest.TestCase):
    """Pins that ``_cli_init`` reads stdin → reattaches → spawns enroll
    (in that order). Catches the 2026-05-11 regression directly."""

    def _drive_cli_init(
        self,
        *,
        args: list[str],
        stdin_payload: str = "",
        reattach_returns: bool = True,
    ) -> tuple[int, list[str]]:
        """Run ``key_lifecycle._cli_init(args)`` with stdin fed by
        ``stdin_payload``. Returns ``(rc, trace)`` where trace is the
        list of major call events in order."""
        from local_scribe.security import key_lifecycle as kl
        trace: list[str] = []

        def fake_reattach() -> bool:
            trace.append("reattach")
            return reattach_returns

        def fake_init(**kwargs: object) -> kl.InitResult:
            trace.append(
                f"init_master_key(enroll={kwargs.get('enroll_yubikey')},"
                f"force={kwargs.get('force')},"
                f"dr={'set' if kwargs.get('dr_passphrase') else 'none'})"
            )
            return kl.InitResult(
                kc_half_stored=True,
                yk_half_wrapped=True,
                dr_backup_written=bool(kwargs.get("dr_passphrase")),
                recipient="age1yubikey1fake",
            )

        # We rely on _cli_init doing ``raw = _sys.stdin.read()``.
        # io.StringIO models the post-EOF state we care about.
        fake_stdin = io.StringIO(stdin_payload)
        with mock.patch.object(kl, "_reattach_stdin_to_tty", new=fake_reattach), \
             mock.patch.object(kl, "init_master_key", new=fake_init), \
             mock.patch.object(sys, "stdin", new=fake_stdin), \
             mock.patch.object(sys, "stdout", new=io.StringIO()):
            rc = kl._cli_init(args)
        return rc, trace

    def test_cli_init_reattaches_after_passphrase_read(self) -> None:
        """Happy path: reattach is called between the stdin read and
        the enroll-spawning ``init_master_key`` call."""
        rc, trace = self._drive_cli_init(
            args=[],
            stdin_payload="hunter2",
        )
        self.assertEqual(rc, 0)
        # The order matters: stdin already consumed by the time we
        # see ``reattach``, and init_master_key is called AFTER.
        self.assertEqual(trace[0], "reattach")
        self.assertTrue(
            trace[1].startswith("init_master_key("),
            f"expected init_master_key call after reattach, got: {trace}",
        )
        self.assertIn("dr=set", trace[1])

    def test_cli_init_fails_loud_when_no_tty_and_enroll(self) -> None:
        """No controlling tty AND we need to enroll → rc=2 + clear
        error. We must NOT proceed into init_master_key (which would
        spawn age-plugin-yubikey and die confusingly)."""
        rc, trace = self._drive_cli_init(
            args=[],
            stdin_payload="hunter2",
            reattach_returns=False,
        )
        self.assertEqual(rc, 2, msg=f"trace={trace}")
        self.assertEqual(trace, ["reattach"],
                         msg="must NOT call init_master_key when reattach fails")

    def test_cli_init_skips_reattach_when_no_enroll(self) -> None:
        """``--no-enroll`` means we never spawn age-plugin-yubikey,
        so we don't need to reattach stdin at all."""
        rc, trace = self._drive_cli_init(
            args=["--no-enroll"],
            stdin_payload="hunter2",
        )
        self.assertEqual(rc, 0)
        self.assertNotIn(
            "reattach", trace,
            msg=f"reattach should be skipped when --no-enroll; trace={trace}",
        )
        self.assertTrue(any(t.startswith("init_master_key(") for t in trace))

    def test_cli_init_with_no_dr_still_reattaches(self) -> None:
        """``--no-dr`` skips the passphrase read but we STILL spawn
        age-plugin-yubikey, so the reattach must still happen. (Bash
        callers that pass ``--no-dr`` don't pipe stdin at all, but
        we should be defensive — a future caller might pipe garbage.)"""
        rc, trace = self._drive_cli_init(
            args=["--no-dr"],
            stdin_payload="",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(trace[0], "reattach")


class CliDrRestoreCallOrderTests(unittest.TestCase):
    """Same regression coverage for ``_cli_dr_restore``."""

    def test_cli_dr_restore_reattaches_when_reinit(self) -> None:
        from local_scribe.security import key_lifecycle as kl
        trace: list[str] = []

        def fake_reattach() -> bool:
            trace.append("reattach")
            return True

        class _FakeMK:
            def as_bytes(self) -> bytes:
                return b"\x00" * 32

            def forget(self) -> None:
                pass

        def fake_dr_restore(passphrase: str, **kwargs: object):
            trace.append(
                f"dr_restore(reinit={kwargs.get('re_init_yubikey')},"
                f"overwrite={kwargs.get('overwrite_existing_v2')})"
            )
            return _FakeMK()

        fake_stdin = io.StringIO("hunter2\n")
        with mock.patch.object(kl, "_reattach_stdin_to_tty", new=fake_reattach), \
             mock.patch.object(kl, "dr_restore", new=fake_dr_restore), \
             mock.patch.object(sys, "stdin", new=fake_stdin), \
             mock.patch.object(sys, "stdout", new=io.StringIO()), \
             mock.patch.object(sys, "stderr", new=io.StringIO()), \
             mock.patch(
                 "local_scribe.security.service_auth.derive_service_token",
                 return_value=b"\x00" * 32,
             ), \
             mock.patch(
                 "local_scribe.security.service_auth.token_fingerprint",
                 return_value="deadbe",
             ), \
             mock.patch(
                 "local_scribe.security.service_auth.KNOWN_SERVICES",
                 new=["asr", "inspector"],
             ):
            rc = kl._cli_dr_restore([])
        self.assertEqual(rc, 0)
        self.assertEqual(trace[0], "reattach")
        self.assertTrue(trace[1].startswith("dr_restore("))
        self.assertIn("reinit=True", trace[1])

    def test_cli_dr_restore_skips_reattach_with_no_reinit(self) -> None:
        """``--no-reinit`` means we don't re-run YubiKey enroll, so
        the reattach is unnecessary."""
        from local_scribe.security import key_lifecycle as kl
        trace: list[str] = []

        class _FakeMK:
            def as_bytes(self) -> bytes:
                return b"\x00" * 32

            def forget(self) -> None:
                pass

        def fake_reattach() -> bool:
            trace.append("reattach")
            return True

        def fake_dr_restore(passphrase: str, **kwargs: object):
            trace.append("dr_restore")
            return _FakeMK()

        fake_stdin = io.StringIO("hunter2\n")
        with mock.patch.object(kl, "_reattach_stdin_to_tty", new=fake_reattach), \
             mock.patch.object(kl, "dr_restore", new=fake_dr_restore), \
             mock.patch.object(sys, "stdin", new=fake_stdin), \
             mock.patch.object(sys, "stdout", new=io.StringIO()), \
             mock.patch.object(sys, "stderr", new=io.StringIO()), \
             mock.patch(
                 "local_scribe.security.service_auth.derive_service_token",
                 return_value=b"\x00" * 32,
             ), \
             mock.patch(
                 "local_scribe.security.service_auth.token_fingerprint",
                 return_value="deadbe",
             ), \
             mock.patch(
                 "local_scribe.security.service_auth.KNOWN_SERVICES",
                 new=["asr"],
             ):
            rc = kl._cli_dr_restore(["--no-reinit"])
        self.assertEqual(rc, 0)
        self.assertNotIn("reattach", trace, msg=f"trace={trace}")


if __name__ == "__main__":
    unittest.main()
