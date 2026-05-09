"""
Manual transcribe-and-summarize CLI for the local ASR + LM Studio rig.

Use case: when Char doesn't auto-transcribe an audio file you dragged
into it, run this script to (1) transcribe the audio using either
NVIDIA Parakeet TDT v3 (default; lowest WER for English on the OpenASR
leaderboard, runs locally via parakeet-mlx) or the legacy Whisper server,
then (2) feed the transcript into LM Studio (Qwen3 by default) to produce
a structured call summary with these sections:

    - Participants
    - Key points discussed
    - Points agreed
    - Next steps

The summary prints to the terminal and can be copied to the clipboard with
--copy so you can paste it back into Char's note for that recording. Note
that for live recordings Char does its own LLM pass against the same LM
Studio backend - this script only matters for the manual / file-upload path.

Usage:
    python transcribe_file.py path/to/call.m4a
    python transcribe_file.py path/to/call.m4a --asr-backend whisper   # use the HTTP whisper server
    python transcribe_file.py path/to/call.m4a --progress
    python transcribe_file.py path/to/call.m4a --copy
    python transcribe_file.py path/to/call.m4a --no-llm
    python transcribe_file.py path/to/call.m4a --save out.md     # clean markdown summary
    python transcribe_file.py path/to/call.m4a --save out.json   # full bundle
    python transcribe_file.py path/to/call.m4a --save out.txt    # raw transcript only
    python transcribe_file.py path/to/call.m4a --no-cache        # bypass cache for this run
    python transcribe_file.py path/to/call.m4a --call-time "2026-05-08T14:30"
    python transcribe_file.py --list-cache                        # table of cached transcripts
    python transcribe_file.py --clear-cache                       # wipe the cache

Caching & bulk analysis:
    Transcripts are cached to ~/.cache/whisper_server/transcripts/<sha256>.json
    keyed by the audio file's content hash. Each cache entry embeds a `_source`
    block with original path, file mtime, hash, transcribed-at timestamp, and
    Whisper-detected duration - so bulk analysis is a simple glob:

        for f in ~/.cache/whisper_server/transcripts/*.json:
            data = json.load(open(f))
            print(data["_source"]["path"], data["_source"]["transcribed_at_human"])

    Or use --list-cache for a quick tabular overview. Override the location
    with --cache-dir or WHISPER_CACHE_DIR.

Env overrides:
    ASR_BACKEND        default parakeet (alternative: whisper)
    PARAKEET_MODEL     default mlx-community/parakeet-tdt-0.6b-v3
    WHISPER_URL        default http://127.0.0.1:8000/v1/listen
    LLM_URL            default http://127.0.0.1:1234/v1/chat/completions
    LLM_MODEL          default qwen3-30b-a3b-instruct-2507
    LLM_MAX_TOKENS     default 4096
    WHISPER_CACHE_DIR  default ~/.cache/whisper_server/transcripts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_ASR_BACKEND = os.getenv("ASR_BACKEND", "parakeet").lower()
DEFAULT_PARAKEET_MODEL = os.getenv(
    "PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"
)
DEFAULT_DIARIZE = os.getenv("DIARIZE", "1").strip() not in {"0", "false", "no", ""}
DEFAULT_NUM_SPEAKERS = int(os.getenv("NUM_SPEAKERS", "0")) or None
DEFAULT_CLUSTER_THRESHOLD = float(os.getenv("CLUSTER_THRESHOLD", "0.5"))
DEFAULT_WHISPER_URL = os.getenv("WHISPER_URL", "http://127.0.0.1:8000/v1/listen")
DEFAULT_LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:1234/v1/chat/completions")
DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-30b-a3b-instruct-2507")
DEFAULT_LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
DEFAULT_CACHE_DIR = Path(
    os.getenv("WHISPER_CACHE_DIR")
    or (Path.home() / ".cache" / "whisper_server" / "transcripts")
)


_SCRIPT_START = time.time()


def _ts() -> str:
    """Elapsed-time prefix for verbose progress lines: '[12.3s] '."""
    return f"[{time.time() - _SCRIPT_START:6.1f}s] "


def _log(msg: str, *, indent: bool = False) -> None:
    """Verbose progress log that always shows where we are in the pipeline."""
    prefix = "         " if indent else _ts()
    print(prefix + msg, flush=True)


def _human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f} {u}" if u != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num_bytes} B"
DEFAULT_SYSTEM_PROMPT = """\
You are an expert call-summary assistant. The user will give you (1) a small
metadata block about the recording and (2) the raw transcript. Produce a
detailed, well-structured markdown summary that someone can revisit weeks or
months later and instantly understand what happened.

# Output structure

Render exactly the sections below, in this order, using the markdown shown.

# {Inferred title - 6-10 words capturing the call's essence}

