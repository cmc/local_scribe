"""Encrypted vault for local_scribe data — an AES-256 sparse-bundle disk image.

Design summary
--------------

Forward-looking design (this module is implemented + unit-tested but
the ``./run.sh start`` wiring that mounts the vault on demand is still
pending — see TODO.md → "Encrypt audio at rest"):

1.  The 256-bit master key is the Option C split-key reconstituted via
    ``key_lifecycle.unlock_master_key()`` -- ``kc_half`` from Keychain
    (Touch ID) XOR ``yk_half`` from an age-encrypted file (YubiKey tap).
    See ``key_lifecycle.py`` and ``key_split.py``.
2.  An AES-256 sparse bundle is created via ``hdiutil`` at
    ``~/Library/Application Support/local_scribe-vault.sparsebundle``.
    The sparse-bundle format only consumes disk space as data is written,
    so the headline 100 GB size is nominal -- a fresh vault is ~4 MB.
3.  Char's actual data dir (``~/Library/Application Support/hyprnote``)
    is moved *into* the mounted vault and replaced with a symlink. Char
    -- a Tauri app that uses bog-standard Foundation FS APIs -- follows
    symlinks transparently, so it never knows anything happened.
4.  Locking calls ``hdiutil detach``; unlocking calls ``key_lifecycle.
    unlock_master_key()`` (Touch ID + YubiKey tap) then ``hdiutil
    attach -stdinpass``. The password is piped in via stdin so it
    never appears on argv -- mirroring the same defence as
    ``char_settings_writer``.

Why a sparse bundle rather than per-file encryption:
    A FUSE/userspace overlay would be far more invasive and require code
    changes in *both* Char and our backends to handle decrypt-on-read /
    encrypt-on-write. Sparse bundles are a 30-year-old Apple primitive
    that Char already uses transparently with zero code on our side.

Why ``-encryption AES-256`` (specifically the 256-bit variant):
    The 128-bit variant exists but with AES-NI on Apple Silicon the
    performance gap is sub-1%. Spec the strongest standardised option.

Threat model
------------

*Defends against:* another local macOS user reading your transcripts,
backup software (Time Machine / external drives) copying plaintext data,
a stolen laptop with the Mac signed-out (FileVault overlap), forensic
imaging of an unmounted disk.

*Does NOT defend against:* malware running as your user *after* you
unlock the vault (the mount is process-agnostic) -- this is why we still
require Touch ID for every unlock, so an unattended-but-unlocked Mac
re-locks every time you ``./run.sh stop`` or reboot.

Notes
-----

- ``hdiutil`` reads passwords from stdin with ``-stdinpass``. Each new
  ``hdiutil`` call gets its own pipe; the key never appears in argv or
  in environment variables.
- The mount point is created on demand and removed on unmount. macOS
  itself maintains the actual mounted volume under ``/Volumes/...``
  regardless; we tell hdiutil to use a fixed mountpoint to avoid the
  "(2)" suffix madness that ``open`` of a sparse bundle would cause.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger("local_scribe.vault")


# ---------------------------------------------------------------------------
# Paths
#
# All overridable via env var for tests. In production these are computed
# once at import time from ``$HOME``; tests monkeypatch by setting the
# env vars then calling ``reload_paths()``.

_DEF_BUNDLE = (Path.home() / "Library" / "Application Support"
               / "local_scribe-vault.sparsebundle")
_DEF_MOUNT = (Path.home() / "Library" / "Application Support"
              / "local_scribe-vault")
_DEF_CHAR_DATA = Path.home() / "Library" / "Application Support" / "hyprnote"

VAULT_BUNDLE_ENV = "LOCAL_SCRIBE_VAULT_BUNDLE"
VAULT_MOUNT_ENV = "LOCAL_SCRIBE_VAULT_MOUNT"
CHAR_DATA_DIR_ENV = "LOCAL_SCRIBE_CHAR_DATA_DIR"

# 100 GB sparse default. Sparse bundles only consume what's written, so
# this is purely the max ceiling. Bumping later requires ``hdiutil
# resize``; halving requires the user to compact + repartition.
DEFAULT_SIZE_GB = 100

# Volume label as it appears in /Volumes/. Cosmetic only.
VOLUME_LABEL = "local_scribe"

# Filesystem for the encrypted volume. APFS case-insensitive is what
# Char's host volume is by default, so we match it to avoid surprises
# (e.g. ``Settings.json`` vs ``settings.json`` on case-sensitive APFS).
FILESYSTEM = "APFS"


@dataclass
class VaultPaths:
    """Resolved paths the rest of the module operates against. Reload via
    ``reload_paths()`` after monkeypatching env vars (used by tests)."""
    bundle: Path
    mount: Path
    char_data: Path

    @property
    def stamp(self) -> Path:
        """Marker file written *inside* the mounted vault. Lets us
        distinguish "mount exists, contains data" from "mount exists but
        was created by an unrelated app" before we touch it."""
        return self.mount / ".local_scribe_vault"


