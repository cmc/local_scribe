"""YubiKey-protected backup of the local_scribe master key.

Wraps ``age`` + ``age-plugin-yubikey`` so the user can keep a recovery
copy of the Keychain master key on disk in a form that's only decryptable
by physically touching their enrolled YubiKey.

Recovery flow
-------------

If the user's Mac dies / the Keychain item is deleted / Touch ID enroll-
ment is wiped, the YubiKey backup is the *only* way to recover the
master key without paying the price of losing every transcript.

Enrollment writes:

  ~/.config/local_scribe/yubikey_identity.txt    age identity stub
  ~/.config/local_scribe/yubikey_recipient.txt   age recipient string
  ~/.config/local_scribe/key_backup.age          encrypted master key

The identity stub itself is *not* a secret — it just points at a slot
on the YubiKey's PIV applet. Without the physical YubiKey it's useless.
The recipient is fully public.

Touch policy
------------

We enroll with ``--touch-policy always`` so every decryption requires a
fresh physical touch. ``--pin-policy never`` is the default; we leave
the PIN flow out of scope to avoid needing pinentry. (Touch is sufficient
for "physical user presence" -- the threat model assumes the user has
custody of the YubiKey; if they don't, the PIN wouldn't save them.)

Slot policy
-----------

Slot 1 (a.k.a. PIV slot 9a, "Authentication") by default. Operators can
override via ``LOCAL_SCRIBE_YUBIKEY_SLOT`` if their key is already in
use for SSH / GPG / FIDO -- the plugin supports slots 1-20.

External tooling
----------------

* ``age`` — github.com/FiloSottile/age. Homebrew: ``brew install age``.
* ``age-plugin-yubikey`` — github.com/str4d/age-plugin-yubikey. Homebrew:
  ``brew install age-plugin-yubikey``.
* ``ykman`` — yubico/yubikey-manager. Used only for connectivity probes.
  Homebrew: ``brew install ykman``.

``./run.sh bootstrap`` installs all three on a fresh Mac.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger("local_scribe.yubikey_backup")


CONFIG_DIR = Path(os.environ.get("LOCAL_SCRIBE_CONFIG_DIR")
                  or Path.home() / ".config" / "local_scribe")
IDENTITY_PATH = CONFIG_DIR / "yubikey_identity.txt"
# Single-recipient file kept for legacy (v1 whole-key) backups; the new
# split-key flow uses ``RECIPIENTS_PATH`` (multi-line) so a second
# YubiKey can be added without re-running enroll on the primary.
RECIPIENT_PATH = CONFIG_DIR / "yubikey_recipient.txt"
RECIPIENTS_PATH = CONFIG_DIR / "yubikey_recipients.txt"
# Legacy v1 backup: whole 32-byte master key, encrypted to one
# recipient. Kept readable for the migration path.
BACKUP_PATH = CONFIG_DIR / "key_backup.age"
# Option C (v2) split-key half: 32 bytes of the XOR-split master key,
# encrypted to one or more YubiKey recipients (multi-recipient age
# files decrypt cleanly with any of the enrolled YubiKeys).
YK_HALF_PATH = CONFIG_DIR / "yk_half.age"

# Defaults selected for low-ceremony recovery UX:
#   - slot 1 (PIV 9a, "Authentication") is usually idle on a fresh YubiKey
#   - pin-policy=never  → no pinentry needed
#   - touch-policy=always → every decrypt requires a fresh tap
DEFAULT_SLOT = int(os.environ.get("LOCAL_SCRIBE_YUBIKEY_SLOT") or "1")
DEFAULT_PIN_POLICY = os.environ.get("LOCAL_SCRIBE_YUBIKEY_PIN_POLICY") or "never"
DEFAULT_TOUCH_POLICY = os.environ.get("LOCAL_SCRIBE_YUBIKEY_TOUCH_POLICY") or "always"
DEFAULT_NAME = os.environ.get("LOCAL_SCRIBE_YUBIKEY_NAME") or "local_scribe"


# ---------------------------------------------------------------------------
# Exceptions

class YubiKeyError(Exception):
    """Generic backup failure."""


class YubiKeyNotPresentError(YubiKeyError):
    """No YubiKey detected on the USB bus."""


class YubiKeyNotEnrolledError(YubiKeyError):
    """No enrollment artefacts on disk. Run ``./run.sh yubikey enroll``."""


class YubiKeyTouchTimeoutError(YubiKeyError):
    """The user didn't tap the key within the prompt window. Subclass
    so callers can re-prompt vs. surface as a hard error."""


class ExternalToolMissingError(YubiKeyError):
    """One of ``age`` / ``age-plugin-yubikey`` / ``ykman`` is not on
    PATH. Run ``./run.sh bootstrap`` to install."""


# ---------------------------------------------------------------------------
# Probes

def _which(name: str) -> Optional[str]:
    """Resolve a CLI tool against PATH. Returns None if not found.

    For ``age`` and ``age-plugin-yubikey`` we honour
    ``LOCAL_SCRIBE_AGE_BIN`` / ``LOCAL_SCRIBE_AGE_PLUGIN_BIN`` env
    overrides first so the test suite can swap in a hermetic shim that
    doesn't require a physical YubiKey. The override path must be
    absolute and executable.
    """
    override_env = {
        "age": "LOCAL_SCRIBE_AGE_BIN",
        "age-plugin-yubikey": "LOCAL_SCRIBE_AGE_PLUGIN_BIN",
        "ykman": "LOCAL_SCRIBE_YKMAN_BIN",
    }.get(name)
    if override_env:
        override = os.environ.get(override_env)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
    return shutil.which(name)


def required_tools_present() -> dict[str, Optional[str]]:
    """Map of tool name -> path (or None). Used by ``./run.sh doctor``."""
    return {
        "age": _which("age"),
        "age-plugin-yubikey": _which("age-plugin-yubikey"),
        "ykman": _which("ykman"),
    }


def assert_tools() -> None:
    """Raise ``ExternalToolMissingError`` if any required tool is missing."""
    tools = required_tools_present()
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise ExternalToolMissingError(
            "missing CLI tools: "
            + ", ".join(missing)
            + " — run `./run.sh bootstrap` or "
            + "`brew install " + " ".join(missing) + "`"
        )


def is_yubikey_present() -> bool:
    """True if ``ykman list`` sees a YubiKey on USB. Doesn't require an
    enrolled slot; just connectivity."""
    yk = _which("ykman")
    if yk is None:
        return False
    try:
        proc = subprocess.run(
            [yk, "list"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def is_enrolled() -> bool:
    """All three enrollment artefacts present and non-empty."""
    for p in (IDENTITY_PATH, RECIPIENT_PATH, BACKUP_PATH):
        try:
            if p.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


@dataclass
class EnrollmentInfo:
    recipient: str
    identity_path: Path
    backup_path: Path
    slot: int
    serial: Optional[str]


def enrollment_info() -> Optional[EnrollmentInfo]:
    """Best-effort summary used by ``./run.sh status`` + the inspector."""
    if not is_enrolled():
        return None
    try:
        recipient = RECIPIENT_PATH.read_text().strip()
    except OSError:
        return None
    serial = None
    # The identity file format is a header comment block followed by the
    # AGE-PLUGIN-YUBIKEY-... line. Serial / slot are in the header.
    try:
        for line in IDENTITY_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("# Serial:"):
                serial = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return EnrollmentInfo(
        recipient=recipient,
        identity_path=IDENTITY_PATH,
        backup_path=BACKUP_PATH,
        slot=DEFAULT_SLOT,
        serial=serial,
    )


# ---------------------------------------------------------------------------
# Enrollment

def enroll(*, slot: int = DEFAULT_SLOT,
           pin_policy: str = DEFAULT_PIN_POLICY,
           touch_policy: str = DEFAULT_TOUCH_POLICY,
           name: str = DEFAULT_NAME,
           force: bool = False) -> EnrollmentInfo:
    """Generate (or re-use) an age identity on the YubiKey's PIV slot.

    ``force=True`` regenerates even if there's existing enrollment data;
    we set this when the user runs ``./run.sh yubikey enroll`` from the
    CLI explicitly. ``force=False`` (used by automatic bootstrap) skips
    if existing data is present, so re-runs don't burn a slot.

    The call writes the recipient to disk; ``backup_key()`` is a
    separate step (we'd typically chain them, but they're factored apart
    so a YubiKey rotation can re-enroll without re-encrypting the key
    in the same call).
    """
    assert_tools()
    if not is_yubikey_present():
        raise YubiKeyNotPresentError(
            "no YubiKey detected — insert your YubiKey into a USB port "
            "and try again"
        )
    if is_enrolled() and not force:
        info = enrollment_info()
        if info is not None:
            logger.info("YubiKey already enrolled (slot=%d, serial=%s); "
                        "skipping (pass force=True to re-enroll)",
                        info.slot, info.serial or "?")
            return info

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass

    # The plugin writes the identity stub to stdout when --identity-output
    # is "-", and the recipient is printed to stderr. We always pin the
    # output to our config path so the file lives where we expect.
    cmd = [
        "age-plugin-yubikey",
        "--generate",
        "--slot", str(slot),
        "--pin-policy", pin_policy,
        "--touch-policy", touch_policy,
        "--name", name,
        "--identity-output", str(IDENTITY_PATH),
    ]
    logger.info("enrolling YubiKey: slot=%d touch=%s pin=%s name=%s",
                slot, touch_policy, pin_policy, name)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise YubiKeyError(
            "age-plugin-yubikey --generate failed "
            f"(rc={proc.returncode}): {stderr or '<no stderr>'}"
        )
    # The plugin emits the recipient on its own line beginning with
    # ``age1yubikey1...``. It also writes a parallel ``# Recipient: ...``
    # comment in the identity file -- parse both as defense in depth.
    recipient = _extract_recipient(proc.stdout, proc.stderr) \
        or _read_recipient_from_identity(IDENTITY_PATH)
    if not recipient:
        raise YubiKeyError(
            "couldn't determine age recipient from age-plugin-yubikey output"
        )
    RECIPIENT_PATH.write_text(recipient.strip() + "\n")
    try:
        os.chmod(RECIPIENT_PATH, 0o600)
        os.chmod(IDENTITY_PATH, 0o600)
    except OSError:
        pass
    logger.info("YubiKey enrolled; recipient written to %s", RECIPIENT_PATH)
    return enrollment_info()  # type: ignore[return-value]


def _extract_recipient(*streams: str) -> Optional[str]:
    """Find the ``age1yubikey1...`` line in tool output. Handles a few
    different plugin-version output variations defensively."""
    for s in streams:
        if not s:
            continue
        for line in s.splitlines():
            line = line.strip()
            if line.startswith("age1yubikey1"):
                return line
            if line.startswith("Recipient: age1yubikey1"):
                return line.split("Recipient:", 1)[1].strip()
    return None


def _read_recipient_from_identity(path: Path) -> Optional[str]:
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# Recipient:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# age encrypt / decrypt helpers
#
# Both the legacy v1 ``backup_key()`` and the Option C ``backup_yk_half()``
# end up shelling out to ``age`` with the same flags; the only differences
# are the output path and (for the new path) supporting an N-recipient
# encrypt so a secondary YubiKey can decrypt the same file. Factoring out
# the common code keeps the touch-prompt handling + timeout logic in one
# place.


def _age_encrypt(payload: bytes, *, recipients: list[str], out_path: Path) -> None:
    """Encrypt ``payload`` to ``recipients`` (one ``-r`` flag each) and
    write the armoured (binary) age file to ``out_path``. No YubiKey
    touch required — encryption is asymmetric.

    Each recipient must already look like an age recipient; we don't
    enforce the ``age1yubikey1`` prefix here so this helper can also
    be reused for future X25519 recipients (e.g. operator paper
    backups) without bifurcating the code.
    """
    if not recipients:
        raise YubiKeyError("no recipients given — refusing to encrypt to no one")
    age = _which("age")
    if not age:
        raise ExternalToolMissingError(
            "missing CLI tool: age — run `./run.sh bootstrap`"
        )
    cmd = [age]
    for r in recipients:
        cmd.extend(["-r", r])
    cmd.extend(["-o", str(out_path)])
    proc = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise YubiKeyError(
            f"age encrypt failed (rc={proc.returncode}): {stderr or '<no stderr>'}"
        )
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass


def _age_decrypt(*, identity: Path, src: Path, expected_len: int,
                 on_touch_prompt=None, label: str = "backup") -> bytes:
    """Decrypt ``src`` using ``identity`` (a YubiKey-plugin age
    identity file). Returns the cleartext bytes; raises
    ``YubiKeyTouchTimeoutError`` if the user doesn't tap within
    60 s. ``expected_len`` is used for a length sanity check so we
    don't return obviously-truncated data to callers."""
    age = _which("age")
    if not age:
        raise ExternalToolMissingError(
            "missing CLI tool: age — run `./run.sh bootstrap`"
        )
    cmd = [age, "-d", "-i", str(identity), str(src)]
    try:
        with subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            if on_touch_prompt is not None:
                try:
                    on_touch_prompt(
                        f"Please touch your YubiKey to decrypt the "
                        f"local_scribe {label}."
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                stdout, stderr = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise YubiKeyTouchTimeoutError(
                    "YubiKey touch not received within 60 seconds"
                ) from exc
            rc = proc.returncode
    except FileNotFoundError as exc:
        raise ExternalToolMissingError(str(exc)) from exc

    if rc != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise YubiKeyError(f"age decrypt failed (rc={rc}): {msg or '<no stderr>'}")

    if len(stdout) != expected_len:
        raise YubiKeyError(
            f"decrypted {label} is {len(stdout)} bytes, expected {expected_len}"
        )
    return stdout


# ---------------------------------------------------------------------------
# Legacy v1 whole-key backup (kept for migration / tests)

def backup_key(key: bytes) -> Path:
    """Encrypt the **whole** master key to the enrolled YubiKey
    recipient. Used by the legacy v1 path and by the migration script
    to surface the existing whole key into the new split-key world.

    New installs do not call this. The Option C path encrypts only
    ``yk_half`` (see :func:`backup_yk_half`); the master key is never
    persisted on disk.
    """
    assert_tools()
    if not RECIPIENT_PATH.is_file():
        raise YubiKeyNotEnrolledError(
            f"no recipient file at {RECIPIENT_PATH} — "
            "run `./run.sh yubikey enroll` first"
        )
    recipient = RECIPIENT_PATH.read_text().strip()
    if not recipient.startswith("age1yubikey1"):
        raise YubiKeyError(
            f"recipient at {RECIPIENT_PATH} doesn't look like a "
            f"yubikey recipient (starts with {recipient[:20]!r})"
        )
    _age_encrypt(key, recipients=[recipient], out_path=BACKUP_PATH)
    logger.info("master key backup written to %s (%d bytes)",
                BACKUP_PATH, BACKUP_PATH.stat().st_size)
    return BACKUP_PATH


def restore_key(*, on_touch_prompt=None) -> bytes:
    """Decrypt the legacy v1 on-disk whole-key backup. Requires the
    enrolled YubiKey to be inserted; with ``touch-policy=always`` the
    user must tap it once during the call.
    """
    assert_tools()
    if not is_enrolled():
        raise YubiKeyNotEnrolledError(
            "YubiKey is not enrolled — run `./run.sh yubikey enroll`"
        )
    if not is_yubikey_present():
        raise YubiKeyNotPresentError(
            "no YubiKey detected — insert your YubiKey before retrying"
        )
    return _age_decrypt(
        identity=IDENTITY_PATH,
        src=BACKUP_PATH,
        expected_len=32,
        on_touch_prompt=on_touch_prompt,
        label="master-key backup",
    )


# ---------------------------------------------------------------------------
# Option C: yk_half wrapping + multi-recipient enrollment

def list_recipients() -> list[str]:
    """Return the list of YubiKey recipients currently enrolled for
    ``yk_half``. Reads from ``RECIPIENTS_PATH`` if present (one
    recipient per line, blank lines + ``#`` comments ignored); falls
    back to ``RECIPIENT_PATH`` (the legacy single-recipient file) so a
    pre-Option-C enrollment can still encrypt to one YubiKey before
    the user runs ``./run.sh key add-yubikey``.
    """
    if RECIPIENTS_PATH.is_file():
        out: list[str] = []
        for line in RECIPIENTS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        if out:
            return out
    if RECIPIENT_PATH.is_file():
        r = RECIPIENT_PATH.read_text().strip()
        if r:
            return [r]
    return []


def _write_recipients(recipients: list[str]) -> None:
    """Persist the recipient set to ``RECIPIENTS_PATH``. Deduplicates
    while preserving insertion order so the primary YubiKey stays
    first (lets us show "primary: ..., backup: ..." in the inspector).
    """
    if not recipients:
        raise YubiKeyError("refusing to write an empty recipient list")
    seen: set[str] = set()
    deduped: list[str] = []
    for r in recipients:
        r = r.strip()
        if not r or r in seen:
            continue
        seen.add(r)
        deduped.append(r)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# local_scribe YubiKey recipients (one per line).\n"
        "# These are PUBLIC keys; they only let `age` encrypt to the\n"
        "# matching YubiKey slots. Decryption still requires the\n"
        "# physical YubiKey to be inserted + tapped.\n"
    )
    RECIPIENTS_PATH.write_text(header + "\n".join(deduped) + "\n")
    try:
        os.chmod(RECIPIENTS_PATH, 0o600)
    except OSError:
        pass


