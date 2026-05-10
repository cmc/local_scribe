"""Workaround for Char's streaming-batch persistence bug.

Background
----------
Char's progressive batch path (the one triggered by ``gpt-4o-transcribe``,
which we use to bypass the 60-second ``BATCH_IDLE_TIMEOUT``) parses our
SSE response in
``crates/owhisper-client/src/adapter/openai/batch.rs::parse_sse_block``
and emits a ``BatchStreamEvent::Result`` whose ``words`` array is
**hardcoded to ``Vec::new()``** (line 276). The downstream persist
callback in ``apps/desktop/src/stt/useRunBatch.ts`` then short-circuits
on ``if (words.length === 0) return;`` (line 121), so **no
``transcript.json`` is ever written for any audio that takes Char's
progressive code path**. (And the non-progressive path aborts long files
at the 60s mark.) See ``CHAR_REVIEW.md § Streaming-batch persistence
bug`` for the full source-level walk-through.

Workaround
----------
After our ASR (and optionally diarization) finishes, identify the Char
session that uploaded the audio by SHA256-matching the temp file we
received against every ``audio.mp3`` under
``~/Library/Application Support/hyprnote/sessions/<uuid>/``. If we find
a unique match, write ``transcript.json`` straight to disk in Char's
exact persister schema (validated against
``apps/desktop/src/store/tinybase/persister/session/load/transcript.ts``).
Char picks the file up the next time the session is re-opened or on
app relaunch.

Why SHA256: the Char request multipart form contains only ``file``,
``model``, ``response_format``, ``stream``, and language — never a
session ID. The audio bytes are the only stable join key.

This module is loopback-only: it touches files inside the user's own
home directory under ``hyprnote``, never anywhere else. Refusing to
write outside that root is enforced by ``_resolve_session_dir``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


logger = logging.getLogger("local_scribe.char_persist")


# Char's persisted memo is a tiptap/prose-mirror document JSON wrapped as
# a string. An empty document -> a single empty paragraph. Char's UI
# happily replaces this with the user's own notes the first time they
# type into the Note field; we just need to put a syntactically valid
# placeholder here so its parser doesn't reject the row.
_EMPTY_MEMO_MD = json.dumps({"type": "doc", "content": [{"type": "paragraph"}]})


@dataclass
class WrittenTranscript:
    """Return value from ``write_transcript_for_audio`` so the caller
    (asr_server.py) can log + surface in the response."""

    session_dir: Path
    session_id: str
    transcript_path: Path
    word_count: int
    speaker_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_dir": str(self.session_dir),
            "session_id": self.session_id,
            "transcript_path": str(self.transcript_path),
            "word_count": self.word_count,
            "speaker_count": self.speaker_count,
            "sha256": self.sha256,
        }


def sha256_file(path: os.PathLike[str] | str, chunk: int = 1 << 20) -> str:
    """SHA256 the file in 1 MiB chunks. Returns hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def _is_safe_subpath(root: Path, candidate: Path) -> bool:
    """Defence-in-depth: refuse to write outside ``root``. Both paths
    must be resolved to canonical absolute form before comparison so a
    symlink can't smuggle us elsewhere."""
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


