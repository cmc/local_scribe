"""Layer A — script-integrity check.

Purpose
-------
Before ``./run.sh start`` hands a bearer token to Char (or the ASR /
inspector services HKDF-derive one from the master key) we want to be
sure the code that's about to run *is* the code that's checked into
git. A bad actor with shell access could modify ``run.sh``,
``service_auth.py``, ``inspector_server.py``, or any of the other
moving parts to:

1.  Skip the typed-DELETE confirmation gate.
2.  Log the master key on first unlock.
3.  Forward audio to a remote endpoint after the Char bearer arrives.
4.  Bypass the firewall block list.

Git hashes are *not* a substitute for code signing (we'll do that in a
later commit with ``git tag -s`` / ``gpg --verify``), but they are a
real defense against unprivileged file tampering: an attacker who
modifies a file in-place but doesn't bother to also rewrite git's
object store will be caught the next time ``./run.sh start`` runs.

What's checked
--------------
For each tracked file matching one of the patterns in
``VERIFY_GLOB`` we compare:

* ``git hash-object <path>`` of the working copy  (what we'd commit
  right now)
* ``git rev-parse HEAD:<path>``                    (what's pinned at
  HEAD)

These are SHA-1 blob hashes; git is the only thing computing them so
we don't depend on Python's hashlib agreeing with anything else. Any
mismatch is **drift**. We also flag:

* tracked files that are entirely missing from the working tree
  (someone deleted ``service_auth.py`` to disable the gate)
* untracked files matching ``*.py`` / ``*.sh`` / ``*.swift`` in the
  repo root or subdirectories we ship from (someone dropped a
  ``service_auth_shim.py`` that ``conftest.py`` would pick up first).

What's NOT checked
------------------
Files under ``tests/``, ``.cache/``, ``.run/``, ``venv/``, and
documentation directories are intentionally excluded — those don't
participate in the runtime path, so drift there is noisy without
being a security signal. The list lives in ``EXCLUDE_GLOBS`` so
operators can review it in one place.

Override
--------
``LOCAL_SCRIBE_ALLOW_DIRTY=1`` (or the ``--allow-dirty`` flag passed
through ``./run.sh``) downgrades the failure to a printed-but-not-
fatal warning. ``run.sh`` then prints a *bright red* banner on
every command until the override is removed — the user is meant to
notice they're operating in a degraded posture.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


def _find_repo_root() -> Path:
    """Walk up from this module's location until we find the repo root.

    Before the package refactor this was simply
    ``Path(__file__).resolve().parent`` because ``script_integrity.py``
    lived at repo root. After the refactor it lives at
    ``local_scribe/security/script_integrity.py`` so the heuristic needs
    to walk up two parents.

    We don't use ``git rev-parse --show-toplevel`` here because this
    module is imported during *startup* before we know whether we have a
    git checkout at all (detached installs run from a tarball have no
    ``.git`` dir but still need integrity verification to short-circuit
    cleanly). We anchor on ``pyproject.toml`` instead — it's guaranteed
    to be at repo root in the new layout, and its absence means we're
    not in a sensible install root.
    """
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    # Fallback: original heuristic, two parents up from this file.
    # (local_scribe/security/script_integrity.py → repo root.)
    return here.parents[2]


REPO_ROOT = _find_repo_root()


VERIFY_GLOBS: tuple[str, ...] = (
    # Operator-facing shell + Python that actually run during ``start``.
    # After the package refactor, runtime Python lives under
    # ``local_scribe/`` so we recurse the whole package. The legacy
    # ``*.py`` glob at repo root is kept for backward compatibility:
    # (a) some downstream callers may run integrity checks against
    #     repos that haven't been refactored yet (e.g. test fixtures
    #     that build a tiny fake repo with ``service_auth.py`` at
    #     root — see tests/test_script_integrity.py);
    # (b) during the refactor itself, files transition through both
    #     locations and we want each to be verified at every step.
    # Post-refactor, no production code lives at repo root, so the
    # ``*.py`` glob will simply catch zero files.
    "local_scribe/**/*.py",
    "*.py",
    "*.sh",
    "*.swift",
    "bin/*.swift",
)


EXCLUDE_GLOBS: tuple[str, ...] = (
    # Tests live in-tree but don't participate in the runtime; drift
    # there is normal during development.
    "tests/*",
    "tests/**/*",
    # Bytecode + venv artefacts that aren't tracked anyway, just
    # belt-and-suspenders.
    "__pycache__/*",
    "**/__pycache__/*",
    "venv/*",
    "venv/**",
    ".run/*",
    ".cache/*",
    # Refactor scratch space — not part of runtime.
    ".refactor-tools/*",
)


ALLOW_DIRTY_ENV = "LOCAL_SCRIBE_ALLOW_DIRTY"


class ScriptIntegrityError(RuntimeError):
    """Raised when verification finds drift and override is not set."""


@dataclass
class FileDrift:
    """A single tampered / missing / surprise file. Rendered by
    ``Report.format()`` into the operator-facing red banner."""
    path: str
    kind: str  # "modified" | "missing" | "untracked"
    head_hash: Optional[str] = None
    working_hash: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class Report:
    """Aggregated verification result. ``clean`` is the property the
    caller actually wants to gate on; the rest is for the banner /
    operator messaging."""
    clean: bool
    drifts: list[FileDrift] = field(default_factory=list)
    head_ref: Optional[str] = None
    head_short: Optional[str] = None
    is_git_repo: bool = True
    checked_count: int = 0
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "head_ref": self.head_ref,
            "head_short": self.head_short,
            "is_git_repo": self.is_git_repo,
            "checked_count": self.checked_count,
            "note": self.note,
            "drifts": [
                {
                    "path": d.path,
                    "kind": d.kind,
                    "head_hash": d.head_hash,
                    "working_hash": d.working_hash,
                    "detail": d.detail,
                }
                for d in self.drifts
            ],
        }


# --- low-level git helpers --------------------------------------------------


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    """Run ``git <args>`` and return its trimmed stdout. Non-zero exit
    raises ``subprocess.CalledProcessError`` so the caller decides
    whether to swallow it (e.g. detached install with no .git dir)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_available() -> bool:
    return shutil.which("git") is not None