def set_recipients(recipients: list[str]) -> None:
    """Replace the recipient set entirely (used by tests + ``./run.sh
    key reset-recipients``). The caller is responsible for re-wrapping
    ``yk_half`` afterwards so the new recipient set actually controls
    decryption."""
    _write_recipients(recipients)


def add_recipient(new_recipient: str, *, yk_half: bytes) -> list[str]:
    """Add ``new_recipient`` to the multi-recipient list and re-wrap
    ``yk_half`` so *any* enrolled YubiKey can decrypt the file. The
    caller passes the cleartext ``yk_half`` because adding a recipient
    requires re-encrypting the payload to ``old_recipients + [new]``.

    Returns the updated recipient list.
    """
    if not new_recipient.startswith("age1yubikey1"):
        raise YubiKeyError(
            f"recipient doesn't look like a YubiKey recipient: {new_recipient[:24]!r}"
        )
    recipients = list_recipients()
    if new_recipient in recipients:
        logger.info("recipient already enrolled: %s", new_recipient[:20])
        return recipients
    recipients.append(new_recipient)
    _write_recipients(recipients)
    _age_encrypt(yk_half, recipients=recipients, out_path=YK_HALF_PATH)
    logger.info("added YubiKey recipient (%d total); yk_half re-wrapped",
                len(recipients))
    return recipients