def find_session_dir_for_audio(
    audio_path: os.PathLike[str] | str,
    char_data_dir: Path,
    *,
    audio_sha: Optional[str] = None,
) -> tuple[Optional[Path], str]:
    """Walk ``<char_data_dir>/sessions/<uuid>/audio.mp3`` looking for
    one whose SHA256 matches the uploaded audio. Returns ``(dir or
    None, sha256)`` so the caller can log the hash even on miss."""
    target = audio_sha or sha256_file(audio_path)
    sessions_dir = char_data_dir / "sessions"
    if not sessions_dir.is_dir():
        return None, target

    matches: list[Path] = []
    # Newest first -- if there happen to be duplicate audio (rare, but
    # possible if the user re-uploaded the same MP3 twice) we prefer
    # the most recently created session.
    candidates = sorted(
        (d for d in sessions_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in candidates:
        if not _is_safe_subpath(char_data_dir, d):
            continue
        a = d / "audio.mp3"
        if not a.is_file():
            continue
        try:
            if sha256_file(a) == target:
                matches.append(d)
                if len(matches) >= 2:
                    break
        except OSError:
            continue

    if not matches:
        return None, target
    if len(matches) > 1:
        # Same audio.mp3 in two sessions (rare). Pick the newest and
        # log the ambiguity so doctor can flag it.
        logger.warning(
            "char_persist: %d sessions share the same audio.mp3 hash %s; "
            "writing to the most recent one (%s). Dedupe in Char if this is unintended.",
            len(matches), target[:12], matches[0].name,
        )
    return matches[0], target


def _ms(seconds: Optional[float]) -> int:
    """Convert seconds (float) to integer milliseconds the way Char
    rounds them in its persister (``Math.round`` -> banker's-rounding-
    equivalent for our positive values)."""
    if seconds is None:
        return 0
    return int(round(float(seconds) * 1000.0))


def _normalise_word_text(text: str) -> str:
    """Char preserves punctuation in ``text`` (visible in
    transcript.json: ``" They"``, ``" were"`` -- with leading space).
    Our Deepgram-style words emit ``punctuated_word`` separately; we
    favour that and fall back to ``word`` to match the visual style of
    Char's own diarized writes."""
    if not text:
        return ""
    # Always lead with a space so the rendered transcript spaces
    # naturally between words. Char's UI joins by concatenation, not by
    # interpolating spaces between word boxes.
    if text.startswith(" "):
        return text
    return " " + text


def _coerce_speaker_index(value: Any, *, fallback: int = 0) -> int:
    """Map sherpa-onnx / asr_server speaker labels into the integer
    ``speaker_index`` Char's persister UI expects.

    Accepts:

      * ``None`` -> ``fallback`` (no diarization info on this word)
      * ``int`` (0, 1, 2, ...) -> returned as-is
      * ``"speaker_0"``, ``"speaker_1"``, ... -> trailing integer parsed
        (asr_server's auto-skip / single-speaker fallback writes this)
      * ``"SPEAKER_00"``, ``"SPEAKER_01"``, ... -> trailing integer
        parsed (raw sherpa-onnx output before remapping)
      * any other string -> ``fallback`` (don't crash on unexpected
        labels; better to ship a transcript with one speaker than no
        transcript at all)

    Splitting this out as a typed helper because we hit the bug
    repeatedly: the ASR layer mixes string labels and ints depending on
    which branch it took (auto-skip vs real diarization), so the
    sidecar writer has to be defensive.
    """
    if value is None:
        return int(fallback)
    if isinstance(value, bool):
        return int(fallback)
    if isinstance(value, int):
        return value
    s = str(value)
    try:
        return int(s)
    except ValueError:
        pass
    if "_" in s:
        tail = s.rsplit("_", 1)[1]
        try:
            return int(tail)
        except ValueError:
            pass
    return int(fallback)


def _build_words_and_hints(
    words: Iterable[dict[str, Any]],
    *,
    provider: str,
    channel: int,
    speaker_for_index: Optional[list[int]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate our internal word list into Char's
    ``{words, speaker_hints}`` pair.

    Each word becomes one entry under ``words`` with a fresh UUID id +
    ``start_ms`` / ``end_ms`` / ``text`` / ``channel``. Each word also
    spawns a parallel ``speaker_hints`` row of the
    ``provider_speaker_index`` shape Char's UI knows how to colour.
    """
    out_words: list[dict[str, Any]] = []
    out_hints: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        wid = str(uuid.uuid4())
        text_field = (
            w.get("punctuated_word")
            or w.get("text")
            or w.get("word")
            or ""
        )
        out_words.append({
            "id": wid,
            "text": _normalise_word_text(str(text_field)),
            "start_ms": _ms(w.get("start") if w.get("start") is not None else w.get("start_ms")),
            "end_ms": _ms(w.get("end") if w.get("end") is not None else w.get("end_ms")),
            "channel": int(w.get("channel", channel)),
        })
        speaker_index = _coerce_speaker_index(
            w.get("speaker"),
            fallback=(
                speaker_for_index[i]
                if speaker_for_index and i < len(speaker_for_index)
                else 0
            ),
        )
        out_hints.append({
            "id": str(uuid.uuid4()),
            "type": "provider_speaker_index",
            "value": json.dumps(
                {"provider": provider, "channel": int(w.get("channel", channel)),
                 "speaker_index": int(speaker_index)},
                separators=(",", ":"),
            ),
            "word_id": wid,
        })
    return out_words, out_hints


def _existing_transcript_id(session_dir: Path) -> Optional[str]:
    """If transcript.json already exists, reuse its top-level
    transcript ``id`` so we don't fork the row away from any UI state
    that already references it (e.g. speaker labels the user typed)."""
    p = session_dir / "transcript.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text() or "{}")
        for t in (d.get("transcripts") or []):
            tid = t.get("id")
            if isinstance(tid, str) and tid:
                return tid
    except (OSError, json.JSONDecodeError):
        return None
    return None


def write_transcript_for_audio(
    audio_path: os.PathLike[str] | str,
    char_data_dir: Path,
    *,
    words: list[dict[str, Any]],
    language: str = "en",
    provider: str = "openai",
    channel: int = 0,
    user_id: str = "00000000-0000-0000-0000-000000000000",
    started_at_ms: Optional[int] = None,
    audio_sha: Optional[str] = None,
    request_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    archive_previous: bool = True,
) -> Optional[WrittenTranscript]:
    """Locate Char's matching session by audio SHA and persist a
    transcript.json in Char's exact schema.

    Returns ``None`` if no matching session was found (caller should
    treat this as a no-op + log it). Returns a ``WrittenTranscript``
    on success so callers can include the path in their response /
    logs.
    """
    session_dir, audio_sha = find_session_dir_for_audio(
        audio_path, char_data_dir, audio_sha=audio_sha,
    )
    if session_dir is None:
        logger.info(
            "char_persist: no Char session matches uploaded audio (sha256=%s); skipping",
            audio_sha[:12],
        )
        return None

    # Re-validate the resolved path is inside char_data_dir (defense
    # against TOCTOU / symlink games between match and write).
    if not _is_safe_subpath(char_data_dir, session_dir):
        logger.error(
            "char_persist: refusing write -- session_dir %s escapes %s",
            session_dir, char_data_dir,
        )
        return None

    transcript_id = _existing_transcript_id(session_dir) or str(uuid.uuid4())
    if started_at_ms is None:
        started_at_ms = int(time.time() * 1000)

    out_words, out_hints = _build_words_and_hints(
        words, provider=provider, channel=channel,
    )

    speaker_count = len({
        json.loads(h["value"]).get("speaker_index", 0) for h in out_hints
    })

    payload: dict[str, Any] = {
        "transcripts": [
            {
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(),
                ),
                "id": transcript_id,
                "memo_md": _EMPTY_MEMO_MD,
                "session_id": session_dir.name,
                "speaker_hints": out_hints,
                "started_at": int(started_at_ms),
                "user_id": user_id,
                "words": out_words,
            },
        ],
    }

    # Embed an out-of-band ``local_scribe`` metadata block so the
    # next overwrite can record which ASR model + diarization path
    # produced the file we're about to archive. Char ignores unknown
    # top-level keys (verified against tinybase's session loader), so
    # this is invisible to Char's UI but visible to our inspector.
    embedded_meta: dict[str, Any] = {
        "written_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        ),
        "written_at_unix": time.time(),
        "word_count": len(out_words),
        "speaker_count": speaker_count,
        "language": language,
        "provider": provider,
        "audio_sha256": audio_sha,
        "session_id": session_dir.name,
    }
    if metadata:
        for k, v in metadata.items():
            embedded_meta.setdefault(k, v)
    payload["local_scribe"] = embedded_meta

    # Archive the previous transcript.json (if any) BEFORE we
    # overwrite it. Imported lazily so the module's import graph stays
    # simple in test settings that mock char_persist without the
    # history module.
    if archive_previous:
        try:
            import transcript_history as _hist
            _hist.archive_existing_transcript(
                session_dir, request_id=request_id,
            )
        except Exception:
            logger.exception(
                "[char_persist%s] history archive failed (continuing with "
                "overwrite)", f" {request_id}" if request_id else "",
            )

    # Atomic write: tmpfile + rename so a crashed half-write can't
    # leave Char's persister parsing a truncated JSON document.
    transcript_path = session_dir / "transcript.json"
    tmp_path = session_dir / f"transcript.json.tmp.{os.getpid()}"
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp_path, transcript_path)

    logger.info(
        "[char_persist%s] wrote transcript.json to %s (words=%d, speakers=%d, sha256=%s)",
        f" {request_id}" if request_id else "",
        transcript_path, len(out_words), speaker_count, audio_sha[:12],
    )

    return WrittenTranscript(
        session_dir=session_dir,
        session_id=session_dir.name,
        transcript_path=transcript_path,
        word_count=len(out_words),
        speaker_count=speaker_count,
        sha256=audio_sha,
    )
