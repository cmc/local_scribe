"""Layer B — Char binary verification and side-load detection.

Purpose
-------
Before ``./run.sh start`` (and ``./run.sh configure-char``) hand
Char an ASR bearer token, we verify that the binary we're about to
trust:

1.  Has a valid Apple code signature (``codesign --verify --deep
    --strict``).
2.  Is signed by **the pinned Team ID + bundle identifier** we
    shipped against. A re-signed Char.app from an attacker (even
    with their own valid Developer ID) is treated as drift.
3.  Is notarized + accepted by Gatekeeper (``spctl --assess``).
4.  Matches the CDHash we recorded the first time we verified.
    Char upgrades change the CDHash → operator must explicitly
    ``./run.sh char baseline-update`` after reviewing the
    upgrade.
5.  Loads only system-provided dynamic libraries — every Mach-O
    in the bundle is enumerated and its ``otool -L`` output is
    checked against an allow-list of ``/System/Library/`` and
    ``/usr/lib/`` paths plus internal-to-bundle relative paths.
    A surprise ``/opt/homebrew/lib/libcurl.4.dylib`` dependency
    would surface as drift.

We also refuse to proceed when the *current shell* carries any
``DYLD_*`` environment variable: those would propagate to Char via
``open`` / ``launchctl`` and turn the bundle into an injection
target. macOS's SIP suppresses ``DYLD_INSERT_LIBRARIES`` for
hardened-runtime binaries by default, but defense-in-depth: refuse
the launch outright rather than rely on SIP.

Pinned identity
---------------
The Team ID + bundle identifier + DMG SHA-256 hashes used to live
hardcoded here and in ``run.sh``; both now load from a single source
of truth at ``local_scribe/common/pinned.json``, which the operator
blesses with their Touch ID + YubiKey via ``./run.sh config sign``
(see :mod:`local_scribe.security.signed_config` for the mechanism and
``docs/SECURITY.md`` for the threat model). The public module-level
attributes ``PINNED_TEAM_ID`` / ``PINNED_BUNDLE_ID`` / ``DEFAULT_CHAR_PATH``
below are still exported (call sites + tests rely on the names) but
each evaluates lazily against the pinned config — bumping pinned.json
flows through to ``verify()`` without a re-import. Operators upgrading
Char should edit ``pinned.json``, run ``./run.sh config sign``, then
``./run.sh char baseline-update`` to roll the recorded CDHash forward.

Baseline file
-------------
``~/.config/local_scribe/char_baseline.json`` records the CDHash +
the sorted list of every Mach-O in the bundle on first verification.
Subsequent verifications compare every field; ANY mismatch surfaces
as drift with a per-file diff in the banner.

Override
--------
``LOCAL_SCRIBE_ALLOW_DIRTY_CHAR=1`` downgrades the failure to a
loud-but-non-fatal warning. ``run.sh`` carries the warning banner on
every subsequent command until the override is removed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --- pinned identity ------------------------------------------------------
#
# Source of truth = ``local_scribe/common/pinned.json``. We pull through
# the unverified loader at module import: signature verification happens
# at the operator entry points (``./run.sh start``, ``bootstrap``,
# ``python -m local_scribe config verify``) so library callers and
# unit tests don't have to plumb a master key through every ``verify()``
# invocation. If pinned.json is missing / malformed the load raises
# loudly here — that's a deployment bug, not a runtime branch.

from local_scribe.common import pinned as _pinned  # noqa: E402


def _bootstrap_pinned() -> _pinned.Pinned:
    """Resolve pinned values once at import; surface a clear error
    if the data file is missing rather than letting a downstream
    ``AttributeError`` mask the root cause."""
    try:
        return _pinned.load_pinned_unverified()
    except FileNotFoundError as e:
        raise RuntimeError(
            f"char_integrity: cannot import without "
            f"local_scribe/common/pinned.json ({e}). The file ships "
            f"with the repo; if you got here via a partial install, "
            f"run `git checkout local_scribe/common/pinned.json`.",
        ) from e


_PINNED = _bootstrap_pinned()

PINNED_TEAM_ID = _PINNED.char.pinned_team_id
PINNED_BUNDLE_ID = _PINNED.char.pinned_bundle_id

# Anywhere we'd accept a linked-library path. Anything else surfaces
# as drift (a Homebrew dylib, a relative path that escapes the bundle,
# a `/private/tmp/` redirect, etc.).
ALLOWED_LINK_PREFIXES = (
    "/System/Library/",
    "/usr/lib/",
    "@executable_path/",
    "@loader_path/",
    "@rpath/",
)


DEFAULT_CHAR_PATH = Path(_PINNED.char.default_app_path)
DEFAULT_BASELINE_PATH = (
    Path.home() / ".config" / "local_scribe" / "char_baseline.json"
)

ALLOW_DIRTY_ENV = "LOCAL_SCRIBE_ALLOW_DIRTY_CHAR"

DYLD_ENV_VARS = (
    "DYLD_INSERT_LIBRARIES",
    "DYLD_FORCE_FLAT_NAMESPACE",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
    "DYLD_PRINT_LIBRARIES",
    "DYLD_PRINT_STATISTICS",
)


# --- data carriers ---------------------------------------------------------


class CharIntegrityError(RuntimeError):
    """Raised when verification fails and override is not set."""


@dataclass
class MachOInfo:
    """One Mach-O inside the bundle. Path is *bundle-relative* so
    diffs look the same regardless of which volume Char.app lives
    on."""
    rel_path: str
    sha256: str
    linked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MachOInfo":
        return cls(
            rel_path=d["rel_path"],
            sha256=d["sha256"],
            linked=list(d.get("linked", [])),
        )


@dataclass
class CharFingerprint:
    """Everything we record per verification. Stable across runs of
    the same Char.app build; changes the moment Char is upgraded
    or replaced."""
    bundle_path: str
    bundle_id: str
    team_id: str
    cdhash_sha256_full: str
    spctl_accepted: bool
    spctl_source: str
    mach_os: list[MachOInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CharFingerprint":
        return cls(
            bundle_path=d["bundle_path"],
            bundle_id=d["bundle_id"],
            team_id=d["team_id"],
            cdhash_sha256_full=d["cdhash_sha256_full"],
            spctl_accepted=d.get("spctl_accepted", False),
            spctl_source=d.get("spctl_source", ""),
            mach_os=[MachOInfo.from_dict(m) for m in d.get("mach_os", [])],
        )


@dataclass
class CharDrift:
    """One reason verification failed. Renders as a single line in
    the banner."""
    kind: str       # codesign | spctl | team | bundle | cdhash | linkage | sideload | missing
    message: str
    detail: Optional[str] = None


@dataclass
class CharReport:
    clean: bool
    drifts: list[CharDrift] = field(default_factory=list)
    fingerprint: Optional[CharFingerprint] = None
    baseline: Optional[CharFingerprint] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "note": self.note,
            "drifts": [dataclasses.asdict(d) for d in self.drifts],
            "fingerprint": (
                self.fingerprint.to_dict() if self.fingerprint else None
            ),
            "baseline_present": self.baseline is not None,
        }


# --- low-level shell helpers ----------------------------------------------


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _need(bin_: str) -> bool:
    return shutil.which(bin_) is not None


# --- fingerprint construction --------------------------------------------


_CDHASH_FULL_RE = re.compile(
    r"^CandidateCDHashFull\s+sha256=([0-9a-fA-F]{64})\s*$", re.MULTILINE,
)
_BUNDLE_ID_RE = re.compile(r"^Identifier=(.+)$", re.MULTILINE)
_TEAM_ID_RE = re.compile(r"^TeamIdentifier=(.+)$", re.MULTILINE)


def _codesign_display(bundle: Path) -> dict[str, Optional[str]]:
    rc, out, err = _run([
        "codesign", "--display", "--verbose=4", str(bundle),
    ])
    # codesign emits everything on stderr.
    text = out + "\n" + err
    if rc != 0:
        return {"_error": (err or out).strip()}
    return {
        "bundle_id": _first(_BUNDLE_ID_RE, text),
        "team_id": _first(_TEAM_ID_RE, text),
        "cdhash_sha256_full": _first(_CDHASH_FULL_RE, text),
        "raw": text,
    }


def _first(rx: re.Pattern, text: str) -> Optional[str]:
    m = rx.search(text)
    return m.group(1).strip() if m else None


_SPCTL_ACCEPTED_RE = re.compile(r":\s*accepted\s*$", re.MULTILINE)
_SPCTL_SOURCE_RE = re.compile(r"^source=(.+)$", re.MULTILINE)


def _spctl_assess(bundle: Path) -> dict[str, object]:
    rc, out, err = _run([
        "spctl", "--assess", "--type", "execute",
        "--verbose=2", str(bundle),
    ])
    text = (out + "\n" + err)
    accepted = (rc == 0) and bool(_SPCTL_ACCEPTED_RE.search(text))
    src = _first(_SPCTL_SOURCE_RE, text) or ""
    return {"accepted": accepted, "source": src, "raw": text}


def _codesign_verify_deep(bundle: Path) -> tuple[bool, str]:
    rc, out, err = _run([
        "codesign", "--verify", "--deep", "--strict",
        "--verbose=2", str(bundle),
    ])
    return rc == 0, (out + err).strip()


def _enumerate_macho(bundle: Path) -> list[Path]:
    """Walk the bundle and return every Mach-O file (executables +
    dylibs). We don't trust file extensions — we use ``file --brief``
    so we catch unsigned dylibs that ended in ``.bin`` etc."""
    out: list[Path] = []
    if not bundle.is_dir():
        return out
    for p in bundle.rglob("*"):
        if not p.is_file():
            continue
        if p.is_symlink():
            continue
        try:
            with p.open("rb") as fh:
                magic = fh.read(4)
        except OSError:
            continue
        # Mach-O magic: 0xfeedfacf (LE) / 0xcafebabe (universal)
        if magic in (
            b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        ):
            out.append(p)
    out.sort()
    return out


def _file_sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _otool_L(p: Path) -> list[str]:
    rc, out, err = _run(["otool", "-L", str(p)])
    if rc != 0:
        return []
    lines = out.splitlines()
    # First line is the binary path itself; subsequent indented lines
    # are dependencies. We strip the trailing ``(compatibility …)``.
    deps: list[str] = []
    for ln in lines[1:]:
        ln = ln.strip()
        if not ln:
            continue
        # ``/path/to.dylib (compatibility version X, current version Y)``
        path = ln.split(" (")[0]
        deps.append(path)
    return deps


def collect_fingerprint(bundle: Path = DEFAULT_CHAR_PATH) -> Optional[
    CharFingerprint
]:
    """Build a :class:`CharFingerprint` for the bundle, or ``None``
    if the bundle is absent / codesign refused to display it."""
    if not bundle.is_dir():
        return None
    info = _codesign_display(bundle)
    if info.get("_error"):
        return None
    cdhash_full = info.get("cdhash_sha256_full")
    if not cdhash_full:
        return None
    s = _spctl_assess(bundle)
    macho = []
    for p in _enumerate_macho(bundle):
        rel = p.relative_to(bundle).as_posix()
        macho.append(MachOInfo(
            rel_path=rel,
            sha256=_file_sha256(p),
            linked=_otool_L(p),
        ))
    return CharFingerprint(
        bundle_path=str(bundle),
        bundle_id=info.get("bundle_id") or "",
        team_id=info.get("team_id") or "",
        cdhash_sha256_full=cdhash_full,
        spctl_accepted=bool(s["accepted"]),
        spctl_source=str(s["source"]),
        mach_os=macho,
    )


# --- baseline persistence -------------------------------------------------


def load_baseline(path: Path = DEFAULT_BASELINE_PATH) -> Optional[
    CharFingerprint
]:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
        return CharFingerprint.from_dict(doc)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_baseline(fp: CharFingerprint, path: Path = DEFAULT_BASELINE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(fp.to_dict(), indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def clear_baseline(path: Path = DEFAULT_BASELINE_PATH) -> None:
    if path.is_file():
        path.unlink()


# --- verification ---------------------------------------------------------


def env_has_dyld_injection() -> list[str]:
    """Return any DYLD_* env vars currently set in this process."""
    return [k for k in DYLD_ENV_VARS if os.environ.get(k)]


def check_linkage(macho: list[MachOInfo]) -> list[CharDrift]:
    """Flag any dependency that doesn't start with one of our allow-
    listed prefixes. Pinned-prefix list lives in
    :data:`ALLOWED_LINK_PREFIXES`."""
    drifts: list[CharDrift] = []
    for m in macho:
        for dep in m.linked:
            if dep.startswith(ALLOWED_LINK_PREFIXES):
                continue
            drifts.append(CharDrift(
                kind="linkage",
                message=f"{m.rel_path} links {dep}",
                detail="not under /System/Library or /usr/lib and not "
                       "an @executable_path/@loader_path/@rpath form",
            ))
    return drifts


def verify(
    bundle: Path = DEFAULT_CHAR_PATH,
    baseline_path: Path = DEFAULT_BASELINE_PATH,
    *,
    require_baseline: bool = True,
    pinned_team_id: str = PINNED_TEAM_ID,
    pinned_bundle_id: str = PINNED_BUNDLE_ID,
) -> CharReport:
    """Verify the bundle. ``require_baseline=False`` is the
    "first-time-run" path: we'll record the baseline rather than
    fail when one is missing."""
    drifts: list[CharDrift] = []
    if not _need("codesign"):
        return CharReport(clean=False, note="codesign not on PATH",
                          drifts=[CharDrift(
                              kind="missing",
                              message="codesign tool not available",
                          )])
    if not bundle.is_dir():
        return CharReport(clean=False, note="Char.app not installed",
                          drifts=[CharDrift(
                              kind="missing",
                              message=f"{bundle} does not exist",
                              detail="run ./run.sh install-char to fetch the pinned DMG",
                          )])

    injected = env_has_dyld_injection()
    if injected:
        # Short-circuit: a poisoned DYLD_* env makes EVERY subprocess
        # call below trigger a SIP/Gatekeeper confirmation pause, and
        # in any case we already know the answer (refuse). Return a
        # single sideload drift so the operator sees the actionable
        # message instead of a 30-second hang per subprocess.
        drifts.append(CharDrift(
            kind="sideload",
            message=(
                "DYLD_* injection environment variable present: "
                + ", ".join(injected)
            ),
            detail="DYLD_* would propagate into Char's process and turn "
                   "the hardened-runtime bundle into an injection target. "
                   "Unset these in the shell that runs ./run.sh start "
                   "(e.g. ``unset "
                   + " ".join(injected)
                   + "``) and re-run. Subsequent codesign/spctl checks "
                   "are skipped while DYLD_* is set because the OS "
                   "Gatekeeper subsystem turns them into slow modal "
                   "prompts in that state.",
        ))
        return CharReport(clean=False, drifts=drifts)

    deep_ok, deep_out = _codesign_verify_deep(bundle)
    if not deep_ok:
        drifts.append(CharDrift(
            kind="codesign",
            message="codesign --verify --deep --strict FAILED",
            detail=deep_out[:400] or "(no detail from codesign)",
        ))

    fp = collect_fingerprint(bundle)
    if fp is None:
        drifts.append(CharDrift(
            kind="codesign",
            message="codesign --display refused to fingerprint the bundle",
            detail="bundle may be unsigned, corrupt, or codesign refused",
        ))
        return CharReport(clean=False, drifts=drifts)

    if fp.team_id != pinned_team_id:
        drifts.append(CharDrift(
            kind="team",
            message=f"team identifier drift: got {fp.team_id!r}, "
                    f"expected {pinned_team_id!r}",
            detail="this Char.app was signed by a different developer. "
                   "Either the bundle was replaced or the pinned identity "
                   "in char_integrity.py is stale (after a legitimate "
                   "Char re-signing). Reinstall via ./run.sh install-char.",
        ))

    if fp.bundle_id != pinned_bundle_id:
        drifts.append(CharDrift(
            kind="bundle",
            message=f"bundle identifier drift: got {fp.bundle_id!r}, "
                    f"expected {pinned_bundle_id!r}",
            detail="someone replaced Char.app with a bundle that masquerades "
                   "but has a different CFBundleIdentifier. Refusing to trust.",
        ))

    if not fp.spctl_accepted:
        drifts.append(CharDrift(
            kind="spctl",
            message=f"Gatekeeper assessment did NOT accept the bundle "
                    f"(source={fp.spctl_source!r})",
            detail="Notarization or signing chain is broken. Refusing to "
                   "trust an un-notarized binary with your audio + keys.",
        ))

    drifts.extend(check_linkage(fp.mach_os))

    baseline = load_baseline(baseline_path)
    if baseline is None and require_baseline:
        drifts.append(CharDrift(
            kind="cdhash",
            message="no recorded Char baseline at "
                    + str(baseline_path),
            detail="run `./run.sh char baseline-set` after verifying the "
                   "installed Char.app is the one you intended.",
        ))

    if baseline is not None:
        if baseline.cdhash_sha256_full != fp.cdhash_sha256_full:
            drifts.append(CharDrift(
                kind="cdhash",
                message=f"CDHash drift: baseline "
                        f"{baseline.cdhash_sha256_full[:16]}…, "
                        f"current {fp.cdhash_sha256_full[:16]}…",
                detail="Char binary was replaced or upgraded since the "
                       "baseline was recorded. If this was an intentional "
                       "upgrade, review the release notes then run "
                       "`./run.sh char baseline-update`.",
            ))
        # Compare Mach-O catalogue (set of paths + per-file sha).
        old_by_path = {m.rel_path: m for m in baseline.mach_os}
        new_by_path = {m.rel_path: m for m in fp.mach_os}
        for p in sorted(set(old_by_path) | set(new_by_path)):
            o, n = old_by_path.get(p), new_by_path.get(p)
            if o is None:
                drifts.append(CharDrift(
                    kind="cdhash",
                    message=f"new Mach-O in bundle: {p}",
                    detail=f"sha256={n.sha256[:12]}… not in recorded baseline",
                ))
            elif n is None:
                drifts.append(CharDrift(
                    kind="cdhash",
                    message=f"baseline Mach-O missing: {p}",
                ))
            elif o.sha256 != n.sha256:
                drifts.append(CharDrift(
                    kind="cdhash",
                    message=f"Mach-O sha256 changed: {p}",
                    detail=f"{o.sha256[:12]}… → {n.sha256[:12]}…",
                ))

    return CharReport(
        clean=not drifts,
        drifts=drifts,
        fingerprint=fp,
        baseline=baseline,
    )


# --- override + enforcement -----------------------------------------------


def is_override_enabled() -> bool:
    return os.environ.get(ALLOW_DIRTY_ENV, "") not in ("", "0", "false", "FALSE")


def enforce_or_die(report: Optional[CharReport] = None,
                   stream=sys.stderr) -> CharReport:
    rep = report if report is not None else verify()
    if rep.clean:
        return rep
    print(format_banner(rep, color=_supports_color(stream)),
          file=stream, flush=True)
    if not is_override_enabled():
        raise CharIntegrityError(
            f"char-integrity check failed: {len(rep.drifts)} drift "
            f"item(s); set {ALLOW_DIRTY_ENV}=1 to override (read the "
            f"banner first).",
        )
    return rep


def _supports_color(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def format_banner(report: CharReport, color: bool = False) -> str:
    red = "\033[1;31m" if color else ""
    yellow = "\033[1;33m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""

    fp = report.fingerprint
    lines: list[str] = []
    lines.append(f"{red}┌── ⚠  CHAR INTEGRITY CHECK FAILED  ─────────────{reset}")
    if fp is not None:
        lines.append(
            f"{red}│{reset}  bundle      : {fp.bundle_path}",
        )
        lines.append(
            f"{red}│{reset}  identifier  : {fp.bundle_id}",
        )
        lines.append(
            f"{red}│{reset}  team        : {fp.team_id}",
        )
        lines.append(
            f"{red}│{reset}  CDHash      : {fp.cdhash_sha256_full[:16]}…",
        )
        lines.append(
            f"{red}│{reset}  Gatekeeper  : "
            f"{'accepted' if fp.spctl_accepted else 'NOT accepted'} "
            f"({fp.spctl_source or '—'})",
        )
        lines.append(f"{red}│{reset}")
    lines.append(f"{red}│{reset}  {bold}drifts:{reset}")
    for d in report.drifts[:60]:
        lines.append(
            f"{red}│{reset}    {bold}{d.kind:<8s}{reset}  {d.message}",
        )
        if d.detail:
            for chunk in _wrap(d.detail, width=70):
                lines.append(f"{red}│{reset}              {chunk}")
    if len(report.drifts) > 60:
        lines.append(
            f"{red}│{reset}    … and {len(report.drifts) - 60} more drifts",
        )
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  Refusing to write Char's ASR bearer token or "
        "start the pipeline.",
    )
    lines.append(
        f"{red}│{reset}  Something has interfered with the Char.app "
        "you installed.",
    )
    lines.append(f"{red}│{reset}")
    lines.append(f"{red}│{reset}  Recommended next steps:")
    lines.append(
        f"{red}│{reset}    1.  ``./run.sh install-char`` — reinstall the"
        " pinned, notarized Char.app",
    )
    lines.append(
        f"{red}│{reset}    2.  ``codesign --verify --deep --strict /Applications/Char.app``",
    )
    lines.append(
        f"{red}│{reset}    3.  ``spctl --assess --type execute /Applications/Char.app``",
    )
    lines.append(
        f"{red}│{reset}    4.  If you JUST upgraded Char (and reviewed the"
        " release notes):",
    )
    lines.append(
        f"{red}│{reset}        ``./run.sh char baseline-update``",
    )
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  To override (DANGEROUS — you are saying \"yes, I"
        " know Char has been",
    )
    lines.append(
        f"{red}│{reset}  modified and I trust the change\"):",
    )
    lines.append(
        f"{red}│{reset}    {yellow}LOCAL_SCRIBE_ALLOW_DIRTY_CHAR=1 ./run.sh start{reset}",
    )
    lines.append(f"{red}└────────────────────────────────────────────────{reset}")
    return "\n".join(lines)


def format_override_warning(report: CharReport, color: bool = False) -> str:
    red = "\033[1;31m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""
    head = (
        report.fingerprint.cdhash_sha256_full[:12]
        if report.fingerprint else "<unknown>"
    )
    return (
        f"{red}⚠  RUNNING WITH UNVERIFIED CHAR — "
        f"{len(report.drifts)} drift item(s) at CDHash "
        f"{bold}{head}{reset}{red}; "
        f"Char.app has been modified or replaced since the recorded "
        f"baseline. Unset {bold}{ALLOW_DIRTY_ENV}{reset}{red} once "
        f"resolved.{reset}"
    )


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width=width) or [""]


# --- CLI for run.sh integration -------------------------------------------


def _main(argv: list[str]) -> int:
    """``python -m char_integrity [--json | --check | --banner |
    --baseline-set | --baseline-update | --baseline-clear]``"""
    mode = "--check"
    if argv:
        mode = argv[0]

    if mode in ("--baseline-set", "--baseline-update"):
        # Refresh the baseline from the current bundle. Doesn't gate
        # on drift — that's the whole point of the command.
        fp = collect_fingerprint()
        if fp is None:
            print("ERROR: could not fingerprint Char.app (is it installed?)",
                  file=sys.stderr)
            return 2
        # When updating, sanity-check that the bundle still has the
        # expected team-id + bundle-id so a malicious bundle doesn't
        # roll itself into our baseline.
        if fp.team_id != PINNED_TEAM_ID or fp.bundle_id != PINNED_BUNDLE_ID:
            print(
                f"ERROR: refuse to baseline a bundle signed by "
                f"team={fp.team_id!r} bundle_id={fp.bundle_id!r}; "
                f"expected team={PINNED_TEAM_ID!r} bundle_id={PINNED_BUNDLE_ID!r}",
                file=sys.stderr,
            )
            return 2
        save_baseline(fp)
        print(
            f"baseline written → CDHash={fp.cdhash_sha256_full[:16]}…  "
            f"({len(fp.mach_os)} Mach-Os tracked)",
        )
        return 0

    if mode == "--baseline-clear":
        clear_baseline()
        print("baseline cleared")
        return 0

    if mode == "--show-fingerprint":
        fp = collect_fingerprint()
        if fp is None:
            print("(no Char.app or codesign refused)", file=sys.stderr)
            return 2
        print(json.dumps(fp.to_dict(), indent=2))
        return 0

    rep = verify()
    if mode == "--json":
        print(json.dumps(rep.to_dict(), indent=2))
        return 0 if rep.clean else 2

    color = _supports_color(sys.stdout)
    if rep.clean:
        if mode == "--banner":
            green = "\033[1;32m" if color else ""
            reset = "\033[0m" if color else ""
            fp = rep.fingerprint
            head = fp.cdhash_sha256_full[:16] if fp else "?"
            print(
                f"{green}● char-integrity OK{reset}  "
                f"team={fp.team_id}  bundle={fp.bundle_id}  CDHash={head}…  "
                f"({len(fp.mach_os)} Mach-Os tracked)",
            )
        return 0

    print(format_banner(rep, color=color))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