def backup_yk_half(yk_half: bytes, *, recipients: Optional[list[str]] = None) -> Path:
    """Encrypt the 32-byte ``yk_half`` to all enrolled YubiKey
    recipients and persist it at ``YK_HALF_PATH``. ``recipients``
    overrides the on-disk list (used during init when we want to
    write the recipients file *after* a successful encrypt so a
    half-finished state doesn't leave dangling files).
    """
    assert_tools()
    if len(yk_half) != 32:
        raise ValueError(f"yk_half must be 32 bytes, got {len(yk_half)}")
    rs = recipients if recipients is not None else list_recipients()
    if not rs:
        raise YubiKeyNotEnrolledError(
            "no YubiKey recipients on disk — run `./run.sh key init` "
            "or `./run.sh yubikey enroll` first"
        )
    for r in rs:
        if not r.startswith("age1yubikey1"):
            raise YubiKeyError(
                f"recipient doesn't look like a YubiKey recipient: {r[:24]!r}"
            )
    _age_encrypt(yk_half, recipients=rs, out_path=YK_HALF_PATH)
    if recipients is not None:
        _write_recipients(recipients)
    logger.info("yk_half written to %s (%d bytes, %d recipients)",
                YK_HALF_PATH, YK_HALF_PATH.stat().st_size, len(rs))
    return YK_HALF_PATH


