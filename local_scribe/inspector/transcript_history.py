"""Transcript history (versioning) for Char sessions.

When the user re-transcribes a session (via ``./run.sh redo-session`` or
Char's *Regenerate* button), the previous ``transcript.json`` is
archived to a per-session history directory under

    <char_session_dir>/.local_scribe_history/<timestamp>_<sha256-short>.json

so the user can inspect / restore / delete it from the Inspector web
UI later. The archive is the *previous* file verbatim, with one
top-level ``local_scribe`` metadata key added (Char's JSON parser
ignores unknown top-level keys, so the file is fully round-trippable
as a Char transcript should the user ever want to restore it by
hand).

Why dot-prefix:
    Char's UI enumerates known files (``transcript.json``,
    ``audio.mp3``, ``*.md``, ``_meta.json``) explicitly and never
    walks the directory blindly. A ``.local_scribe_history`` subdir
    is therefore invisible to Char even after a re-open. We picked
    a leading dot anyway so backup software / Finder grouping treats
    it as hidden state rather than user content.

Safety:
    All path inputs are validated against the session root with the
    same ``_is_safe_subpath`` helper used by ``char_persist`` (refuses
    traversal even via symlinks). Filenames coming over HTTP are
    additionally restricted to ``[A-Za-z0-9._-]+\\.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("local_scribe.transcript_history")


HISTORY_DIR_NAME = ".local_scribe_history"

# Generous but bounded: enough for tooling like ``redo-session`` to
# back up dozens of attempts without filling the disk. Inspector UI
# exposes per-file delete so the user can prune older entries.
DEFAULT_MAX_BACKUPS = 50

# Restricts what the inspector will fetch / delete. Matches the
# filenames we emit (``YYYYMMDDTHHMMSSZ_<sha7>.json``) and tolerates
# legacy / hand-renamed files so long as they don't contain path
# separators or ``..``.
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._\-]+\.json$")


@dataclass
class ArchiveEntry:
    """Per-archive summary surfaced by the inspector API. ``metadata``
    is the previous run's embedded ``local_scribe`` block (empty dict
    if the original file pre-dated this feature)."""

    filename: str
    size_bytes: int
    archived_at_iso: str
    archived_at_unix: float
    transcript_sha256: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "archived_at_iso": self.archived_at_iso,
            "archived_at_unix": self.archived_at_unix,
            "transcript_sha256": self.transcript_sha256,
            "metadata": dict(self.metadata),
        }


def _is_safe_subpath(root: Path, candidate: Path) -> bool:
    """Same defense-in-depth as ``char_persist._is_safe_subpath``: both
    paths resolved, candidate must be inside ``root``."""
    try:
        root_r = root.resolve()
        cand_r = candidate.resolve()
    except OSError:
        return False
    try:
        cand_r.relative_to(root_r)
        return True
    except ValueError:
        return False


def is_safe_filename(name: str) -> bool:
    """True if ``name`` is a bare json filename without traversal
    components. Inspector + redo-session use this to validate any name
    that came over the wire."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_SAFE_FILENAME.match(name))


def history_dir(session_dir: Path) -> Path:
    """Resolve the history directory for a session. Does NOT create
    it; callers that intend to write use ``ensure_history_dir``."""
    return session_dir / HISTORY_DIR_NAME


def ensure_history_dir(session_dir: Path) -> Path:
    """Create the history directory if it doesn't already exist, with
    mode 0o700 so other macOS users on the same machine can't read
    archived transcripts."""
    h = history_dir(session_dir)
    h.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(h, 0o700)
    except OSError:
        # Best-effort; failures here aren't fatal (e.g. on case-
        # insensitive FS quirks). The transcripts are already inside
        # the user's own home so this is a defense-in-depth ask.
        pass
    return h


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp_compact(now: Optional[float] = None) -> str:
    """``YYYYMMDDTHHMMSSZ`` -- sortable filename-safe ISO 8601."""
    t = now if now is not None else time.time()
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def _read_existing_metadata(transcript_path: Path) -> dict[str, Any]:
    """Pull out the ``local_scribe`` block from a transcript.json if it
    has one (i.e. it was written by this codebase post-history-feature).
    Returns ``{}`` for legacy files or unreadable JSON."""
    if not transcript_path.is_file():
        return {}
    try:
        d = json.loads(transcript_path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    meta = d.get("local_scribe")
    if isinstance(meta, dict):
        return meta
    return {}


def archive_existing_transcript(
    session_dir: Path,
    *,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    request_id: str = "",
) -> Optional[Path]:
    """If ``<session_dir>/transcript.json`` exists, copy it into the
    history directory before the caller overwrites it. Returns the
    path of the archive on success, ``None`` if there was nothing to
    archive (no existing file) or an unrecoverable error.

    The archive is the previous file's bytes verbatim -- we do NOT
    rewrite content, only the filename encodes the timestamp + content
    hash. This keeps the JSON exactly as Char (or a previous run of
    this codebase) wrote it, so it can be inspected as-is or even
    copied back over ``transcript.json`` for a manual restore.

    Old archives beyond ``max_backups`` are pruned (oldest first) so
    the directory can't grow without bound.
    """
    transcript_path = session_dir / "transcript.json"
    if not transcript_path.is_file():
        return None

    try:
        data = transcript_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "%shistory: cannot read %s for archive: %s",
            f"[{request_id}] " if request_id else "",
            transcript_path, exc,
        )
        return None

    sha = _sha256_bytes(data)
    hdir = ensure_history_dir(session_dir)
    fname = f"{_timestamp_compact()}_{sha[:7]}.json"
    dest = hdir / fname

    # If we happen to archive twice in the same second AND the content
    # is identical (sha collision in the 7-char prefix is astronomically
    # unlikely on real data), don't bother re-writing. The file's
    # already there with the same bytes.
    if dest.exists():
        try:
            if dest.read_bytes() == data:
                logger.debug(
                    "%shistory: identical archive %s already present; "
                    "skipping",
                    f"[{request_id}] " if request_id else "", dest.name,
                )
                return dest
        except OSError:
            pass

    if not _is_safe_subpath(session_dir, dest):
        logger.error(
            "%shistory: refusing to write archive outside %s (resolved %s)",
            f"[{request_id}] " if request_id else "", session_dir, dest,
        )
        return None

    try:
        # Atomic write so a crash mid-archive can't leave half a file
        # behind. We use a sibling temp inside the *history* dir
        # specifically -- not the session root -- so we don't litter
        # Char's directory with our scratch files.
        tmp = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError as exc:
        logger.warning(
            "%shistory: failed to write archive %s: %s",
            f"[{request_id}] " if request_id else "", dest, exc,
        )
        return None

    _prune_old_archives(hdir, max_backups=max_backups)

    logger.info(
        "%shistory: archived previous transcript -> %s (sha256=%s, %d bytes)",
        f"[{request_id}] " if request_id else "",
        dest.name, sha[:12], len(data),
    )
    return dest


