"""
Speaker diarization backend backed by sherpa-onnx (no HF auth required).

Pipeline:
    audio_file
      → sherpa-onnx pyannote segmentation 3.0  (who's talking when)
      → 3D-Speaker / TitaNet embedding + clustering  (which turns are the
                                                      same speaker)
      → list of (speaker_id, start, end) turns

We then cross-reference the turns against the word-level timestamps already
produced by the Parakeet ASR backend, group consecutive same-speaker words
into spoken "lines", and finally hand the line-by-line diarized transcript
off to the LLM so it can rename SPEAKER_00 / SPEAKER_01 to real names by
reading conversational cues (introductions, "thanks for calling", etc.).

The intermediate model files are pulled once from
https://github.com/k2-fsa/sherpa-onnx/releases (public, no auth) and cached
to ~/.cache/local_transcriber/diarization/.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Optional

import requests

DEFAULT_CACHE_DIR = Path(
    os.getenv("DIARIZATION_CACHE_DIR")
    or (Path.home() / ".cache" / "local_transcriber" / "diarization")
)

SEGMENTATION_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
SEGMENTATION_MODEL_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_MODEL_FILE = "model.onnx"

EMBEDDING_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/nemo_en_titanet_small.onnx"
)
EMBEDDING_MODEL_FILE = "nemo_en_titanet_small.onnx"


def ensure_models(cache_dir: Path = DEFAULT_CACHE_DIR) -> tuple[Path, Path]:
    """Download segmentation + embedding models into `cache_dir` if missing.
    Returns (segmentation_path, embedding_path)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = cache_dir / SEGMENTATION_MODEL_DIR
    seg_path = seg_dir / SEGMENTATION_MODEL_FILE
    if not seg_path.exists():
        archive = cache_dir / "segmentation.tar.bz2"
        urllib.request.urlretrieve(SEGMENTATION_MODEL_URL, archive)
        import tarfile

        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(cache_dir)
        archive.unlink(missing_ok=True)
    emb_path = cache_dir / EMBEDDING_MODEL_FILE
    if not emb_path.exists():
        urllib.request.urlretrieve(EMBEDDING_MODEL_URL, emb_path)
    return seg_path, emb_path


_CACHED_DIARIZER = None
_CACHED_DIARIZER_KEY: tuple | None = None


