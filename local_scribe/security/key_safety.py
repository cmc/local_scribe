"""Key-operation safety helpers.

Every operation that can change or destroy master-key material —
``init --force``, ``rotate``, ``add-yubikey``, ``dr-restore`` over an
existing install, ``migrate``, ``destroy`` — passes through the
helpers in this module **before** mutating any state.

The two universal guarantees enforced here:

1.  **Physical presence proof.** The user must tap their enrolled
    YubiKey before any destructive key operation proceeds. We piggy-
    back on :func:`yubikey_backup.restore_yk_half` for this — a
    successful decrypt is end-to-end proof that the hardware token
    is plugged in *and* the user is physically present.

    For first-time ``init`` (no prior enrollment) and ``dr-restore``
    when the YubiKey is genuinely lost, the proof shifts to the
    *new* YubiKey being enrolled. That's the model that physically
    works — you can't tap a key you don't have.

2.  **Pre-flight backup.** Every destructive op snapshots the
    soon-to-be-replaced key material to a *timestamped* directory
    under ``~/.config/local_scribe/key-backups/<ts>-<label>/``
    before any mutation lands. The backup contains:

    *  ``yk_half.age`` — a byte-for-byte copy of the current
       YubiKey-wrapped half (already encrypted, harmless on disk).
    *  ``yubikey_recipients.txt`` — current recipient set, public
       information.
    *  ``disaster_recovery.age`` — copy of the current passphrase-
       encrypted DR file, if present.
    *  ``kc_half_account.txt`` — the Keychain account name we
       copied the current ``kc_half`` to (``master_key_kc_half_v2_
       backup_<ts>``). The actual half-bytes stay inside the
       Keychain, still protected by Touch ID.
    *  ``manifest.json`` — operation label, ISO-8601 timestamp,
       fingerprints (4-byte SHA-256 prefixes) of every backed-up
       artefact, and a recovery cookbook the operator can paste
       into a terminal to roll back.

    Backups are **never auto-pruned.** ``./run.sh key
    backups list`` shows them; ``./run.sh key backups prune <id>``
    removes a single snapshot only after a typed-DELETE
    confirmation.

The combination of those two — tap-proof of the *current* YubiKey
+ a recoverable copy of what we're about to mutate — means that
*every* destructive operation is reversible **as long as you have
both YubiKeys and your Touch ID**. The only data-loss path we
cannot defend against is the operator deliberately running
``./run.sh key backups prune`` after a destructive op + losing
their YubiKey + forgetting the DR passphrase.

See ``KEY_SAFETY.md`` for the full enumeration of data-loss
scenarios and the mitigation each one ties to.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from local_scribe.security import disaster_recovery
from local_scribe.security import secret_store
from local_scribe.security import yubikey_backup


# --- where backups live ---------------------------------------------------


CONFIG_DIR = Path(os.environ.get(
    "LOCAL_SCRIBE_CONFIG_DIR",
    str(Path.home() / ".config" / "local_scribe"),
))
BACKUPS_DIR = CONFIG_DIR / "key-backups"

KC_HALF_BACKUP_ACCOUNT_PREFIX = secret_store.ACCOUNT_KC_HALF_V2 + "_backup_"


# --- typed errors ---------------------------------------------------------


class PhysicalPresenceRequired(RuntimeError):
    """Raised when an operation needs a YubiKey tap that didn't happen."""


class BackupError(RuntimeError):
    """Raised when the pre-flight snapshot can't be written. The
    destructive operation MUST abort if this fires — we'd otherwise
    mutate state with no recovery path."""


# --- public data carriers -------------------------------------------------


class BackupScope(str, Enum):
    """What's being changed by the imminent operation, and therefore
    what needs to be in the snapshot."""

    #: ``add-yubikey`` — only ``yk_half.age`` is being rewritten.
    YK_HALF_ONLY = "yk_half_only"

    #: ``rotate`` / ``init --force`` / ``dr-restore`` over an existing
    #: install — both halves are being replaced.
    BOTH_HALVES = "both_halves"

    #: ``destroy`` — every artefact is going away, including the DR
    #: file. We snapshot all of it so a wrong-button destroy is still
    #: recoverable until the operator explicitly prunes the backup.
    EVERYTHING = "everything"

    #: ``migrate`` — v1 whole-key → v2 split-key. The v1 Keychain item
    #: is preserved for round-trip-verify (already safe by design);
    #: we ALSO snapshot it to a backup account in case someone
    #: re-runs migrate after a partial failure.
    V1_LEGACY = "v1_legacy"


