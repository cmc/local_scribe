"""
Parakeet-MLX ASR backend. Apple Silicon native via MLX.

Wraps NVIDIA Parakeet TDT 0.6B v3 (top of the HuggingFace OpenASR
leaderboard for English; ~15-20% lower WER than Whisper large-v3 on the
same benchmark) and shapes results to Deepgram's batch JSON contract so
the rest of our pipeline (caching, summary prompt, --list-cache) doesn't
need to know which ASR engine ran.

Tradeoff vs. Whisper: less rich punctuation/casing. Our downstream LLM
pass handles all human-facing formatting, so for the call-summary pipeline
the lower WER is the dominant factor.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

_CACHED_MODEL = None
_CACHED_MODEL_ID: str | None = None


def load_model(model_id: str = DEFAULT_PARAKEET_MODEL):
    """Lazy-load the Parakeet model. Cached across calls so the first
    transcription pays the load cost (~3-10s after the model is on disk)
    and subsequent ones are instant."""
    global _CACHED_MODEL, _CACHED_MODEL_ID
    if _CACHED_MODEL is not None and _CACHED_MODEL_ID == model_id:
        return _CACHED_MODEL
    from parakeet_mlx import from_pretrained

    _CACHED_MODEL = from_pretrained(model_id)
    _CACHED_MODEL_ID = model_id
    return _CACHED_MODEL


def audio_duration_seconds(audio_path: Path) -> float:
    """Best-effort audio duration. Tries soundfile (fast for wav/flac/ogg),
    falls back to librosa (handles m4a/mp3 via audioread). Returns 0.0 if
    unable - the rest of the pipeline tolerates a zero duration."""
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        return float(info.duration)
    except Exception:
        pass
    try:
        import librosa

        return float(librosa.get_duration(path=str(audio_path)))
    except Exception:
        return 0.0


def _tokens_to_words(tokens: list) -> list[dict]:
    """Merge parakeet sub-word BPE tokens into clean whole words.

    Parakeet/SentencePiece marks word starts with a leading space in the
    token text (' H', ' it', ' going'). Continuation tokens have no
    leading space ('ow', "'", 's', '?'). We accumulate continuations onto
    the open word and snap punctuation tokens onto the previous word as
    the `punctuated_word` so the Deepgram-shape stays faithful."""
    words: list[dict] = []
    cur: dict | None = None
    for tok in tokens or []:
        raw = getattr(tok, "text", "") or ""
        if not raw.strip():
            continue
        starts_word = raw.startswith(" ") or raw.startswith("\u2581")
        clean = raw.lstrip(" \u2581")
        start = float(getattr(tok, "start", 0.0) or 0.0)
        end = float(getattr(tok, "end", start) or start)
        confidence = float(getattr(tok, "confidence", 1.0) or 1.0)
        if cur is None or starts_word:
            cur = {
                "word": clean,
                "start": start,
                "end": end,
                "confidence": confidence,
                "punctuated_word": clean,
            }
            words.append(cur)
        else:
            cur["word"] = cur["word"] + clean
            cur["punctuated_word"] = cur["punctuated_word"] + clean
            cur["end"] = end
            cur["confidence"] = min(cur["confidence"], confidence)
    return words


def aligned_result_to_deepgram(
    result,
    *,
    duration_s: float,
    model_id: str,
    sha256: str = "",
) -> dict:
    """Shape a parakeet-mlx AlignedResult into a Deepgram batch JSON
    payload. Pure / no I/O, so this is straightforward to unit test."""
    text = (getattr(result, "text", "") or "").strip()

    words: list[dict] = []
    for sentence in getattr(result, "sentences", None) or []:
        words.extend(_tokens_to_words(getattr(sentence, "tokens", None) or []))

    return {
        "metadata": {
            "transaction_key": "local",
            "request_id": "local",
            "sha256": sha256,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration": float(duration_s),
            "channels": 1,
            "models": [model_id],
            "model_info": {
                model_id: {
                    "name": model_id,
                    "version": "parakeet-mlx",
                    "arch": "Parakeet-TDT",
                }
            },
        },
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": text,
                            "confidence": 1.0,
                            "languages": ["en"],
                            "words": words,
                        }
                    ],
                    "detected_language": "en",
                }
            ],
        },
    }


def transcribe_to_deepgram(
    audio_path: Path,
    *,
    model_id: str = DEFAULT_PARAKEET_MODEL,
    on_start: Optional[Callable[[float, str], None]] = None,
    on_progress: Optional[Callable[[float, float, float], None]] = None,
    chunk_duration: float = 120.0,
    overlap_duration: float = 15.0,
) -> dict:
    """Transcribe `audio_path` using parakeet-mlx and return a
    Deepgram-shaped dict.

    Callbacks (all optional):
        on_start(duration_s, language)          - fired once we know the
                                                  audio duration.
        on_progress(progress_0_1, current_s, total_s)
                                                - fired on each chunk
                                                  callback from parakeet-mlx.
    """
    model = load_model(model_id)

    duration_s = audio_duration_seconds(audio_path)
    if on_start:
        try:
            on_start(duration_s, "en")
        except Exception:
            pass

    def chunk_cb(current_pos: float, total_pos: float) -> None:
        # parakeet-mlx reports sample positions, not seconds. We translate
        # back to wall-clock seconds using the duration we already have so
        # the on_progress contract is consistently expressed in seconds.
        if not on_progress:
            return
        if total_pos <= 0:
            return
        progress = min(1.0, max(0.0, current_pos / total_pos))
        if duration_s > 0:
            current_s = progress * duration_s
            total_s = duration_s
        else:
            current_s = float(current_pos)
            total_s = float(total_pos)
        try:
            on_progress(progress, current_s, total_s)
        except Exception:
            pass

    result = model.transcribe(
        str(audio_path),
        chunk_duration=chunk_duration,
        overlap_duration=overlap_duration,
        chunk_callback=chunk_cb,
    )

    return aligned_result_to_deepgram(
        result,
        duration_s=duration_s if duration_s > 0 else _last_token_end(result),
        model_id=model_id,
    )


def _last_token_end(result) -> float:
    """Fallback duration estimate from the last aligned token's end-time
    when soundfile/librosa couldn't read the file."""
    last = 0.0
    for sentence in getattr(result, "sentences", None) or []:
        for tok in getattr(sentence, "tokens", None) or []:
            end = float(getattr(tok, "end", 0.0) or 0.0)
            if end > last:
                last = end
    return last