**Recorded:** {from metadata - call_recorded_at, with the qualifier shown there}  \n\
**Transcribed:** {from metadata - transcribed_at}  \n\
**Duration:** {from metadata - duration_human}  \n\
**Language:** {from metadata - language}  \n\
**Source:** `{from metadata - source_file}`

## TL;DR
2-3 sentences. Someone reading only this section should know what the call
was about, who was on it, and the most important outcome.

## Context & purpose
Why this call happened. Prior context referenced (earlier calls, threads,
projects, deadlines). Omit this section entirely if no context was
established on the call.

## Participants
- **Name or role** (affiliation if mentioned) - one line on their stake or
  position in the conversation.

Use real names when introduced or addressed by name. Otherwise label by role
("Caller", "Support agent", "Host", "Customer"). Never guess names.

## Discussion
Group the substantive content by *topic*, not chronology. Each topic is an
H3 with a 1-3 sentence summary paragraph followed by bullets for specifics:
numbers, dates, names, concrete examples, partial conclusions, etc.

### {Topic 1}
{1-3 sentence summary paragraph.}
- {specific 1}
- {specific 2}

### {Topic 2}
... and so on for each substantive topic covered.

## Decisions
Concrete things agreed on the call. Each decision must be a standalone
sentence understandable without reading the rest of the note.
- {What was decided}{ - one-clause context if needed for clarity}.

If no decisions were reached, write exactly:
- *No explicit decisions reached on this call.*

## Action items
Format: `- **Owner** - action (timeframe)`. Drop the parenthetical if no
timeframe was given. Use "Unassigned" if the owner is unclear.

If no action items were defined, write exactly:
- *No action items defined on this call.*

## Open questions
Issues raised but not resolved - the unfinished-business list. Omit this
section entirely if everything raised was answered.

## Risks & concerns
Problems, blockers, or worries flagged on the call. Distinct from open
questions: these are *known problems*, not unknowns. Omit this section
entirely if none were raised.

## Notable quotes
0-3 short verbatim quotes that capture nuance the summary loses. Format:
> "..." - {speaker}

Omit this section entirely if none qualify. Copy verbatim from the
transcript - never paraphrase or invent.

# Rules

- Extract only. Never invent facts, names, numbers, dates, or quotes.
- Where a section permits omission and has no content, omit it entirely
  rather than padding. Where it doesn't (TL;DR, Participants, Decisions,
  Action items), use the explicit "no content" line shown above.
- Group Discussion by topic, not chronology. A reader should be able to
  jump to any H3 section in isolation and understand it.
- Keep speaker labels consistent throughout the note.
- Do not include the raw transcript or large quoted blocks of it.
- Do not output any preamble, postscript, or commentary outside the
  structured output. Start with the title (H1) and stop after the last
  rendered section.
"""


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Stream-hash a file's contents. ~250MB/s on modern hardware, plenty fast
    even for hour-long recordings."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_path_for(audio_path: Path, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Cache key is the sha256 of the audio file's contents - the cache survives
    file renames and only invalidates when the audio actually changes."""
    return cache_dir / f"{file_sha256(audio_path)}.json"


def cache_load(cache_file: Path) -> dict | None:
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def cache_save(cache_file: Path, payload: dict) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload))


def annotate_payload_with_source(payload: dict, file_path: Path, sha: str) -> dict:
    """Attach a `_source` block to a Deepgram-shaped payload so cache entries
    are self-describing - useful for `--list-cache` and bulk analysis later.
    The Deepgram-shaped `metadata` and `results` keys are left untouched."""
    try:
        st = file_path.stat()
        size_bytes = st.st_size
        file_mtime = st.st_mtime
    except OSError:
        size_bytes = 0
        file_mtime = None
    pmeta = payload.get("metadata") or {}
    models = pmeta.get("models") or []
    model_info = pmeta.get("model_info") or {}
    payload["_source"] = {
        "path": str(file_path.resolve()),
        "name": file_path.name,
        "size_bytes": size_bytes,
        "sha256": sha,
        "file_mtime": file_mtime,
        "file_mtime_human": _format_local(file_mtime) if file_mtime else None,
        "transcribed_at_unix": time.time(),
        "transcribed_at_human": _format_local(time.time()),
        "whisper_duration_seconds": float(pmeta.get("duration") or 0.0),
        "whisper_model": models[0] if models else None,
        "whisper_model_info": model_info.get(models[0]) if models else None,
    }
    return payload


def cache_clear(cache_dir: Path = DEFAULT_CACHE_DIR) -> int:
    """Wipe the on-disk cache. Returns number of entries removed."""
    if not cache_dir.exists():
        return 0
    count = sum(1 for _ in cache_dir.glob("*.json"))
    shutil.rmtree(cache_dir, ignore_errors=True)
    return count


