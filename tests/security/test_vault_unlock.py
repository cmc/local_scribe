"""Unit tests for ``vault_unlock``.

We don't exercise the live hdiutil path here -- that needs real macOS
and real disk -- but we DO pin down two invariants that the rest of
the stack relies on:

1.  ``derive_password(master)`` is deterministic and reproducible
    across processes, so unlocking with the same master always yields
    the same hdiutil passphrase. Without this, a code reload would
    lock the operator out of their vault.

2.  The HKDF construction is **versioned** (v1 today, v2 tomorrow if
    we ever change it). Unknown versions raise rather than silently
    fall back, so a stale caller can't accidentally generate the wrong
    passphrase.

3.  The hdiutil passphrase is *fully ASCII* (hex-encoded), 128 chars
    long. hdiutil's stdin tolerates raw bytes but mailing-list lore
    is full of "is this UTF-8 or latin-1?" foot-guns; we just lock
    down to hex.
"""

from __future__ import annotations

import os
import sys
import unittest

# Ensure repo root is importable when run via `python -m pytest tests/`.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # tests/security/ -> tests/ -> repo
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from local_scribe.security import vault_unlock  # noqa: E402


class DerivePasswordTests(unittest.TestCase):
    def setUp(self) -> None:
        # Two distinct 32-byte master keys to spot collisions.
        self.master_a = b"A" * 32
        self.master_b = b"B" * 32

    def test_length_is_128_hex_chars(self) -> None:
        pw = vault_unlock.derive_password(self.master_a)
        # 64 bytes of HKDF output, hex-encoded -> 128 chars.
        self.assertEqual(len(pw), 128)

    def test_is_ascii_hex(self) -> None:
        pw = vault_unlock.derive_password(self.master_a)
        self.assertTrue(all(c in b"0123456789abcdef" for c in pw),
                        f"non-hex byte in passphrase: {pw!r}")

    def test_deterministic_same_input(self) -> None:
        # Same master -> same passphrase. This is the invariant that
        # makes the vault unlockable on subsequent boots.
        a1 = vault_unlock.derive_password(self.master_a)
        a2 = vault_unlock.derive_password(self.master_a)
        self.assertEqual(a1, a2)

    def test_different_masters_different_passphrases(self) -> None:
        a = vault_unlock.derive_password(self.master_a)
        b = vault_unlock.derive_password(self.master_b)
        self.assertNotEqual(a, b)

    def test_rejects_short_master(self) -> None:
        with self.assertRaises(ValueError):
            vault_unlock.derive_password(b"\x00" * 31)

    def test_rejects_long_master(self) -> None:
        with self.assertRaises(ValueError):
            vault_unlock.derive_password(b"\x00" * 33)

    def test_rejects_str_master(self) -> None:
        # Strict typing: we never want anyone passing a str. The vault
        # passphrase is bytes-in, bytes-out.
        with self.assertRaises((ValueError, TypeError)):
            vault_unlock.derive_password("A" * 32)  # type: ignore[arg-type]

    def test_unknown_version_raises(self) -> None:
        with self.assertRaises(ValueError):
            vault_unlock.derive_password(self.master_a, version=2)

    def test_independent_from_service_tokens(self) -> None:
        # Sanity: the vault passphrase MUST NOT collide with any
        # service bearer token derived from the same master. We use
        # different HKDF salts + info labels for exactly this reason;
        # this test pins down that those choices haven't drifted into
        # accidental equivalence.
        from local_scribe.security import service_auth
        vp = vault_unlock.derive_password(self.master_a)
        asr = service_auth.derive_service_token(self.master_a, "asr").encode()
        self.assertNotEqual(vp, asr)


class StatusTests(unittest.TestCase):
    def test_status_runs_without_master_key(self) -> None:
        # Should not raise even when no key is enrolled. It's the
        # operator-facing status command; we surface "everything is
        # absent" rather than crashing.
        s = vault_unlock.status()
        self.assertIn("master_key_present", s)
        self.assertIn("yubikey_enrolled", s)
        self.assertIn("yk_half_present", s)
        self.assertIn("mounted", s)
        self.assertIn("exists", s)


if __name__ == "__main__":
    unittest.main()