def restore_yk_half(*, on_touch_prompt=None) -> bytes:
    """Decrypt the on-disk ``yk_half`` using the inserted YubiKey.
    Returns 32 bytes; raises ``YubiKeyTouchTimeoutError`` if the user
    doesn't tap within the timeout.
    """
    assert_tools()
    if not is_yubikey_present():
        raise YubiKeyNotPresentError(
            "no YubiKey detected — insert your YubiKey before retrying"
        )
    if not IDENTITY_PATH.is_file():
        raise YubiKeyNotEnrolledError(
            f"no identity file at {IDENTITY_PATH} — run `./run.sh key init`"
        )
    if not YK_HALF_PATH.is_file():
        raise YubiKeyNotEnrolledError(
            f"no yk_half ciphertext at {YK_HALF_PATH} — run `./run.sh key init`"
        )
    return _age_decrypt(
        identity=IDENTITY_PATH,
        src=YK_HALF_PATH,
        expected_len=32,
        on_touch_prompt=on_touch_prompt,
        label="key half",
    )


def has_yk_half() -> bool:
    """``True`` if the YubiKey-encrypted ``yk_half`` ciphertext is on
    disk. Used by status checks; does *not* attempt decryption."""
    try:
        return YK_HALF_PATH.stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Disable / forget

