"""Operator-signed configuration files.

Adds a Defense layer on top of script_integrity: an HMAC-SHA256 over
the file bytes, keyed by an HKDF-domain-separated subkey of the master
key (which itself requires Touch ID + YubiKey to reconstitute — see
``local_scribe.security.key_lifecycle``). This is the operator's
*explicit* trust statement about a configuration file — "yes, I have
reviewed this and authorise the server to use it" — and it sits
alongside the git-baseline integrity check that ``script_integrity``
provides.

Threat model the layer addresses:

* A local attacker (malware running as the operator, a script that
  slipped through bootstrap, a compromised editor extension) flips a
  byte in a pinned-config file — say, changing the expected Char DMG
  SHA-256 to a malicious build's hash, or weakening the expected
  ``PINNED_TEAM_ID``. The git-tracked baseline (``script_integrity``)
  would catch this *if* the file is git-tracked AND HEAD hasn't moved.
  This layer catches it independently: the HMAC is keyed by something
  the attacker doesn't have (the master key, which requires a YubiKey
  tap), so they can't re-sign.
* A supply-chain rewrite — someone pushes a malicious commit upstream
  that bumps the pinned hashes, the operator pulls without reading
  the diff. Git baseline updates as soon as ``git pull`` lands; this
  layer doesn't, because the operator never tapped the YubiKey to
  bless the new file. Hard-fail with a banner.

Format of the ``.sig`` sidecar (designed to be human-readable):

    local_scribe-sig-v1 fp=<6hex> alg=hmac-sha256
    <64 hex HMAC over the canonical bytes of the protected file>

  * ``fp`` is the first 6 hex chars of HKDF(master_key, info=
    "config-sign-fp:v1"). It lets ``verify()`` distinguish "tampered"
    from "you re-keyed and this signature is stale" so the banner can
    suggest the right remediation.
  * ``alg`` is fixed at the moment but pinned in the header so a
    future migration (Ed25519, BLAKE3, …) is one ``alg=`` lookup away.

Key derivation:

    signing_subkey = HKDF-SHA256(
        ikm    = master_key (32 bytes),
        salt   = b"local_scribe.signed_config.v1",
        info   = b"config-sign:v1",
        length = 32,
    )

    fingerprint = first 6 hex of HKDF-SHA256(
        ikm    = master_key,
        salt   = b"local_scribe.signed_config.v1",
        info   = b"config-sign-fp:v1",
        length = 6,
    )

The HKDF call is domain-separated from
``service_auth.derive_service_token`` (different salt + info), so a
leak of a service bearer token can't be replayed as a config signature
and vice versa.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from local_scribe.security import service_auth as _sa


# --- format constants ------------------------------------------------------

SIG_FORMAT_HEADER = "local_scribe-sig-v1"
SIG_ALG = "hmac-sha256"

HKDF_SALT = b"local_scribe.signed_config.v1"
HKDF_INFO_SIGN = b"config-sign:v1"
HKDF_INFO_FP = b"config-sign-fp:v1"

SIGNING_KEY_BYTES = 32
FP_HEX_LEN = 6


# --- typed errors ----------------------------------------------------------


class SignedConfigError(Exception):
    """Base class for any verification failure. Callers should
    branch on the concrete subclass to surface the right UX."""


class SignatureMissingError(SignedConfigError):
    """No ``.sig`` sidecar exists for the protected file. Either the
    operator never signed it (fresh install / pre-bootstrap) or the
    sidecar was deleted."""


class SignatureMalformedError(SignedConfigError):
    """The ``.sig`` sidecar exists but doesn't parse as the v1 format
    documented in this module's header. Usually means corruption or
    a future-version downgrade."""


class SignatureMismatchError(SignedConfigError):
    """The HMAC over the protected file doesn't match the recorded
    HMAC. Either the file was modified after signing, OR the master
    key changed (rotated) and the signature is stale — distinguish
    via :class:`KeyFingerprintMismatchError`."""


class KeyFingerprintMismatchError(SignedConfigError):
    """The signing-key fingerprint recorded in the sidecar differs
    from the fingerprint derived from the current in-memory master
    key. Almost always means the operator rotated the master key
    after signing; re-sign to recover."""


# --- key derivation --------------------------------------------------------


def _signing_subkey(master_key: Union[bytes, bytearray]) -> bytes:
    """HKDF-derived 32-byte subkey, domain-separated from every other
    HKDF use in the codebase by ``salt`` + ``info``."""
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != 32:
        raise ValueError("master_key must be 32 bytes")
    return _sa.hkdf_sha256(
        ikm=bytes(master_key),
        salt=HKDF_SALT,
        info=HKDF_INFO_SIGN,
        length=SIGNING_KEY_BYTES,
    )


def fingerprint(master_key: Union[bytes, bytearray]) -> str:
    """Public, leak-safe identifier for the master key. 6 hex chars,
    HKDF-derived so it can't be reverse-engineered into key bits."""
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) != 32:
        raise ValueError("master_key must be 32 bytes")
    raw = _sa.hkdf_sha256(
        ikm=bytes(master_key),
        salt=HKDF_SALT,
        info=HKDF_INFO_FP,
        length=FP_HEX_LEN // 2 + 1,
    )
    return raw.hex()[:FP_HEX_LEN]


