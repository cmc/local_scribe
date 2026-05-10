"""Tests for yubikey_backup.py.

The real flow requires a physical YubiKey + ``age-plugin-yubikey``,
neither of which is available in CI / unit tests. We work around that
by injecting fake binaries via the documented env hooks:

  - ``LOCAL_SCRIBE_AGE_BIN``         — replaces the ``age`` CLI
  - ``LOCAL_SCRIBE_YKMAN_BIN``       — replaces ``ykman``
  - ``LOCAL_SCRIBE_AGE_PLUGIN_BIN``  — replaces ``age-plugin-yubikey``
  - ``LOCAL_SCRIBE_CONFIG_DIR``      — points config at a tmpdir

The fake ``age`` implements a deliberately tiny envelope format that
the matching ``-d`` invocation knows how to undo. It supports
multi-``-r`` encryption (multiple recipients pasted into the header)
and ``age -d -i IDENTITY SRC`` decryption that succeeds only when one
of the recipients in the SRC header matches the ``# Recipient:``
line in IDENTITY. This is enough to model the part of age's behaviour
we depend on (any-of-N decrypt) without pulling in the real crypto.

The legacy ``backup_key`` / ``restore_key`` round-trip is tested
alongside the new ``backup_yk_half`` / ``restore_yk_half`` so we
catch regressions in the shared ``_age_encrypt`` / ``_age_decrypt``
helpers from either side.
"""

from __future__ import annotations

import importlib
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path


# ----- fake age shim --------------------------------------------------

_FAKE_AGE = r"""#!/usr/bin/env bash
# Tiny age substitute for tests. Supported invocations:
#   age -r R1 [-r R2 ...] -o OUT          (encrypt stdin -> OUT)
#   age -d -i IDENTITY SRC                (decrypt SRC -> stdout)
# File format on disk:
#   FAKE_AGE\n
#   <comma-separated recipients>\n
#   <hex-encoded payload>\n
# Decrypt parses the recipient list and matches against a
# ``# Recipient: <r>`` line in the identity file. Any match unlocks.

set -u
recipients=()
mode=""
out=""
identity=""
src=""
while [ $# -gt 0 ]; do
  case "$1" in
    -r) recipients+=("$2"); shift 2 ;;
    -o) out="$2"; shift 2 ;;
    -d) mode="decrypt"; shift ;;
    -i) identity="$2"; shift 2 ;;
    *)  src="$1"; shift ;;
  esac
done

if [ "$mode" = "decrypt" ]; then
  if [ -z "$identity" ] || [ -z "$src" ]; then
    echo "fake age: --identity and src are required" >&2; exit 2
  fi
  header_marker=$(sed -n '1p' "$src")
  if [ "$header_marker" != "FAKE_AGE" ]; then
    echo "fake age: bad header in $src" >&2; exit 3
  fi
  enc_recipients=$(sed -n '2p' "$src")
  hex=$(sed -n '3p' "$src")
  id_recipient=$(grep -E '^# Recipient: ' "$identity" | head -n1 | sed 's/^# Recipient: //')
  if [ -z "$id_recipient" ]; then
    echo "fake age: identity missing # Recipient: line" >&2; exit 4
  fi
  IFS=',' read -ra parts <<<"$enc_recipients"
  matched="no"
  for r in "${parts[@]}"; do
    if [ "$r" = "$id_recipient" ]; then matched="yes"; break; fi
  done
  if [ "$matched" != "yes" ]; then
    echo "fake age: identity does not decrypt this file" >&2; exit 5
  fi
  # Convert hex to bytes and write to stdout.
  python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))" "$hex"
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
# Tiny ykman stub: ``ykman list`` returns one fake serial so
# ``is_yubikey_present()`` reports True. Anything else exits 0 with
# no output (we don't drive any other ykman commands).
case "$1" in
  list) echo "YubiKey 5 NFC (fake) Serial: 12345678";;
  *) ;;
esac
exit 0
"""