def disable() -> None:
    """Remove the on-disk enrollment artefacts. Doesn't touch the YubiKey
    itself -- the user can run ``ykman piv keys delete`` separately to
    wipe the slot if they want.

    Covers BOTH the legacy v1 file set (``IDENTITY_PATH``,
    ``RECIPIENT_PATH``, ``BACKUP_PATH``) and the v2 split-key set
    (``RECIPIENTS_PATH``, ``YK_HALF_PATH``). Missing :func:`disable`
    coverage of the v2 files was a real safety bug: ``./run.sh key
    destroy`` would leave ``yk_half.age`` on disk, which paired with
    a leaked kc_half is enough to reconstruct the master key. Now
    every on-disk yubikey artefact is removed.
    """
    for p in (
        IDENTITY_PATH,
        RECIPIENT_PATH,
        RECIPIENTS_PATH,
        BACKUP_PATH,
        YK_HALF_PATH,
    ):
        try:
            p.unlink()
            logger.info("removed %s", p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove %s: %s", p, exc)


def status() -> dict:
    """JSON-safe status snapshot for diagnostics + the inspector UI."""
    info = enrollment_info()
    recipients = list_recipients()
    return {
        "tools": {name: bool(path) for name, path in required_tools_present().items()},
        "yubikey_present": is_yubikey_present(),
        "enrolled": is_enrolled(),
        "recipient": info.recipient if info else None,
        "recipients": recipients,
        "recipient_count": len(recipients),
        "serial": info.serial if info else None,
        "slot": info.slot if info else None,
        "identity_path": str(IDENTITY_PATH),
        "backup_path": str(BACKUP_PATH),
        "yk_half_path": str(YK_HALF_PATH),
        "yk_half_present": has_yk_half(),
        "backup_size_bytes": (BACKUP_PATH.stat().st_size
                              if BACKUP_PATH.is_file() else 0),
        "yk_half_size_bytes": (YK_HALF_PATH.stat().st_size
                               if YK_HALF_PATH.is_file() else 0),
    }


__all__ = [
    "YubiKeyError",
    "YubiKeyNotPresentError",
    "YubiKeyNotEnrolledError",
    "YubiKeyTouchTimeoutError",
    "ExternalToolMissingError",
    "EnrollmentInfo",
    "BACKUP_PATH",
    "IDENTITY_PATH",
    "RECIPIENT_PATH",
    "RECIPIENTS_PATH",
    "YK_HALF_PATH",
    "required_tools_present",
    "assert_tools",
    "is_yubikey_present",
    "is_enrolled",
    "enrollment_info",
    "enroll",
    "backup_key",
    "restore_key",
    "list_recipients",
    "set_recipients",
    "add_recipient",
    "backup_yk_half",
    "restore_yk_half",
    "has_yk_half",
    "disable",
    "status",
]
