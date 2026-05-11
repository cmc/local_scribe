"""Unit tests for key_safety.py — the pre-flight-backup + physical-
presence helpers that every destructive key op goes through.

These tests verify the *safety invariants* in isolation: snapshot
directories are written before any state changes, manifests record
fingerprints and a rollback cookbook, the Keychain backup account
gets a unique time-stamped name, and ``require_physical_presence``
refuses to proceed when there's no YubiKey to tap.

The integration with ``key_lifecycle`` (rotate / destroy / dr-restore /
add-yubikey actually call into these helpers) is exercised in
``test_key_lifecycle.py``; here we drive ``key_safety`` directly with
hand-picked fixtures so each property is testable on its own.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

# Re-use the lifecycle test rig so we get the fake Touch ID / age /
# ykman shims for free.
from tests.security.test_key_lifecycle import (  # type: ignore[import-not-found]
    _LifecycleBase,
    _FAKE_AGE,
    _FAKE_TOUCHID,
    _FAKE_YKMAN,
    _FAKE_AGE_PLUGIN,
    _write_exec,
    _make_identity,
)


class _SafetyBase(_LifecycleBase):
    """Subclass that ALSO reloads ``key_safety`` so its module-level
    constants pick up the temporary config dir."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        from local_scribe.security import key_safety
        importlib.reload(key_safety)
        self.key_safety = key_safety

    def tearDown(self) -> None:  # type: ignore[override]
        from local_scribe.security import key_safety
        importlib.reload(key_safety)
        super().tearDown()


# ---------- preflight_backup --------------------------------------------