def _is_git_repo(root: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _head_hashes(root: Path) -> dict[str, str]:
    """Map ``path -> blob-sha`` for every tracked file at HEAD.
    Uses ``ls-tree -r`` so we get sha + path in one round-trip."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    result: dict[str, str] = {}
    for line in out.stdout.splitlines():
        # ``<mode> SP <type> SP <sha> TAB <path>``
        try:
            meta, path = line.split("\t", 1)
            parts = meta.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            result[path] = parts[2]
        except ValueError:
            continue
    return result


def _working_hashes(root: Path, paths: Iterable[str]) -> dict[str, str]:
    """Map ``path -> blob-sha`` for the on-disk copy via
    ``git hash-object --stdin-paths``. Batched for speed (40 paths in
    ~30 ms on the dev mac)."""
    paths = list(paths)
    if not paths:
        return {}
    proc = subprocess.run(
        ["git", "-C", str(root), "hash-object", "--stdin-paths"],
        input="\n".join(paths) + "\n",
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        # ``hash-object`` is verbose about missing files; we treat
        # them as missing-from-working-tree drift later.
        pass
    hashes = proc.stdout.splitlines()
    out: dict[str, str] = {}
    for path, sha in zip(paths, hashes):
        if sha and len(sha) == 40 and not sha.startswith("error"):
            out[path] = sha
    return out


def _path_matches_any(path: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _is_verified(path: str) -> bool:
    if _path_matches_any(path, EXCLUDE_GLOBS):
        return False
    return _path_matches_any(path, VERIFY_GLOBS)


def _untracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others",
         "--exclude-standard"],
        capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.splitlines() if p]


# --- public surface ---------------------------------------------------------


def verify(root: Optional[Path] = None) -> Report:
    """Return a :class:`Report` describing whether the working tree at
    ``root`` matches HEAD for every operator-facing file. Never raises
    on drift; the caller decides whether to escalate via
    :func:`enforce_or_die`."""
    root = root or REPO_ROOT
    rep = Report(clean=True)
    if not _git_available():
        rep.is_git_repo = False
        rep.note = (
            "git not on PATH; skipping integrity check. Install git or "
            "set LOCAL_SCRIBE_ALLOW_DIRTY=1 to suppress this warning."
        )
        return rep
    if not _is_git_repo(root):
        rep.is_git_repo = False
        rep.note = (
            "not running from a git checkout (no .git dir); skipping "
            "integrity check. This is normal for a tarball install."
        )
        return rep

    try:
        rep.head_ref = _git("rev-parse", "HEAD", cwd=root)
        rep.head_short = _git(
            "rev-parse", "--short", "HEAD", cwd=root,
        )
    except subprocess.CalledProcessError as exc:
        rep.note = f"git rev-parse failed: {exc.stderr or exc}"
        rep.clean = False
        return rep

    head = _head_hashes(root)
    tracked_paths = [p for p in head.keys() if _is_verified(p)]
    rep.checked_count = len(tracked_paths)

    # Existence check first: a file that's tracked at HEAD but missing
    # locally is its own kind of drift — somebody deleted
    # ``service_auth.py`` and our gate would happily start without it.
    existing: list[str] = []
    for p in tracked_paths:
        full = root / p
        if full.is_file():
            existing.append(p)
        else:
            rep.drifts.append(FileDrift(
                path=p, kind="missing", head_hash=head[p],
                detail="tracked file is absent from the working tree",
            ))
            rep.clean = False

    working = _working_hashes(root, existing)
    for p in existing:
        h_head = head.get(p)
        h_work = working.get(p)
        if h_work is None:
            rep.drifts.append(FileDrift(
                path=p, kind="modified", head_hash=h_head,
                detail="hash-object refused this file",
            ))
            rep.clean = False
            continue
        if h_head != h_work:
            rep.drifts.append(FileDrift(
                path=p, kind="modified",
                head_hash=h_head, working_hash=h_work,
                detail="working copy differs from HEAD",
            ))
            rep.clean = False

    # Untracked files that match our patterns. A new ``foo.py`` next
    # to ``service_auth.py`` could be imported by ``conftest.py`` or
    # ``run.sh`` and run with our privileges.
    for p in _untracked_files(root):
        if _is_verified(p):
            rep.drifts.append(FileDrift(
                path=p, kind="untracked",
                detail="new file matches a verified pattern; refusing "
                       "to trust an unreviewed module on the import path",
            ))
            rep.clean = False

    return rep


def is_override_enabled() -> bool:
    return os.environ.get(ALLOW_DIRTY_ENV, "") not in ("", "0", "false", "FALSE")


def enforce_or_die(report: Optional[Report] = None,
                   stream=sys.stderr) -> Report:
    """Pretty-print the report. If ``report.clean`` is False and the
    override env var is unset, raise :class:`ScriptIntegrityError`.

    Returns the report so callers can also log or pass it through."""
    rep = report if report is not None else verify()
    if rep.clean:
        return rep
    banner = format_banner(rep, color=_supports_color(stream))
    print(banner, file=stream, flush=True)
    if not is_override_enabled():
        raise ScriptIntegrityError(
            f"script-integrity check failed: {len(rep.drifts)} drift "
            f"item(s); set {ALLOW_DIRTY_ENV}=1 to override (read the "
            f"banner first).",
        )
    return rep


# --- output rendering -------------------------------------------------------


def _supports_color(stream) -> bool:
    """Be conservative — only emit ANSI when we're talking to a TTY."""
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def format_banner(report: Report, color: bool = False) -> str:
    """Operator-facing red banner. Same content shape regardless of
    TTY (so logs grep cleanly) — colour only changes the styling."""
    red = "\033[1;31m" if color else ""
    yellow = "\033[1;33m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""

    lines: list[str] = []
    head = report.head_short or "<unknown>"
    lines.append(f"{red}┌── ⚠  SCRIPT INTEGRITY DRIFT DETECTED  ──────────{reset}")
    lines.append(f"{red}│{reset}  HEAD: {bold}{head}{reset}"
                 f"  ({report.checked_count} tracked files verified)")
    lines.append(f"{red}│{reset}")
    for d in report.drifts[:50]:
        if d.kind == "modified":
            head_h = (d.head_hash or "?")[:10]
            work_h = (d.working_hash or "?")[:10]
            lines.append(
                f"{red}│{reset}  {bold}MODIFIED{reset}  {d.path}"
                f"   ({head_h} → {work_h})",
            )
        elif d.kind == "missing":
            lines.append(
                f"{red}│{reset}  {bold}MISSING {reset}  {d.path}",
            )
        elif d.kind == "untracked":
            lines.append(
                f"{red}│{reset}  {bold}NEW FILE{reset}  {d.path}",
            )
    if len(report.drifts) > 50:
        lines.append(f"{red}│{reset}  … and {len(report.drifts) - 50} more")
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  Working-tree code does NOT match the git-tracked"
        " version.",
    )
    lines.append(
        f"{red}│{reset}  Refusing to start so a tampered ``service_auth``,"
        " ``inspector_server``,",
    )
    lines.append(
        f"{red}│{reset}  ``run.sh``, or similar can't quietly skip the"
        " gates that protect",
    )
    lines.append(
        f"{red}│{reset}  your audio + Keychain master key.",
    )
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  Recommended next steps:",
    )
    lines.append(
        f"{red}│{reset}    1.  ``git status``       — see what changed.",
    )
    lines.append(
        f"{red}│{reset}    2.  ``git diff``         — review every line.",
    )
    lines.append(
        f"{red}│{reset}    3.  ``git stash`` or ``git restore .`` to"
        " return to a clean tree.",
    )
    lines.append(
        f"{red}│{reset}    4.  If the changes are YOURS and intentional,"
        " commit them.",
    )
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  To override (DANGEROUS — you are saying"
        " \"yes, I modified the",
    )
    lines.append(
        f"{red}│{reset}  trusted base myself; please don't crash on me\"):",
    )
    lines.append(
        f"{red}│{reset}    {yellow}LOCAL_SCRIBE_ALLOW_DIRTY=1 ./run.sh"
        f" start{reset}",
    )
    lines.append(f"{red}│{reset}")
    lines.append(
        f"{red}│{reset}  Override persists for the lifetime of the"
        " environment variable;",
    )
    lines.append(
        f"{red}│{reset}  every subsequent command in that shell will"
        " carry the banner.",
    )
    lines.append(f"{red}└────────────────────────────────────────────────{reset}")
    return "\n".join(lines)


