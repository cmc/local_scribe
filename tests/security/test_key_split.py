"""Tests for key_split.py.

The split-key construction is the cryptographic core of Option C, so
the tests focus on the algebra (XOR identity, length checks,
round-trips) rather than on storage. No Touch ID / age / YubiKey
involvement here — those are exercised by
``test_key_lifecycle.py``.
"""

from __future__ import annotations

import unittest

from local_scribe.security.key_split import (
    KEY_BYTES,
    SplitKey,
    combine_halves,
    generate_split_key,
    split_existing_key,
    xor_bytes,
    zero_bytes,
)


class XorAlgebraTests(unittest.TestCase):
    def test_xor_is_self_inverse(self):
        a = b"\xa5" * KEY_BYTES
        b = bytes(range(KEY_BYTES))
        self.assertEqual(xor_bytes(xor_bytes(a, b), b), a)

    def test_xor_with_self_is_zero(self):
        a = b"\xa5" * KEY_BYTES
        self.assertEqual(xor_bytes(a, a), b"\x00" * KEY_BYTES)

    def test_xor_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            xor_bytes(b"\x00" * 16, b"\x00" * 32)


class GenerateSplitKeyTests(unittest.TestCase):
    def test_returns_split_key_namedtuple(self):
        sk = generate_split_key()
        self.assertIsInstance(sk, SplitKey)

    def test_master_key_is_32_bytes(self):
        sk = generate_split_key()
        self.assertEqual(len(sk.master_key), KEY_BYTES)
        self.assertEqual(len(sk.kc_half), KEY_BYTES)
        self.assertEqual(len(sk.yk_half), KEY_BYTES)

    def test_halves_xor_back_to_master(self):
        # The defining property: kc_half XOR yk_half == master_key.
        # If this ever fails, the system is irrecoverable; every test
        # below depends on it.
        for _ in range(64):
            sk = generate_split_key()
            self.assertEqual(xor_bytes(sk.kc_half, sk.yk_half), sk.master_key)

    def test_halves_look_random(self):
        # Two independent calls produce different halves (the OS CSPRNG
        # would have to collide on 32 bytes simultaneously for this to
        # flake — astronomical odds, fine for a regression guard).
        a = generate_split_key()
        b = generate_split_key()
        self.assertNotEqual(a.master_key, b.master_key)
        self.assertNotEqual(a.kc_half, b.kc_half)
        self.assertNotEqual(a.yk_half, b.yk_half)

    def test_single_half_reveals_nothing_about_master(self):
        # Information-theoretic guarantee: kc_half is sampled
        # independently from master_key, so they should be
        # uncorrelated. We can't prove "independent" from samples but
        # we *can* sanity-check that there's no obvious overlap.
        sk = generate_split_key()
        common = sum(1 for a, b in zip(sk.kc_half, sk.master_key) if a == b)
        # In 32 bytes of uniform-random the expected number of equal
        # bytes is 32/256 = 0.125. Half the bytes matching would be a
        # red flag.
        self.assertLess(common, 16)


class CombineHalvesTests(unittest.TestCase):
    def test_combine_round_trips_generated_pair(self):
        sk = generate_split_key()
        self.assertEqual(combine_halves(sk.kc_half, sk.yk_half), sk.master_key)

    def test_combine_is_commutative_via_self_xor(self):
        # Combine accepts the halves in either order because XOR is
        # commutative; we document the contract by testing both
        # orderings explicitly.
        sk = generate_split_key()
        self.assertEqual(
            combine_halves(sk.kc_half, sk.yk_half),
            combine_halves(sk.yk_half, sk.kc_half),
        )

    def test_combine_rejects_wrong_length_kc(self):
        sk = generate_split_key()
        with self.assertRaises(ValueError):
            combine_halves(b"\x00" * 16, sk.yk_half)

    def test_combine_rejects_wrong_length_yk(self):
        sk = generate_split_key()
        with self.assertRaises(ValueError):
            combine_halves(sk.kc_half, b"\x00" * 24)

    def test_combine_rejects_non_bytes(self):
        sk = generate_split_key()
        with self.assertRaises(ValueError):
            combine_halves("not-bytes", sk.yk_half)  # type: ignore[arg-type]


class SplitExistingKeyTests(unittest.TestCase):
    def test_round_trips_an_existing_key(self):
        # The migration path: take an existing whole key, split it,
        # later combine the two halves back into the same key.
        existing = b"\xab" * KEY_BYTES
        kc, yk = split_existing_key(existing)
        self.assertEqual(combine_halves(kc, yk), existing)

    def test_kc_half_is_independent_of_master(self):
        existing = b"\xab" * KEY_BYTES
        kc, _ = split_existing_key(existing)
        # kc_half is fresh randomness; it should not be equal to the
        # input under any sane construction.
        self.assertNotEqual(kc, existing)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            split_existing_key(b"\x00" * 16)


class ZeroBytesTests(unittest.TestCase):
    def test_overwrites_in_place(self):
        buf = bytearray(b"\xab" * 32)
        zero_bytes(buf)
        self.assertEqual(bytes(buf), b"\x00" * 32)

    def test_handles_empty_buffer(self):
        buf = bytearray()
        zero_bytes(buf)
        self.assertEqual(len(buf), 0)


if __name__ == "__main__":
    unittest.main()
