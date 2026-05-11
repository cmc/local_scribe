"""Loader for ``local_scribe/common/pinned.json``.

The JSON file is the single source of truth for distribution-pinned
constants — Char's expected version + DMG hashes + signing identity,
LM Studio's version, etc. Everything that *used* to be hardcoded in
``run.sh`` or ``char_integrity.py``.

Why a JSON file with a sidecar signature instead of Python constants:

* **Separation of code and data.** ``script_integrity`` already covers
  Python source; the values that determine "this is the Char binary we
  trust" are configuration data, not behaviour, so they belong in a
  data file with an independent (operator-controlled) signature.
* **Bumping a value doesn't require a commit.** Operators on the
  release-engineer path can edit ``pinned.json``, re-run
  ``./run.sh config sign`` to bless it with the YubiKey, and continue.
  No git commit hash flux for what's really just a version bump.
* **Auditing is one diff.** ``git diff pinned.json`` is the entire
  change surface; with constants embedded in ``.py`` files, a hash
  flip can hide alongside an innocuous refactor.

Strict / lax loading:

* :func:`load_pinned_strict` requires a verified signature and is the
  default at server startup. Failures raise the typed exceptions from
  :mod:`local_scribe.security.signed_config` so the caller can present
  the right osascript / banner.
* :func:`load_pinned_unverified` is the unsigned-read escape hatch for
  bootstrap (no master key yet) and for tooling that genuinely doesn't
  need the cryptographic guarantee (``config show --shell`` for
  ``run.sh``, ``doctor`` status reporting). It logs a warning every
  time it's called so the log trail makes the boundary obvious.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union


logger = logging.getLogger("local_scribe.pinned")


# --- paths -----------------------------------------------------------------


def _find_repo_root() -> Path:
    """Walk up from this module's location to the repo root (the
    directory containing ``pyproject.toml``)."""
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return here.parents[2]


DEFAULT_PINNED_PATH = _find_repo_root() / "local_scribe" / "common" / "pinned.json"

# Env-var override (tests, sandboxed runs).
PINNED_PATH_ENV = "LOCAL_SCRIBE_PINNED_PATH"
PINNED_SIG_PATH_ENV = "LOCAL_SCRIBE_PINNED_SIG_PATH"

# Hard escape hatch: any value other than "" / "0" / "false" will let
# strict callers fall back to an unverified read with a loud warning.
# Mirrors the LOCAL_SCRIBE_ALLOW_DIRTY_CHAR pattern; documented in
# SECURITY.md.
ALLOW_UNSIGNED_ENV = "LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG"


def pinned_path() -> Path:
    override = os.environ.get(PINNED_PATH_ENV)
    return Path(override) if override else DEFAULT_PINNED_PATH


def pinned_sig_path() -> Path:
    override = os.environ.get(PINNED_SIG_PATH_ENV)
    if override:
        return Path(override)
    return pinned_path().with_name(pinned_path().name + ".sig")


def _allow_unsigned() -> bool:
    v = (os.environ.get(ALLOW_UNSIGNED_ENV) or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


# --- typed accessors -------------------------------------------------------


@dataclass(frozen=True)
class CharPinned:
    known_good_version: str
    release_tag: str
    release_base_url: str
    dmg_sha256_aarch64: str
    dmg_sha256_x86_64: str
    pinned_team_id: str
    pinned_bundle_id: str
    default_app_path: str


@dataclass(frozen=True)
class LMStudioPinned:
    known_good_version: str
    app_path: str
    default_port: int


@dataclass(frozen=True)
class Pinned:
    version: int
    char: CharPinned
    lmstudio: LMStudioPinned
    raw: dict
    signed: bool
    fp_hex: Optional[str]

    @classmethod
    def from_dict(
        cls, d: dict, *, signed: bool, fp_hex: Optional[str] = None,
    ) -> "Pinned":
        return cls(
            version=int(d["version"]),
            char=CharPinned(**d["char"]),
            lmstudio=LMStudioPinned(**d["lmstudio"]),
            raw=d,
            signed=signed,
            fp_hex=fp_hex,
        )


# --- loaders ---------------------------------------------------------------


def load_pinned_unverified(
    path: Optional[Union[str, Path]] = None,
) -> Pinned:
    """Read + parse ``pinned.json`` without checking any signature.

    Use cases:

    * Bootstrap, before the master key exists.
    * ``run.sh`` sourcing version strings for shell-side use.
    * ``doctor`` / ``config status`` reporting where we want to print
      values even when the signature is broken.

    Logs at WARNING the first time it's called per-process so the log
    trail makes the unverified read obvious.
    """
    p = Path(path) if path else pinned_path()
    if not p.is_file():
        raise FileNotFoundError(f"pinned config missing: {p}")
    if not _unverified_warned.flag:
        logger.warning(
            "loading pinned config without signature verification "
            "(path=%s). This is OK for bootstrap / shell sourcing / "
            "doctor reporting but the server must use load_pinned() "
            "at startup.", p,
        )
        _unverified_warned.flag = True
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"pinned config is not valid JSON ({p}): {e}") from e
    return Pinned.from_dict(d, signed=False, fp_hex=None)


class _WarnFlag:
    flag = False


_unverified_warned = _WarnFlag()


def load_pinned(
    master_key: Union[bytes, bytearray],
    path: Optional[Union[str, Path]] = None,
    sig_path: Optional[Union[str, Path]] = None,
) -> Pinned:
    """Verify the operator signature, then parse + return :class:`Pinned`.

    Raises whichever :class:`SignedConfigError` subclass the failure
    matches — callers should branch on the concrete type to present
    the right remediation in the osascript dialog or red banner:

    * :class:`SignatureMissingError`  → "operator never signed; run
      ``./run.sh config sign`` to bless the current file."
    * :class:`SignatureMismatchError` → "file changed since signing;
      audit ``git diff`` then ``./run.sh config sign`` to re-bless,
      OR ``git checkout local_scribe/common/pinned.json`` to revert."
    * :class:`KeyFingerprintMismatchError` → "master key was rotated;
      ``./run.sh config sign`` to refresh."

    Honoured env var ``LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG`` falls back
    to the unverified read after logging at ERROR (not WARNING) so
    its use is auditable in the log trail.
    """
    if _allow_unsigned():
        logger.error(
            "LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG is set; bypassing "
            "pinned-config signature check. This is a Defense-layer "
            "downgrade; do not leave this set in production.",
        )
        return load_pinned_unverified(path)

    from local_scribe.security import signed_config as sc

    p = Path(path) if path else pinned_path()
    sp = Path(sig_path) if sig_path else pinned_sig_path()
    sig = sc.verify_file(p, master_key, sig_path=sp)
    d = json.loads(p.read_text(encoding="utf-8"))
    return Pinned.from_dict(d, signed=True, fp_hex=sig.fp_hex)


# --- thin sugar accessors --------------------------------------------------
#
# These exist so callers that only need ONE field don't have to wire up
# the whole Pinned dataclass. They take an already-loaded :class:`Pinned`
# rather than reloading from disk — the *startup* path verifies once
# and threads the result through.


def char_known_good_version(p: Pinned) -> str:
    return p.char.known_good_version


def char_dmg_sha256(p: Pinned, arch: str) -> str:
    """``arch`` is the suffix in CHAR_DMG_SHA256_<ARCH> as it was in
    run.sh — i.e. ``aarch64`` or ``x86_64``."""
    if arch == "aarch64":
        return p.char.dmg_sha256_aarch64
    if arch == "x86_64":
        return p.char.dmg_sha256_x86_64
    raise ValueError(f"unknown arch: {arch!r}")


def lmstudio_known_good_version(p: Pinned) -> str:
    return p.lmstudio.known_good_version
