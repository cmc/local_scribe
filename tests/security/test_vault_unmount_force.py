"""Unit tests for ``vault.unmount(force=...)`` and the matching
``vault_unlock.unmount_vault(force=...)`` + ``vault_unlock lock
--polite`` CLI flag.

Background
----------

``vault.unmount()`` originally tried a polite ``hdiutil detach`` and
then automatically retried with ``-force`` if anything held the
volume open. That's the right behaviour for the *interactive*
``./run.sh vault lock`` command -- the operator clicked lock and
expects the plaintext to vanish, even if Spotlight has a stale
indexer running.

It is *not* the right behaviour for the *lock-on-stop* path in
``cmd_stop``. That path fires every time the operator hits
``./run.sh stop``. If Char.app is still running, its SQLite handle on
``app.db`` is open; ``-force`` would yank the volume out from under
that handle mid-write and ``app.db`` would surface as ``database disk
image is malformed`` the next time Char tries to read it. The user's
notes are too important to risk.

The fix:

* ``vault.unmount(force=False)`` — polite detach only, propagates
  :class:`vault.VaultError` on failure.
* ``vault.unmount(force=True)`` — the historical behaviour (kept as
  default so existing callers don't change semantics).
* ``vault_unlock.unmount_vault(force=False)`` — public Python wrapper.
* ``python -m local_scribe.security.vault_unlock lock --polite`` — CLI
  flag used by ``vault_lock_on_stop`` in ``run.sh``.

These tests pin the behaviour without touching real hdiutil.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from local_scribe.security import vault, vault_unlock


# ----------------------------------------------------------------------
# vault.unmount(force=...)


class UnmountForceTrueTests(unittest.TestCase):
    """``force=True`` (the default) retries with ``-force`` on polite
    failure. This preserves the historical contract for the
    interactive ``./run.sh vault lock`` path."""

    def test_already_unmounted_is_noop(self) -> None:
        with mock.patch.object(vault, "is_mounted", return_value=False), \
             mock.patch.object(vault, "_hdiutil") as fake_hdiutil:
            vault.unmount()
        fake_hdiutil.assert_not_called()

    def test_polite_succeeds_no_force(self) -> None:
        with mock.patch.object(vault, "is_mounted", return_value=True), \
             mock.patch.object(vault, "_hdiutil") as fake_hdiutil:
            vault.unmount()
        self.assertEqual(fake_hdiutil.call_count, 1)
        args, _ = fake_hdiutil.call_args
        # First positional arg is the hdiutil arg list.
        self.assertEqual(args[0][0], "detach")
        self.assertNotIn("-force", args[0])

    def test_polite_fails_falls_back_to_force(self) -> None:
        calls: list[list[str]] = []

        def fake(args, **_kw):
            calls.append(list(args))
            if "-force" in args:
                return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")
            raise vault.VaultError("Resource busy")

        with mock.patch.object(vault, "is_mounted", return_value=True), \
             mock.patch.object(vault, "_hdiutil", side_effect=fake):
            vault.unmount(force=True)

        self.assertEqual(len(calls), 2)
        self.assertNotIn("-force", calls[0])
        self.assertIn("-force", calls[1])


class UnmountForceFalseTests(unittest.TestCase):
    """``force=False`` is the SQLite-safe path used by lock-on-stop:
    polite detach only, raises on failure rather than corrupting a
    live ``app.db``."""

    def test_already_unmounted_is_noop(self) -> None:
        with mock.patch.object(vault, "is_mounted", return_value=False), \
             mock.patch.object(vault, "_hdiutil") as fake_hdiutil:
            vault.unmount(force=False)
        fake_hdiutil.assert_not_called()

    def test_polite_succeeds(self) -> None:
        with mock.patch.object(vault, "is_mounted", return_value=True), \
             mock.patch.object(vault, "_hdiutil") as fake_hdiutil:
            vault.unmount(force=False)
        self.assertEqual(fake_hdiutil.call_count, 1)
        args, _ = fake_hdiutil.call_args
        self.assertNotIn("-force", args[0])

    def test_polite_fails_does_NOT_force(self) -> None:
        """The whole point: polite failure raises, we never reach the
        ``-force`` codepath that could corrupt SQLite."""
        calls: list[list[str]] = []

        def fake(args, **_kw):
            calls.append(list(args))
            raise vault.VaultError("Resource busy (Char has app.db open)")

        with mock.patch.object(vault, "is_mounted", return_value=True), \
             mock.patch.object(vault, "_hdiutil", side_effect=fake):
            with self.assertRaises(vault.VaultError) as cm:
                vault.unmount(force=False)
        self.assertEqual(len(calls), 1, f"force=False retried with -force! calls={calls}")
        self.assertNotIn("-force", calls[0])
        self.assertIn("Resource busy", str(cm.exception))


# ----------------------------------------------------------------------
# vault_unlock.unmount_vault(force=...) — thin wrapper, but the
# default must NOT change behaviour for existing call sites.


class UnmountVaultWrapperTests(unittest.TestCase):
    def test_default_is_force_true(self) -> None:
        """Existing callers (``./run.sh vault lock`` without a flag)
        must keep getting the force-on-polite-failure semantics."""
        with mock.patch.object(vault, "unmount") as fake_unmount:
            vault_unlock.unmount_vault()
        fake_unmount.assert_called_once_with(force=True)

    def test_explicit_force_false_propagates(self) -> None:
        with mock.patch.object(vault, "unmount") as fake_unmount:
            vault_unlock.unmount_vault(force=False)
        fake_unmount.assert_called_once_with(force=False)


# ----------------------------------------------------------------------
# CLI: ``vault_unlock lock`` ± ``--polite``


class CliLockTests(unittest.TestCase):
    """Pin the contract of the CLI lock subcommand as seen from
    ``run.sh``'s ``vault_lock_on_stop``: ``--polite`` opts out of
    ``-force`` and surfaces a non-zero exit code on polite failure."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err):
            rc = vault_unlock._cli_lock(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_lock_default_force_true(self) -> None:
        with mock.patch.object(vault, "unmount") as fake_unmount:
            rc, out, err = self._run([])
        self.assertEqual(rc, 0)
        fake_unmount.assert_called_once_with(force=True)
        payload = json.loads(out)
        self.assertTrue(payload["unmounted"])
        self.assertFalse(payload["polite"])

    def test_lock_polite_passes_force_false(self) -> None:
        with mock.patch.object(vault, "unmount") as fake_unmount:
            rc, out, err = self._run(["--polite"])
        self.assertEqual(rc, 0)
        fake_unmount.assert_called_once_with(force=False)
        payload = json.loads(out)
        self.assertTrue(payload["unmounted"])
        self.assertTrue(payload["polite"])

    def test_lock_polite_returns_nonzero_on_polite_failure(self) -> None:
        """The bash caller (``vault_lock_on_stop``) keys off this
        exit code to decide whether to print the "vault left mounted"
        warning. If we ever swallow the failure silently, the user
        sees a green "vault dismounted" message even though their
        plaintext is still readable."""
        with mock.patch.object(
            vault, "unmount",
            side_effect=vault.VaultError("Resource busy"),
        ):
            rc, out, err = self._run(["--polite"])
        self.assertNotEqual(rc, 0, f"polite failure must surface as non-zero (got rc={rc}, stderr={err!r})")
        self.assertIn("Resource busy", err)


if __name__ == "__main__":
    unittest.main()
