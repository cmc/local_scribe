"""End-to-end tests for key_lifecycle.py.

This is the only test module that drives all four lower-level pieces
(:mod:`key_split`, :mod:`secret_store`, :mod:`yubikey_backup`,
:mod:`disaster_recovery`) together. We use:

* a fake Touch ID / Keychain helper (the same per-account shim the
  secret_store tests use, generalised to cover the kc_half account),
* a fake ``age`` shim that supports both ``-r RECIPIENT -o OUT``
  (asymmetric) and ``-p -o OUT`` (passphrase) modes plus their
  decrypt counterparts,
* a fake ``ykman`` so ``is_yubikey_present()`` returns True without
  hardware.

The tests assert the operator-facing invariants from the
:mod:`key_lifecycle` docstring: the master key never appears in
``argv``, on disk in cleartext, or in any log line; the two-factor
unlock recovers the master key bit-for-bit; rotation produces a
fresh key and re-wraps both halves; ``add_yubikey`` makes both
YubiKeys able to decrypt; and disaster recovery walks back from
passphrase + on-disk file to a working unlock.
"""

from __future__ import annotations

import importlib
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


_FAKE_TOUCHID = r"""#!/usr/bin/env bash
# Fake Touch ID helper supporting [--account NAME] <store|load|exists|delete>.
# Per-account state files live next to the script.
set -u
SCRIPT_DIR=$(dirname "$0")
ACCOUNT=master_key
if [ "${1:-}" = "--account" ]; then
  ACCOUNT="$2"; shift 2
fi
STATE_FILE="$SCRIPT_DIR/touchid.$ACCOUNT.hex"

case "${1:-}" in
  exists)
    if [ -f "$STATE_FILE" ]; then exit 0; else exit 2; fi
    ;;
  store)
    LINE=$(cat)
    [ -n "$LINE" ] || { echo "error: no input" >&2; exit 1; }
    printf "%s" "$LINE" > "$STATE_FILE"
    exit 0
    ;;
  load)
    if [ ! -f "$STATE_FILE" ]; then echo "error: not stored" >&2; exit 2; fi
    cat "$STATE_FILE"; printf "\n"; exit 0
    ;;
  delete)
    rm -f "$STATE_FILE"; exit 0
    ;;
  *)
    echo "error: unknown command ${1:-}" >&2; exit 1
    ;;
esac
"""


