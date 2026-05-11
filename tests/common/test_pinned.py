"""Unit tests for ``local_scribe.common.pinned``.

The interesting paths are:

* Unverified read round-trips against the canonical in-tree
  ``pinned.json`` (sanity-checks the schema + the dataclass parsing).
* Strict read against a signed temp copy round-trips.
* Strict read against a tampered file raises ``SignatureMismatchError``.
* The ``LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG`` escape hatch downgrades
  to unverified read with an ERROR log.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_scribe.common import pinned
from local_scribe.security import signed_config as sc


KEY_A = bytes((0x10 + i) & 0xFF for i in range(32))


class CanonicalFileSanityTests(unittest.TestCase):
    """The in-tree pinned.json should parse + match the values we
    moved out of run.sh + char_integrity.py. If anyone bumps the file,
    this test confirms the JSON schema + the dataclass adapter still
    line up."""

    def test_in_tree_pinned_parses(self):
        p = pinned.load_pinned_unverified()
        self.assertEqual(p.version, 1)
        self.assertEqual(p.char.known_good_version, "1.0.24")
        self.assertEqual(p.char.pinned_team_id, "6SLY7V277V")
        self.assertEqual(p.char.pinned_bundle_id, "com.hyprnote.stable")
        self.assertEqual(p.char.default_app_path, "/Applications/Char.app")
        self.assertEqual(p.lmstudio.known_good_version, "0.4.12")
        self.assertEqual(p.lmstudio.app_path, "/Applications/LM Studio.app")
        self.assertEqual(p.lmstudio.default_port, 1234)
        # SHA256s are 64-hex.
        self.assertEqual(len(p.char.dmg_sha256_aarch64), 64)
        self.assertEqual(len(p.char.dmg_sha256_x86_64), 64)
        int(p.char.dmg_sha256_aarch64, 16)
        int(p.char.dmg_sha256_x86_64, 16)
        self.assertFalse(p.signed)
        self.assertIsNone(p.fp_hex)

    def test_release_tag_matches_known_good(self):
        p = pinned.load_pinned_unverified()
        self.assertEqual(p.char.release_tag, f"desktop_v{p.char.known_good_version}")
        self.assertIn(p.char.release_tag, p.char.release_base_url)

    def test_sugar_accessors(self):
        p = pinned.load_pinned_unverified()
        self.assertEqual(
            pinned.char_known_good_version(p), p.char.known_good_version,
        )
        self.assertEqual(
            pinned.char_dmg_sha256(p, "aarch64"), p.char.dmg_sha256_aarch64,
        )
        self.assertEqual(
            pinned.char_dmg_sha256(p, "x86_64"), p.char.dmg_sha256_x86_64,
        )
        with self.assertRaises(ValueError):
            pinned.char_dmg_sha256(p, "powerpc")


class StrictLoadTests(unittest.TestCase):
    """Cover the strict path with a tempfile fixture so we can sign
    + tamper freely without touching the in-tree canonical file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fixture = self.tmp / "pinned.json"
        self.fixture.write_text(json.dumps({
            "version": 1,
            "char": {
                "known_good_version": "9.9.9",
                "release_tag": "desktop_v9.9.9",
                "release_base_url": "https://example/",
                "dmg_sha256_aarch64": "a" * 64,
                "dmg_sha256_x86_64": "b" * 64,
                "pinned_team_id": "TEST123",
                "pinned_bundle_id": "com.example.test",
                "default_app_path": "/Applications/Example.app",
            },
            "lmstudio": {
                "known_good_version": "0.0.1",
                "app_path": "/Applications/Example LM.app",
                "default_port": 9999,
            },
        }))
        self.env_patches = mock.patch.dict(os.environ, {
            pinned.PINNED_PATH_ENV: str(self.fixture),
            pinned.PINNED_SIG_PATH_ENV: str(self.fixture) + ".sig",
        })
        self.env_patches.start()
        # Reset the per-process "unverified read" warn flag so each
        # test starts clean.
        pinned._unverified_warned.flag = False

    def tearDown(self):
        self.env_patches.stop()
        self._tmp.cleanup()

    def test_signed_round_trip(self):
        sc.sign_file(self.fixture, KEY_A)
        p = pinned.load_pinned(KEY_A)
        self.assertEqual(p.char.known_good_version, "9.9.9")
        self.assertTrue(p.signed)
        self.assertEqual(p.fp_hex, sc.fingerprint(KEY_A))

    def test_missing_signature_raises(self):
        with self.assertRaises(sc.SignatureMissingError):
            pinned.load_pinned(KEY_A)

    def test_tampered_file_raises_mismatch(self):
        sc.sign_file(self.fixture, KEY_A)
        d = json.loads(self.fixture.read_text())
        d["char"]["dmg_sha256_aarch64"] = "f" * 64
        self.fixture.write_text(json.dumps(d))
        with self.assertRaises(sc.SignatureMismatchError):
            pinned.load_pinned(KEY_A)

    def test_wrong_key_raises_fingerprint_mismatch(self):
        sc.sign_file(self.fixture, KEY_A)
        other = bytes((0x90 + i) & 0xFF for i in range(32))
        with self.assertRaises(sc.KeyFingerprintMismatchError):
            pinned.load_pinned(other)

    def test_allow_unsigned_env_downgrades_with_error_log(self):
        sc.sign_file(self.fixture, KEY_A)
        # Tamper *after* signing so strict mode WOULD fail.
        self.fixture.write_text(self.fixture.read_text() + " \n")
        with mock.patch.dict(os.environ, {pinned.ALLOW_UNSIGNED_ENV: "1"}):
            with self.assertLogs(pinned.logger, level=logging.ERROR) as cap:
                p = pinned.load_pinned(KEY_A)
        self.assertFalse(p.signed)
        # The Error log should mention the escape hatch by name so the
        # log trail is greppable.
        self.assertTrue(
            any(pinned.ALLOW_UNSIGNED_ENV in m for m in cap.output),
            f"expected env-var name in log output: {cap.output}",
        )

    def test_missing_file_raises_filenotfound(self):
        self.fixture.unlink()
        with self.assertRaises(FileNotFoundError):
            pinned.load_pinned_unverified()

    def test_unverified_warn_fires_once_per_process(self):
        with self.assertLogs(pinned.logger, level=logging.WARNING) as cap:
            pinned.load_pinned_unverified()
            pinned.load_pinned_unverified()
            pinned.load_pinned_unverified()
        # Exactly one WARNING regardless of how many calls.
        self.assertEqual(
            sum(1 for m in cap.output if "WARNING:" in m), 1,
            f"expected single WARNING, got {cap.output}",
        )


if __name__ == "__main__":
    unittest.main()
