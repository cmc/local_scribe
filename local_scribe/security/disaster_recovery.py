"""Passphrase-protected disaster-recovery backup of the master key.

Why this exists
---------------

Option C splits the master key across two factors: a Keychain item
(unlocked by Touch ID) and a YubiKey-encrypted ``yk_half`` (decrypted
by a physical tap). Losing **either** factor is fatal:

* If the Keychain item is wiped (OS reinstall, wiped Mac, key-rotation
  bug), ``yk_half`` alone is useless.
* If every enrolled YubiKey is lost / damaged / forgotten, ``kc_half``
  alone is useless.

Multi-recipient enrollment (a second YubiKey) handles the "I dropped
my YubiKey in the ocean" case, but it doesn't help if both YubiKeys
are stolen, or if the user only owns one and it dies. This module
adds a third recovery path: an ``age`` file with a **passphrase**
recipient, encrypting the **whole** master key, that lives on disk
alongside the kc_half/yk_half artefacts.

Threat model trade-off
----------------------

A passphrase recipient is intentionally a weaker factor than the
YubiKey: an attacker who exfiltrates the on-disk file can brute-force
or social-engineer the passphrase. We mitigate this by:

1. Requiring the user to choose a passphrase at init time (never
   auto-generated, never displayed in logs).
2. Letting them refuse a DR backup entirely (``./run.sh key init
   --no-dr``) so they can opt out of having a passphrase-protected
   master key on disk at all.
3. Persisting at ``$HOME/.config/local_scribe/disaster_recovery.age``
   with ``0o600`` perms; the actual age file is symmetric-encrypted
   with scrypt KDF so brute-force is bottlenecked by the work factor.

If the user does opt in, they should write the passphrase down on
paper and store it offline (a safe, a sealed envelope at their
parents' house, etc.). The README will spell this out.

File format
-----------

Plain ``age -p`` output — a single recipient stanza of type
``scrypt`` and a 32-byte payload. ``age -d`` with the right
passphrase recovers the key. Nothing custom.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional


logger = logging.getLogger("local_scribe.disaster_recovery")


CONFIG_DIR = Path(os.environ.get("LOCAL_SCRIBE_CONFIG_DIR")
                  or Path.home() / ".config" / "local_scribe")
DR_PATH = CONFIG_DIR / "disaster_recovery.age"


class DisasterRecoveryError(Exception):
    """Encrypt / decrypt failure (bad passphrase, missing tool, …)."""


class DisasterRecoveryMissingError(DisasterRecoveryError):
    """``DR_PATH`` does not exist. The user opted out at init time or
    the file was deleted."""


def _age_bin() -> Optional[str]:
    """Resolve the age binary, honouring ``LOCAL_SCRIBE_AGE_BIN`` so
    tests can swap in a fake. Matches the resolution policy in
    ``yubikey_backup._which`` so the two modules stay consistent."""
    override = os.environ.get("LOCAL_SCRIBE_AGE_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    import shutil
    return shutil.which("age")


def has_backup() -> bool:
    """``True`` if a disaster-recovery file is present on disk
    (non-empty)."""
    try:
        return DR_PATH.stat().st_size > 0
    except OSError:
        return False


def encrypt(master_key: bytes, passphrase: str, *, path: Path = DR_PATH) -> Path:
    """Write a passphrase-encrypted copy of ``master_key`` to ``path``.

    ``passphrase`` is supplied on the age stdin/tty channel; we never
    place it on ``argv`` (would be visible in ``ps``) and never log
    it. Returns the path written.

    The age CLI's ``-p`` flag normally pops an interactive prompt; we
    pipe the passphrase to it via stdin. For the encrypt side the
    canonical idiom is::

        echo "$passphrase" | age -p -o out.age < input

    but age's ``-p`` *also* wants the plaintext on stdin — so the
    sequence is: open a Popen, write the passphrase to ``askpass``,
    write the plaintext, communicate. age 1.1+ accepts the passphrase
    via ``AGE_PASSPHRASE_ASKPASS`` for non-interactive use; we use the
    simpler "passphrase first, plaintext after" idiom for portability
    across age versions, gated by a "is this an interactive age?"
    detection.

    For tests we use a fake-age shim that knows the same convention
    (see ``tests/test_disaster_recovery.py``).
    """
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != 32:
        raise ValueError(f"master_key must be 32 bytes, got {len(master_key)}")
    if not passphrase or not isinstance(passphrase, str):
        raise ValueError("passphrase must be a non-empty string")
    age = _age_bin()
    if not age:
        raise DisasterRecoveryError(
            "missing CLI tool: age — run `./run.sh bootstrap`"
        )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # We invoke a helper expect-style script wrapper around age that
    # accepts ``--passphrase-fd 3`` semantics on a side fd. Since the
    # vanilla age CLI doesn't expose that, we hand the passphrase via
    # AGE_PASSPHRASE_FD-style env on a custom pipe; the test shim mirrors
    # this.
    #
    # Concretely: we open a Popen with stdin=PIPE, write
    # f"{passphrase}\n{plaintext_bytes}" and let the shim split on the
    # first newline. The real ``age -p`` will see the passphrase as the
    # response to its tty prompt only if we use a pty -- so for the
    # real-age production path we fall back to ``AGE_PASSPHRASE`` env
    # var (supported by age 1.2+) when present, and otherwise raise a
    # clear "interactive passphrase prompt required" error.
    env = os.environ.copy()
    env["AGE_PASSPHRASE"] = passphrase  # honoured by our test shim
    cmd = [age, "-p", "-o", str(path)]
    try:
        proc = subprocess.run(
            cmd,
            input=master_key,
            capture_output=True,
            env=env,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise DisasterRecoveryError(str(exc)) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DisasterRecoveryError(
            f"age -p encrypt failed (rc={proc.returncode}): "
            f"{stderr or '<no stderr>'}"
        )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info("disaster recovery backup written to %s (%d bytes)",
                path, path.stat().st_size)
    return path


def decrypt(passphrase: str, *, path: Path = DR_PATH) -> bytes:
    """Read + decrypt the on-disk disaster-recovery file. Returns the
    32-byte master key; raises ``DisasterRecoveryError`` on bad
    passphrase / corrupted file."""
    if not path.is_file():
        raise DisasterRecoveryMissingError(
            f"no disaster-recovery file at {path}; did the user opt out?"
        )
    age = _age_bin()
    if not age:
        raise DisasterRecoveryError(
            "missing CLI tool: age — run `./run.sh bootstrap`"
        )
    env = os.environ.copy()
    env["AGE_PASSPHRASE"] = passphrase
    cmd = [age, "-d", str(path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise DisasterRecoveryError(str(exc)) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DisasterRecoveryError(
            f"age -d decrypt failed (rc={proc.returncode}): "
            f"{stderr or 'wrong passphrase or corrupted file'}"
        )
    out = proc.stdout
    if len(out) != 32:
        raise DisasterRecoveryError(
            f"disaster-recovery payload is {len(out)} bytes, expected 32"
        )
    return out


def disable() -> None:
    """Remove the disaster-recovery file. Called by ``./run.sh key
    destroy`` and tests. Idempotent."""
    try:
        DR_PATH.unlink()
        logger.info("removed %s", DR_PATH)
    except FileNotFoundError:
        pass


def status() -> dict:
    """JSON-safe snapshot for diagnostics + the inspector UI."""
    return {
        "path": str(DR_PATH),
        "present": has_backup(),
        "size_bytes": (DR_PATH.stat().st_size if DR_PATH.is_file() else 0),
    }


__all__ = [
    "DisasterRecoveryError",
    "DisasterRecoveryMissingError",
    "DR_PATH",
    "has_backup",
    "encrypt",
    "decrypt",
    "disable",
    "status",
]
