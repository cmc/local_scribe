"""Unit tests for ``local_scribe.security.signed_config``.

Covers the four documented failure modes plus the round-trip happy
path. No Touch ID / YubiKey involvement — we pass synthetic 32-byte
keys directly to the sign / verify entry points.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from local_scribe.security import signed_config as sc


def _key(seed: int) -> bytes:
    """Deterministic synthetic 32-byte key for repeatable assertions."""
    return bytes((seed + i) & 0xFF for i in range(32))


KEY_A = _key(0x10)
KEY_B = _key(0x20)


class DerivationTests(unittest.TestCase):
    """The HKDF-derived subkey + fingerprint should be deterministic
    per-key, distinct across keys, and never expose key bits."""

    def test_subkey_is_deterministic(self):
        self.assertEqual(sc._signing_subkey(KEY_A), sc._signing_subkey(KEY_A))

    def test_subkey_differs_per_master(self):
        self.assertNotEqual(sc._signing_subkey(KEY_A), sc._signing_subkey(KEY_B))

    def test_subkey_size(self):
        self.assertEqual(len(sc._signing_subkey(KEY_A)), 32)

    def test_subkey_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            sc._signing_subkey(b"too short")

    def test_fingerprint_is_six_lowercase_hex(self):
        fp = sc.fingerprint(KEY_A)
        self.assertEqual(len(fp), 6)
        self.assertEqual(fp, fp.lower())
        int(fp, 16)

    def test_fingerprint_differs_per_master(self):
        self.assertNotEqual(sc.fingerprint(KEY_A), sc.fingerprint(KEY_B))

    def test_fingerprint_does_not_expose_subkey(self):
        # First 6 hex of the subkey shouldn't match the fingerprint —
        # confirms the HKDF domain separation is working.
        self.assertNotEqual(
            sc.fingerprint(KEY_A),
            sc._signing_subkey(KEY_A).hex()[:6],
        )


class ParseRenderTests(unittest.TestCase):

    def test_round_trip(self):
        sig = sc.Signature(
            format_header=sc.SIG_FORMAT_HEADER,
            fp_hex="abcdef",
            alg=sc.SIG_ALG,
            hmac_hex="0" * 64,
        )
        text = sc.render_signature(sig)
        self.assertEqual(sc.parse_signature(text), sig)

    def test_rejects_wrong_header(self):
        text = "not-a-sig fp=abcdef alg=hmac-sha256\n" + "0" * 64 + "\n"
        with self.assertRaises(sc.SignatureMalformedError):
            sc.parse_signature(text)

    def test_rejects_wrong_alg(self):
        text = (
            f"{sc.SIG_FORMAT_HEADER} fp=abcdef alg=blake3\n"
            + "0" * 64 + "\n"
        )
        with self.assertRaises(sc.SignatureMalformedError):
            sc.parse_signature(text)

    def test_rejects_missing_fp(self):
        text = f"{sc.SIG_FORMAT_HEADER} alg=hmac-sha256\n" + "0" * 64 + "\n"
        with self.assertRaises(sc.SignatureMalformedError):
            sc.parse_signature(text)

    def test_rejects_short_hmac(self):
        text = (
            f"{sc.SIG_FORMAT_HEADER} fp=abcdef alg=hmac-sha256\n"
            + "0" * 32 + "\n"
        )
        with self.assertRaises(sc.SignatureMalformedError):
            sc.parse_signature(text)

    def test_rejects_non_hex_hmac(self):
        text = (
            f"{sc.SIG_FORMAT_HEADER} fp=abcdef alg=hmac-sha256\n"
            + "z" * 64 + "\n"
        )
        with self.assertRaises(sc.SignatureMalformedError):
            sc.parse_signature(text)


class SignVerifyRoundTripTests(unittest.TestCase):
    """Happy path + the four documented failure modes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.protected = self.tmp / "pinned.json"
        self.protected.write_bytes(b'{"char": {"v": "1.0.24"}}\n')

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        out = sc.sign_file(self.protected, KEY_A)
        self.assertTrue(out.exists())
        sig = sc.verify_file(self.protected, KEY_A)
        self.assertEqual(sig.fp_hex, sc.fingerprint(KEY_A))
        self.assertEqual(sig.alg, sc.SIG_ALG)

    def test_sidecar_perms_user_rw_only(self):
        out = sc.sign_file(self.protected, KEY_A)
        mode = out.stat().st_mode & 0o777
        # 0o600 is the strict target; some filesystems normalise the
        # x bit so accept anything with no group/other read or write.
        self.assertEqual(mode & 0o077, 0,
                         f"unexpected group/other bits in {oct(mode)}")

    def test_sidecar_custom_path(self):
        custom = self.tmp / "elsewhere.sig"
        out = sc.sign_file(self.protected, KEY_A, sig_path=custom)
        self.assertEqual(out, custom)
        # Default location should NOT exist.
        self.assertFalse((self.tmp / "pinned.json.sig").exists())
        sc.verify_file(self.protected, KEY_A, sig_path=custom)

    def test_missing_sidecar_raises(self):
        with self.assertRaises(sc.SignatureMissingError):
            sc.verify_file(self.protected, KEY_A)

    def test_tampered_file_raises_mismatch(self):
        sc.sign_file(self.protected, KEY_A)
        self.protected.write_bytes(b'{"char": {"v": "0.0.1-MALICIOUS"}}\n')
        with self.assertRaises(sc.SignatureMismatchError):
            sc.verify_file(self.protected, KEY_A)

    def test_wrong_key_raises_fingerprint_mismatch(self):
        sc.sign_file(self.protected, KEY_A)
        with self.assertRaises(sc.KeyFingerprintMismatchError):
            sc.verify_file(self.protected, KEY_B)

    def test_tampered_sidecar_raises_mismatch(self):
        sp = sc.sign_file(self.protected, KEY_A)
        text = sp.read_text().splitlines()
        # Flip the last hex digit of the HMAC.
        last_byte = text[1][-1]
        new_last = "0" if last_byte != "0" else "1"
        text[1] = text[1][:-1] + new_last
        sp.write_text("\n".join(text) + "\n")
        with self.assertRaises(sc.SignatureMismatchError):
            sc.verify_file(self.protected, KEY_A)

    def test_tampered_sidecar_header_raises_malformed(self):
        sp = sc.sign_file(self.protected, KEY_A)
        sp.write_text("garbage\nnot-a-hash\n")
        with self.assertRaises(sc.SignatureMalformedError):
            sc.verify_file(self.protected, KEY_A)

    def test_protected_file_missing_raises_filenotfound(self):
        sc.sign_file(self.protected, KEY_A)
        self.protected.unlink()
        with self.assertRaises(FileNotFoundError):
            sc.verify_file(self.protected, KEY_A)