def cache_list(cache_dir: Path = DEFAULT_CACHE_DIR) -> list[dict]:
    """Return a list of cached transcripts with their source metadata, sorted
    newest-first by transcribed_at."""
    if not cache_dir.exists():
        return []
    entries: list[dict] = []
    for f in cache_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        src = data.get("_source", {}) or {}
        results = (data.get("results") or {}).get("channels") or [{}]
        alt = (results[0].get("alternatives") or [{}])[0]
        transcript = alt.get("transcript", "") or ""
        entries.append({
            "cache_file": str(f),
            "sha256": src.get("sha256") or f.stem,
            "source_path": src.get("path"),
            "source_name": src.get("name"),
            "size_bytes": src.get("size_bytes"),
            "file_mtime_human": src.get("file_mtime_human"),
            "transcribed_at_human": src.get("transcribed_at_human"),
            "duration_seconds": src.get("whisper_duration_seconds"),
            "whisper_model": src.get("whisper_model"),
            "transcript_chars": len(transcript),
            "transcript_words": len(transcript.split()),
            "language": (alt.get("languages") or ["?"])[0] if alt.get("languages")
                        else (results[0].get("detected_language") or "?"),
        })
    entries.sort(
        key=lambda e: e.get("transcribed_at_human") or "",
        reverse=True,
    )
    return entries