def format_override_warning(report: Report, color: bool = False) -> str:
    """Compact reminder for the post-override state (red banner on
    every ``./run.sh`` command after the user opts in)."""
    red = "\033[1;31m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""
    head = report.head_short or "<unknown>"
    return (
        f"{red}⚠  RUNNING IN DIRTY-OVERRIDE MODE — "
        f"{len(report.drifts)} drift item(s) at HEAD {bold}{head}{reset}"
        f"{red}; security defenses may be tampered. "
        f"Unset {bold}LOCAL_SCRIBE_ALLOW_DIRTY{reset}{red} once your"
        f" working tree is clean.{reset}"
    )


# --- CLI for run.sh integration --------------------------------------------


def _main(argv: list[str]) -> int:
    """``python -m script_integrity [--json | --check | --banner]``

    * ``--check``  : print human banner if dirty, exit 0 on clean,
                     exit 2 on dirty.
    * ``--banner`` : same as --check but always print the banner (even
                     when clean, in which case it's a one-liner).
    * ``--json``   : machine-readable report → stdout, exit 0 on clean,
                     exit 2 on dirty.
    """
    mode = "--check"
    if argv:
        mode = argv[0]
    rep = verify()
    if mode == "--json":
        print(json.dumps(rep.to_dict(), indent=2))
        return 0 if rep.clean else 2
    color = _supports_color(sys.stdout)
    if rep.clean:
        if mode == "--banner":
            green = "\033[1;32m" if color else ""
            reset = "\033[0m" if color else ""
            head = rep.head_short or "<unknown>"
            print(
                f"{green}● script-integrity OK{reset}  "
                f"HEAD={head}  ({rep.checked_count} files verified)",
            )
        return 0
    print(format_banner(rep, color=color))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