class StatusTests(unittest.TestCase):
    """``status()`` is a read-only snapshot the doctor / inspector
    can call without a master key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.protected = self.tmp / "pinned.json"
        self.protected.write_bytes(b"{}\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_status_no_sidecar(self):
        st = sc.status(self.protected)
        self.assertTrue(st.protected_present)
        self.assertFalse(st.sig_present)
        self.assertFalse(st.sig_parseable)
        self.assertIsNone(st.sig_fp)
        self.assertIsNotNone(st.note)
        self.assertIn("missing", st.note.lower())

    def test_status_with_sidecar(self):
        sc.sign_file(self.protected, KEY_A)
        st = sc.status(self.protected)
        self.assertTrue(st.sig_present)
        self.assertTrue(st.sig_parseable)
        self.assertEqual(st.sig_fp, sc.fingerprint(KEY_A))
        self.assertEqual(st.sig_alg, sc.SIG_ALG)

    def test_status_with_malformed_sidecar(self):
        sp = sc.default_sig_path(self.protected)
        sp.write_text("garbage\n")
        st = sc.status(self.protected)
        self.assertTrue(st.sig_present)
        self.assertFalse(st.sig_parseable)
        self.assertIn("malformed", st.note.lower())


if __name__ == "__main__":
    unittest.main()