def print_cache_listing(cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    entries = cache_list(cache_dir)
    print(f"📚 Cache: {cache_dir}")
    if not entries:
        print("   (empty)")
        return
    print(f"   {len(entries)} cached transcript(s)")
    print()
    print(f"   {'#':>3}  {'Recorded':<22}  {'Duration':>10}  {'Words':>7}  {'Lang':<5}  {'Model':<18}  Source")
    print(f"   {'-'*3}  {'-'*22}  {'-'*10}  {'-'*7}  {'-'*5}  {'-'*18}  {'-'*40}")
    for i, e in enumerate(entries, 1):
        rec = (e.get("file_mtime_human") or "unknown")[:22]
        dur = _format_duration(e.get("duration_seconds") or 0)
        words = e.get("transcript_words") or 0
        lang = (e.get("language") or "?")[:5]
        model = (e.get("whisper_model") or "?")[:18]
        path = e.get("source_path") or e.get("source_name") or e["cache_file"]
        print(f"   {i:>3}  {rec:<22}  {dur:>10}  {words:>7,}  {lang:<5}  {model:<18}  {path}")


def transcribe(file_path: Path, whisper_url: str) -> dict:
    print(f"⏳ Transcribing {file_path.name} via {whisper_url} ...", flush=True)
    with file_path.open("rb") as f:
        audio = f.read()
    resp = requests.post(
        whisper_url,
        data=audio,
        headers={
            "Content-Type": "application/octet-stream",
            "Authorization": "Token local",
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()


def transcribe_with_parakeet(
    file_path: Path,
    model_id: str = DEFAULT_PARAKEET_MODEL,
    progress: bool = True,
) -> dict:
    """Run transcription in-process via parakeet-mlx and return a
    Deepgram-shaped payload (so the caching/summary downstream is
    backend-agnostic).

    Lazy-imports `parakeet_backend` so users who never enable the
    Parakeet backend don't need parakeet-mlx installed."""
    from parakeet_backend import transcribe_to_deepgram

    print(
        f"⏳ Transcribing {file_path.name} locally via parakeet-mlx "
        f"(model={model_id}) ...",
        flush=True,
    )

    last_render_len = {"n": 0}

    def on_start(duration_s: float, language: str) -> None:
        print(f"   audio: {duration_s:.1f}s, language={language}", flush=True)

    def on_progress(progress_frac: float, current_s: float, total_s: float) -> None:
        if not progress:
            return
        bar = _render_bar(progress_frac)
        line = (
            f"   {bar} {progress_frac * 100:5.1f}%  "
            f"{current_s:6.1f}s / {total_s:6.1f}s"
        )
        pad = max(0, last_render_len["n"] - len(line))
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        last_render_len["n"] = len(line)

    payload = transcribe_to_deepgram(
        file_path,
        model_id=model_id,
        on_start=on_start,
        on_progress=on_progress if progress else None,
    )

    if progress and last_render_len["n"]:
        sys.stdout.write(
            "\r   "
            + _render_bar(1.0)
            + " 100.0%"
            + " " * max(0, last_render_len["n"] - 40)
            + "\n"
        )
        sys.stdout.flush()
    return payload


def _render_bar(progress: float, width: int = 28) -> str:
    progress = max(0.0, min(1.0, progress))
    filled = int(round(width * progress))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _stream_url_for(whisper_url: str) -> str:
    if whisper_url.endswith("/v1/listen"):
        return whisper_url + "/stream"
    if whisper_url.endswith("/v1/listen/"):
        return whisper_url[:-1] + "/stream"
    if "/v1/listen/stream" in whisper_url:
        return whisper_url
    return whisper_url.rstrip("/") + "/stream"


def transcribe_streaming(file_path: Path, whisper_url: str) -> dict | None:
    stream_url = _stream_url_for(whisper_url)
    print(f"⏳ Transcribing {file_path.name} via {stream_url} ...", flush=True)
    with file_path.open("rb") as f:
        audio = f.read()
    final: dict | None = None
    last_render_len = 0
    with requests.post(
        stream_url,
        data=audio,
        headers={
            "Content-Type": "application/octet-stream",
            "Authorization": "Token local",
        },
        stream=True,
        timeout=600,
    ) as resp:
        resp.raise_for_status()
        resp.encoding = "utf-8"
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "start":
                dur = event.get("duration", 0.0)
                lang = event.get("language", "?")
                print(f"   audio: {dur:.1f}s, language={lang}", flush=True)
            elif etype == "segment":
                pct = event.get("progress", 0.0) * 100
                seg = event.get("segment", {})
                snippet = seg.get("text", "")[:60].replace("\n", " ")
                line = f"   {_render_bar(event.get('progress', 0.0))} {pct:5.1f}%  {snippet}"
                pad = max(0, last_render_len - len(line))
                sys.stdout.write("\r" + line + (" " * pad))
                sys.stdout.flush()
                last_render_len = len(line)
            elif etype == "done":
                if last_render_len:
                    sys.stdout.write(
                        "\r   " + _render_bar(1.0) + " 100.0%" + " " * max(0, last_render_len - 40) + "\n"
                    )
                    sys.stdout.flush()
                final = event.get("result")
            elif etype == "error":
                if last_render_len:
                    sys.stdout.write("\n")
                print(f"❌ Server error: {event.get('message')}", file=sys.stderr)
                return None
    return final


def extract_transcript(payload: dict) -> str:
    return payload["results"]["channels"][0]["alternatives"][0]["transcript"].strip()


def extract_words(payload: dict) -> list[dict]:
    """Pull the word-level alignment list out of a Deepgram-shaped payload.
    Returns [] if the ASR backend didn't emit word-level timestamps."""
    try:
        return list(
            payload["results"]["channels"][0]["alternatives"][0].get("words") or []
        )
    except (KeyError, IndexError, TypeError):
        return []


def run_diarization(
    audio_path: Path,
    payload: dict,
    *,
    num_speakers: int | None = None,
    cluster_threshold: float = 0.5,
    progress: bool = True,
    llm_url: str | None = None,
    llm_model: str | None = None,
) -> dict | None:
    """Orchestrates the diarization pipeline:
        1. sherpa-onnx OfflineSpeakerDiarization → speaker turns
        2. align with the ASR's word-level timestamps
        3. group consecutive same-speaker words into turn lines
        4. (optional) ask the LLM to map SPEAKER_NN → real names

    Returns a dict with `lines`, `speaker_labels`, `diarized_text` (with
    label substitution applied), and `raw_speaker_text` (with raw labels)
    or None if diarization was skipped because there were no word
    timestamps available."""
    words = extract_words(payload)
    if not words:
        _log(
            "⚠️  ASR payload has no word-level timestamps - skipping diarization. "
            "Check that the ASR backend emits a 'words' array."
        )
        return None

    from diarization_backend import (
        attach_speaker_to_words,
        diarize,
        group_words_into_lines,
        infer_speaker_labels,
        render_diarized_transcript,
    )

    _log(
        "🧑‍🤝‍🧑 Running speaker diarization "
        f"(sherpa-onnx, num_speakers={num_speakers or 'auto'}, "
        f"threshold={cluster_threshold:.2f}) ..."
    )

    last_render_len = {"n": 0}

    def on_progress(frac: float) -> None:
        if not progress:
            return
        bar = _render_bar(frac)
        line = f"   {bar} {frac * 100:5.1f}%  (diarization)"
        pad = max(0, last_render_len["n"] - len(line))
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        last_render_len["n"] = len(line)

    t0 = time.time()
    turns = diarize(
        audio_path,
        num_clusters=num_speakers,
        cluster_threshold=cluster_threshold,
        on_progress=on_progress if progress else None,
    )
    if progress and last_render_len["n"]:
        sys.stdout.write(
            "\r   " + _render_bar(1.0) + " 100.0%" + " " * 20 + "\n"
        )
        sys.stdout.flush()

    speaker_count = len({t["speaker"] for t in turns})
    _log(
        f"   diarization: {len(turns)} turns, {speaker_count} speakers, "
        f"{time.time() - t0:.1f}s",
        indent=True,
    )

    diarized_words = attach_speaker_to_words(words, turns)
    lines = group_words_into_lines(diarized_words)
    raw_text = render_diarized_transcript(lines)

    speaker_labels: dict[str, str] = {}
    if llm_url and llm_model:
        _log("🏷️  Asking LLM to identify speakers from context ...")
        speaker_labels = infer_speaker_labels(
            raw_text, llm_url=llm_url, llm_model=llm_model,
        )
        if speaker_labels:
            _log(
                "   labels: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(speaker_labels.items())
                ),
                indent=True,
            )
        else:
            _log("   (no labels inferred - using raw SPEAKER_NN ids)", indent=True)

    diarized_text = render_diarized_transcript(lines, speaker_labels=speaker_labels)
    return {
        "lines": lines,
        "speaker_labels": speaker_labels,
        "raw_speaker_text": raw_text,
        "diarized_text": diarized_text,
        "speaker_count": speaker_count,
        "turn_count": len(turns),
    }


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _format_local(ts: float) -> str:
    """Format a unix timestamp as a local-time string with offset."""
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _parse_call_time(s: str | None) -> tuple[float | None, str | None]:
    """Parse a user-supplied --call-time. Accepts 'now', ISO 8601, or a unix epoch.
    Returns (unix_ts, display_string) or (None, None) on failure."""
    if not s:
        return None, None
    s = s.strip()
    if s.lower() == "now":
        ts = time.time()
        return ts, _format_local(ts)
    try:
        return float(s), _format_local(float(s))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp(), dt.strftime("%Y-%m-%d %H:%M %Z").strip()
    except ValueError:
        return None, None


def build_metadata(
    payload: dict,
    file_path: Path,
    call_time_override: str | None = None,
) -> dict:
    meta = payload.get("metadata", {}) or {}
    results = payload.get("results", {}) or {}
    channels = results.get("channels") or [{}]
    channel0 = channels[0]
    alt0 = (channel0.get("alternatives") or [{}])[0]
    languages = alt0.get("languages") or []
    language = (
        languages[0] if languages
        else channel0.get("detected_language")
        or "unknown"
    )
    duration_s = float(meta.get("duration") or 0.0)

    source = payload.get("_source", {}) or {}

    if call_time_override:
        ts, display = _parse_call_time(call_time_override)
        if ts is not None and display:
            recorded_at = display
            recorded_qualifier = display + " (user-provided)"
            recorded_unix = ts
        else:
            recorded_at = recorded_qualifier = "unknown"
            recorded_unix = None
    else:
        mtime = source.get("file_mtime")
        if mtime is None:
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None
        if mtime is not None:
            recorded_at = _format_local(mtime)
            recorded_qualifier = recorded_at + " (file mtime - approximate)"
            recorded_unix = mtime
        else:
            recorded_at = recorded_qualifier = "unknown"
            recorded_unix = None

    transcribed_unix = source.get("transcribed_at_unix") or time.time()
    transcribed_at = _format_local(transcribed_unix)

    return {
        "source_file": file_path.name,
        "source_path": str(file_path.resolve()),
        "duration_seconds": duration_s,
        "duration_human": _format_duration(duration_s),
        "language": language,
        "recorded_at": recorded_at,
        "call_recorded_at": recorded_qualifier,
        "recorded_at_unix": recorded_unix,
        "transcribed_at": transcribed_at,
        "transcribed_at_unix": transcribed_unix,
    }


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_think_tags(text: str) -> str:
    """Remove Qwen3-style <think>...</think> reasoning blocks from a response."""
    return _THINK_BLOCK_RE.sub("", text).strip()


def _approx_token_count(text: str) -> int:
    """Rough token count (~4 chars per token). Good enough for warnings."""
    return max(1, len(text) // 4)


def _build_llm_request_body(
    transcript: str, model: str, system_prompt: str,
    metadata: dict | None, max_tokens: int, no_think: bool, stream: bool,
) -> tuple[dict, int]:
    if metadata:
        meta_block = (
            "Metadata:\n"
            f"- Source file: {metadata.get('source_file', 'unknown')}\n"
            f"- call_recorded_at: {metadata.get('call_recorded_at', 'unknown')}\n"
            f"- transcribed_at: {metadata.get('transcribed_at', 'unknown')}\n"
            f"- duration_human: {metadata.get('duration_human', 'unknown')}\n"
            f"- language: {metadata.get('language', 'unknown')}\n"
        )
    else:
        meta_block = "Metadata: not available.\n"
    user_msg = f"{meta_block}\nTranscript:\n\n{transcript}"
    if no_think:
        user_msg = "/no_think\n\n" + user_msg
    approx_in = _approx_token_count(system_prompt) + _approx_token_count(user_msg)
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": stream,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body, approx_in


def _raise_llm_http_error(resp, llm_url: str) -> None:
    body_text = resp.text
    try:
        err = resp.json()
        body_text = json.dumps(err, indent=2)
    except ValueError:
        pass
    raise requests.HTTPError(
        f"{resp.status_code} {resp.reason} from {llm_url}\n"
        f"Response body:\n{body_text}",
        response=resp,
    )


def run_llm(
    transcript: str,
    llm_url: str,
    model: str,
    system_prompt: str,
    metadata: dict | None = None,
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
    no_think: bool = False,
    stream: bool = True,
    print_stream: bool = True,
) -> str:
    body, approx_in = _build_llm_request_body(
        transcript, model, system_prompt, metadata, max_tokens, no_think, stream,
    )
    _log(f"🤖 Sending to {model} at {llm_url}")
    _log(f"prompt size: ~{approx_in:,} tokens (system + user)", indent=True)
    _log(f"max output: {max_tokens:,} tokens", indent=True)
    _log(f"streaming: {'on' if stream else 'off'}", indent=True)
    _log(f"context required: at least ~{approx_in + max_tokens + 512:,} tokens "
         f"(set in LM Studio Load tab)", indent=True)

    if not stream:
        resp = requests.post(llm_url, json=body, timeout=600)
        if not resp.ok:
            _raise_llm_http_error(resp, llm_url)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _strip_think_tags(content).strip()

    if print_stream:
        _log("💡 CALL SUMMARY (streaming):")
        print()

    chunks: list[str] = []
    delta_count = 0
    completion_tokens: int | None = None
    t_request = time.time()
    t_first = None

    with requests.post(llm_url, json=body, stream=True, timeout=600) as resp:
        if not resp.ok:
            _raise_llm_http_error(resp, llm_url)
        resp.encoding = "utf-8"
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if raw_line.startswith(":"):
                continue
            if raw_line.startswith("data: "):
                raw_line = raw_line[len("data: "):]
            if raw_line.strip() == "[DONE]":
                break
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage")
            if usage:
                completion_tokens = usage.get("completion_tokens")
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                if t_first is None:
                    t_first = time.time()
                chunks.append(piece)
                delta_count += 1
                if print_stream:
                    sys.stdout.write(piece)
                    sys.stdout.flush()

    elapsed = time.time() - t_request
    ttft = (t_first - t_request) if t_first is not None else None
    out_tokens = completion_tokens if completion_tokens is not None else delta_count
    tps = (out_tokens / elapsed) if elapsed > 0 else 0.0

    if print_stream:
        print()
    _log(
        f"✅ LLM done: {out_tokens:,} tokens in {elapsed:.1f}s "
        f"({tps:.1f} tok/s, ttft={ttft:.2f}s)" if ttft is not None else
        f"✅ LLM done: {out_tokens:,} tokens in {elapsed:.1f}s ({tps:.1f} tok/s)"
    )
    if out_tokens == 0:
        raise RuntimeError(
            "LLM returned 0 tokens. Most common cause: prompt + max_tokens exceeds "
            "the model's loaded_context_length. Check it with:\n"
            "    curl -s http://127.0.0.1:1234/api/v0/models | python -m json.tool\n"
            "and reload with a larger context if needed:\n"
            f"    lms unload {model} && lms load {model} --context-length 65536"
        )
    return _strip_think_tags("".join(chunks)).strip()


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Transcribe an audio file with local Whisper, then run it through LM Studio.",
    )
    p.add_argument("file", type=Path, nargs="?",
                   help="Path to the audio file to transcribe (omit when using --clear-cache)")
    p.add_argument("--asr-backend", choices=["parakeet", "whisper"],
                   default=DEFAULT_ASR_BACKEND,
                   help=f"Which ASR engine to use (default: {DEFAULT_ASR_BACKEND}). "
                        "'parakeet' runs NVIDIA Parakeet-TDT v3 in-process via parakeet-mlx "
                        "(top of OpenASR leaderboard for English; recommended for max accuracy). "
                        "'whisper' POSTs to the local Whisper HTTP server.")
    p.add_argument("--parakeet-model", default=DEFAULT_PARAKEET_MODEL,
                   help=f"Parakeet model id (default: {DEFAULT_PARAKEET_MODEL})")
    p.add_argument("--server", default=DEFAULT_WHISPER_URL,
                   help=f"Whisper server URL (default: {DEFAULT_WHISPER_URL})")
    p.add_argument("--llm-url", default=DEFAULT_LLM_URL,
                   help=f"LM Studio chat completions URL (default: {DEFAULT_LLM_URL})")
    p.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                   help=f"LLM model id (default: {DEFAULT_LLM_MODEL})")
    p.add_argument("--system", default=DEFAULT_SYSTEM_PROMPT,
                   help="System prompt for the LLM step")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip the LLM step, just print the transcript")
    p.add_argument("--copy", action="store_true",
                   help="Copy the final result to the macOS clipboard so you can paste it into Char")
    p.add_argument("--save", type=Path,
                   help="Save output to a file. Format inferred from extension: "
                        ".md/.markdown writes the summary, .txt writes the transcript, "
                        "anything else writes a full JSON bundle.")
    p.add_argument("--progress", action="store_true",
                   help="Use the streaming endpoint and render a live progress bar")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_LLM_MAX_TOKENS,
                   help=f"Cap on the LLM's output length (default: {DEFAULT_LLM_MAX_TOKENS}). "
                        "Lower this if you hit context-length errors.")
    p.add_argument("--no-think", action="store_true",
                   help="Suppress Qwen3 <think>...</think> reasoning traces (saves context and latency).")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                   help=f"Where transcription results are cached (default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--no-cache", action="store_true",
                   help="Don't read or write the transcription cache for this run")
    p.add_argument("--clear-cache", action="store_true",
                   help="Wipe the transcription cache and exit (no transcription needed)")
    p.add_argument("--list-cache", action="store_true",
                   help="Print a table of cached transcripts and exit")
    p.add_argument("--call-time", default=None,
                   help="Override the call's recorded-at timestamp. Accepts an ISO 8601 "
                        "string ('2026-05-08T14:30'), a unix epoch, or 'now'. "
                        "Defaults to the audio file's mtime.")
    p.add_argument("--no-stream", action="store_true",
                   help="Disable LLM response streaming (collect full response, then print).")
    p.add_argument("--diarize", dest="diarize", action="store_true",
                   default=DEFAULT_DIARIZE,
                   help="Run speaker diarization and prefix each transcript line "
                        "with the speaker name. On by default.")
    p.add_argument("--no-diarize", dest="diarize", action="store_false",
                   help="Skip speaker diarization (much faster but the transcript "
                        "is one continuous run).")
    p.add_argument("--num-speakers", type=int, default=DEFAULT_NUM_SPEAKERS,
                   help="Number of speakers if known (e.g. 2 for a one-on-one call). "
                        "Leave unset for auto-detection via clustering.")
    p.add_argument("--cluster-threshold", type=float, default=DEFAULT_CLUSTER_THRESHOLD,
                   help="Speaker-clustering threshold when --num-speakers is unset. "
                        "Smaller → more speakers, larger → fewer. Default: 0.5")
    p.add_argument("--save-transcript", type=Path,
                   help="Also save the (possibly diarized) transcript to this path "
                        "as plain text. Independent of --save.")
    args = p.parse_args(argv)

    if args.list_cache:
        print_cache_listing(args.cache_dir)
        return 0

    if args.clear_cache:
        removed = cache_clear(args.cache_dir)
        print(f"🗑️  Cleared {removed} cached transcript(s) from {args.cache_dir}")
        return 0

    if args.file is None:
        p.error("the 'file' argument is required (unless using --clear-cache or --list-cache)")

    if not args.file.exists():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 1

    try:
        size_bytes = args.file.stat().st_size
    except OSError:
        size_bytes = 0
    _log(f"📁 Input: {args.file} ({_human_size(size_bytes)})")

    _log("🔑 Computing file hash (sha256)...")
    t0 = time.time()
    sha = file_sha256(args.file)
    _log(f"   hash: {sha[:16]}... (took {time.time() - t0:.2f}s)", indent=True)

    cache_file = args.cache_dir / f"{sha}.json"
    _log(f"💾 Cache file: {cache_file}")

    payload: dict | None = None
    if not args.no_cache:
        payload = cache_load(cache_file)
        if payload is not None:
            _log(f"   ✅ Cache HIT - skipping Whisper transcription.", indent=True)
        else:
            _log(f"   ❌ Cache MISS - will transcribe via Whisper.", indent=True)
    else:
        _log("   --no-cache set, bypassing cache.", indent=True)

    if payload is None:
        try:
            if args.asr_backend == "parakeet":
                _log(
                    f"🎙️  Transcribing via Parakeet-MLX ({args.parakeet_model}) ..."
                )
                payload = transcribe_with_parakeet(
                    args.file, args.parakeet_model, progress=args.progress
                )
            elif args.progress:
                _log(f"🎙️  Transcribing via Whisper streaming endpoint at {args.server} ...")
                payload = transcribe_streaming(args.file, args.server)
                if payload is None:
                    return 2
            else:
                _log(f"🎙️  Transcribing via Whisper batch endpoint at {args.server} ...")
                payload = transcribe(args.file, args.server)
        except requests.RequestException as exc:
            print(f"\n❌ Transcription request failed: {exc}", file=sys.stderr)
            print("   Is the whisper server running? (uvicorn whisper_server:app --port 8000)",
                  file=sys.stderr)
            return 2
        except ImportError as exc:
            print(f"\n❌ Parakeet backend unavailable: {exc}", file=sys.stderr)
            print("   Install with: pip install parakeet-mlx", file=sys.stderr)
            return 2

        payload = annotate_payload_with_source(payload, args.file, sha)

        if not args.no_cache:
            try:
                cache_save(cache_file, payload)
                _log(f"💾 Cached transcript to {cache_file}")
            except OSError as exc:
                print(f"⚠️  Could not write cache: {exc}", file=sys.stderr)
    elif "_source" not in payload:
        _log("   (cache entry is from before source metadata was tracked; backfilling)",
             indent=True)
        payload = annotate_payload_with_source(payload, args.file, sha)
        if not args.no_cache:
            try:
                cache_save(cache_file, payload)
            except OSError:
                pass

    transcript = extract_transcript(payload)
    metadata = build_metadata(payload, args.file, call_time_override=args.call_time)

    word_count = len(transcript.split())
    char_count = len(transcript)
    approx_tokens = _approx_token_count(transcript)
    whisper_model = (payload.get("_source", {}) or {}).get("whisper_model") or "?"
    _log(
        f"📝 Transcript loaded: {metadata['duration_human']} of audio, "
        f"{word_count:,} words, {char_count:,} chars (~{approx_tokens:,} tokens), "
        f"language={metadata['language']}, asr_model={whisper_model}"
    )
    _log(f"📅 call_recorded_at: {metadata.get('call_recorded_at')}")
    _log(f"📅 transcribed_at:   {metadata.get('transcribed_at')}")

    diarization: dict | None = None
    transcript_for_llm = transcript
    if args.diarize:
        try:
            diarization = run_diarization(
                args.file, payload,
                num_speakers=args.num_speakers,
                cluster_threshold=args.cluster_threshold,
                progress=args.progress,
                llm_url=args.llm_url if not args.no_llm else None,
                llm_model=args.llm_model if not args.no_llm else None,
            )
        except ImportError as exc:
            print(f"\n⚠️  Diarization unavailable: {exc}", file=sys.stderr)
            print("   Install with: pip install sherpa-onnx librosa", file=sys.stderr)
        except Exception as exc:
            print(f"\n⚠️  Diarization step failed: {exc}", file=sys.stderr)
        else:
            if diarization is not None:
                transcript_for_llm = diarization["diarized_text"]
                metadata["speaker_count"] = diarization["speaker_count"]
                metadata["speaker_labels"] = diarization["speaker_labels"]
                _log(
                    f"🗣️  Diarized transcript: {diarization['speaker_count']} speakers, "
                    f"{len(diarization['lines']):,} lines"
                )

    summary: str | None = None
    if not args.no_llm:
        try:
            summary = run_llm(
                transcript_for_llm, args.llm_url, args.llm_model, args.system,
                metadata=metadata,
                max_tokens=args.max_tokens,
                no_think=args.no_think,
                stream=not args.no_stream,
                print_stream=not args.no_stream,
            )
            if args.no_stream:
                print("\n💡 CALL SUMMARY:\n")
                print(summary)
        except requests.RequestException as exc:
            print(f"\n⚠️  LLM step failed: {exc}", file=sys.stderr)
            print("   Is LM Studio's local server running with a model loaded?", file=sys.stderr)
    else:
        _log("ℹ️  --no-llm set, skipping LLM step. Printing transcript:")
        print()
        print(transcript_for_llm)

    if args.copy:
        clip_text = summary if summary else transcript_for_llm
        if copy_to_clipboard(clip_text):
            _log("📋 Copied to clipboard. Paste into Char to attach to the recording.")
        else:
            print("⚠️  Could not access pbcopy.", file=sys.stderr)

    if args.save_transcript:
        args.save_transcript.write_text(transcript_for_llm + "\n")
        kind = "diarized transcript" if diarization else "transcript"
        _log(f"💾 Saved {kind} to {args.save_transcript}")

    if args.save:
        ext = args.save.suffix.lower()
        if ext in {".md", ".markdown"}:
            if summary is None:
                print(f"⚠️  --save {args.save} is markdown but no summary was produced "
                      "(use without --no-llm).", file=sys.stderr)
            else:
                args.save.write_text(summary + "\n")
                _log(f"💾 Saved markdown summary to {args.save}")
        elif ext == ".txt":
            args.save.write_text(transcript_for_llm + "\n")
            kind = "diarized transcript" if diarization else "transcript"
            _log(f"💾 Saved {kind} to {args.save}")
        else:
            out = {
                "file": str(args.file),
                "metadata": metadata,
                "transcript": transcript,
                "diarized_transcript": transcript_for_llm if diarization else None,
                "diarization": (
                    {
                        "speaker_labels": diarization["speaker_labels"],
                        "speaker_count": diarization["speaker_count"],
                        "turn_count": diarization["turn_count"],
                        "lines": diarization["lines"],
                    }
                    if diarization
                    else None
                ),
                "summary": summary,
                "raw": payload,
            }
            args.save.write_text(json.dumps(out, indent=2))
            _log(f"💾 Saved JSON bundle to {args.save}")

    _log(f"🏁 Total wall time: {time.time() - _SCRIPT_START:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