# --- HMAC + sidecar I/O ----------------------------------------------------


def compute_hmac(master_key: Union[bytes, bytearray], data: bytes) -> bytes:
    """HMAC-SHA256 of ``data`` under the signing subkey."""
    subkey = _signing_subkey(master_key)
    try:
        return hmac.new(subkey, data, hashlib.sha256).digest()
    finally:
        # Best-effort wipe of the local subkey copy. The master key
        # itself is the caller's responsibility (use ``MasterKey.forget``).
        subkey_ba = bytearray(subkey)
        for i in range(len(subkey_ba)):
            subkey_ba[i] = 0


def default_sig_path(protected_path: Union[str, Path]) -> Path:
    """The conventional sidecar location: same basename + ``.sig``."""
    p = Path(protected_path)
    return p.with_name(p.name + ".sig")


@dataclass(frozen=True)
class Signature:
    """Parsed sidecar contents."""
    format_header: str
    fp_hex: str
    alg: str
    hmac_hex: str


def parse_signature(text: str) -> Signature:
    """Parse the two-line sidecar format. Raises
    :class:`SignatureMalformedError` on any deviation."""
    lines = text.strip().splitlines()
    if len(lines) != 2:
        raise SignatureMalformedError(
            f"expected 2 lines, got {len(lines)}",
        )
    header_line = lines[0].strip()
    hmac_line = lines[1].strip()

    parts = header_line.split()
    if not parts or parts[0] != SIG_FORMAT_HEADER:
        raise SignatureMalformedError(
            f"unknown header: {header_line!r}",
        )
    kv = {}
    for token in parts[1:]:
        if "=" not in token:
            raise SignatureMalformedError(f"malformed header token: {token!r}")
        k, v = token.split("=", 1)
        kv[k] = v

    fp_hex = kv.get("fp", "")
    alg = kv.get("alg", "")
    if not fp_hex or len(fp_hex) != FP_HEX_LEN or not _is_hex(fp_hex):
        raise SignatureMalformedError(f"bad or missing fp= in header: {fp_hex!r}")
    if alg != SIG_ALG:
        raise SignatureMalformedError(f"unsupported alg= in header: {alg!r}")
    if len(hmac_line) != 64 or not _is_hex(hmac_line):
        raise SignatureMalformedError(f"bad hmac line: {hmac_line!r}")

    return Signature(
        format_header=SIG_FORMAT_HEADER,
        fp_hex=fp_hex,
        alg=alg,
        hmac_hex=hmac_line,
    )


def render_signature(sig: Signature) -> str:
    """Render a :class:`Signature` to the canonical two-line text form."""
    return (
        f"{sig.format_header} fp={sig.fp_hex} alg={sig.alg}\n"
        f"{sig.hmac_hex}\n"
    )


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


# --- public sign / verify --------------------------------------------------


