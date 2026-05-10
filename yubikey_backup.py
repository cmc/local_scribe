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
RECIPIENT_PATH = CONFIG_DIR / "yubikey_recipient.txt"
BACKUP_PATH = CONFIG_DIR / "key_backup.age"

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
    """Resolve a CLI tool against PATH. Returns None if not found."""
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
    # comment in the identity file -- parse both as defence in depth.
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
# Encrypt + decrypt

def backup_key(key: bytes) -> Path:
    """Encrypt ``key`` to the enrolled YubiKey recipient and persist it
    at ``BACKUP_PATH``. This call does *not* require a YubiKey touch —
    encryption is asymmetric, only decryption needs the hardware."""
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
    cmd = ["age", "-r", recipient, "-o", str(BACKUP_PATH)]
    proc = subprocess.run(cmd, input=key, capture_output=True, timeout=30)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise YubiKeyError(
            f"age encrypt failed (rc={proc.returncode}): {stderr or '<no stderr>'}"
        )
    try:
        os.chmod(BACKUP_PATH, 0o600)
    except OSError:
        pass
    logger.info("master key backup written to %s (%d bytes)",
                BACKUP_PATH, BACKUP_PATH.stat().st_size)
    return BACKUP_PATH


def restore_key(*, on_touch_prompt=None) -> bytes:
    """Decrypt the on-disk backup. Requires the enrolled YubiKey to be
    inserted, and (with the default ``touch-policy=always``) for the
    user to tap it once during the call.

    ``on_touch_prompt`` is an optional callable: when ``age-plugin-yubikey``
    writes "Please touch your YubiKey" to stderr we forward it (and
    any user-visible context) here so a CLI / GUI caller can render its
    own prompt UI. If None, we just print the plugin's stderr verbatim
    to our own stderr.
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

    cmd = ["age", "-d", "-i", str(IDENTITY_PATH), str(BACKUP_PATH)]
    # We pipe stderr through Popen.communicate so we can surface the
    # plugin's touch-prompt message in real time. timeout is generous
    # because the user may need a moment to physically touch the key.
    try:
        with subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            # No streaming UI for now (would block indefinitely on the
            # touch prompt). The plugin's stderr is captured + dumped
            # after the fact. Tests assert on the on_touch_prompt path
            # by injecting a fake age.
            if on_touch_prompt is not None:
                # Give the caller a chance to render a UI hint BEFORE
                # the touch becomes necessary. The plugin sometimes
                # holds the message until just before the touch.
                try:
                    on_touch_prompt(
                        "Please touch your YubiKey to decrypt the "
                        "master-key backup."
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

    if len(stdout) != 32:
        raise YubiKeyError(
            f"decrypted backup is {len(stdout)} bytes, expected 32"
        )
    return stdout


# ---------------------------------------------------------------------------
# Disable / forget

def disable() -> None:
    """Remove the on-disk enrollment artefacts. Doesn't touch the YubiKey
    itself -- the user can run ``ykman piv keys delete`` separately to
    wipe the slot if they want."""
    for p in (IDENTITY_PATH, RECIPIENT_PATH, BACKUP_PATH):
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
    return {
        "tools": {name: bool(path) for name, path in required_tools_present().items()},
        "yubikey_present": is_yubikey_present(),
        "enrolled": is_enrolled(),
        "recipient": info.recipient if info else None,
        "serial": info.serial if info else None,
        "slot": info.slot if info else None,
        "identity_path": str(IDENTITY_PATH),
        "backup_path": str(BACKUP_PATH),
        "backup_size_bytes": (BACKUP_PATH.stat().st_size
                              if BACKUP_PATH.is_file() else 0),
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
    "required_tools_present",
    "assert_tools",
    "is_yubikey_present",
    "is_enrolled",
    "enrollment_info",
    "enroll",
    "backup_key",
    "restore_key",
    "disable",
    "status",
]