_FAKE_AGE_PLUGIN = """#!/usr/bin/env bash
# We don't run the real enroll path in tests (it would need a YubiKey).
# This stub exists only so ``assert_tools()`` doesn't bail.
exit 0
"""


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_fake_identity(path: Path, recipient: str) -> None:
    """Mirror the layout written by age-plugin-yubikey so
    yubikey_backup's parsers can pick the recipient out."""
    path.write_text(textwrap.dedent(f"""\
        # Serial: 12345678
        # Slot: 1
        # Recipient: {recipient}
        AGE-PLUGIN-YUBIKEY-FAKE-IDENTITY-STUB
        """))


class _BaseShimTests(unittest.TestCase):
    """Common setup: fresh tmpdir-as-config + fake age/ykman, then
    reload the module so module-level constants pick up the new
    ``LOCAL_SCRIBE_CONFIG_DIR``."""

    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.config_dir = tmp / "config"
        self.config_dir.mkdir()
        self.fake_age = _write_executable(self.bin_dir / "age", _FAKE_AGE)
        self.fake_ykman = _write_executable(self.bin_dir / "ykman", _FAKE_YKMAN)
        self.fake_plugin = _write_executable(
            self.bin_dir / "age-plugin-yubikey", _FAKE_AGE_PLUGIN
        )
        self._old_env: dict[str, str | None] = {}
        overrides = {
            "LOCAL_SCRIBE_CONFIG_DIR": str(self.config_dir),
            "LOCAL_SCRIBE_AGE_BIN": str(self.fake_age),
            "LOCAL_SCRIBE_YKMAN_BIN": str(self.fake_ykman),
            "LOCAL_SCRIBE_AGE_PLUGIN_BIN": str(self.fake_plugin),
        }
        for k, v in overrides.items():
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Reload so module-level CONFIG_DIR / paths recompute from the
        # new env. We deliberately reload only yubikey_backup here —
        # secret_store / key_lifecycle pick up env via call-time
        # helpers, and reloading them would invalidate exception
        # classes already imported by their dedicated test modules.
        import yubikey_backup
        importlib.reload(yubikey_backup)
        self.yk = yubikey_backup

    def tearDown(self) -> None:  # type: ignore[override]
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import yubikey_backup
        importlib.reload(yubikey_backup)
        self._tmp.cleanup()


# ---------- legacy whole-key round-trip ------------------------------


class LegacyBackupKeyRoundTripTests(_BaseShimTests):
    def test_round_trip(self):
        recipient = "age1yubikey1legacyrecipient"
        _make_fake_identity(self.yk.IDENTITY_PATH, recipient)
        self.yk.RECIPIENT_PATH.write_text(recipient + "\n")
        key = b"\xab" * 32
        self.yk.backup_key(key)
        self.assertTrue(self.yk.BACKUP_PATH.is_file())
        decrypted = self.yk.restore_key()
        self.assertEqual(decrypted, key)

    def test_restore_without_enrollment_raises(self):
        with self.assertRaises(self.yk.YubiKeyNotEnrolledError):
            self.yk.restore_key()


# ---------- Option C yk_half round-trip ------------------------------


class YkHalfRoundTripTests(_BaseShimTests):
    def test_single_recipient_round_trip(self):
        recipient = "age1yubikey1primaryrecipient"
        _make_fake_identity(self.yk.IDENTITY_PATH, recipient)
        self.yk.set_recipients([recipient])
        yk_half = b"\xcd" * 32
        self.yk.backup_yk_half(yk_half)
        self.assertTrue(self.yk.has_yk_half())
        restored = self.yk.restore_yk_half()
        self.assertEqual(restored, yk_half)

    def test_backup_rejects_wrong_length(self):
        recipient = "age1yubikey1primaryrecipient"
        _make_fake_identity(self.yk.IDENTITY_PATH, recipient)
        self.yk.set_recipients([recipient])
        with self.assertRaises(ValueError):
            self.yk.backup_yk_half(b"\x00" * 16)

    def test_backup_rejects_non_yubikey_recipient(self):
        self.yk.set_recipients(["age1notayubikey0000"])
        with self.assertRaises(self.yk.YubiKeyError):
            self.yk.backup_yk_half(b"\x00" * 32)

    def test_backup_rejects_no_recipients(self):
        with self.assertRaises(self.yk.YubiKeyNotEnrolledError):
            self.yk.backup_yk_half(b"\x00" * 32)

    def test_recipients_override_writes_recipient_file(self):
        # Calling backup_yk_half(..., recipients=...) should persist
        # the override so subsequent restore + list_recipients see it.
        recipient = "age1yubikey1explicitrecipient"
        _make_fake_identity(self.yk.IDENTITY_PATH, recipient)
        yk_half = b"\xee" * 32
        self.yk.backup_yk_half(yk_half, recipients=[recipient])
        self.assertEqual(self.yk.list_recipients(), [recipient])
        self.assertEqual(self.yk.restore_yk_half(), yk_half)


