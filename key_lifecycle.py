"""High-level key lifecycle for Option C (Keychain XOR YubiKey).

This module is the single source of truth for "how does the master
key get into / out of memory?". All four other modules
(:mod:`key_split`, :mod:`secret_store`, :mod:`yubikey_backup`,
:mod:`disaster_recovery`) implement isolated pieces; ``key_lifecycle``
composes them into the operator-facing commands.

Public entry points
-------------------

* :func:`init_master_key`
    First-time setup: enroll the primary YubiKey, generate a fresh
    master key, XOR-split it into ``kc_half`` (Keychain) +
    ``yk_half`` (YubiKey-encrypted), and write a passphrase-protected
    disaster-recovery copy. Called from ``./run.sh key init``.

* :func:`unlock_master_key`
    Routine unlock: prompt Touch ID for ``kc_half``, prompt YubiKey
    tap for ``yk_half``, XOR them, return a :class:`secret_store.MasterKey`.
    Called from FastAPI app startup + the CLI tools.

* :func:`rotate_master_key`
    Atomic key rotation: unlock current key, generate fresh master,
    write new halves to both factors, return both keys so the caller
    can re-encrypt the vault's keybag from old → new. Called from
    ``./run.sh key rotate``.

* :func:`add_yubikey`
    Enroll a second YubiKey and re-wrap ``yk_half`` so either
    YubiKey can decrypt it. Requires the existing YubiKey to be
    inserted first (to obtain ``yk_half``) before the new one is
    enrolled. Called from ``./run.sh key add-yubikey``.

* :func:`dr_restore`
    Disaster-recovery path: prompt for passphrase, decrypt the on-disk
    DR file, return the recovered master key, *and* offer to re-split
    it across a fresh kc_half + (new-)YubiKey so the routine unlock
    path is restored. Called from ``./run.sh key dr-restore``.

* :func:`migrate_v1_to_v2`
    Walk a v1 (legacy whole-key Keychain item) install over to the v2
    split-key shape. Idempotent: a clean v2 install is left alone.
    Called automatically from :func:`unlock_master_key` if it detects
    the legacy item.

Threat model invariants
-----------------------

Each public function maintains these invariants. Tests assert them.

* The master key bytes are never written to disk in any
  unwrapped form. (DR file uses age scrypt; vault uses hdiutil
  AES-256.)
* The master key bytes never appear on ``argv`` or in any
  ``os.environ`` we control. (We pipe via stdin / FDs only.)
* No function logs the master key, kc_half, or yk_half (verified by
  inspection of every ``logger.*`` call in this module).
* All flows that touch the YubiKey wait at most 60 s for a touch and
  surface a typed ``YubiKeyTouchTimeoutError`` so callers can re-
  prompt cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import disaster_recovery
import key_split
import secret_store
import yubikey_backup


logger = logging.getLogger("local_scribe.key_lifecycle")


# ---------- typed result containers ----------------------------------


@dataclass
class InitResult:
    """Returned by :func:`init_master_key`. Carries booleans for what
    actually happened so ``./run.sh`` can render a clear post-init
    summary ("kc_half stored, yk_half wrapped to 1 YubiKey, DR backup
    written")."""
    kc_half_stored: bool
    yk_half_wrapped: bool
    dr_backup_written: bool
    recipient: str


@dataclass
class RotationResult:
    """Old + new master key handles, plus the operation log. The
    caller is responsible for re-wrapping any downstream artefacts
    (vault keybag, etc.) before zeroing ``old``."""
    old: secret_store.MasterKey
    new: secret_store.MasterKey


# ---------- init -----------------------------------------------------


def init_master_key(
    *,
    dr_passphrase: Optional[str] = None,
    enroll_yubikey: bool = True,
    yubikey_slot: Optional[int] = None,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> InitResult:
    """First-time master-key setup. Returns an :class:`InitResult`
    describing what landed on disk / in the Keychain.

    Behaviour:

    1. Refuse to clobber an existing v2 install unless ``force=True``
       (preserves user data — a buggy re-bootstrap would otherwise
       wipe the Keychain half + leave the vault un-decryptable).
    2. Enroll the YubiKey (if ``enroll_yubikey`` is True and no
       enrollment is on disk yet). Skipped if the operator has
       pre-enrolled.
    3. Generate the split key (:func:`key_split.generate_split_key`).
    4. Write ``kc_half`` to the Keychain (no Touch ID prompt — the
       ACL only triggers on read).
    5. Wrap ``yk_half`` to the enrolled YubiKey recipient(s) and
       write to ``yk_half.age``.
    6. If ``dr_passphrase`` is given, write a passphrase-encrypted
       disaster-recovery copy of the *whole* master key. Skipped if
       ``None`` (user opted out).
    """
    if secret_store.has_kc_half() and not force:
        raise RuntimeError(
            "kc_half already in Keychain — refusing to overwrite. "
            "Pass force=True (or `./run.sh key init --force`) only if "
            "you've backed up any vault data first."
        )

    if enroll_yubikey and not yubikey_backup.is_enrolled():
        kwargs = {}
        if yubikey_slot is not None:
            kwargs["slot"] = yubikey_slot
        info = yubikey_backup.enroll(**kwargs)
        recipient = info.recipient
        logger.info("YubiKey enrolled for split-key (slot=%s)", info.slot)
    else:
        recipients = yubikey_backup.list_recipients()
        if not recipients:
            raise yubikey_backup.YubiKeyNotEnrolledError(
                "no YubiKey enrollment on disk; run `./run.sh key init` "
                "with --enroll-yubikey or pre-enroll via "
                "`./run.sh yubikey enroll`"
            )
        recipient = recipients[0]

    sk = key_split.generate_split_key()
    try:
        secret_store.store_kc_half(sk.kc_half)
        kc_stored = True

        yubikey_backup.backup_yk_half(sk.yk_half, recipients=[recipient])
        yk_wrapped = True

        dr_written = False
        if dr_passphrase is not None:
            disaster_recovery.encrypt(sk.master_key, dr_passphrase)
            dr_written = True

        return InitResult(
            kc_half_stored=kc_stored,
            yk_half_wrapped=yk_wrapped,
            dr_backup_written=dr_written,
            recipient=recipient,
        )
    finally:
        # Best-effort scrub of the freshly-generated halves and master.
        # We can't guarantee no aliases linger in the CPython heap, but
        # zeroing the bytearray we still hold makes it less likely the
        # values stay reachable from a long-running process.
        try:
            ba = bytearray(sk.master_key); key_split.zero_bytes(ba)
            ba = bytearray(sk.kc_half);    key_split.zero_bytes(ba)
            ba = bytearray(sk.yk_half);    key_split.zero_bytes(ba)
        except Exception:  # noqa: BLE001
            pass


# ---------- unlock ---------------------------------------------------


def unlock_master_key(
    *,
    prompt: str = "Unlock local_scribe vault",
    on_touch_prompt: Optional[Callable[[str], None]] = None,
) -> secret_store.MasterKey:
    """Routine unlock: combine the Keychain half + the YubiKey half.

    Prompts the Touch ID sheet first (typically resolves in well under
    a second once the user authenticates), then asks for the YubiKey
    tap. We deliberately serialise the prompts so the user only sees
    one outstanding modal at a time — having both fire concurrently
    would confuse anyone who hasn't memorised the flow.

    Migration: if no v2 kc_half is present but a legacy v1 item is,
    we run the migration helper and then retry. This makes the first
    ``unlock`` after a code update Just Work.
    """
    if not secret_store.has_kc_half():
        if secret_store.has_master_key():
            logger.info("detected legacy v1 whole-key item; running migration")
            migrate_v1_to_v2(on_touch_prompt=on_touch_prompt)
        else:
            raise RuntimeError(
                "no master key on this machine — run `./run.sh key init`"
            )

    kc_half = secret_store.load_kc_half(prompt=prompt)
    try:
        yk_half = yubikey_backup.restore_yk_half(on_touch_prompt=on_touch_prompt)
    except yubikey_backup.YubiKeyError:
        # Wipe kc_half from the in-memory bytes object best-effort.
        try:
            kc_ba = bytearray(kc_half); key_split.zero_bytes(kc_ba)
        except Exception:  # noqa: BLE001
            pass
        raise

    try:
        master = key_split.combine_halves(kc_half, yk_half)
        return secret_store.MasterKey.from_bytes(master)
    finally:
        # Scrub the halves we just XOR'd; only the master should be
        # alive in the returned MasterKey buffer.
        try:
            kc_ba = bytearray(kc_half); key_split.zero_bytes(kc_ba)
            yk_ba = bytearray(yk_half); key_split.zero_bytes(yk_ba)
        except Exception:  # noqa: BLE001
            pass


# ---------- rotate ---------------------------------------------------


def rotate_master_key(
    *,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
    dr_passphrase: Optional[str] = None,
) -> RotationResult:
    """Generate a fresh master key, atomically replace both halves.

    Steps:

    1. Unlock the current key (Touch ID + YubiKey tap) → ``old``.
    2. Generate a new master key + split it.
    3. Write the new ``kc_half`` (replace-on-add at Keychain layer).
    4. Wrap the new ``yk_half`` to the existing recipient set (we
       reuse them so the caller doesn't have to re-enroll).
    5. If a DR passphrase is supplied, rewrite the DR file with the
       new master key. Otherwise leave the old DR file untouched —
       which means it still recovers the *old* master key. The
       caller should re-DR after rotating.

    Returns both keys so the immediate caller can transcribe the
    vault keybag from old → new before zeroing ``old``.
    """
    old = unlock_master_key(
        prompt="Unlock current master key (rotation)",
        on_touch_prompt=on_touch_prompt,
    )

    recipients = yubikey_backup.list_recipients()
    if not recipients:
        old.forget()
        raise yubikey_backup.YubiKeyNotEnrolledError(
            "no YubiKey recipients enrolled — rotation requires at least one"
        )

    sk = key_split.generate_split_key()
    try:
        secret_store.store_kc_half(sk.kc_half)
        yubikey_backup.backup_yk_half(sk.yk_half, recipients=recipients)
        if dr_passphrase is not None:
            disaster_recovery.encrypt(sk.master_key, dr_passphrase)
        new = secret_store.MasterKey.from_bytes(sk.master_key)
        return RotationResult(old=old, new=new)
    finally:
        try:
            ba = bytearray(sk.master_key); key_split.zero_bytes(ba)
            ba = bytearray(sk.kc_half);    key_split.zero_bytes(ba)
            ba = bytearray(sk.yk_half);    key_split.zero_bytes(ba)
        except Exception:  # noqa: BLE001
            pass


# ---------- add a second YubiKey -------------------------------------


def add_yubikey(
    new_recipient: str,
    *,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """Re-wrap ``yk_half`` so a second YubiKey (matching
    ``new_recipient``) can also decrypt it.

    To produce a multi-recipient ciphertext we need ``yk_half`` in
    cleartext — so this call prompts the user to insert the *current*
    YubiKey first, decrypts ``yk_half.age``, then re-encrypts to the
    union of ``current_recipients + [new]``. The new recipient is
    typically obtained out-of-band via ``age-plugin-yubikey --identity``
    on the second YubiKey (the inspector + ``./run.sh key
    add-yubikey`` walk the operator through that).

    Returns the updated recipient list. Idempotent: passing an
    already-enrolled recipient is a no-op.
    """
    if not new_recipient.startswith("age1yubikey1"):
        raise yubikey_backup.YubiKeyError(
            f"recipient doesn't look like a YubiKey recipient: "
            f"{new_recipient[:24]!r}"
        )
    yk_half = yubikey_backup.restore_yk_half(on_touch_prompt=on_touch_prompt)
    try:
        return yubikey_backup.add_recipient(new_recipient, yk_half=yk_half)
    finally:
        try:
            ba = bytearray(yk_half); key_split.zero_bytes(ba)
        except Exception:  # noqa: BLE001
            pass


# ---------- disaster recovery ----------------------------------------


def dr_restore(
    passphrase: str,
    *,
    re_init_yubikey: bool = True,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
) -> secret_store.MasterKey:
    """Recover the master key from the disaster-recovery file.

    Used when both YubiKeys are lost / dead: the user runs ``./run.sh
    key dr-restore``, types their DR passphrase, and we hand back a
    ``MasterKey`` that decrypts the vault. By default we *also*
    re-initialise the split-key flow:

    * Draw a fresh ``kc_half`` and store it.
    * Wrap a fresh ``yk_half`` (the XOR partner of the recovered
      master key, given the new kc_half).
    * Persist to the existing YubiKey recipient list (so the operator
      can still unlock once they have a key again).

    Passing ``re_init_yubikey=False`` skips that re-init (useful when
    you literally don't have a YubiKey to re-enroll yet; you can
    decrypt the vault but routine unlock won't work until you re-init).
    """
    master = disaster_recovery.decrypt(passphrase)
    if re_init_yubikey:
        recipients = yubikey_backup.list_recipients()
        if recipients:
            kc, yk = key_split.split_existing_key(master)
            try:
                secret_store.store_kc_half(kc)
                yubikey_backup.backup_yk_half(yk, recipients=recipients)
            finally:
                try:
                    ba = bytearray(kc); key_split.zero_bytes(ba)
                    ba = bytearray(yk); key_split.zero_bytes(ba)
                except Exception:  # noqa: BLE001
                    pass
    return secret_store.MasterKey.from_bytes(master)


# ---------- legacy v1 migration --------------------------------------


def migrate_v1_to_v2(
    *,
    on_touch_prompt: Optional[Callable[[str], None]] = None,
) -> None:
    """Walk a pre-Option-C install over to the new split-key shape.

    Order matters: we MUST write the new artefacts before deleting
    the old ones so a crash mid-migration leaves the user with a
    recoverable state. Concretely:

    1. Load the v1 whole-key item (Touch ID).
    2. Split it (``kc_half`` is random; ``yk_half = master XOR kc_half``).
    3. Store ``kc_half`` (new Keychain account).
    4. Wrap ``yk_half`` to the existing YubiKey recipient.
    5. Verify the round-trip recovers the same master key.
    6. Delete the legacy v1 Keychain item.

    If step 4 fails (no YubiKey), we leave both v1 + v2 items in
    place. The next ``unlock`` call retries the migration.
    """
    if secret_store.has_kc_half():
        logger.info("v2 kc_half already present; nothing to migrate")
        return
    if not secret_store.has_master_key():
        logger.info("no v1 whole-key item; nothing to migrate")
        return
    recipients = yubikey_backup.list_recipients()
    if not recipients:
        raise yubikey_backup.YubiKeyNotEnrolledError(
            "v1 item present but no YubiKey enrolled; "
            "run `./run.sh yubikey enroll` before migrating"
        )

    master = secret_store.load_master_key(prompt="Migrate to split-key (Touch ID)")
    try:
        kc, yk = key_split.split_existing_key(master)
        try:
            secret_store.store_kc_half(kc)
            yubikey_backup.backup_yk_half(yk, recipients=recipients)
            # Sanity: round-trip-verify before deleting the legacy item.
            # NOTE: this triggers the YubiKey-tap prompt a second time;
            # the alternative would be to trust the encrypt-decrypt
            # algebra without a real round-trip, which is fine when
            # the age plugin is healthy but I'd rather catch a busted
            # YubiKey *before* deleting the legacy item.
            yk_round = yubikey_backup.restore_yk_half(on_touch_prompt=on_touch_prompt)
            try:
                recovered = key_split.combine_halves(kc, yk_round)
                if recovered != master:
                    raise RuntimeError(
                        "split-key round-trip verification failed; "
                        "v1 item left in place"
                    )
            finally:
                try:
                    ba = bytearray(yk_round); key_split.zero_bytes(ba)
                except Exception:  # noqa: BLE001
                    pass
            secret_store.delete_master_key()
            logger.info("migration to split-key complete; legacy item deleted")
        finally:
            try:
                ba = bytearray(kc); key_split.zero_bytes(ba)
                ba = bytearray(yk); key_split.zero_bytes(ba)
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            ba = bytearray(master); key_split.zero_bytes(ba)
        except Exception:  # noqa: BLE001
            pass


# ---------- status (no Touch ID / no YubiKey tap) --------------------


def status() -> dict:
    """JSON-safe snapshot, no prompts. Used by ``./run.sh status``,
    the inspector UI, and ``char_audit``."""
    return {
        "shape": "v2_split" if secret_store.has_kc_half() else
                 ("v1_legacy" if secret_store.has_master_key() else "uninitialised"),
        "kc_half_present": secret_store.has_kc_half(),
        "legacy_v1_present": secret_store.has_master_key(),
        "yubikey": yubikey_backup.status(),
        "disaster_recovery": disaster_recovery.status(),
    }


__all__ = [
    "InitResult",
    "RotationResult",
    "init_master_key",
    "unlock_master_key",
    "rotate_master_key",
    "add_yubikey",
    "dr_restore",
    "migrate_v1_to_v2",
    "status",
]


# ---------- CLI entry points -----------------------------------------
#
# ``run.sh key …`` invokes these via ``python -m key_lifecycle <cmd>``
# so passphrases / keys can be piped over stdin without leaking into
# argv or the environment. Bash heredocs can't co-exist with piped
# stdin (the heredoc wins), so we keep the heredoc-as-Python-source
# pattern *out* of ``run.sh`` and put the Python in this module.
#
# Output is always a single JSON document on stdout (so callers can
# parse it) plus optional human-readable status on stderr. Exit codes:
#   0  success
#   1  generic failure (Python exception, etc.)
#   2  usage error / bad subcommand


def _cli_status(_args: list[str]) -> int:
    import json as _json
    import sys as _sys
    _sys.stdout.write(_json.dumps(status(), indent=2) + "\n")
    return 0


def _cli_init(args: list[str]) -> int:
    """``init [--no-dr] [--force]`` — read the DR passphrase (if any)
    from stdin (single line, no trailing newline expected). Empty
    stdin => no DR passphrase. The passphrase never appears in argv
    or in the environment."""
    import json as _json
    import sys as _sys
    no_dr = "--no-dr" in args
    force = "--force" in args
    enroll = "--no-enroll" not in args
    passphrase: Optional[str] = None
    if not no_dr:
        # Read all of stdin (blocks if nothing is piped in). The empty
        # string is allowed and means "no DR backup".
        raw = _sys.stdin.read()
        # Strip ONLY a trailing newline (the operator's terminal
        # ``read -s`` doesn't add one but printf '%s' is even safer).
        if raw.endswith("\n"):
            raw = raw[:-1]
        passphrase = raw or None
    result = init_master_key(
        dr_passphrase=passphrase,
        enroll_yubikey=enroll,
        force=force,
    )
    _sys.stdout.write(_json.dumps({
        "kc_half_stored": result.kc_half_stored,
        "yk_half_wrapped": result.yk_half_wrapped,
        "dr_backup_written": result.dr_backup_written,
        "recipient": result.recipient,
    }, indent=2) + "\n")
    return 0


def _cli_unlock(_args: list[str]) -> int:
    """``unlock`` — smoke-test the unlock path. Prints the service-token
    *fingerprints* for every known service. Never prints the master
    key or full tokens."""
    import json as _json
    import sys as _sys
    import service_auth
    mk = unlock_master_key(prompt="Unlock local_scribe (CLI smoke test)")
    try:
        fps = {
            svc: service_auth.token_fingerprint(
                service_auth.derive_service_token(mk.as_bytes(), svc))
            for svc in service_auth.KNOWN_SERVICES
        }
    finally:
        mk.forget()
    _sys.stdout.write(_json.dumps({"unlocked": True, "fingerprints": fps}, indent=2) + "\n")
    return 0


def _cli_rotate(_args: list[str]) -> int:
    import json as _json
    import sys as _sys
    result = rotate_master_key()
    try:
        # Show 4-byte fingerprints (8 hex chars), never the keys.
        old_fp = result.old.as_bytes()[:4].hex()
        new_fp = result.new.as_bytes()[:4].hex()
    finally:
        result.old.forget()
        result.new.forget()
    _sys.stdout.write(_json.dumps({
        "old_fingerprint": old_fp,
        "new_fingerprint": new_fp,
        "kc_half_stored": True,
        "yk_half_rewrapped": True,
    }, indent=2) + "\n")
    return 0


def _cli_add_yubikey(args: list[str]) -> int:
    import json as _json
    import sys as _sys
    if len(args) != 1:
        _sys.stderr.write("usage: python -m key_lifecycle add-yubikey age1yubikey1...\n")
        return 2
    recipients = add_yubikey(args[0])
    _sys.stdout.write(_json.dumps({"recipients": recipients}, indent=2) + "\n")
    return 0


def _cli_dr_restore(args: list[str]) -> int:
    """``dr-restore [--no-reinit]`` — reads the DR passphrase from
    stdin and recovers the master key. Re-initialises the split-key
    flow by default; pass ``--no-reinit`` to skip (e.g. when no
    YubiKey is available to enroll)."""
    import json as _json
    import sys as _sys
    import service_auth
    reinit = "--no-reinit" not in args
    raw = _sys.stdin.read()
    if raw.endswith("\n"):
        raw = raw[:-1]
    if not raw:
        _sys.stderr.write("error: passphrase required on stdin\n")
        return 2
    mk = dr_restore(raw, re_init_yubikey=reinit)
    try:
        fps = {
            svc: service_auth.token_fingerprint(
                service_auth.derive_service_token(mk.as_bytes(), svc))
            for svc in service_auth.KNOWN_SERVICES
        }
    finally:
        mk.forget()
    _sys.stdout.write(_json.dumps({
        "recovered": True,
        "fingerprints": fps,
        "re_initialised": reinit,
    }, indent=2) + "\n")
    return 0


def _cli_migrate(_args: list[str]) -> int:
    import json as _json
    import sys as _sys
    migrate_v1_to_v2()
    _sys.stdout.write(_json.dumps({"migrated": True, "status": status()}, indent=2) + "\n")
    return 0


def _cli_destroy(_args: list[str]) -> int:
    """``destroy`` — delete every key artefact. Idempotent; doesn't
    prompt. ``./run.sh key destroy`` does the user-facing
    confirmation; this CLI is the underlying mechanism."""
    import json as _json
    import sys as _sys
    import secret_store
    import yubikey_backup
    import disaster_recovery
    secret_store.delete_kc_half()
    secret_store.delete_master_key()  # legacy v1
    yubikey_backup.disable()
    disaster_recovery.disable()
    _sys.stdout.write(_json.dumps({"destroyed": True}, indent=2) + "\n")
    return 0


def _cli_main(argv: list[str]) -> int:
    import sys as _sys
    table = {
        "status":       _cli_status,
        "init":         _cli_init,
        "unlock":       _cli_unlock,
        "rotate":       _cli_rotate,
        "add-yubikey":  _cli_add_yubikey,
        "add_yubikey":  _cli_add_yubikey,
        "dr-restore":   _cli_dr_restore,
        "dr_restore":   _cli_dr_restore,
        "migrate":      _cli_migrate,
        "destroy":      _cli_destroy,
    }
    if len(argv) < 2:
        _sys.stderr.write(f"usage: {argv[0]} <{'|'.join(sorted(set(table)))}> [args]\n")
        return 2
    cmd = argv[1]
    fn = table.get(cmd)
    if fn is None:
        _sys.stderr.write(f"unknown subcommand: {cmd}\n")
        return 2
    try:
        return fn(argv[2:])
    except Exception as exc:  # noqa: BLE001
        # Surface a one-line typed error on stderr; full traceback on
        # stderr only if LOCAL_SCRIBE_DEBUG=1 (callers reading stdout
        # for JSON must not see a Python traceback).
        import os as _os
        _sys.stderr.write(f"error ({type(exc).__name__}): {exc}\n")
        if _os.environ.get("LOCAL_SCRIBE_DEBUG"):
            import traceback
            traceback.print_exc(file=_sys.stderr)
        return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli_main(_sys.argv))