def sign_file(
    protected_path: Union[str, Path],
    master_key: Union[bytes, bytearray],
    *,
    sig_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Compute the HMAC of ``protected_path`` and write the sidecar.

    Returns the path the signature was written to. Overwrites any
    existing sidecar. The caller is responsible for already having
    unlocked the master key (Touch ID + YubiKey).
    """
    p = Path(protected_path)
    if not p.is_file():
        raise FileNotFoundError(f"cannot sign missing file: {p}")
    sp = Path(sig_path) if sig_path else default_sig_path(p)
    data = p.read_bytes()
    mac = compute_hmac(master_key, data).hex()
    sig = Signature(
        format_header=SIG_FORMAT_HEADER,
        fp_hex=fingerprint(master_key),
        alg=SIG_ALG,
        hmac_hex=mac,
    )
    sp.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tempfile next to sp, then rename.
    tmp = sp.with_suffix(sp.suffix + ".tmp")
    tmp.write_text(render_signature(sig))
    os.replace(tmp, sp)
    try:
        sp.chmod(0o600)
    except OSError:
        pass
    return sp


def verify_file(
    protected_path: Union[str, Path],
    master_key: Union[bytes, bytearray],
    *,
    sig_path: Optional[Union[str, Path]] = None,
) -> Signature:
    """Verify ``protected_path``'s sidecar against the current
    ``master_key``. Returns the parsed sidecar on success; raises one
    of the typed errors on any failure mode.

    Constant-time HMAC comparison via :func:`hmac.compare_digest`.
    """
    p = Path(protected_path)
    if not p.is_file():
        raise FileNotFoundError(f"protected file missing: {p}")
    sp = Path(sig_path) if sig_path else default_sig_path(p)
    if not sp.is_file():
        raise SignatureMissingError(f"no signature sidecar at {sp}")
    sig = parse_signature(sp.read_text())

    cur_fp = fingerprint(master_key)
    if not hmac.compare_digest(sig.fp_hex, cur_fp):
        raise KeyFingerprintMismatchError(
            f"signature was issued by key fp={sig.fp_hex}, "
            f"current key fp={cur_fp}. The master key was rotated; "
            f"re-sign with `./run.sh config sign`.",
        )

    data = p.read_bytes()
    expected_mac = compute_hmac(master_key, data)
    try:
        actual_mac = bytes.fromhex(sig.hmac_hex)
    except ValueError as e:
        raise SignatureMalformedError(f"bad hex in sidecar: {e}") from e
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise SignatureMismatchError(
            f"{p.name} bytes don't match its sidecar; either the file "
            f"was modified after signing, or the sidecar was. "
            f"Refusing to load. Re-sign with `./run.sh config sign` "
            f"after auditing the diff.",
        )
    return sig


# --- non-cryptographic status (no master key needed) -----------------------


@dataclass(frozen=True)
class SignatureStatus:
    """Plain-old-data snapshot suitable for surfacing via
    ``./run.sh doctor`` and the inspector without ever asking for the
    master key."""
    protected_path: Path
    sig_path: Path
    protected_present: bool
    sig_present: bool
    sig_parseable: bool
    sig_fp: Optional[str]
    sig_alg: Optional[str]
    note: Optional[str]


def status(
    protected_path: Union[str, Path],
    *,
    sig_path: Optional[Union[str, Path]] = None,
) -> SignatureStatus:
    """Read-only snapshot of the on-disk state — never touches the
    master key, never prompts. Tells the caller what they'd need to
    do to recover ("sign", "rotate", "absent")."""
    p = Path(protected_path)
    sp = Path(sig_path) if sig_path else default_sig_path(p)
    protected_present = p.is_file()
    sig_present = sp.is_file()
    sig_parseable = False
    sig_fp: Optional[str] = None
    sig_alg: Optional[str] = None
    note: Optional[str] = None
    if sig_present:
        try:
            sig = parse_signature(sp.read_text())
            sig_parseable = True
            sig_fp = sig.fp_hex
            sig_alg = sig.alg
        except SignatureMalformedError as e:
            note = f"sidecar present but malformed: {e}"
    elif not protected_present:
        note = "neither file nor sidecar present"
    else:
        note = "file present, signature missing — operator never ran `config sign`"
    return SignatureStatus(
        protected_path=p,
        sig_path=sp,
        protected_present=protected_present,
        sig_present=sig_present,
        sig_parseable=sig_parseable,
        sig_fp=sig_fp,
        sig_alg=sig_alg,
        note=note,
    )