# ---------- multi-recipient enrollment -------------------------------


class MultiRecipientTests(_BaseShimTests):
    def test_add_recipient_re_wraps_for_both_identities(self):
        primary = "age1yubikey1primaryAAAAAAAAA"
        secondary = "age1yubikey1secondaryBBBBBBB"
        # Start enrolled to the primary only.
        _make_fake_identity(self.yk.IDENTITY_PATH, primary)
        yk_half = b"\x77" * 32
        self.yk.backup_yk_half(yk_half, recipients=[primary])
        self.assertEqual(self.yk.list_recipients(), [primary])

        # Add a secondary recipient + re-wrap.
        updated = self.yk.add_recipient(secondary, yk_half=yk_half)
        self.assertEqual(updated, [primary, secondary])
        self.assertEqual(self.yk.list_recipients(), [primary, secondary])

        # Primary identity decrypts (we left IDENTITY_PATH pointed at
        # the primary).
        self.assertEqual(self.yk.restore_yk_half(), yk_half)

        # Swap IDENTITY_PATH to the secondary's identity and confirm
        # the same ciphertext still decrypts.
        _make_fake_identity(self.yk.IDENTITY_PATH, secondary)
        self.assertEqual(self.yk.restore_yk_half(), yk_half)

    def test_add_recipient_idempotent(self):
        primary = "age1yubikey1primaryAAAA"
        _make_fake_identity(self.yk.IDENTITY_PATH, primary)
        self.yk.backup_yk_half(b"\x00" * 32, recipients=[primary])
        result = self.yk.add_recipient(primary, yk_half=b"\x00" * 32)
        self.assertEqual(result, [primary])

    def test_add_recipient_rejects_non_yubikey(self):
        self.yk.set_recipients(["age1yubikey1primaryAAAA"])
        with self.assertRaises(self.yk.YubiKeyError):
            self.yk.add_recipient("age1xxxxxxx", yk_half=b"\x00" * 32)

    def test_list_recipients_falls_back_to_legacy_single_file(self):
        recipient = "age1yubikey1legacyFFFF"
        self.yk.RECIPIENT_PATH.write_text(recipient + "\n")
        self.assertEqual(self.yk.list_recipients(), [recipient])

    def test_list_recipients_strips_comments_and_blanks(self):
        self.yk.RECIPIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.yk.RECIPIENTS_PATH.write_text(
            "# comment\n\n  age1yubikey1one  \n# also a comment\nage1yubikey1two\n"
        )
        self.assertEqual(
            self.yk.list_recipients(),
            ["age1yubikey1one", "age1yubikey1two"],
        )


# ---------- status -----------------------------------------------------


class StatusTests(_BaseShimTests):
    def test_status_includes_yk_half_fields(self):
        s = self.yk.status()
        self.assertIn("yk_half_present", s)
        self.assertIn("yk_half_path", s)
        self.assertIn("recipients", s)
        self.assertIn("recipient_count", s)
        self.assertFalse(s["yk_half_present"])
        self.assertEqual(s["recipients"], [])
        self.assertEqual(s["recipient_count"], 0)

    def test_status_reflects_present_yk_half(self):
        recipient = "age1yubikey1statusZZZZZ"
        _make_fake_identity(self.yk.IDENTITY_PATH, recipient)
        self.yk.backup_yk_half(b"\x42" * 32, recipients=[recipient])
        s = self.yk.status()
        self.assertTrue(s["yk_half_present"])
        self.assertEqual(s["recipient_count"], 1)
        self.assertGreater(s["yk_half_size_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
