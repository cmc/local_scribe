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
      ``forget()`` zeroes the bytearray as a defence-in-depth gesture,
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
DEFAULT_HELPER_PATH = Path(__file__).resolve().parent / "bin" / "touchid-keychain"

# Env-var override exists primarily for tests, which point at a fake
# helper that returns canned hex without involving the real Keychain.
HELPER_ENV = "LOCAL_SCRIBE_TOUCHID_HELPER"


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
                allow_exits: tuple[int, ...] = (0,)
                ) -> subprocess.CompletedProcess:
    """Execute the helper, returning a CompletedProcess. Translates exit
    codes documented by the Swift CLI into the corresponding Python
    exceptions for ergonomic call sites."""
    binpath = helper_path()
    if not binpath.is_file() or not os.access(binpath, os.X_OK):
        raise HelperMissingError(
            f"Touch ID helper not found / not executable: {binpath}. "
            f"Run `./run.sh bootstrap` to compile it."
        )
    try:
        proc = subprocess.run(
            [str(binpath), *args],
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


def has_master_key() -> bool:
    """True if the master key is present in the Keychain. Does *not*
    trigger a Touch ID prompt (uses kSecUseAuthenticationUISkip
    internally) so it's safe to call from status-check code paths."""
    proc = _run_helper(["exists"], allow_exits=(0, 2))
    return proc.returncode == 0


def generate_master_key() -> bytes:
    """Generate a fresh 256-bit key from the OS CSPRNG."""
    return secrets.token_bytes(KEY_BYTES)


def store_master_key(key: bytes) -> None:
    """Persist ``key`` into the Keychain, replacing any existing item.

    This call does *not* prompt for Touch ID -- the access-control
    metadata is attached and only triggers on subsequent loads. This
    matters for bootstrap UX: the user gets exactly one Touch ID prompt
    (when the vault is first mounted), not two (store + mount).
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    _run_helper(["store"], stdin=bytes(key).hex())
    logger.info("stored master key in Keychain (service=local_scribe / "
                "account=master_key)")


def load_master_key(*, prompt: str = "Unlock local_scribe vault") -> bytes:
    """Fetch the master key, prompting Touch ID. ``prompt`` is the text
    shown in the LocalAuthentication sheet -- callers should phrase it
    in user-visible terms ("Unlock vault to start ASR server", etc.).

    Raises:
        SecretStoreError: key not stored (call ``store_master_key`` first).
        UserCancelledError: user dismissed the sheet or failed biometrics.
    """
    proc = _run_helper(["load", prompt])
    hex_data = (proc.stdout or "").strip()
    try:
        key = bytes.fromhex(hex_data)
    except ValueError as exc:
        raise SecretStoreError(f"helper returned non-hex data: {exc}") from exc
    if len(key) != KEY_BYTES:
        raise SecretStoreError(
            f"helper returned {len(key)} bytes, expected {KEY_BYTES}"
        )
    return key


def delete_master_key() -> None:
    """Remove the Keychain item entirely. Used by uninstall + tests."""
    _run_helper(["delete"])
    logger.info("deleted Keychain item (service=local_scribe / "
                "account=master_key)")


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
        """Read the key from the Keychain (prompts Touch ID)."""
        b = load_master_key(prompt=prompt)
        return cls(_buf=bytearray(b))

    @classmethod
    def generate_and_store(cls) -> "MasterKey":
        """Generate + persist a fresh key. Returns the in-memory handle
        so the immediate caller (bootstrap) can keep using it."""
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