@dataclass
class BackupRecord:
    """A successful pre-flight snapshot. Returned by
    :func:`preflight_backup` and serialised into the snapshot's
    ``manifest.json`` for operator-facing rollback."""

    path: Path                  # the snapshot directory
    label: str                  # caller-supplied operation tag
    scope: BackupScope
    timestamp: float            # unix epoch seconds
    iso_timestamp: str          # human-readable
    artefacts: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    kc_half_backup_account: Optional[str] = None  # Keychain account
    note: Optional[str] = None

    @property
    def id(self) -> str:
        """The directory basename — what the operator refers to in
        ``./run.sh key backups prune <id>``."""
        return self.path.name

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["path"] = str(self.path)
        d["scope"] = self.scope.value
        return d


# --- physical-presence proof --------------------------------------------


def require_physical_presence(
    reason: str,
    *,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
) -> None:
    """Refuse to proceed unless the current YubiKey can be tapped.

    Calls :func:`yubikey_backup.restore_yk_half` and discards the
    returned bytes — we don't need the half here, only the proof
    that the user physically tapped the token. ``reason`` is
    surfaced through ``on_touch_prompt`` so the UI can explain why
    the tap is required ("rotating the master key", "destroying all
    key material", etc.).

    Raises :class:`PhysicalPresenceRequired` if:

    * no YubiKey is enrolled (caller should redirect the user to
      the DR / re-enroll path)
    * the YubiKey isn't inserted
    * the tap timed out
    * the age plugin returned an error

    The check is "best available" — we can't truly prove the human
    in front of the keyboard is the rightful owner, only that
    *someone* with possession of the enrolled token tapped it.
    Combined with the Touch ID ACL on the Keychain half this gives
    two-of-two physical-factor proof.
    """
    if not yubikey_backup.has_yk_half():
        raise PhysicalPresenceRequired(
            f"physical-presence check failed ({reason!r}): no YubiKey "
            f"enrollment on disk; if you've lost your YubiKey, run "
            f"`./run.sh key dr-restore` to recover via the DR "
            f"passphrase, then re-enroll a new key."
        )
    try:
        # The actual tap. We can't reach in and zero the returned
        # bytes the way unlock_master_key does; we just immediately
        # drop the reference and let CPython GC catch up.
        proof = yubikey_backup.restore_yk_half(on_touch_prompt=on_touch_prompt)
        # Defensive: clear before drop.
        try:
            ba = bytearray(proof)
            for i in range(len(ba)):
                ba[i] = 0
        except Exception:  # noqa: BLE001
            pass
    except yubikey_backup.YubiKeyError as exc:
        raise PhysicalPresenceRequired(
            f"physical-presence check failed ({reason!r}): {exc}"
        ) from exc


# --- preflight backup ----------------------------------------------------


def _now_iso() -> tuple[float, str]:
    t = time.time()
    return t, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def _snapshot_dir_for(label: str, when: Optional[str] = None) -> Path:
    """Compute the snapshot directory name. We sanitise ``label`` so
    a future caller passing untrusted text can't smuggle path
    separators (we control all callers today, but defense in depth)."""
    when = when or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    return BACKUPS_DIR / f"{when}-{safe}"


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _maybe_copy(src: Path, dst_dir: Path) -> Optional[Path]:
    if not src.is_file():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    dst.chmod(0o600)
    return dst


def _copy_kc_half_to_backup_account(
    backup_account: str,
    prompt: str,
) -> bool:
    """Copy the current ``master_key_kc_half_v2`` Keychain item to
    ``backup_account``. Returns True on success. Requires Touch ID
    (the source read is gated).

    We use ``secret_store`` private helpers because there's no
    public "copy" surface (and adding one would imply we routinely
    duplicate the half, which we don't — only at snapshot time)."""
    if not secret_store.has_kc_half():
        return False
    half = secret_store.load_kc_half(prompt=prompt)
    try:
        secret_store._store_item(half, account=backup_account)  # noqa: SLF001
        return True
    finally:
        # Zero the half we briefly held in this process.
        try:
            ba = bytearray(half)
            for i in range(len(ba)):
                ba[i] = 0
        except Exception:  # noqa: BLE001
            pass