class PreflightBackupTests(_SafetyBase):
    def test_yk_half_only_snapshot_skips_kc_half(self):
        """``BackupScope.YK_HALF_ONLY`` must NOT touch the Keychain
        (no Touch ID prompt for the operator). It snapshots only
        yk_half.age + recipients."""
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        record = self.key_safety.preflight_backup(
            "test-yk-only",
            scope=self.key_safety.BackupScope.YK_HALF_ONLY,
        )
        self.assertTrue(record.path.is_dir())
        self.assertIn("yk_half_age", record.artefacts)
        self.assertIn("yubikey_recipients", record.artefacts)
        # No DR file in the snapshot for YK_HALF_ONLY scope.
        self.assertNotIn("disaster_recovery_age", record.artefacts)
        # No Keychain backup account for YK_HALF_ONLY.
        self.assertIsNone(record.kc_half_backup_account)
        # Permissions: 0700 on the dir, 0600 on files.
        self.assertEqual(record.path.stat().st_mode & 0o777, 0o700)
        yk_copy = Path(record.artefacts["yk_half_age"])
        self.assertEqual(yk_copy.stat().st_mode & 0o777, 0o600)

    def test_both_halves_snapshot_includes_kc_half_account(self):
        """``BOTH_HALVES`` snapshots yk + dr + creates a Keychain
        backup account named ``master_key_kc_half_v2_backup_<ts>``."""
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        record = self.key_safety.preflight_backup(
            "test-both",
            scope=self.key_safety.BackupScope.BOTH_HALVES,
        )
        self.assertIn("yk_half_age", record.artefacts)
        self.assertIn("disaster_recovery_age", record.artefacts)
        self.assertIsNotNone(record.kc_half_backup_account)
        self.assertTrue(record.kc_half_backup_account.startswith(
            "master_key_kc_half_v2_backup_"
        ))
        # The backup account is readable via secret_store (Touch ID
        # in production; our fake helper just reads the file).
        backup_bytes = self.secret_store._load_item(  # noqa: SLF001
            prompt="test",
            account=record.kc_half_backup_account,
        )
        live_bytes = self.secret_store.load_kc_half(prompt="test")
        self.assertEqual(backup_bytes, live_bytes)

    def test_everything_scope_includes_dr(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        record = self.key_safety.preflight_backup(
            "test-everything",
            scope=self.key_safety.BackupScope.EVERYTHING,
        )
        self.assertIn("disaster_recovery_age", record.artefacts)
        self.assertIn("yk_half_age", record.artefacts)
        self.assertIsNotNone(record.kc_half_backup_account)

    def test_manifest_contains_fingerprints_and_cookbook(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        record = self.key_safety.preflight_backup(
            "test-manifest",
            scope=self.key_safety.BackupScope.BOTH_HALVES,
        )
        m_path = record.path / "manifest.json"
        self.assertTrue(m_path.is_file())
        m = json.loads(m_path.read_text())
        self.assertEqual(m["label"], "test-manifest")
        self.assertEqual(m["scope"], "both_halves")
        self.assertGreater(len(m["fingerprints"]), 0)
        self.assertIn("recovery_cookbook", m)
        cookbook = "\n".join(m["recovery_cookbook"])
        # Recovery instructions reference the snapshot's actual paths.
        self.assertIn(str(record.path), cookbook)
        # Cookbook explicitly mentions the Keychain restore command.
        self.assertIn("restore-kc-half", cookbook)

    def test_v1_legacy_scope_snapshots_legacy_item(self):
        # Stand up a v1-only install.
        self._preenroll_primary()
        self.secret_store.store_master_key(b"\xc1" * 32)
        record = self.key_safety.preflight_backup(
            "test-migrate",
            scope=self.key_safety.BackupScope.V1_LEGACY,
        )
        # The v1 key was duplicated to a backup account too.
        self.assertIn("v1_keychain_account", record.artefacts)
        v1_account = record.artefacts["v1_keychain_account"]
        self.assertTrue(v1_account.startswith("master_key_v1_backup_"))
        backup = self.secret_store._load_item(  # noqa: SLF001
            prompt="t", account=v1_account,
        )
        self.assertEqual(backup, b"\xc1" * 32)

    def test_failure_during_snapshot_cleans_up_partial_dir(self):
        """If snapshot dir creation succeeds but a later step explodes,
        we MUST remove the half-built dir so retry isn't blocked."""
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        # Sabotage the recipients-file copy by replacing the
        # RECIPIENTS_PATH with an unreadable file. We can't easily
        # cause shutil.copy2 to fail on a real file, so we patch
        # _maybe_copy to raise on the second invocation.
        from local_scribe.security import key_safety as ks_mod
        original = ks_mod._maybe_copy
        call_count = {"n": 0}

        def boom(src, dst_dir):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated I/O failure")
            return original(src, dst_dir)

        ks_mod._maybe_copy = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(ks_mod.BackupError):
                self.key_safety.preflight_backup(
                    "test-fail",
                    scope=self.key_safety.BackupScope.BOTH_HALVES,
                )
        finally:
            ks_mod._maybe_copy = original  # type: ignore[assignment]
        # Verify no partial directory was left around.
        for snap in self.key_safety.list_backups():
            self.assertNotEqual(snap.label, "test-fail")


# ---------- list / prune -------------------------------------------------


class ListAndPruneTests(_SafetyBase):
    def _make_snapshot(self, label: str) -> "self.key_safety.BackupRecord":
        return self.key_safety.preflight_backup(
            label, scope=self.key_safety.BackupScope.BOTH_HALVES,
        )

    def test_list_backups_newest_first(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        a = self._make_snapshot("alpha")
        import time
        time.sleep(1.1)  # different timestamp prefix
        b = self._make_snapshot("beta")
        listed = self.key_safety.list_backups()
        self.assertEqual([r.id for r in listed[:2]], [b.id, a.id])

    def test_prune_removes_dir_and_keychain_account(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr",
        )
        rec = self._make_snapshot("to-prune")
        kc_acct = rec.kc_half_backup_account
        self.assertIsNotNone(kc_acct)
        self.assertTrue(self.secret_store._has_item(account=kc_acct))  # noqa: SLF001
        result = self.key_safety.prune_backup(rec.id)
        self.assertFalse(rec.path.exists())
        self.assertTrue(result["keychain_deleted"])
        self.assertFalse(self.secret_store._has_item(account=kc_acct))  # noqa: SLF001

    def test_prune_refuses_path_traversal(self):
        self._preenroll_primary()
        # Try to prune something outside the backups dir.
        with self.assertRaises(FileNotFoundError):
            self.key_safety.prune_backup("../etc")
        # And a same-named-but-outside attempt: create a sibling dir
        # then try to prune by its name with a traversal prefix.
        outside = self.key_safety.BACKUPS_DIR.parent / "evil-target"
        outside.mkdir(parents=True, exist_ok=True)
        try:
            with self.assertRaises(FileNotFoundError):
                self.key_safety.prune_backup("../evil-target")
        finally:
            outside.rmdir()

    def test_prune_unknown_id_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.key_safety.prune_backup("does-not-exist")


# ---------- physical-presence proof -------------------------------------


class PhysicalPresenceTests(_SafetyBase):
    def test_succeeds_when_yubikey_can_decrypt(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        # No assertion needed — passing call is the success case.
        self.key_safety.require_physical_presence("test-success")

    def test_raises_when_no_enrollment(self):
        # No init at all -> no yk_half.age -> presence check fails.
        with self.assertRaises(self.key_safety.PhysicalPresenceRequired):
            self.key_safety.require_physical_presence("test-no-enrollment")

    def test_raises_when_age_plugin_fails(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        # Corrupt the yk_half.age file so age -d returns nonzero.
        self.yubikey_backup.YK_HALF_PATH.write_text("corrupt")
        with self.assertRaises(self.key_safety.PhysicalPresenceRequired):
            self.key_safety.require_physical_presence("test-corrupt")


# ---------- CLI: restore-kc-half ----------------------------------------


class RestoreKcHalfCliTests(_SafetyBase):
    def test_refuses_account_without_backup_prefix(self):
        # Try to "restore" from an attacker-named account.
        rc = self.key_safety._cli_restore_kc_half(  # noqa: SLF001
            ["master_key_kc_half_v2"]
        )
        self.assertEqual(rc, 2)

    def test_round_trip_restore_replaces_live_kc_half(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        live_before = self.secret_store.load_kc_half(prompt="t")
        # Make a snapshot, then rotate to change the live half.
        rec = self.key_safety.preflight_backup(
            "test-rkc", scope=self.key_safety.BackupScope.BOTH_HALVES,
        )
        # Replace the live kc_half with random data to simulate a
        # later rotate.
        self.secret_store.store_kc_half(b"\xff" * 32)
        self.assertNotEqual(self.secret_store.load_kc_half(prompt="t"), live_before)
        # Now restore from the backup account.
        rc = self.key_safety._cli_restore_kc_half(  # noqa: SLF001
            [rec.kc_half_backup_account]
        )
        self.assertEqual(rc, 0)
        # Live kc_half is back to the pre-rotate value.
        self.assertEqual(self.secret_store.load_kc_half(prompt="t"), live_before)


if __name__ == "__main__":
    unittest.main()