def _resolve_paths() -> VaultPaths:
    bundle = Path(os.environ.get(VAULT_BUNDLE_ENV) or _DEF_BUNDLE)
    mount = Path(os.environ.get(VAULT_MOUNT_ENV) or _DEF_MOUNT)
    char = Path(os.environ.get(CHAR_DATA_DIR_ENV) or _DEF_CHAR_DATA)
    return VaultPaths(bundle=bundle, mount=mount, char_data=char)


_paths = _resolve_paths()


def reload_paths() -> VaultPaths:
    """Re-read env-var overrides. Tests call this after fiddling with
    ``os.environ`` to point at temp directories."""
    global _paths
    _paths = _resolve_paths()
    return _paths


def paths() -> VaultPaths:
    return _paths


# ---------------------------------------------------------------------------
# Exceptions

class VaultError(Exception):
    """Generic vault failure. Subclasses below for the specific
    well-known failure modes so call sites can branch."""


class VaultExistsError(VaultError):
    """Tried to ``create()`` but the bundle is already on disk. Re-running
    bootstrap on an existing vault is expected, so callers should treat
    this as "no-op success" rather than fatal."""


class VaultMissingError(VaultError):
    """No bundle at the expected path. Surface to the user with the
    bootstrap hint."""


class VaultMountError(VaultError):
    """``hdiutil attach`` failed -- usually wrong key or corrupt bundle."""


# ---------------------------------------------------------------------------
# Helpers