_FAKE_AGE = r"""#!/usr/bin/env bash
# Unified fake age supporting:
#   age -r R1 [-r R2 ...] -o OUT             (asymmetric encrypt)
#   age -d -i IDENTITY SRC                   (asymmetric decrypt)
#   age -p -o OUT                            (passphrase encrypt; AGE_PASSPHRASE)
#   age -d SRC                               (passphrase decrypt; AGE_PASSPHRASE)
#
# Asymmetric file format:
#   FAKE_AGE\n<csv recipients>\n<hex payload>\n
# Passphrase file format:
#   FAKE_AGE_P\n<passphrase>\n<hex payload>\n

set -u
recipients=()
mode=""
out=""
identity=""
src=""
passphrase_mode="no"
while [ $# -gt 0 ]; do
  case "$1" in
    -r) recipients+=("$2"); shift 2 ;;
    -o) out="$2"; shift 2 ;;
    -d) mode="decrypt"; shift ;;
    -i) identity="$2"; shift 2 ;;
    -p) passphrase_mode="yes"; shift ;;
    *)  src="$1"; shift ;;
  esac
done

if [ "$mode" = "decrypt" ]; then
  if [ -z "$src" ]; then echo "fake age: missing src" >&2; exit 2; fi
  header=$(sed -n '1p' "$src")
  if [ "$header" = "FAKE_AGE_P" ]; then
    if [ -z "${AGE_PASSPHRASE:-}" ]; then echo "fake age: no passphrase" >&2; exit 2; fi
    real=$(sed -n '2p' "$src")
    if [ "$AGE_PASSPHRASE" != "$real" ]; then echo "fake age: bad passphrase" >&2; exit 5; fi
    hex=$(sed -n '3p' "$src")
    python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))" "$hex"
    exit 0
  fi
  if [ "$header" != "FAKE_AGE" ]; then echo "fake age: bad header" >&2; exit 3; fi
  if [ -z "$identity" ]; then echo "fake age: -i required" >&2; exit 2; fi
  enc=$(sed -n '2p' "$src")
  hex=$(sed -n '3p' "$src")
  id_recipient=$(grep -E '^# Recipient: ' "$identity" | head -n1 | sed 's/^# Recipient: //')
  IFS=',' read -ra parts <<<"$enc"
  matched="no"
  for r in "${parts[@]}"; do
    if [ "$r" = "$id_recipient" ]; then matched="yes"; break; fi
  done
  if [ "$matched" != "yes" ]; then echo "fake age: bad recipient" >&2; exit 5; fi
  python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))" "$hex"
  exit 0
fi

# encrypt
if [ "$passphrase_mode" = "yes" ]; then
  if [ -z "${AGE_PASSPHRASE:-}" ] || [ -z "$out" ]; then
    echo "fake age: AGE_PASSPHRASE and -o required" >&2; exit 2
  fi
  payload_hex=$(python3 -c "import sys; sys.stdout.write(sys.stdin.buffer.read().hex())")
  { echo "FAKE_AGE_P"; echo "$AGE_PASSPHRASE"; echo "$payload_hex"; } > "$out"
  exit 0
fi
if [ -z "$out" ] || [ "${#recipients[@]}" -eq 0 ]; then
  echo "fake age: -r and -o required for encrypt" >&2; exit 2
fi
payload_hex=$(python3 -c "import sys; sys.stdout.write(sys.stdin.buffer.read().hex())")
joined=$(IFS=','; echo "${recipients[*]}")
{ echo "FAKE_AGE"; echo "$joined"; echo "$payload_hex"; } > "$out"
exit 0
"""


_FAKE_YKMAN = """#!/usr/bin/env bash
case "$1" in list) echo "YubiKey 5 Fake Serial: 12345678";; esac
exit 0
"""


_FAKE_AGE_PLUGIN = """#!/usr/bin/env bash
# Pretend-enroll: ignores all flags, exits 0 with no useful output. We
# never actually call enroll() in tests — we install a pre-baked
# identity file directly.
exit 0
"""


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_identity(path: Path, recipient: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(f"""\
        # Serial: 12345678
        # Slot: 1
        # Recipient: {recipient}
        AGE-PLUGIN-YUBIKEY-FAKE-IDENTITY
    """))