def _prune_old_archives(history: Path, *, max_backups: int) -> None:
    """Keep the newest ``max_backups`` files; drop the rest. ``mtime``
    sort because filenames are timestamp-prefixed (so sorting by name
    works too) but mtime is robust against hand-renames."""
    if max_backups <= 0:
        return
    try:
        files = [
            p for p in history.iterdir()
            if p.is_file() and p.name.endswith(".json")
        ]
    except OSError:
        return
    if len(files) <= max_backups:
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[max_backups:]:
        try:
            stale.unlink()
            logger.info(
                "history: pruned old archive %s (max_backups=%d)",
                stale.name, max_backups,
            )
        except OSError as exc:
            logger.debug(
                "history: prune skip %s: %s", stale.name, exc,
            )


def _entry_for_file(path: Path) -> Optional[ArchiveEntry]:
    """Build the listing entry for one archived JSON file. Returns
    ``None`` if the file is unreadable or has no recoverable shape --
    inspector should still surface a useful row (filename + size) so
    we fall back to a degraded entry in that case rather than hide
    broken files from the user."""
    try:
        stat = path.stat()
    except OSError:
        return None

    metadata: dict[str, Any] = {}
    sha = ""
    try:
        raw = path.read_text()
    except OSError:
        raw = ""
    if raw:
        try:
            doc = json.loads(raw)
            ls = doc.get("local_scribe")
            if isinstance(ls, dict):
                metadata = ls
            # If the document predates this feature it still has a
            # transcript.json shape -- we hash the file bytes so the
            # inspector has something stable to display + match.
        except json.JSONDecodeError:
            pass
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return ArchiveEntry(
        filename=path.name,
        size_bytes=stat.st_size,
        archived_at_iso=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime),
        ),
        archived_at_unix=stat.st_mtime,
        transcript_sha256=sha,
        metadata=metadata,
    )


def list_archives(session_dir: Path) -> list[ArchiveEntry]:
    """List archive entries for a session, newest first."""
    h = history_dir(session_dir)
    if not h.is_dir():
        return []
    entries: list[ArchiveEntry] = []
    try:
        files = sorted(
            (p for p in h.iterdir() if p.is_file() and p.name.endswith(".json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    for f in files:
        e = _entry_for_file(f)
        if e is not None:
            entries.append(e)
    return entries


def read_archive(session_dir: Path, filename: str) -> Optional[bytes]:
    """Return the raw bytes of one archive file. Validates the
    filename before touching disk so a hostile request like
    ``../../etc/passwd`` is rejected upfront."""
    if not is_safe_filename(filename):
        return None
    h = history_dir(session_dir)
    if not h.is_dir():
        return None
    path = h / filename
    if not _is_safe_subpath(h, path):
        return None
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def delete_archive(session_dir: Path, filename: str) -> bool:
    """Delete one archive file. Returns ``True`` if the file existed
    and was removed, ``False`` otherwise (missing / unsafe name /
    permission error)."""
    if not is_safe_filename(filename):
        return False
    h = history_dir(session_dir)
    if not h.is_dir():
        return False
    path = h / filename
    if not _is_safe_subpath(h, path):
        return False
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("history: delete %s failed: %s", path, exc)
        return False
    logger.info("history: deleted %s", path.name)
    return True


def embed_metadata(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Inject a ``local_scribe`` block into a Char transcript payload
    in-place and return the payload for chaining. Char's parser
    ignores unknown top-level keys -- we verified this against the
    tinybase persister source -- so this is a no-op for Char while
    giving us a self-describing record of what wrote the file."""
    payload["local_scribe"] = dict(metadata)
    return payload


def restore_archive(session_dir: Path, filename: str) -> Optional[Path]:
    """Restore one archived transcript over ``transcript.json``.

    The current file is archived first (so a restore is itself
    reversible). Returns the path of the restored transcript on
    success, ``None`` on failure.

    Not currently exposed by the inspector UI (the user asked only
    for view + delete), but we surface it as a stable API so the
    restore button can be added later without touching this module.
    """
    if not is_safe_filename(filename):
        return None
    src = history_dir(session_dir) / filename
    if not src.is_file():
        return None
    if not _is_safe_subpath(history_dir(session_dir), src):
        return None
    archive_existing_transcript(session_dir)
    dest = session_dir / "transcript.json"
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        logger.warning("history: restore %s -> %s failed: %s", src, dest, exc)
        return None
    logger.info("history: restored %s -> transcript.json", src.name)
    return dest