def _hdiutil(args: list[str], *, password: Optional[bytes] = None,
             timeout: int = 120) -> subprocess.CompletedProcess:
    """Thin wrapper around ``hdiutil`` that pipes a password on stdin
    when provided and surfaces stderr in raised exceptions for
    diagnosability."""
    cmd = ["hdiutil", *args]
    proc = subprocess.run(
        cmd,
        input=password if password is not None else None,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        # Don't include the password (it was never in argv) but do
        # include the redacted command for context.
        raise VaultError(
            f"hdiutil {' '.join(args)!r} failed (rc={proc.returncode}): "
            f"{stderr or '<no stderr>'}"
        )
    return proc


def _hdiutil_info() -> dict:
    """Parse ``hdiutil info -plist`` into a Python dict. We use ``-plist``
    rather than JSON because ``hdiutil`` doesn't have a JSON flag, and
    rely on ``plistlib`` from stdlib."""
    import plistlib  # stdlib; deferred import keeps cold-start cheap
    proc = _hdiutil(["info", "-plist"])
    try:
        return plistlib.loads(proc.stdout)
    except Exception as exc:
        raise VaultError(f"hdiutil info parse: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API

def exists() -> bool:
    """True if the sparse bundle is on disk (mounted or not)."""
    return _paths.bundle.exists()


def is_mounted() -> bool:
    """True if our bundle is currently attached. We check ``hdiutil
    info`` rather than just ``mount.is_dir()`` because the mount point
    directory persists after detach until ``unmount()`` removes it."""
    if not exists():
        return False
    target = str(_paths.bundle.resolve())
    try:
        info = _hdiutil_info()
    except VaultError:
        return False
    for image in info.get("images", []):
        if image.get("image-path") == target:
            # An attached image has at least one system-entity with a
            # mount-point. (Unmounted-but-attached is a thing -- if some
            # other process attached without mounting -- but for our
            # purposes "is it mounted at our path" is what matters.)
            for ent in image.get("system-entities", []):
                if ent.get("mount-point"):
                    return True
    return False


def vault_already_mounted_at(mount: Path) -> bool:
    """Defensive check used by ``mount()``: is something *else* already
    using our mount point? If so we refuse to clobber it."""
    if not mount.exists():
        return False
    # macOS lists mounts via ``mount`` shell command. We check whether
    # our mount path matches the second word of any line.
    try:
        out = subprocess.check_output(["mount"], text=True, timeout=10)
    except subprocess.CalledProcessError:
        return False
    target = str(mount.resolve())
    for line in out.splitlines():
        # Format: "/dev/diskNsM on /Volumes/foo (apfs, local, nodev, ...)"
        parts = line.split(" on ", 1)
        if len(parts) != 2:
            continue
        mpath = parts[1].split(" (", 1)[0].strip()
        if mpath == target:
            return True
    return False


def create(password: bytes, *, size_gb: int = DEFAULT_SIZE_GB) -> Path:
    """Create the sparse bundle, encrypted with ``password``. Returns the
    bundle path. Raises ``VaultExistsError`` if already present."""
    if exists():
        raise VaultExistsError(f"vault already exists at {_paths.bundle}")
    _paths.bundle.parent.mkdir(parents=True, exist_ok=True)

    # hdiutil create flags:
    #   -encryption AES-256        AES-XTS 256-bit, hardware-accelerated
    #   -stdinpass                 read passphrase from stdin
    #   -size {N}g                 max size (sparse: actual usage starts ~4 MB)
    #   -type SPARSEBUNDLE         banded sparse format (works well over
    #                              network shares + Time Machine)
    #   -fs APFS                   case-insensitive APFS to match Char's
    #                              host volume semantics
    #   -volname {label}           friendly name in /Volumes/
    args = [
        "create",
        "-encryption", "AES-256",
        "-stdinpass",
        "-size", f"{size_gb}g",
        "-type", "SPARSEBUNDLE",
        "-fs", FILESYSTEM,
        "-volname", VOLUME_LABEL,
        str(_paths.bundle),
    ]
    logger.info("creating encrypted vault at %s (size=%sG)",
                _paths.bundle, size_gb)
    _hdiutil(args, password=password, timeout=300)
    # Lock down perms on the bundle dir itself (sparse bundles are
    # technically directories on APFS).
    try:
        os.chmod(_paths.bundle, 0o700)
    except OSError:
        pass
    return _paths.bundle


def mount(password: bytes) -> Path:
    """Attach + mount the bundle at our fixed mountpoint. Returns the
    mount path. Idempotent: returns immediately if already mounted."""
    if not exists():
        raise VaultMissingError(
            f"no vault at {_paths.bundle} (call vault.create() first; "
            "the operator-facing `./run.sh start` wiring that creates "
            "the sparse bundle on demand is still pending — see TODO.md)"
        )
    if is_mounted():
        logger.info("vault already mounted at %s; no-op", _paths.mount)
        return _paths.mount
    if vault_already_mounted_at(_paths.mount):
        raise VaultMountError(
            f"something else is already mounted at {_paths.mount}; "
            f"unmount it manually before retrying"
        )
    _paths.mount.mkdir(parents=True, exist_ok=True)
    # -nobrowse keeps the volume out of Finder's sidebar (cosmetic).
    # -owners on preserves uid/gid on the contained files (needed for
    # symlinks to work cleanly).
    args = [
        "attach",
        "-stdinpass",
        "-mountpoint", str(_paths.mount),
        "-nobrowse",
        "-owners", "on",
        str(_paths.bundle),
    ]
    logger.info("mounting vault at %s", _paths.mount)
    try:
        _hdiutil(args, password=password, timeout=60)
    except VaultError as exc:
        # The most common failure here is "Authentication error" =
        # wrong password. Map that to a more useful exception message.
        msg = str(exc).lower()
        if "authentication" in msg or "no mountable" in msg:
            raise VaultMountError(
                "wrong password or corrupt vault — "
                "did the Keychain key get reset?"
            ) from exc
        raise
    # Drop a stamp file so we can identify the vault after the fact.
    try:
        stamp = _paths.stamp
        if not stamp.is_file():
            stamp.write_text(json.dumps({
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
                "label": VOLUME_LABEL,
            }) + "\n")
    except OSError as exc:
        logger.debug("could not write vault stamp: %s", exc)
    return _paths.mount


def unmount() -> None:
    """Detach the bundle. Idempotent: returns silently if not mounted.

    Tries a polite ``detach`` first, then ``detach -force`` if anything
    holds open the volume (e.g. a stale Finder window). We surface the
    final failure rather than swallow it because "I clicked stop but
    plaintext is still visible on disk" is a footgun."""
    if not is_mounted():
        return
    try:
        _hdiutil(["detach", str(_paths.mount)], timeout=30)
    except VaultError as exc:
        logger.warning("polite detach failed, retrying with -force: %s", exc)
        _hdiutil(["detach", str(_paths.mount), "-force"], timeout=30)
    # Best-effort: remove the now-empty mountpoint dir so the user
    # doesn't see a confusing empty folder.
    try:
        if _paths.mount.is_dir() and not any(_paths.mount.iterdir()):
            _paths.mount.rmdir()
    except OSError as exc:
        logger.debug("mountpoint cleanup skipped: %s", exc)
    logger.info("vault unmounted")


def rotate_password(old_password: bytes, new_password: bytes) -> None:
    """Re-encrypt the bundle's keybag with a new password. The data
    itself isn't re-encrypted (that would be O(disk)); only the keybag
    (~few KB) is. Used when the user runs ``./run.sh key rotate`` or
    rotates a YubiKey backup.

    ``hdiutil chpass`` *requires* the bundle to be unmounted, so we
    detach first if needed."""
    if is_mounted():
        unmount()
    # hdiutil chpass takes both passwords interactively (or via
    # -stdinpass for old and -newstdinpass for new, but the exact flag
    # name has shifted across macOS versions). The safe / portable
    # approach is to write them to a temp pipe in the documented stdin
    # order: old\n new\n new\n (confirmation).
    # An alternative is to use ``-oldpassword`` and ``-newpassword`` env
    # vars in the older API, but those expose the password in /proc.
    # We use the modern -stdinpass / -newstdinpass.
    args = [
        "chpass",
        "-stdinpass",
        "-newstdinpass",
        str(_paths.bundle),
    ]
    payload = old_password + b"\n" + new_password + b"\n"
    logger.info("rotating vault password")
    _hdiutil(args, password=payload, timeout=60)


# ---------------------------------------------------------------------------
# Char data dir migration
#
# Idempotent. Three input states matter:
#
#   1. Char dir doesn't exist (fresh Mac):
#        create char_data target inside the vault, drop a symlink at the
#        canonical location pointing to it.
#   2. Char dir exists, is already a symlink to the vault:
#        no-op (we've migrated already).
#   3. Char dir exists, is a real directory with content:
#        copy the contents into the vault (preserving xattrs, perms,
#        timestamps), then rename the original to a ``.pre_vault_backup``
#        sibling, then drop the symlink.
#
# We never delete the user's existing Char data outright — too easy to
# corrupt a recording in flight, and `bootstrap` is allowed to be re-run
# at any time. The backup sibling can be deleted manually once the user
# confirms the vault is working.


def char_data_relocated() -> bool:
    """True if Char's canonical data dir is a symlink that resolves into
    the vault mount. (Doesn't check mount state -- that's a different
    question.)"""
    char = _paths.char_data
    if not char.exists():
        return False
    if not char.is_symlink():
        return False
    try:
        target = char.resolve()
    except OSError:
        return False
    try:
        target.relative_to(_paths.mount.resolve())
        return True
    except (ValueError, OSError):
        return False


def relocate_char_data(*, dry_run: bool = False) -> Optional[Path]:
    """Move Char's data dir into the vault (state 3 above) or create it
    fresh (state 1). Returns the path the symlink points at. Returns
    ``None`` if no work was needed (state 2)."""
    char = _paths.char_data
    inside = _paths.mount / "hyprnote"
    if char_data_relocated():
        logger.info("char data already lives inside the vault: %s -> %s",
                    char, inside)
        return None

    if not _paths.mount.is_dir():
        raise VaultError(
            f"mount point not present at {_paths.mount}; "
            f"call vault.mount() before relocate_char_data()"
        )

    if dry_run:
        return inside

    inside.mkdir(parents=True, exist_ok=True)

    if char.exists() and not char.is_symlink():
        # Real directory with content -> copy in.
        logger.info("copying existing Char data %s -> %s", char, inside)
        # shutil.copytree refuses to write into a non-empty dest dir.
        # Walk + copyfile to handle partial-merge scenarios.
        _merge_dir(char, inside)
        # Back up the old location so the user can verify before
        # deleting. timestamp suffix protects against repeated runs.
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = char.with_name(char.name + f".pre_vault_backup.{ts}")
        os.rename(char, backup)
        logger.info("renamed original Char data to %s (delete it after you've "
                    "verified the vault is working)", backup)
    elif char.is_symlink():
        # Stale symlink (state 3 left over from a previous attempt).
        os.unlink(char)

    # Drop the symlink. Always a relative-to-parent absolute path so
    # tools that walk symlinks resolve unambiguously.
    char.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(inside.resolve(), char)
    logger.info("symlink: %s -> %s", char, inside)
    return inside


def _merge_dir(src: Path, dst: Path) -> None:
    """``cp -R``-like merge that preserves metadata and survives
    re-running over a partially-copied dest."""
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src_f = Path(root) / name
            dst_f = out_dir / name
            if dst_f.exists():
                # Already copied; skip rather than overwrite. ``shutil
                # .copy2`` would silently clobber, which we don't want
                # if a previous run partially populated the vault.
                continue
            shutil.copy2(src_f, dst_f)


# ---------------------------------------------------------------------------
# Status reporting (run.sh `vault status` and the inspector)

def status() -> dict:
    """Snapshot for diagnostic output. Cheap; no Touch ID prompts."""
    s = {
        "bundle_path": str(_paths.bundle),
        "mount_path": str(_paths.mount),
        "char_data_path": str(_paths.char_data),
        "exists": exists(),
        "mounted": False,
        "char_data_relocated": False,
        "bundle_size_bytes": 0,
    }
    if s["exists"]:
        try:
            total = 0
            for r, _d, fs in os.walk(_paths.bundle):
                for f in fs:
                    try:
                        total += (Path(r) / f).stat().st_size
                    except OSError:
                        pass
            s["bundle_size_bytes"] = total
        except OSError:
            pass
        try:
            s["mounted"] = is_mounted()
        except VaultError:
            s["mounted"] = False
    s["char_data_relocated"] = char_data_relocated()
    return s