def load_diarizer(
    *,
    num_clusters: int | None = None,
    cluster_threshold: float = 0.5,
    num_threads: int = 4,
    cache_dir: Path = DEFAULT_CACHE_DIR,
):
    """Build a sherpa-onnx OfflineSpeakerDiarization. Cached across calls
    so the second invocation in the same process skips ~1s of init."""
    global _CACHED_DIARIZER, _CACHED_DIARIZER_KEY
    key = (num_clusters, cluster_threshold, num_threads, str(cache_dir))
    if _CACHED_DIARIZER is not None and _CACHED_DIARIZER_KEY == key:
        return _CACHED_DIARIZER
    import sherpa_onnx

    seg_path, emb_path = ensure_models(cache_dir)

    seg_cfg = sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
            model=str(seg_path)
        ),
        num_threads=num_threads,
    )
    emb_cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(emb_path), num_threads=num_threads
    )
    cluster_cfg = sherpa_onnx.FastClusteringConfig(
        num_clusters=num_clusters if num_clusters else -1,
        threshold=cluster_threshold,
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=seg_cfg,
        embedding=emb_cfg,
        clustering=cluster_cfg,
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("sherpa-onnx diarization config failed to validate")
    _CACHED_DIARIZER = sherpa_onnx.OfflineSpeakerDiarization(config)
    _CACHED_DIARIZER_KEY = key
    return _CACHED_DIARIZER


def _load_wav_16k_mono(audio_path: Path):
    """sherpa-onnx wants float32 samples at the diarizer's native sample
    rate (16kHz). We use librosa to handle whatever container the user
    threw at us (m4a, mp3, wav, ogg, flac)."""
    import numpy as np

    sample_rate_target = 16000
    if audio_path.suffix.lower() == ".wav":
        with wave.open(str(audio_path), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getframerate() == sample_rate_target:
                frames = wf.readframes(wf.getnframes())
                samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                return samples, sample_rate_target

    import librosa

    samples, sr = librosa.load(str(audio_path), sr=sample_rate_target, mono=True)
    return samples.astype("float32"), sr


def diarize(
    audio_path: Path,
    *,
    num_clusters: int | None = None,
    cluster_threshold: float = 0.5,
    num_threads: int = 4,
    on_progress: Optional[Callable[[float], None]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[dict]:
    """Run speaker diarization on `audio_path`. Returns segments in order:
        [{"speaker": "SPEAKER_00", "start": 0.31, "end": 6.86}, ...]

    Pass `num_clusters` if you know the speaker count exactly. Otherwise
    `cluster_threshold` (0.5 default) controls sensitivity - smaller →
    more speakers, larger → fewer speakers."""
    diarizer = load_diarizer(
        num_clusters=num_clusters,
        cluster_threshold=cluster_threshold,
        num_threads=num_threads,
        cache_dir=cache_dir,
    )

    samples, sr = _load_wav_16k_mono(audio_path)
    if sr != diarizer.sample_rate:
        raise RuntimeError(
            f"Audio sample rate {sr} doesn't match diarizer's "
            f"native rate {diarizer.sample_rate}"
        )

    def progress_cb(num_processed: int, num_total: int) -> int:
        if on_progress and num_total > 0:
            try:
                on_progress(num_processed / num_total)
            except Exception:
                pass
        return 0

    result = diarizer.process(samples.tolist(), callback=progress_cb)
    segments = []
    for seg in result.sort_by_start_time():
        segments.append(
            {
                "speaker": f"SPEAKER_{int(seg.speaker):02d}",
                "start": float(seg.start),
                "end": float(seg.end),
            }
        )
    return segments


def attach_speaker_to_words(
    words: list[dict], turns: list[dict]
) -> list[dict]:
    """Given Deepgram-shaped `words` (each with start/end) and diarization
    `turns` (start/end/speaker), return a new list of words with a
    `speaker` field attached. A word is assigned to whichever turn
    contains its midpoint; if no turn matches, the nearest turn wins."""
    if not turns:
        return [dict(w, speaker="SPEAKER_00") for w in words]

    sorted_turns = sorted(turns, key=lambda t: t["start"])

    def find_speaker(word_mid: float) -> str:
        # exact-overlap fast path
        for t in sorted_turns:
            if t["start"] <= word_mid <= t["end"]:
                return t["speaker"]
        # nearest-turn fallback (handles small gaps in diarization)
        nearest = min(
            sorted_turns,
            key=lambda t: min(
                abs(t["start"] - word_mid), abs(t["end"] - word_mid)
            ),
        )
        return nearest["speaker"]

    out = []
    for w in words:
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)
        mid = (start + end) / 2.0
        out.append(dict(w, speaker=find_speaker(mid)))
    return out


def group_words_into_lines(
    diarized_words: list[dict],
    *,
    max_gap_seconds: float = 1.5,
) -> list[dict]:
    """Collapse a flat list of diarized words into spoken "lines" where
    each line is one speaker speaking continuously. A gap of more than
    `max_gap_seconds` between words from the same speaker also starts a
    new line (so the transcript reads as natural turns rather than one
    huge run-on per speaker)."""
    lines: list[dict] = []
    cur: dict | None = None
    for w in diarized_words:
        speaker = w.get("speaker") or "SPEAKER_00"
        word_text = w.get("punctuated_word") or w.get("word") or ""
        if not word_text:
            continue
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)

        if (
            cur is None
            or cur["speaker"] != speaker
            or (start - cur["end"]) > max_gap_seconds
        ):
            cur = {
                "speaker": speaker,
                "start": start,
                "end": end,
                "words": [word_text],
            }
            lines.append(cur)
        else:
            cur["words"].append(word_text)
            cur["end"] = end
    for line in lines:
        line["text"] = " ".join(line["words"]).strip()
        line["text"] = re.sub(r"\s+([,.!?;:])", r"\1", line["text"])
    return lines


def render_diarized_transcript(
    lines: list[dict],
    *,
    speaker_labels: dict[str, str] | None = None,
    include_timestamps: bool = False,
) -> str:
    """Pretty-print diarized lines as 'Name: text' (or 'SPEAKER_NN: text'
    if no mapping). Timestamps optional - we keep them off by default for
    paste-back to Char but they're useful for debugging.

    `speaker_labels` should be a dict like {"SPEAKER_00": "Moss",
    "SPEAKER_01": "Caller"}. Missing keys fall back to the raw id."""
    out_lines: list[str] = []
    for line in lines:
        spk = line["speaker"]
        label = (speaker_labels or {}).get(spk, spk)
        if include_timestamps:
            ts = f"[{_fmt_ts(line['start'])} - {_fmt_ts(line['end'])}] "
        else:
            ts = ""
        out_lines.append(f"{ts}{label}: {line['text']}")
    return "\n".join(out_lines)


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - 3600 * h - 60 * m
    if h:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


SPEAKER_NAMING_SYSTEM_PROMPT = """\
You are an expert at identifying speakers from a call transcript. The user
will give you the first chunk of a diarized transcript that uses placeholder
labels (SPEAKER_00, SPEAKER_01, ...). Read the conversation and infer the
real name (or descriptive role) of each speaker based on:

  - explicit self-introductions ("Hi, this is Alex...")
  - other speakers addressing them by name ("So Alex, what do you think?")
  - role/affiliation context (a support agent, the customer, the host, etc.)

Output ONLY a JSON object that maps every SPEAKER_xx label that appears in
the input to a name or role string. Do NOT add any other prose. Use the
shortest reasonable label - first names if introduced, otherwise a role.
Use "Caller" / "Host" / "Customer" / "Support agent" if no name is given.

Example output:
{"SPEAKER_00": "Moss", "SPEAKER_01": "Caller"}
"""


def infer_speaker_labels(
    diarized_text: str,
    *,
    llm_url: str,
    llm_model: str,
    max_chars: int = 4000,
    timeout: float = 120.0,
) -> dict[str, str]:
    """Ask the local LLM to map SPEAKER_NN labels to real names. Returns
    a dict; on any failure (network error, malformed JSON, missing
    speakers) returns the empty dict, and the caller is expected to fall
    back to the raw labels."""
    speakers_seen = sorted(set(re.findall(r"SPEAKER_\d{2}", diarized_text)))
    if not speakers_seen:
        return {}

    sample = diarized_text[:max_chars]
    user_msg = (
        f"Speakers present in this transcript: {', '.join(speakers_seen)}.\n"
        f"Map each to a short name (preferred) or role.\n\n"
        f"Transcript excerpt:\n\n{sample}\n"
    )
    try:
        resp = requests.post(
            llm_url,
            json={
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": SPEAKER_NAMING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": 256,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return {}

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    match = re.search(r"\{[^{}]*\}", content, flags=re.DOTALL)
    if not match:
        return {}
    try:
        mapping = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(mapping, dict):
        return {}

    cleaned: dict[str, str] = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if not re.fullmatch(r"SPEAKER_\d{2}", k):
            continue
        v = v.strip()
        if v:
            cleaned[k] = v
    return cleaned