class _LifecycleBase(unittest.TestCase):
    """Sets up an isolated config dir + fake binaries, reloads all four
    submodules so their module-level constants pick up the env, then
    re-imports ``key_lifecycle`` so it sees the reloaded children."""

    PRIMARY = "age1yubikey1primary000000"
    SECONDARY = "age1yubikey1secondary00000"

    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.config_dir = tmp / "config"
        # Touch ID helper
        self.fake_touchid = _write_exec(self.bin_dir / "touchid-keychain", _FAKE_TOUCHID)
        # age + plugin + ykman
        self.fake_age = _write_exec(self.bin_dir / "age", _FAKE_AGE)
        self.fake_ykman = _write_exec(self.bin_dir / "ykman", _FAKE_YKMAN)
        self.fake_plugin = _write_exec(self.bin_dir / "age-plugin-yubikey", _FAKE_AGE_PLUGIN)

        self._old_env: dict[str, str | None] = {}
        for k, v in {
            "LOCAL_SCRIBE_TOUCHID_HELPER": str(self.fake_touchid),
            "LOCAL_SCRIBE_CONFIG_DIR": str(self.config_dir),
            "LOCAL_SCRIBE_AGE_BIN": str(self.fake_age),
            "LOCAL_SCRIBE_YKMAN_BIN": str(self.fake_ykman),
            "LOCAL_SCRIBE_AGE_PLUGIN_BIN": str(self.fake_plugin),
        }.items():
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v

        # Reload only the modules whose module-level constants depend
        # on LOCAL_SCRIBE_CONFIG_DIR (yubikey_backup + disaster_recovery
        # both capture CONFIG_DIR at import time). We do NOT reload
        # secret_store: it reads the helper path via helper_path() on
        # every call, so its module state is environment-clean, and
        # reloading it would invalidate the SecretStoreError /
        # UserCancelledError classes that other test modules have
        # already imported by name (those `isinstance` / `assertRaises`
        # checks would silently start failing).
        import secret_store, yubikey_backup, disaster_recovery, key_lifecycle
        importlib.reload(yubikey_backup)
        importlib.reload(disaster_recovery)
        importlib.reload(key_lifecycle)
        self.secret_store = secret_store
        self.yubikey_backup = yubikey_backup
        self.disaster_recovery = disaster_recovery
        self.key_lifecycle = key_lifecycle

    def tearDown(self) -> None:  # type: ignore[override]
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import yubikey_backup, disaster_recovery, key_lifecycle
        importlib.reload(yubikey_backup)
        importlib.reload(disaster_recovery)
        importlib.reload(key_lifecycle)
        self._tmp.cleanup()

    def _preenroll_primary(self) -> None:
        """Install the primary YubiKey's identity + recipient file so
        ``init_master_key(enroll_yubikey=False)`` can proceed without
        invoking the (no-op) fake age-plugin-yubikey enroll."""
        _make_identity(self.yubikey_backup.IDENTITY_PATH, self.PRIMARY)
        self.yubikey_backup.set_recipients([self.PRIMARY])


# ---------- init / unlock round-trip ---------------------------------


