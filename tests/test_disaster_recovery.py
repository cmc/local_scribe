"""Tests for disaster_recovery.py.

A fake ``age`` shim handles the ``-p`` (passphrase) modes via the
``AGE_PASSPHRASE`` env var, mirroring the convention real age 1.2+
supports. Wrong passphrase yields a non-zero exit so the Python wrapper
surfaces ``DisasterRecoveryError`` (the same UX as the real CLI).
"""

from __future__ import annotations

import importlib
import os
import stat
import tempfile
import unittest
from pathlib import Path


_FAKE_AGE = r"""#!/usr/bin/env bash
# Tiny age substitute for disaster_recovery tests.
#
# Supports two invocations:
#   age -p -o OUT                  (passphrase encrypt; stdin is plaintext;
#                                   passphrase from $AGE_PASSPHRASE)
#   age -d SRC                     (passphrase decrypt; passphrase from
#                                   $AGE_PASSPHRASE)
# File format:
#   FAKE_AGE_P\n<passphrase>\n<hex-payload>\n

set -u
mode="encrypt"
out=""
src=""
while [ $# -gt 0 ]; do
  case "$1" in
    -p) shift ;;
    -o) out="$2"; shift 2 ;;
    -d) mode="decrypt"; shift ;;
    *)  src="$1"; shift ;;
  esac
done

if [ "$mode" = "encrypt" ]; then
  if [ -z "${AGE_PASSPHRASE:-}" ] || [ -z "$out" ]; then
    echo "fake age: AGE_PASSPHRASE and -o required" >&2; exit 2
  fi
  payload_hex=$(python3 -c "import sys; sys.stdout.write(sys.stdin.buffer.read().hex())")
  { echo "FAKE_AGE_P"; echo "$AGE_PASSPHRASE"; echo "$payload_hex"; } > "$out"
  exit 0
fi

# decrypt
if [ -z "$src" ] || [ -z "${AGE_PASSPHRASE:-}" ]; then
  echo "fake age: AGE_PASSPHRASE + SRC required for decrypt" >&2; exit 2
fi
header=$(sed -n '1p' "$src")
if [ "$header" != "FAKE_AGE_P" ]; then
  echo "fake age: bad header" >&2; exit 3
fi
real_pass=$(sed -n '2p' "$src")
hex=$(sed -n '3p' "$src")
if [ "$AGE_PASSPHRASE" != "$real_pass" ]; then
  echo "fake age: bad passphrase" >&2; exit 5
fi
python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))" "$hex"
exit 0
"""


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _ShimBase(unittest.TestCase):
    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        self.config_dir = tmp / "config"
        self.fake_age = _write_executable(self.bin_dir / "age", _FAKE_AGE)
        self._old_env: dict[str, str | None] = {}
        for k, v in {
            "LOCAL_SCRIBE_CONFIG_DIR": str(self.config_dir),
            "LOCAL_SCRIBE_AGE_BIN": str(self.fake_age),
        }.items():
            self._old_env[k] = os.environ.get(k)
            os.environ[k] = v
        import disaster_recovery
        importlib.reload(disaster_recovery)
        self.dr = disaster_recovery

    def tearDown(self) -> None:  # type: ignore[override]
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import disaster_recovery
        importlib.reload(disaster_recovery)
        self._tmp.cleanup()


class RoundTripTests(_ShimBase):
    def test_encrypt_then_decrypt(self):
        master = b"\x42" * 32
        path = self.dr.encrypt(master, "correct horse battery staple")
        self.assertTrue(path.is_file())
        # Permissions 0o600.
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # has_backup() returns True after encrypt.
        self.assertTrue(self.dr.has_backup())
        # Round-trip recovers the master key.
        out = self.dr.decrypt("correct horse battery staple")
        self.assertEqual(out, master)

    def test_status_reflects_state(self):
        s_before = self.dr.status()
        self.assertFalse(s_before["present"])
        self.dr.encrypt(b"\x00" * 32, "p")
        s_after = self.dr.status()
        self.assertTrue(s_after["present"])
        self.assertGreater(s_after["size_bytes"], 0)


class FailureModeTests(_ShimBase):
    def test_decrypt_wrong_passphrase_raises(self):
        self.dr.encrypt(b"\x00" * 32, "real")
        with self.assertRaises(self.dr.DisasterRecoveryError):
            self.dr.decrypt("wrong")

    def test_decrypt_missing_file_raises_missing(self):
        with self.assertRaises(self.dr.DisasterRecoveryMissingError):
            self.dr.decrypt("p")

    def test_encrypt_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            self.dr.encrypt(b"\x00" * 16, "p")

    def test_encrypt_rejects_empty_passphrase(self):
        with self.assertRaises(ValueError):
            self.dr.encrypt(b"\x00" * 32, "")

    def test_disable_removes_file(self):
        self.dr.encrypt(b"\x00" * 32, "p")
        self.assertTrue(self.dr.has_backup())
        self.dr.disable()
        self.assertFalse(self.dr.has_backup())
        self.dr.disable()  # idempotent — must not raise


if __name__ == "__main__":
    unittest.main()
