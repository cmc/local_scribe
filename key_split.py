"""Two-factor master key construction for local_scribe (Option C).

Architecture
------------

The 32-byte AES-256 master key is **never** stored as a single blob on
disk or in any single secure-storage item. Instead it is split via XOR
into two independent 32-byte halves::

    master_key = kc_half  XOR  yk_half

* ``kc_half`` lives in the macOS Keychain under
  ``service=local_scribe / account=master_key_kc_half_v2`` with an ACL
  of ``.userPresence`` (Touch ID, falling back to device passcode).
* ``yk_half`` lives on disk as an ``age``-encrypted file whose
  recipient is a YubiKey PIV slot's age recipient; touch policy on the
  slot is ``always`` so decryption requires a fresh physical tap.

**Both** halves are required to derive the master key. An adversary
that pwns the macOS Keychain (e.g. a TCC bypass + an unlocked
biometric session) has only random bytes; without a YubiKey tap they
still can't decrypt the vault. An adversary that steals the YubiKey
has the ciphertext but no Keychain access. Theft of the laptop alone
yields ciphertext at rest (the sparse-bundle vault) and one half of
the key (in the at-rest-encrypted Keychain) — the other half lives
only on a YubiKey the attacker doesn't have.

Why XOR (and not concatenation)
-------------------------------

XOR with two independent uniformly-random halves is the textbook
information-theoretic split-secret construction:

* Knowing one half tells you literally nothing about the master key
  (each half is uncorrelated with the master).
* Knowing both halves recovers the master key trivially.
* The construction is its own inverse: ``a XOR (a XOR m) == m``.

Concatenation (kc || yk) gives the same *total* security in the
brute-force sense (an attacker with one half still needs 2^128 work)
but leaks 128 bits of the AES key on partial compromise, which can
matter against future cryptanalytic improvements or side-channel
attacks. XOR has no such partial leakage.

Disaster recovery
-----------------

The split-key model means losing **either** half is fatal: a wiped
Keychain leaves the YubiKey-encrypted half useless, and a destroyed
YubiKey leaves the Keychain half useless. To avoid stranding the user,
``./run.sh key init`` *also* writes a separate passphrase-encrypted
copy of the **whole** master key (see ``disaster_recovery.py``). The
passphrase is shown once at enroll time; the user is expected to print
it / store it offline.

Module surface
--------------

This module is intentionally tiny and pure: it deals only in
``bytes``, has no I/O, and depends on nothing outside stdlib. The
storage-side wiring lives in ``secret_store.py`` (Keychain),
``yubikey_backup.py`` (YubiKey age wrapping), and
``key_lifecycle.py`` (the orchestrator).
"""

from __future__ import annotations

import secrets
from typing import NamedTuple


KEY_BYTES = 32  # AES-256 / HKDF input keying material size


class SplitKey(NamedTuple):
    """A freshly generated master key and its two halves.

    Wraps three 32-byte values together for the brief moment between
    "generated" and "split halves persisted; master forgotten".
    Callers should:

      1. Persist ``kc_half`` to the Keychain (``secret_store.store_kc_half``).
      2. Persist ``yk_half`` to the YubiKey-encrypted age file
         (``yubikey_backup.backup_yk_half``).
      3. Persist a passphrase-encrypted disaster-recovery copy of
         ``master_key`` (``disaster_recovery.encrypt``).
      4. Discard the in-memory ``master_key`` (the callers use the
         master key, then drop it; we do **not** keep a long-lived
         in-process copy).

    A failure between steps 1 and 3 is recoverable: re-run init, which
    is idempotent at the Keychain layer (replace-on-add).
    """
    master_key: bytes
    kc_half: bytes
    yk_half: bytes


def generate_split_key() -> SplitKey:
    """Generate a fresh master key + the two independent halves that
    reconstitute it.

    All three values come from ``secrets.token_bytes`` (OS CSPRNG),
    which is the same source used by ``secret_store.generate_master_key``
    and ``ssl.create_default_context``. We draw ``kc_half`` first as
    raw uniform random; ``yk_half`` is then derived as
    ``master XOR kc_half`` so the two halves recombine cleanly.

    Returns a :class:`SplitKey` carrying all three so the caller has a
    single object to thread through ``init`` without needing to keep
    them straight.
    """
    master = secrets.token_bytes(KEY_BYTES)
    kc = secrets.token_bytes(KEY_BYTES)
    yk = xor_bytes(master, kc)
    return SplitKey(master_key=master, kc_half=kc, yk_half=yk)


def combine_halves(kc_half: bytes, yk_half: bytes) -> bytes:
    """Reconstitute the master key from its two persisted halves.

    Raises:
        ValueError: if either half is the wrong length.

    Used by ``key_lifecycle.unlock()`` after both halves have been
    fetched (Touch ID + YubiKey tap). The returned ``bytes`` is the
    32-byte master key suitable for ``secret_store.MasterKey.from_bytes``
    and ``service_auth.derive_service_token``.
    """
    if not isinstance(kc_half, (bytes, bytearray)) or len(kc_half) != KEY_BYTES:
        raise ValueError(
            f"kc_half must be {KEY_BYTES} bytes, got "
            f"{len(kc_half) if isinstance(kc_half, (bytes, bytearray)) else '?'}"
        )
    if not isinstance(yk_half, (bytes, bytearray)) or len(yk_half) != KEY_BYTES:
        raise ValueError(
            f"yk_half must be {KEY_BYTES} bytes, got "
            f"{len(yk_half) if isinstance(yk_half, (bytes, bytearray)) else '?'}"
        )
    return xor_bytes(bytes(kc_half), bytes(yk_half))


def split_existing_key(master_key: bytes) -> tuple[bytes, bytes]:
    """Split an *existing* master key into two random halves.

    Used during ``./run.sh key migrate`` (legacy whole-key Keychain
    item → split-key) so the operation is data-preserving: we draw a
    fresh ``kc_half``, derive ``yk_half = master XOR kc_half``, write
    them to their respective stores, then delete the legacy item.

    Returns ``(kc_half, yk_half)``.
    """
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != KEY_BYTES:
        raise ValueError(f"master_key must be {KEY_BYTES} bytes")
    kc = secrets.token_bytes(KEY_BYTES)
    yk = xor_bytes(bytes(master_key), kc)
    return kc, yk


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """Constant-length byte-wise XOR. Both inputs must have the same
    length; we raise ``ValueError`` otherwise rather than truncate
    silently (truncation would be a subtle silent-corruption bug)."""
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    return bytes(x ^ y for x, y in zip(a, b))


def zero_bytes(b: bytearray) -> None:
    """Overwrite a mutable buffer with zeros. Best-effort: CPython's
    bytes interning + GC make this advisory, not a guarantee. Use on
    half buffers + transient master-key reconstructions so they don't
    linger in long-lived dicts / call-stack frames."""
    for i in range(len(b)):
        b[i] = 0


__all__ = [
    "KEY_BYTES",
    "SplitKey",
    "generate_split_key",
    "combine_halves",
    "split_existing_key",
    "xor_bytes",
    "zero_bytes",
]