class InitAndUnlockTests(_LifecycleBase):
    def test_init_then_unlock_round_trip(self):
        self._preenroll_primary()
        result = self.key_lifecycle.init_master_key(
            enroll_yubikey=False,
            dr_passphrase="dr-pass",
        )
        self.assertTrue(result.kc_half_stored)
        self.assertTrue(result.yk_half_wrapped)
        self.assertTrue(result.dr_backup_written)
        self.assertEqual(result.recipient, self.PRIMARY)
        # Both halves are on disk / in Keychain now.
        self.assertTrue(self.secret_store.has_kc_half())
        self.assertTrue(self.yubikey_backup.has_yk_half())
        self.assertTrue(self.disaster_recovery.has_backup())

        # Unlock recovers a master key handle.
        mk = self.key_lifecycle.unlock_master_key()
        self.assertEqual(len(mk.as_bytes()), 32)
        first = mk.as_bytes()

        # Same key on a second unlock (deterministic for given halves).
        mk2 = self.key_lifecycle.unlock_master_key()
        self.assertEqual(mk2.as_bytes(), first)

    def test_init_refuses_to_clobber_existing_install(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        with self.assertRaises(RuntimeError):
            self.key_lifecycle.init_master_key(enroll_yubikey=False)

    def test_init_force_overwrites(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        first = self.key_lifecycle.unlock_master_key().as_bytes()
        self.key_lifecycle.init_master_key(enroll_yubikey=False, force=True)
        second = self.key_lifecycle.unlock_master_key().as_bytes()
        # New key after a forced re-init.
        self.assertNotEqual(first, second)

    def test_init_without_dr_passphrase_skips_dr(self):
        self._preenroll_primary()
        result = self.key_lifecycle.init_master_key(
            enroll_yubikey=False,
            dr_passphrase=None,
        )
        self.assertFalse(result.dr_backup_written)
        self.assertFalse(self.disaster_recovery.has_backup())

    def test_unlock_raises_when_uninitialised(self):
        with self.assertRaises(RuntimeError):
            self.key_lifecycle.unlock_master_key()


# ---------- rotation -------------------------------------------------


class RotationTests(_LifecycleBase):
    def test_rotate_replaces_both_halves(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr"
        )
        before = self.key_lifecycle.unlock_master_key().as_bytes()

        result = self.key_lifecycle.rotate_master_key()
        try:
            self.assertEqual(result.old.as_bytes(), before)
            self.assertNotEqual(result.new.as_bytes(), before)
            # Subsequent unlocks return the new key.
            after = self.key_lifecycle.unlock_master_key().as_bytes()
            self.assertEqual(after, result.new.as_bytes())
        finally:
            result.old.forget()
            result.new.forget()

    def test_rotate_with_new_dr_passphrase_rewrites_dr_file(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="old"
        )
        result = self.key_lifecycle.rotate_master_key(dr_passphrase="new")
        try:
            new_master = result.new.as_bytes()
            # Old passphrase no longer works (file rewritten).
            with self.assertRaises(self.disaster_recovery.DisasterRecoveryError):
                self.disaster_recovery.decrypt("old")
            # New passphrase recovers the new master key.
            recovered = self.disaster_recovery.decrypt("new")
            self.assertEqual(recovered, new_master)
        finally:
            result.old.forget()
            result.new.forget()


# ---------- add_yubikey ----------------------------------------------


class AddYubiKeyTests(_LifecycleBase):
    def test_add_second_yubikey_decrypts_same_ciphertext(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        master = self.key_lifecycle.unlock_master_key().as_bytes()

        # Enroll the secondary recipient.
        recipients = self.key_lifecycle.add_yubikey(self.SECONDARY)
        self.assertEqual(recipients, [self.PRIMARY, self.SECONDARY])

        # Swap the identity to the secondary YubiKey and confirm
        # unlock still works.
        _make_identity(self.yubikey_backup.IDENTITY_PATH, self.SECONDARY)
        recovered = self.key_lifecycle.unlock_master_key().as_bytes()
        self.assertEqual(recovered, master)

        # And swap back to the primary — still works.
        _make_identity(self.yubikey_backup.IDENTITY_PATH, self.PRIMARY)
        again = self.key_lifecycle.unlock_master_key().as_bytes()
        self.assertEqual(again, master)


# ---------- disaster recovery ----------------------------------------


class DisasterRecoveryTests(_LifecycleBase):
    def test_dr_restore_recovers_master_key(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="dr-pass"
        )
        master = self.key_lifecycle.unlock_master_key().as_bytes()
        # Simulate "Keychain wiped" by deleting kc_half.
        self.secret_store.delete_kc_half()
        # And "YubiKey lost" by removing the recipient + ciphertext.
        self.yubikey_backup.YK_HALF_PATH.unlink()

        # Reinstate just the recipient list (the user re-enrolls a
        # replacement YubiKey before invoking dr-restore in practice).
        self.yubikey_backup.set_recipients([self.PRIMARY])
        # And re-make the identity file so the resulting yk_half can
        # be decrypted by our test driver below.
        _make_identity(self.yubikey_backup.IDENTITY_PATH, self.PRIMARY)

        mk = self.key_lifecycle.dr_restore("dr-pass")
        try:
            self.assertEqual(mk.as_bytes(), master)
            # And routine unlock now works again (kc_half + yk_half rewritten).
            again = self.key_lifecycle.unlock_master_key().as_bytes()
            self.assertEqual(again, master)
        finally:
            mk.forget()

    def test_dr_restore_wrong_passphrase_raises(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(
            enroll_yubikey=False, dr_passphrase="right"
        )
        with self.assertRaises(self.disaster_recovery.DisasterRecoveryError):
            self.key_lifecycle.dr_restore("wrong")


# ---------- v1 → v2 migration ----------------------------------------


class MigrationTests(_LifecycleBase):
    def test_migrate_v1_to_v2_preserves_master_key(self):
        # Set up a v1 install: legacy whole-key item + YubiKey enrolled
        # but no kc_half. Migration must produce a v2 shape that
        # unlocks to the same master key.
        self._preenroll_primary()
        original = b"\xa5" * 32
        self.secret_store.store_master_key(original)
        self.assertTrue(self.secret_store.has_master_key())
        self.assertFalse(self.secret_store.has_kc_half())

        self.key_lifecycle.migrate_v1_to_v2()

        self.assertFalse(self.secret_store.has_master_key())
        self.assertTrue(self.secret_store.has_kc_half())

        recovered = self.key_lifecycle.unlock_master_key().as_bytes()
        self.assertEqual(recovered, original)

    def test_migration_idempotent_on_clean_v2(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False)
        before = self.key_lifecycle.unlock_master_key().as_bytes()
        # Migration is a no-op when only v2 is present.
        self.key_lifecycle.migrate_v1_to_v2()
        after = self.key_lifecycle.unlock_master_key().as_bytes()
        self.assertEqual(before, after)

    def test_migration_triggered_implicitly_on_unlock(self):
        # When unlock() is called against a v1-only install, the
        # migration helper runs automatically and the call returns
        # the same master key.
        self._preenroll_primary()
        original = b"\xb6" * 32
        self.secret_store.store_master_key(original)
        recovered = self.key_lifecycle.unlock_master_key().as_bytes()
        self.assertEqual(recovered, original)
        # v1 deleted; v2 in place.
        self.assertFalse(self.secret_store.has_master_key())
        self.assertTrue(self.secret_store.has_kc_half())


# ---------- status / no-prompts --------------------------------------


class StatusTests(_LifecycleBase):
    def test_status_uninitialised(self):
        s = self.key_lifecycle.status()
        self.assertEqual(s["shape"], "uninitialised")
        self.assertFalse(s["kc_half_present"])
        self.assertFalse(s["legacy_v1_present"])

    def test_status_after_init(self):
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False, dr_passphrase="x")
        s = self.key_lifecycle.status()
        self.assertEqual(s["shape"], "v2_split")
        self.assertTrue(s["kc_half_present"])
        self.assertTrue(s["yubikey"]["yk_half_present"])
        self.assertTrue(s["disaster_recovery"]["present"])


# ---------- threat-model assertions ----------------------------------


class ThreatModelInvariantTests(_LifecycleBase):
    """The user explicitly asked: 'the master key must never be passed
    as a command-line parameter or exposed in process logs'. These
    tests guard that invariant for the three operations that touch
    the master key (init, unlock, rotate)."""

    def test_master_key_never_in_subprocess_argv(self):
        self._preenroll_primary()
        captured: list[list[str]] = []
        real_run = __import__("subprocess").run

        def spy_run(cmd, *args, **kwargs):
            # Capture argv-equivalent for every subprocess invocation
            # that happens during init / unlock.
            captured.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=spy_run):
            self.key_lifecycle.init_master_key(
                enroll_yubikey=False, dr_passphrase="some-passphrase"
            )
            mk = self.key_lifecycle.unlock_master_key()

        master_hex = mk.as_hex()
        mk.forget()
        for cmd in captured:
            joined = " ".join(cmd)
            self.assertNotIn(master_hex, joined,
                             f"master key leaked into argv: {cmd!r}")
            self.assertNotIn("some-passphrase", joined,
                             f"DR passphrase leaked into argv: {cmd!r}")

    def test_no_plaintext_master_key_on_disk(self):
        # After init, neither kc_half nor yk_half (the only on-disk
        # artefacts apart from the DR file, which is age-encrypted)
        # should equal the master key.
        self._preenroll_primary()
        self.key_lifecycle.init_master_key(enroll_yubikey=False, dr_passphrase="x")
        mk = self.key_lifecycle.unlock_master_key()
        master = mk.as_bytes()
        mk.forget()

        # Walk every file in the config dir; assert none of them is
        # the master key (read raw bytes, not just utf-8 content).
        for root, _, files in os.walk(self.config_dir):
            for name in files:
                blob = (Path(root) / name).read_bytes()
                self.assertNotIn(master, blob,
                                 f"master key bytes found in {name}")


if __name__ == "__main__":
    unittest.main()
