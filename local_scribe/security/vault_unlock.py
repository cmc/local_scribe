"""Thin glue between :mod:`key_lifecycle` and :mod:`vault`.

The encrypted sparse-bundle vault is locked behind an hdiutil
passphrase. We derive that passphrase **deterministically** from
the Option C master key via HKDF-SHA256 with the dedicated label
``b"local_scribe.vault.passphrase.v1"``. This gives us three
properties:

* The vault passphrase is **never written to disk** and never shown
  to the operator. It lives in process memory between
  ``key_lifecycle.unlock_master_key()`` and the hdiutil ``-stdinpass``
  call.
* Unlocking the master key (Touch ID + YubiKey tap) is sufficient to
  unlock the vault — there is no separate passphrase the operator
  has to memorise.
* Rotating the master key (``./run.sh key rotate``) automatically
  rotates the vault passphrase via :func:`vault.rotate_password`.

The HKDF label is **versioned** (``v1``); if we ever need to change
the derivation construction, we bump to ``v2`` and migrate by calling
``vault.rotate_password(derive_password(mk, version=1), derive_password(mk, version=2))``
once.

This module is intentionally side-effect-free at import time. The CLI
surface in ``./run.sh vault`` and ``./run.sh yubikey`` invokes the
public functions here, which in turn handle the Touch ID + YubiKey
prompts and the hdiutil orchestration.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

from local_scribe.security import key_lifecycle
from local_scribe.security import secret_store
from local_scribe.security import service_auth
from local_scribe.security import vault
from local_scribe.security import yubikey_backup


logger = logging.getLogger("local_scribe.vault_unlock")


# ---------- passphrase derivation -----------------------------------


# HKDF salt identifies BOTH the application and the derivation
# version. Bump the version suffix (and add a corresponding migration
# path) if the construction ever changes.
_HKDF_SALT_V1 = b"local_scribe.vault.passphrase.v1"

# 64 bytes of HKDF output, hex-encoded, gives a 128-char ASCII
# passphrase. hdiutil's ``-stdinpass`` is tolerant of binary input
# but hex keeps the wire format obvious and grep-resistant.
_PASSPHRASE_BYTES = 64


def derive_password(master_key: bytes, *, version: int = 1) -> bytes:
    """Return the hdiutil passphrase derived from ``master_key``.

    Output is hex-encoded ASCII (128 chars from 64 bytes of HKDF).
    Versioned so a future construction change can co-exist with the
    old one during migration.
    """
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != 32:
        raise ValueError(
            f"master_key must be 32 bytes, got "
            f"{len(master_key) if hasattr(master_key, '__len__') else '?'}"
        )
    if version != 1:
        raise ValueError(f"unsupported vault passphrase version: {version}")
    raw = service_auth.hkdf_sha256(
        ikm=bytes(master_key),
        salt=_HKDF_SALT_V1,
        info=b"vault.hdiutil.passphrase",
        length=_PASSPHRASE_BYTES,
    )
    return raw.hex().encode("ascii")


@contextlib.contextmanager
def _hold_password(
    *,
    prompt: str = "Unlock local_scribe vault",
    on_touch_prompt=None,
) -> Iterator[bytes]:
    """Yield the vault passphrase, zeroising both the master key and
    the derived passphrase on exit. Use this around any code that
    needs the passphrase in scope (vault.create / vault.mount /
    vault.rotate_password)."""
    mk = key_lifecycle.unlock_master_key(
        prompt=prompt, on_touch_prompt=on_touch_prompt,
    )
    pw_bytes: Optional[bytearray] = None
    try:
        pw_bytes = bytearray(derive_password(mk.as_bytes()))
        yield bytes(pw_bytes)
    finally:
        if pw_bytes is not None:
            for i in range(len(pw_bytes)):
                pw_bytes[i] = 0
        mk.forget()


# ---------- public operations ---------------------------------------


def init_vault(*, size_gb: Optional[int] = None) -> vault.Path:
    """Create the encrypted sparse bundle for the first time. Requires
    the master key (Touch ID + YubiKey tap). Idempotent: returns the
    existing bundle path if one is already there."""
    if vault.exists():
        logger.info("vault already exists at %s; init is a no-op",
                    vault.paths().bundle)
        return vault.paths().bundle
    kwargs = {}
    if size_gb is not None:
        kwargs["size_gb"] = size_gb
    with _hold_password(prompt="Initialise local_scribe vault") as pw:
        return vault.create(pw, **kwargs)


def mount_vault(*, relocate_char: bool = True) -> vault.Path:
    """Mount the vault and optionally relocate Char's data dir into
    it (idempotent). Requires the master key."""
    if not vault.exists():
        raise vault.VaultMissingError(
            "no vault on disk yet — run `./run.sh vault init` first"
        )
    with _hold_password(prompt="Mount local_scribe vault") as pw:
        mount = vault.mount(pw)
    if relocate_char:
        vault.relocate_char_data()
    return mount


def unmount_vault() -> None:
    """Detach the vault. No master-key prompt needed (we're locking
    on the way out, not unlocking)."""
    vault.unmount()


def rotate_vault_passphrase(old_master: bytes, new_master: bytes) -> None:
    """Re-key the hdiutil envelope from the OLD master to the NEW master.

    Called by ``./run.sh key rotate`` (when the vault is wired) right
    after :func:`key_lifecycle.rotate_master_key` produces both
    handles. The vault must be unmounted before this can run — hdiutil
    refuses to re-key a live volume.
    """
    if vault.is_mounted():
        raise vault.VaultError(
            "cannot rotate vault passphrase while it's mounted; "
            "call vault_unlock.unmount_vault() first"
        )
    old_pw = bytearray(derive_password(old_master))
    new_pw = bytearray(derive_password(new_master))
    try:
        vault.rotate_password(bytes(old_pw), bytes(new_pw))
    finally:
        for i in range(len(old_pw)):
            old_pw[i] = 0
        for i in range(len(new_pw)):
            new_pw[i] = 0


def status() -> dict:
    """JSON-safe combined snapshot: ``vault.status()`` + a flag for
    whether the master key is available (no prompts)."""
    s = vault.status()
    s["master_key_present"] = (
        secret_store.has_kc_half() or secret_store.has_master_key()
    )
    s["yubikey_enrolled"] = yubikey_backup.is_enrolled()
    s["yk_half_present"] = yubikey_backup.has_yk_half()
    return s


# ---------- CLI -----------------------------------------------------


def _cli_init(args: list[str]) -> int:
    """``init [--size-gb N]`` — create the encrypted vault. Prompts
    Touch ID + YubiKey tap to derive the passphrase."""
    import json as _json
    import sys as _sys
    size_gb: Optional[int] = None
    i = 0
    while i < len(args):
        if args[i] == "--size-gb" and i + 1 < len(args):
            size_gb = int(args[i + 1])
            i += 2
            continue
        _sys.stderr.write(f"unknown init flag: {args[i]}\n")
        return 2
    bundle = init_vault(size_gb=size_gb)
    _sys.stdout.write(_json.dumps({
        "created": True,
        "bundle_path": str(bundle),
    }, indent=2) + "\n")
    return 0


def _cli_unlock(args: list[str]) -> int:
    """``unlock`` — mount the vault and relocate Char's data into
    it (idempotent on both fronts)."""
    import json as _json
    import sys as _sys
    relocate = "--no-relocate" not in args
    mount = mount_vault(relocate_char=relocate)
    _sys.stdout.write(_json.dumps({
        "mounted": True,
        "mount_path": str(mount),
        "char_data_relocated": vault.char_data_relocated(),
    }, indent=2) + "\n")
    return 0


def _cli_lock(_args: list[str]) -> int:
    """``lock`` — unmount the vault. Idempotent (already-unmounted is
    a no-op)."""
    import json as _json
    import sys as _sys
    unmount_vault()
    _sys.stdout.write(_json.dumps({"unmounted": True}, indent=2) + "\n")
    return 0


def _cli_status(_args: list[str]) -> int:
    import json as _json
    import sys as _sys
    _sys.stdout.write(_json.dumps(status(), indent=2) + "\n")
    return 0


def _cli_main(argv: list[str]) -> int:
    import sys as _sys
    table = {
        "init":    _cli_init,
        "unlock":  _cli_unlock,
        "mount":   _cli_unlock,    # alias
        "lock":    _cli_lock,
        "unmount": _cli_lock,      # alias
        "status":  _cli_status,
    }
    if len(argv) < 2:
        _sys.stderr.write(f"usage: {argv[0]} <{'|'.join(sorted(set(table)))}>\n")
        return 2
    fn = table.get(argv[1])
    if fn is None:
        _sys.stderr.write(f"unknown subcommand: {argv[1]}\n")
        return 2
    try:
        return fn(argv[2:])
    except Exception as exc:  # noqa: BLE001
        import os as _os
        _sys.stderr.write(f"error ({type(exc).__name__}): {exc}\n")
        if _os.environ.get("LOCAL_SCRIBE_DEBUG"):
            import traceback
            traceback.print_exc(file=_sys.stderr)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv))