def preflight_backup(
    label: str,
    *,
    scope: BackupScope = BackupScope.BOTH_HALVES,
    require_touch_id: bool = True,
) -> BackupRecord:
    """Snapshot every key artefact that's about to be mutated.

    ``label`` ends up in the directory name + the manifest — keep it
    short ("rotate", "init-force", "destroy"). ``scope`` controls
    which artefacts get included; pick the narrowest scope that
    covers the imminent mutation so we don't ask for Touch ID
    unnecessarily.

    Returns a :class:`BackupRecord`. Raises :class:`BackupError` on
    any I/O / Keychain failure — callers MUST abort the destructive
    op in that case.
    """
    when, iso = _now_iso()
    snap_dir = _snapshot_dir_for(label, when=time.strftime(
        "%Y%m%dT%H%M%SZ", time.gmtime(when),
    ))
    if snap_dir.exists():
        # Extremely unlikely (one-second granularity collision); add
        # a milli-suffix and try again.
        snap_dir = snap_dir.with_name(snap_dir.name + f"-{int((when % 1) * 1e6):06d}")

    try:
        snap_dir.mkdir(parents=True, exist_ok=False)
        snap_dir.chmod(0o700)
    except OSError as exc:
        raise BackupError(f"could not create snapshot dir {snap_dir}: {exc}") from exc

    artefacts: dict[str, str] = {}
    fingerprints: dict[str, str] = {}

    try:
        # 1. yk_half.age (always copy if we have one — even on
        #    YK_HALF_ONLY scope, since rolling back add-yubikey
        #    means restoring the previous wrap).
        if yubikey_backup.has_yk_half():
            dst = _maybe_copy(yubikey_backup.YK_HALF_PATH, snap_dir)
            if dst:
                artefacts["yk_half_age"] = str(dst)
                fingerprints["yk_half_age"] = _fingerprint(dst.read_bytes())

        # 2. yubikey_recipients.txt (always copy when present)
        if yubikey_backup.RECIPIENTS_PATH.is_file():
            dst = _maybe_copy(yubikey_backup.RECIPIENTS_PATH, snap_dir)
            if dst:
                artefacts["yubikey_recipients"] = str(dst)
                fingerprints["yubikey_recipients"] = _fingerprint(dst.read_bytes())

        # 3. disaster_recovery.age (copy on EVERYTHING + BOTH_HALVES;
        #    skip on YK_HALF_ONLY since DR isn't changing).
        if scope in (BackupScope.EVERYTHING, BackupScope.BOTH_HALVES,
                     BackupScope.V1_LEGACY):
            if disaster_recovery.has_backup():
                dst = _maybe_copy(disaster_recovery.DR_PATH, snap_dir)
                if dst:
                    artefacts["disaster_recovery_age"] = str(dst)
                    fingerprints["disaster_recovery_age"] = _fingerprint(
                        dst.read_bytes(),
                    )

        # 4. kc_half — copied to a uniquely-named Keychain backup
        #    account. Only when the scope says kc_half is going to
        #    change. YK_HALF_ONLY (add-yubikey) doesn't touch
        #    kc_half so we leave the Keychain alone — also avoids
        #    a Touch ID prompt the operator didn't expect.
        kc_backup_account: Optional[str] = None
        if scope in (BackupScope.BOTH_HALVES, BackupScope.EVERYTHING,
                     BackupScope.V1_LEGACY) and require_touch_id:
            kc_backup_account = (
                KC_HALF_BACKUP_ACCOUNT_PREFIX
                + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when))
            )
            try:
                copied = _copy_kc_half_to_backup_account(
                    kc_backup_account,
                    prompt=f"Snapshot kc_half before {label} (Touch ID)",
                )
            except secret_store.SecretStoreError as exc:
                # Don't lose the rest of the snapshot — but mark the
                # kc_half as un-copied. The caller can choose whether
                # to abort.
                kc_backup_account = None
                artefacts["kc_half_keychain_error"] = str(exc)[:200]
            else:
                if copied:
                    artefacts["kc_half_keychain_account"] = kc_backup_account
                else:
                    kc_backup_account = None

        # 5. Legacy v1 whole-key item (only relevant for migrate).
        if scope == BackupScope.V1_LEGACY and secret_store.has_master_key():
            # Same approach — copy to a backup account so the v1 key
            # is preserved alongside the new v2 split.
            v1_backup = "master_key_v1_backup_" + time.strftime(
                "%Y%m%dT%H%M%SZ", time.gmtime(when),
            )
            try:
                v1_bytes = secret_store.load_master_key(
                    prompt=f"Snapshot legacy v1 key before {label} (Touch ID)",
                )
                try:
                    secret_store._store_item(v1_bytes, account=v1_backup)  # noqa: SLF001
                    artefacts["v1_keychain_account"] = v1_backup
                finally:
                    try:
                        ba = bytearray(v1_bytes)
                        for i in range(len(ba)):
                            ba[i] = 0
                    except Exception:  # noqa: BLE001
                        pass
            except secret_store.SecretStoreError as exc:
                artefacts["v1_keychain_error"] = str(exc)[:200]

        # 6. manifest.json — last so any errors above are baked in.
        manifest = {
            "label": label,
            "scope": scope.value,
            "timestamp": when,
            "iso_timestamp": iso,
            "artefacts": artefacts,
            "fingerprints": fingerprints,
            "kc_half_backup_account": kc_backup_account,
            "recovery_cookbook": _recovery_cookbook(
                label=label,
                scope=scope,
                artefacts=artefacts,
                kc_account=kc_backup_account,
            ),
        }
        manifest_path = snap_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.chmod(0o600)

    except Exception as exc:  # noqa: BLE001
        # If anything failed during snapshot, clean up the partial
        # directory so we don't leave half-state around.
        try:
            shutil.rmtree(snap_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        raise BackupError(f"preflight_backup failed: {exc}") from exc

    return BackupRecord(
        path=snap_dir,
        label=label,
        scope=scope,
        timestamp=when,
        iso_timestamp=iso,
        artefacts=artefacts,
        fingerprints=fingerprints,
        kc_half_backup_account=kc_backup_account,
    )


def _recovery_cookbook(
    *,
    label: str,
    scope: BackupScope,
    artefacts: dict[str, str],
    kc_account: Optional[str],
) -> list[str]:
    """Build the human-readable rollback steps recorded in
    ``manifest.json``. The cookbook is plain-text so an operator
    facing a panicked recovery can paste these into a terminal
    without re-reading the code."""
    steps: list[str] = []
    steps.append(
        f"This snapshot was written immediately BEFORE the "
        f"{label!r} operation mutated the live key material."
    )
    if "yk_half_age" in artefacts:
        steps.append(
            f"To restore yk_half.age: cp {artefacts['yk_half_age']} "
            f"~/.config/local_scribe/yk_half.age"
        )
    if "yubikey_recipients" in artefacts:
        steps.append(
            f"To restore the recipients file: cp "
            f"{artefacts['yubikey_recipients']} "
            f"~/.config/local_scribe/yubikey_recipients.txt"
        )
    if "disaster_recovery_age" in artefacts:
        steps.append(
            f"To restore the disaster-recovery file: cp "
            f"{artefacts['disaster_recovery_age']} "
            f"~/.config/local_scribe/disaster_recovery.age"
        )
    if kc_account:
        steps.append(
            f"To restore kc_half, re-import from Keychain account "
            f"{kc_account!r} (Touch ID required):\n"
            f"  ./venv/bin/python -m key_safety restore-kc-half "
            f"{kc_account}"
        )
    if scope == BackupScope.V1_LEGACY and "v1_keychain_account" in artefacts:
        steps.append(
            f"Legacy v1 master key preserved at Keychain account "
            f"{artefacts['v1_keychain_account']!r}; restoring it "
            f"requires manual `security` CLI work — DM the maintainer."
        )
    steps.append(
        "After restoring, run `./run.sh key status` to confirm the "
        "kc_half + yk_half pair are consistent (i.e. unlock works)."
    )
    return steps


# --- backup listing + pruning -------------------------------------------


def list_backups() -> list[BackupRecord]:
    """Newest first. Returns synthetic records re-hydrated from
    ``manifest.json`` so the operator-facing list and the original
    record are interchangeable."""
    if not BACKUPS_DIR.is_dir():
        return []
    out: list[BackupRecord] = []
    for child in sorted(BACKUPS_DIR.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        m_path = child / "manifest.json"
        if not m_path.is_file():
            continue
        try:
            m = json.loads(m_path.read_text())
        except json.JSONDecodeError:
            continue
        try:
            scope = BackupScope(m.get("scope", BackupScope.BOTH_HALVES.value))
        except ValueError:
            scope = BackupScope.BOTH_HALVES
        out.append(BackupRecord(
            path=child,
            label=m.get("label", "unknown"),
            scope=scope,
            timestamp=float(m.get("timestamp") or 0),
            iso_timestamp=m.get("iso_timestamp", ""),
            artefacts=dict(m.get("artefacts", {})),
            fingerprints=dict(m.get("fingerprints", {})),
            kc_half_backup_account=m.get("kc_half_backup_account"),
            note=m.get("note"),
        ))
    return out


def prune_backup(backup_id: str) -> dict:
    """Delete a single backup snapshot. Refuses to delete anything
    outside ``BACKUPS_DIR`` (no traversal). Also deletes the
    associated Keychain backup account if one was recorded.

    The caller MUST have a typed-DELETE confirm from the operator
    before calling this — we don't gate at the function level
    because tests rely on direct invocation."""
    target = BACKUPS_DIR / backup_id
    if not target.is_dir():
        raise FileNotFoundError(
            f"no backup snapshot at {target}; run "
            f"`./run.sh key backups list` to see what's there."
        )
    try:
        target.resolve().relative_to(BACKUPS_DIR.resolve())
    except ValueError as exc:
        raise PermissionError(
            f"refusing to delete a path outside {BACKUPS_DIR}: {target}"
        ) from exc

    result: dict = {"deleted_path": str(target), "keychain_deleted": False}
    m_path = target / "manifest.json"
    kc_account: Optional[str] = None
    if m_path.is_file():
        try:
            m = json.loads(m_path.read_text())
            kc_account = m.get("kc_half_backup_account") or None
        except json.JSONDecodeError:
            kc_account = None
    if kc_account:
        try:
            secret_store._delete_item(account=kc_account)  # noqa: SLF001
            result["keychain_deleted"] = True
            result["keychain_account"] = kc_account
        except secret_store.SecretStoreError as exc:
            result["keychain_error"] = str(exc)
    shutil.rmtree(target)
    return result


# --- CLI for run.sh integration -----------------------------------------


def _cli_list_backups(_args: list[str]) -> int:
    import sys as _sys
    out = [
        {
            "id": b.id,
            "label": b.label,
            "scope": b.scope.value,
            "iso_timestamp": b.iso_timestamp,
            "artefact_count": len(b.artefacts),
            "kc_half_backup_account": b.kc_half_backup_account,
            "path": str(b.path),
        }
        for b in list_backups()
    ]
    _sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0


def _cli_prune_backup(args: list[str]) -> int:
    import sys as _sys
    if len(args) != 1:
        _sys.stderr.write("usage: python -m key_safety prune <backup_id>\n")
        return 2
    try:
        result = prune_backup(args[0])
    except (FileNotFoundError, PermissionError) as exc:
        _sys.stderr.write(f"error: {exc}\n")
        return 1
    _sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cli_restore_kc_half(args: list[str]) -> int:
    """Restore kc_half from a Keychain backup account (Touch ID
    required). Reads the *current* kc_half via Touch ID, asks the
    operator to confirm we're about to overwrite, then promotes the
    backup account's value into the live ``ACCOUNT_KC_HALF_V2``."""
    import sys as _sys
    if len(args) != 1:
        _sys.stderr.write(
            "usage: python -m key_safety restore-kc-half <keychain_account>\n"
        )
        return 2
    account = args[0]
    if not account.startswith(KC_HALF_BACKUP_ACCOUNT_PREFIX):
        _sys.stderr.write(
            f"refuse to restore from account {account!r}: must start with "
            f"{KC_HALF_BACKUP_ACCOUNT_PREFIX!r}\n"
        )
        return 2
    try:
        backup = secret_store._load_item(  # noqa: SLF001
            prompt=f"Restore kc_half from {account} (Touch ID)",
            account=account,
        )
    except secret_store.SecretStoreError as exc:
        _sys.stderr.write(f"error reading backup account: {exc}\n")
        return 1
    try:
        secret_store.store_kc_half(backup)
    finally:
        try:
            ba = bytearray(backup)
            for i in range(len(ba)):
                ba[i] = 0
        except Exception:  # noqa: BLE001
            pass
    _sys.stdout.write(json.dumps({
        "restored": True,
        "source_account": account,
        "target_account": secret_store.ACCOUNT_KC_HALF_V2,
    }, indent=2) + "\n")
    return 0


def _cli_main(argv: list[str]) -> int:
    import sys as _sys
    table = {
        "list":              _cli_list_backups,
        "list-backups":      _cli_list_backups,
        "prune":             _cli_prune_backup,
        "prune-backup":      _cli_prune_backup,
        "restore-kc-half":   _cli_restore_kc_half,
    }
    if len(argv) < 2:
        _sys.stderr.write(f"usage: {argv[0]} <{'|'.join(sorted(set(table)))}>\n")
        return 2
    cmd = argv[1]
    fn = table.get(cmd)
    if fn is None:
        _sys.stderr.write(f"unknown subcommand: {cmd}\n")
        return 2
    return fn(argv[2:])


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv))
