"""Touch-ID-gated storage of the local_scribe master encryption key.

The master key (32 random bytes, AES-256) is held in the macOS Keychain
under ``service=local_scribe / account=master_key`` with an access control
of ``.userPresence`` — i.e. Touch ID, falling back to the device passcode.
Once the user authenticates, the OS hands the bytes back to us; we keep
them in memory only and pass them to ``hdiutil`` to mount the encrypted
vault. Nothing else on disk needs to know the key.

Why a Swift helper instead of PyObjC:
    PyObjC pulls in ~50 MB of Objective-C bridge wheels just to get to
    ``SecItemCopyMatching``. Swift / ``swiftc`` already ship with Xcode CLT
    (a hard dependency of homebrew, which we use elsewhere) so a tiny
    ``touchid-keychain`` binary is the lighter path. The source lives at
    ``bin/touchid_keychain.swift`` and bootstrap compiles it once.

Why a binary instead of inlining via ``swift -e``:
    A scripted invocation re-parses + JIT-compiles every call (~1-3 s on a
    cold disk), which is unacceptable when ``./run.sh start`` calls us a
    handful of times in a row. Pre-compiled binary is ~5 ms.

Threat model:
    - Plaintext key never appears in argv (passed on stdin / returned on
      stdout, hex-encoded). ``ps`` listings only see ``touchid-keychain
      store|load|...``.
    - In-process the bytes live for as long as the calling Python process.
      ``forget()`` zeroes the bytearray as a defense-in-depth gesture,
      acknowledging that CPython's GC + interned strings make true
      "secure erase" impossible from Python.
    - The Keychain ACL is ``kSecAttrAccessibleWhenUnlockedThisDeviceOnly``:
      the item never syncs to iCloud and is unreadable when the Mac is
      locked.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger("local_scribe.secret_store")


KEY_BYTES = 32  # AES-256

# Default install location for the compiled helper binary. ``run.sh
# bootstrap`` compiles ``bin/touchid_keychain.swift`` into ``bin/touchid-
# keychain`` next to this module so the venv python finds it relative to
# the repo root.
def _repo_root() -> Path:
    """Walk up from this module's location to the repo root.

    Before the package refactor this module lived at repo root and
    ``Path(__file__).resolve().parent`` was the right anchor. After the
    refactor it lives at ``local_scribe/security/secret_store.py`` so
    the heuristic anchors on ``pyproject.toml`` (guaranteed at repo
    root) instead, with a fixed-walk fallback.
    """
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return here.parents[2]


DEFAULT_HELPER_PATH = _repo_root() / "bin" / "touchid-keychain"

# Env-var override exists primarily for tests, which point at a fake
# helper that returns canned hex without involving the real Keychain.
HELPER_ENV = "LOCAL_SCRIBE_TOUCHID_HELPER"

# Keychain account names. ``ACCOUNT_LEGACY_V1`` is the pre-split-key
# whole-key item; the migration path reads from it and then writes
# ``ACCOUNT_KC_HALF_V2`` before deleting the v1 item. New installs
# only ever see the v2 account.
ACCOUNT_LEGACY_V1 = "master_key"
ACCOUNT_KC_HALF_V2 = "master_key_kc_half_v2"


class SecretStoreError(Exception):
    """Raised when the underlying Keychain helper fails for any reason
    other than "item not found" (which is a normal control-flow case
    represented by ``has_master_key() == False``)."""


class UserCancelledError(SecretStoreError):
    """Raised when the user cancels the Touch ID prompt or fails biometric
    auth too many times. Callers should re-prompt rather than treat this
    as a hard failure -- the user might just have tapped the wrong
    fingerprint."""


class HelperMissingError(SecretStoreError):
    """The compiled helper binary couldn't be found. Run ``./run.sh
    bootstrap`` to compile it from ``bin/touchid_keychain.swift``."""


def helper_path() -> Path:
    """Path to the compiled Swift helper. Respects ``LOCAL_SCRIBE_TOUCHID_
    HELPER`` (for tests / out-of-tree installs)."""
    override = os.environ.get(HELPER_ENV)
    if override:
        return Path(override)
    return DEFAULT_HELPER_PATH


def _run_helper(args: list[str], *, stdin: Optional[str] = None,
                allow_exits: tuple[int, ...] = (0,),
                account: Optional[str] = None,
                ) -> subprocess.CompletedProcess:
    """Execute the helper, returning a CompletedProcess. Translates exit
    codes documented by the Swift CLI into the corresponding Python
    exceptions for ergonomic call sites.

    When ``account`` is given the helper is invoked with
    ``--account NAME`` *before* the subcommand so the same binary can
    manage multiple keychain items (currently the legacy whole-key item
    and the split-key ``kc_half``). The Swift CLI documents the flag
    and falls back to ``master_key`` when absent for backward
    compatibility with the pre-Option-C wire format.
    """
    binpath = helper_path()
    if not binpath.is_file() or not os.access(binpath, os.X_OK):
        raise HelperMissingError(
            f"Touch ID helper not found / not executable: {binpath}. "
            f"Run `./run.sh bootstrap` to compile it."
        )
    prefix = ["--account", account] if account else []
    try:
        proc = subprocess.run(
            [str(binpath), *prefix, *args],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise HelperMissingError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        # 120 s is generous: even the Touch ID sheet has its own ~30 s
        # timeout. Hitting our limit means something locked up; surface
        # it explicitly rather than hanging the parent.
        raise SecretStoreError(f"Touch ID helper timed out: {exc}") from exc

    if proc.returncode in allow_exits:
        return proc

    # Map the documented exit codes back to typed exceptions.
    msg = (proc.stderr or "").strip() or f"helper exit {proc.returncode}"
    if proc.returncode == 2:
        raise SecretStoreError(f"key not stored ({msg})")
    if proc.returncode == 3:
        raise UserCancelledError(f"Touch ID cancelled ({msg})")
    raise SecretStoreError(msg)


def _has_item(*, account: Optional[str] = None) -> bool:
    proc = _run_helper(["exists"], allow_exits=(0, 2), account=account)
    return proc.returncode == 0


def _store_item(data: bytes, *, account: Optional[str] = None) -> None:
    if not isinstance(data, (bytes, bytearray)) or len(data) != KEY_BYTES:
        raise ValueError(f"data must be {KEY_BYTES} bytes, got {len(data)}")
    _run_helper(["store"], stdin=bytes(data).hex(), account=account)


def _load_item(*, prompt: str, account: Optional[str] = None) -> bytes:
    proc = _run_helper(["load", prompt], account=account)
    hex_data = (proc.stdout or "").strip()
    try:
        data = bytes.fromhex(hex_data)
    except ValueError as exc:
        raise SecretStoreError(f"helper returned non-hex data: {exc}") from exc
    if len(data) != KEY_BYTES:
        raise SecretStoreError(
            f"helper returned {len(data)} bytes, expected {KEY_BYTES}"
        )
    return data


def _delete_item(*, account: Optional[str] = None) -> None:
    _run_helper(["delete"], account=account)


# ---- legacy whole-key API (v1) — kept for migration + tests ---------


def has_master_key() -> bool:
    """True if the legacy v1 *whole-key* item is present in the
    Keychain. Used only by the migration path
    (``key_lifecycle.migrate_v1_to_v2``); new code should prefer
    :func:`has_kc_half`.

    Does *not* trigger a Touch ID prompt (uses
    ``kSecUseAuthenticationUISkip`` internally) so it's safe to call
    from status-check code paths.
    """
    return _has_item(account=ACCOUNT_LEGACY_V1)


def generate_master_key() -> bytes:
    """Generate a fresh 256-bit key from the OS CSPRNG."""
    return secrets.token_bytes(KEY_BYTES)


def store_master_key(key: bytes) -> None:
    """Persist a legacy v1 whole-key item. Used by migration tests and
    the v1 backward-compat path. New installs use the split-key flow
    in :mod:`key_lifecycle` and never call this.

    This call does *not* prompt for Touch ID -- the access-control
    metadata is attached and only triggers on subsequent loads.
    """
    _store_item(key, account=ACCOUNT_LEGACY_V1)
    logger.info("stored master key in Keychain (service=local_scribe / "
                "account=%s)", ACCOUNT_LEGACY_V1)


def load_master_key(*, prompt: str = "Unlock local_scribe vault") -> bytes:
    """Fetch the legacy v1 whole-key item, prompting Touch ID. Used
    only by the migration path. New code calls :func:`load_kc_half`
    and reconstitutes the master key via ``key_split.combine_halves``.

    Raises:
        SecretStoreError: key not stored.
        UserCancelledError: user dismissed the sheet or failed biometrics.
    """
    return _load_item(prompt=prompt, account=ACCOUNT_LEGACY_V1)


def delete_master_key() -> None:
    """Remove the legacy v1 Keychain item entirely. Used by uninstall +
    tests + the second step of the migration path."""
    _delete_item(account=ACCOUNT_LEGACY_V1)
    logger.info("deleted Keychain item (service=local_scribe / "
                "account=%s)", ACCOUNT_LEGACY_V1)


# ---- split-key kc_half API (v2) — primary path on new installs ------


def has_kc_half() -> bool:
    """True if the split-key ``kc_half`` item is present in the
    Keychain (no Touch ID prompt). This is the canonical
    "is local_scribe set up?" check on a v2 install."""
    return _has_item(account=ACCOUNT_KC_HALF_V2)


def store_kc_half(half: bytes) -> None:
    """Persist the 32-byte ``kc_half`` to the Keychain (no Touch ID
    prompt; the ACL only triggers on read). The half is one of two
    XOR factors required to reconstitute the master key — see
    :mod:`key_split`."""
    _store_item(half, account=ACCOUNT_KC_HALF_V2)
    logger.info("stored kc_half in Keychain (service=local_scribe / "
                "account=%s)", ACCOUNT_KC_HALF_V2)


def load_kc_half(*, prompt: str = "Unlock local_scribe vault") -> bytes:
    """Fetch the 32-byte ``kc_half`` from the Keychain, prompting
    Touch ID. The caller XORs this with ``yk_half`` (decrypted via
    YubiKey) to obtain the master key."""
    return _load_item(prompt=prompt, account=ACCOUNT_KC_HALF_V2)


def delete_kc_half() -> None:
    """Remove the kc_half item. Used by ``./run.sh key destroy`` and
    rotation tests."""
    _delete_item(account=ACCOUNT_KC_HALF_V2)
    logger.info("deleted Keychain item (service=local_scribe / "
                "account=%s)", ACCOUNT_KC_HALF_V2)


# ----------------------------------------------------------------------
# In-process key handle
#
# ``MasterKey`` is a tiny dataclass-like wrapper that lets the rest of
# the codebase pass keys around without spreading ``bytes`` literals,
# and offers a ``forget()`` method that overwrites the buffer. The
# overwrite is best-effort -- Python doesn't guarantee no aliases exist
# elsewhere -- but it's enough to keep the bytes out of long-lived
# objects like the FastAPI app state.


@dataclass
class MasterKey:
    """Wraps a 32-byte master key.

    Always construct via ``MasterKey.unlock()`` or
    ``MasterKey.generate_and_store()`` -- never instantiate directly with
    raw bytes outside of tests, since the validation lives in the
    factory methods.
    """
    _buf: bytearray

    @classmethod
    def unlock(cls, *, prompt: str = "Unlock local_scribe vault") -> "MasterKey":
        """Read the *legacy v1 whole-key* item from the Keychain (prompts
        Touch ID).

        New code should prefer :func:`key_lifecycle.unlock_master_key`,
        which implements the split-key Option C flow (Touch ID + YubiKey
        tap). This method is retained for the migration path and for
        tests that drive only the Keychain layer; calling it on a v2
        install raises ``SecretStoreError("key not stored")``.
        """
        b = load_master_key(prompt=prompt)
        return cls(_buf=bytearray(b))

    @classmethod
    def generate_and_store(cls) -> "MasterKey":
        """Generate + persist a fresh *legacy v1 whole-key* item.
        Returns the in-memory handle so the immediate caller (tests,
        migration) can keep using it.

        New installs go through ``key_lifecycle.init_master_key``,
        which splits the key into ``kc_half`` (Keychain) +
        ``yk_half`` (YubiKey-wrapped) and never stores the whole key.
        """
        key = generate_master_key()
        store_master_key(key)
        return cls(_buf=bytearray(key))

    @classmethod
    def from_bytes(cls, b: bytes) -> "MasterKey":
        """Test/restore-only constructor: build a handle from raw bytes
        (e.g. after YubiKey-decrypting the on-disk backup)."""
        if not isinstance(b, (bytes, bytearray)) or len(b) != KEY_BYTES:
            raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(b)}")
        return cls(_buf=bytearray(b))

    def as_bytes(self) -> bytes:
        """Return a *copy* of the key bytes. We hand out copies so a
        caller mutating the result doesn't accidentally zero our buffer
        out from under another caller."""
        return bytes(self._buf)

    def as_hex(self) -> str:
        return self._buf.hex()

    def forget(self) -> None:
        """Zero the in-memory buffer. Best-effort; see module docstring."""
        for i in range(len(self._buf)):
            self._buf[i] = 0

    # Make repr safe: do not print the key bytes.
    def __repr__(self) -> str:
        return f"<MasterKey {len(self._buf)} bytes>"
