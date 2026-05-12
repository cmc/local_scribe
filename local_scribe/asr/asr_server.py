"""
local_scribe - Local ASR server speaking both Deepgram's and OpenAI's
batch transcription contracts, so Char's Live recording (Custom Deepgram URL)
*and* its file-import "Generate" flow (OpenAI Batch Only provider) both route
through the same on-device Parakeet/Whisper engine.

Endpoints:

  * POST  /v1/listen                 - Deepgram batch (raw audio body or multipart)
                                       used by Char's Live recording when the
                                       "Custom" provider points at this server.
  * POST  /v1/listen/stream          - same as /v1/listen but streams NDJSON
                                       progress events (a transcribe_file.py
                                       extension; not part of the Deepgram spec).
  * WS    /v1/listen                 - Deepgram WebSocket streaming (linear16 PCM)
                                       same as POST /v1/listen but over WS.
  * POST  /v1/audio/transcriptions   - OpenAI Whisper-API batch (multipart form),
                                       used by Char's "OpenAI (Batch Only)"
                                       provider when configured with
                                       Base URL = http://127.0.0.1:8000/v1.
                                       Honours response_format = diarized_json
                                       | verbose_json | json | text | srt | vtt.
                                       Also honours `stream=true` and emits
                                       OpenAI-style transcript.text.delta /
                                       transcript.text.done SSE events
                                       (Char's `gpt-4o-transcribe` progressive
                                       batch path), with periodic delta
                                       heartbeats so Char's 60-second
                                       BATCH_IDLE_TIMEOUT doesn't fire while
                                       long ASR jobs run.
  * GET   /health                    - liveness/readiness probe with backend info.

Two ASR backends are pluggable via ASR_BACKEND:

  * parakeet (default) - NVIDIA Parakeet-TDT 0.6B v3 via parakeet-mlx
                         (top of the OpenASR leaderboard for English; runs
                         natively on Apple Silicon)
  * whisper            - faster-whisper large-v3-turbo (multilingual fallback)

Char setup
----------
In Char.app -> Settings -> Transcription, configure the providers you want to
route through this server.

  * Live recording  -> "Custom" provider, Base URL http://127.0.0.1:8000,
                       any non-empty API key (we ignore auth locally).
                       (Char only supports Deepgram-compatible endpoints
                       for its Custom provider, which is exactly what we are.)

  * Generate from   -> "OpenAI (Batch Only)" provider, Advanced -> Base URL
    existing audio     http://127.0.0.1:8000/v1, any non-empty API key. Char
                       defaults to model gpt-4o-transcribe-diarize with
                       response_format=diarized_json; we honour that and
                       return a single anonymous speaker (use
                       transcribe_file.py for real multi-speaker diarization).

Run
---
    uvicorn local_scribe.asr.asr_server:app --host 0.0.0.0 --port 8000

Configuration env vars
----------------------
    ASR_BACKEND            (default: parakeet)        parakeet | whisper
    PARAKEET_MODEL         (default: mlx-community/parakeet-tdt-0.6b-v3)
    WHISPER_MODEL          (default: large-v3-turbo)
                              tiny | base | small | medium | large-v3 | large-v3-turbo |
                              distil-large-v3 | distil-medium.en | distil-small.en
    WHISPER_COMPUTE_TYPE   (default: int8)    int8 | int16 | float32 (CPU)
                                              int8_float16 | float16 (CUDA only)
    WHISPER_DEVICE         (default: auto)    cpu | cuda | auto
    WHISPER_LANGUAGE       (default: unset)   ISO-639 code; leave unset to autodetect
                                              (only respected by the whisper backend)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi import status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
# NOTE: starlette's UploadFile (not fastapi's). request.form() yields starlette
# UploadFile instances, and fastapi.UploadFile is a subclass — so an isinstance
# check against fastapi.UploadFile silently rejects all uploads. Always use
# starlette's class for runtime checks.
from starlette.datastructures import UploadFile

logger = logging.getLogger("local_scribe")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from local_scribe.common.config import load_config as _load_config
from local_scribe.char import char_persist as _char_persist

# Loaded once at import time. Env vars still win (layered inside
# load_config), so existing scripts/tests that ``os.environ[...]`` keep
# working unchanged. Live edits via the inspector UI take effect on
# next ASR-server restart -- documented in README.
_CFG = _load_config()

# Char's progressive batch path silently drops every transcript whose
# words array is empty (see CHAR_REVIEW.md § Streaming-batch persistence
# bug). Until that's fixed upstream we sidecar-write transcript.json
# straight to disk on a SHA256 match. Toggle off by setting
# CHAR_PERSIST=0 if you want the upstream-broken behaviour for testing.
_CHAR_PERSIST_ENABLED = os.getenv("CHAR_PERSIST", "1").lower() not in (
    "0", "false", "no", "off",
)
_CHAR_DATA_DIR = _CFG.char_data_dir


def _maybe_write_char_transcript(
    audio_path: str,
    words: list[dict[str, Any]],
    lang: str,
    request_id: str,
    *,
    channel: int = 2,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort sidecar write so Char's UI can display the transcript
    despite its progressive-parser dropping our words. Failures are
    logged but never raised: the SSE response must always succeed.

    ``metadata`` is an optional dict describing the ASR/diarization run
    (model name, num_speakers, override flags, etc.). It's embedded
    in the new transcript.json so the next re-transcription can archive
    a self-describing record of the previous run.
    """
    if not _CHAR_PERSIST_ENABLED or not words:
        return
    try:
        result = _char_persist.write_transcript_for_audio(
            audio_path, _CHAR_DATA_DIR,
            words=words, language=lang or "en",
            provider="openai", channel=channel,
            request_id=request_id,
            metadata=metadata,
        )
        if result is None:
            logger.info(
                "[openai %s] char_persist: no matching Char session "
                "(was the audio uploaded outside Char?)", request_id,
            )
    except Exception:
        logger.exception(
            "[openai %s] char_persist sidecar write failed", request_id,
        )


def _compute_speaker_airtime(
    words: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Aggregate per-speaker airtime + mean cluster confidence.

    Returns a list of ``{label, seconds, percent, mean_confidence,
    word_count}`` dicts sorted by ``seconds`` descending so the UI can
    render them in talk-time order (largest contributor first, the
    same ordering Otter / Granola / Otter use).

    ``percent`` is share of *speech* time, not of clock time — silent
    gaps between turns aren't allocated to any speaker, so the
    percentages sum to 100% across the speakers who actually spoke
    rather than to the wall-clock duration.

    ``mean_confidence`` is the average of per-word
    ``speaker_confidence`` values where available, expressed as a
    float in [0, 1]. ``None`` when no word in that bucket carried a
    confidence (single-speaker fallback path, or diarization
    skipped). Inspector / transcript.txt rendering both look at this.
    """
    if not words:
        return []
    import collections
    buckets: dict[str, dict[str, Any]] = collections.OrderedDict()
    for w in words:
        label = w.get("speaker") or "speaker_0"
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)
        dur = max(0.0, end - start)
        b = buckets.setdefault(label, {
            "label": label, "seconds": 0.0, "word_count": 0,
            "_conf_sum": 0.0, "_conf_n": 0,
        })
        b["seconds"] += dur
        b["word_count"] += 1
        conf = w.get("speaker_confidence")
        if conf is not None:
            b["_conf_sum"] += float(conf)
            b["_conf_n"] += 1
    total_speech = sum(b["seconds"] for b in buckets.values()) or 0.0
    out: list[dict[str, Any]] = []
    for b in buckets.values():
        n = b.pop("_conf_n")
        s = b.pop("_conf_sum")
        b["mean_confidence"] = (s / n) if n else None
        b["percent"] = (
            (b["seconds"] / total_speech) if total_speech > 0 else 0.0
        )
        b["seconds"] = round(b["seconds"], 3)
        out.append(b)
    out.sort(key=lambda d: d["seconds"], reverse=True)
    return out


def _format_airtime_log(speakers: list[dict[str, Any]] | None) -> str:
    """Compact one-liner airtime summary appended to the per-request
    'done' log. Empty string when there's nothing to show, so the
    existing log shape is preserved for diarize-skipped runs.

    Example output (single space prefix):
        '  airtime: speaker_0=42% (12m 30s, 78% conf), speaker_1=58% ...'
    """
    if not speakers:
        return ""
    parts = []
    for s in speakers:
        secs = float(s.get("seconds") or 0.0)
        pct = round(float(s.get("percent") or 0.0) * 100)
        mins, rem = divmod(int(round(secs)), 60)
        mc = s.get("mean_confidence")
        mc_str = (
            f", {round(float(mc) * 100)}% conf"
            if mc is not None else ""
        )
        parts.append(
            f"{s.get('label', '?')}={pct}% ({mins}m {rem:02d}s{mc_str})"
        )
    return "  airtime: " + ", ".join(parts)


def _diarization_metadata(
    *,
    diarize_enabled: bool,
    duration: float | None,
    num_speakers: int,
    num_speakers_override: int | None,
    cluster_threshold_override: float | None,
    skipped_reason: str | None = None,
    speakers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``local_scribe`` metadata block embedded in each
    transcript.json. Keeps a stable shape so the inspector UI can
    render archives from any prior run without per-version branching.

    ``algorithm`` summarises the decision tree in ``_attach_speakers_to_words``:
      * ``manual_ahc``  -- explicit num_speakers or cluster_threshold
      * ``auto_silhouette`` -- default ``diarize_auto`` path
      * ``skipped``     -- diarization globally off, audio over cap, or empty words

    ``speakers`` is the per-label airtime + confidence aggregate from
    ``_compute_speaker_airtime``. Surfaces in the inspector's
    Transcript airtime panel.
    """
    if not diarize_enabled or skipped_reason:
        algorithm = "skipped"
    elif num_speakers_override is not None or cluster_threshold_override is not None:
        algorithm = "manual_ahc"
    else:
        algorithm = "auto_silhouette"
    return {
        "asr_backend": ASR_BACKEND,
        "asr_model": MODEL_NAME,
        "audio_duration_seconds": (
            round(float(duration), 3) if duration is not None else None
        ),
        "diarization": {
            "algorithm": algorithm,
            "enabled": bool(diarize_enabled),
            "num_speakers": int(num_speakers),
            "num_speakers_override": num_speakers_override,
            "cluster_threshold_override": cluster_threshold_override,
            "skipped_reason": skipped_reason,
            "speakers": speakers or [],
        },
    }

ASR_BACKEND = _CFG.asr_backend
if ASR_BACKEND not in {"parakeet", "whisper"}:
    raise RuntimeError(
        f"Unknown asr.backend={ASR_BACKEND!r}; use 'parakeet' or 'whisper'"
    )

WHISPER_MODEL_NAME = _CFG.whisper_model
# Whisper-only tuning knobs stay env-var-only -- low-frequency overrides
# that don't deserve a top-level config slot.
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
DEVICE = os.getenv("WHISPER_DEVICE", "auto")
LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None
PARAKEET_MODEL_ID = _CFG.parakeet_model

_whisper_model = None
_arch: str
if ASR_BACKEND == "whisper":
    from faster_whisper import WhisperModel

    logger.info(
        "loading faster-whisper model=%s compute=%s device=%s",
        WHISPER_MODEL_NAME, COMPUTE_TYPE, DEVICE,
    )
    _whisper_model = WhisperModel(
        WHISPER_MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE
    )
    MODEL_NAME = WHISPER_MODEL_NAME
    _arch = "whisper"
else:
    import mlx.core as mx  # noqa: E402

    from local_scribe.asr.backends.parakeet_backend import (  # noqa: E402
        load_model as _load_parakeet_model,
        transcribe_to_deepgram as _parakeet_transcribe_to_deepgram,
    )

    MODEL_NAME = PARAKEET_MODEL_ID
    _arch = "Parakeet-TDT"


def _mlx_thread_init() -> None:
    """MLX is thread-local: streams are per-thread, and crucially the model
    weights also need to live on the same thread that runs inference. We
    therefore both register a stream and lazy-load parakeet on the worker
    thread itself rather than on the main thread at import time."""
    if ASR_BACKEND != "parakeet":
        return
    mx.set_default_stream(mx.new_stream(mx.default_device()))
    logger.info(
        "[mlx-worker] loading parakeet-mlx model=%s ...", PARAKEET_MODEL_ID
    )
    _load_parakeet_model(PARAKEET_MODEL_ID)
    logger.info("[mlx-worker] parakeet model ready")


# All parakeet/MLX work runs on this single worker thread so the stream
# stays warm and weights stay co-located. faster-whisper sticks with the
# default executor; the initializer is a no-op in that case.
_mlx_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="mlx",
    initializer=_mlx_thread_init,
)

# Force the worker thread (and the parakeet model load) to spin up now so
# the first /v1/listen request doesn't pay the cold-load tax.
_mlx_executor.submit(lambda: None).result()

logger.info("model ready (backend=%s, model=%s)", ASR_BACKEND, MODEL_NAME)


# --- Per-service bearer-token auth ----------------------------------
#
# All routes other than /health require a token derived from the
# Keychain master key via HKDF-SHA256 (see ``service_auth.py``). The
# token is populated inside a FastAPI lifespan so test imports of this
# module don't trigger a Touch ID prompt; the route decorators bind to
# a *callable* that resolves the token at request time.
#
# Test hooks (none of these are for production use; documented in the
# README "Service authentication" section):
#
#   LOCAL_SCRIBE_DISABLE_AUTH=1
#       Disables the check entirely. Used by CI and the existing
#       FastAPI TestClient suites which were written pre-auth.
#
#   LOCAL_SCRIBE_TEST_MASTER_KEY_HEX=<64 hex chars>
#       Skip Touch ID; derive the ASR token from these 32 bytes. Used
#       by the auth integration tests that need to exercise the real
#       gating logic without a Keychain.

from local_scribe.security import service_auth  # noqa: E402


_asr_token: service_auth.ServiceToken | None = None


def _resolve_asr_token() -> service_auth.ServiceToken | None:
    """Token provider passed to ``make_token_dependency``. Returned
    object is read fresh on each request so a future ``rotate-token``
    command can swap it without restarting the server."""
    return _asr_token


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Populate ``_asr_token`` from the Keychain (prompts Touch ID) or
    from the test env var. Logs the token *fingerprint* (first 6 hex
    chars after the prefix) so operators can verify "Char's saved api
    key matches the server's current token" without ever surfacing the
    secret itself.

    SIP gate first: if the kernel isn't enforcing process boundaries,
    the master key we're about to unlock can be read out of our heap
    by any user-space process. ``./run.sh start`` already refuses to
    launch us without SIP — this is defense-in-depth in case the
    operator runs ``uvicorn local_scribe.asr.asr_server:app`` directly."""
    from local_scribe.security import sip_check
    from local_scribe.common import dev_mode
    sip_report = sip_check.status()
    if sip_report.state != sip_check.SIPState.FULLY_ENABLED:
        if dev_mode.is_enabled():
            # Dev mode opts the operator into the SIP-bypass path
            # *explicitly*; we still emit the loud banner (once per
            # process) and continue. See
            # local_scribe/common/dev_mode.py for the full rationale
            # — short version: this is for iterating on the pipeline
            # on a host that doesn't have SIP configured. NOT a
            # production configuration; the inspector renders a red
            # banner on every page while dev mode is on.
            import sys
            dev_mode.emit_banner_once(sys.stderr)
            logger.warning(
                "ASR service starting with SIP NOT fully enabled "
                "(state=%s) — LOCAL_SCRIBE_DEV_MODE=1 bypassed the "
                "gate. The reconstituted master key in this process "
                "heap is readable by any cohabiting user-space "
                "process. See SECURITY.md § 'Dev mode'.",
                sip_report.state.value,
            )
        else:
            # Print the rich banner to stderr so the operator sees
            # what's wrong, then refuse to start. We accept the test
            # override (LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT) and the
            # auth-bypass env var honours its own bypass path below
            # — but key-unlock will not proceed on a SIP-disabled
            # host without explicit ``LOCAL_SCRIBE_DEV_MODE=1``.
            import sys
            sys.stderr.write(
                sip_check.format_banner(sip_report, color=sys.stderr.isatty())
                + "\n"
            )
            raise RuntimeError(
                "ASR service refusing to start: SIP not fully enabled "
                f"(state={sip_report.state.value}). See SECURITY.md § "
                "'Defense layer 0' for why this is non-negotiable. "
                "If this is intentional (development only), set "
                "LOCAL_SCRIBE_DEV_MODE=1 — but be aware of what that "
                "costs you; see SECURITY.md § 'Dev mode'."
            )
    global _asr_token
    if service_auth.is_bypass_enabled():
        # Loud warning -- people forgetting to flip this back off in
        # production would be a sharp footgun.
        logger.warning(
            "AUTH BYPASS ENABLED via %s=1 — every endpoint is OPEN. "
            "Unset this env var in production.",
            service_auth.BYPASS_ENV,
        )
    else:
        # Pre-derived token via env var. ``cmd_start`` in run.sh
        # uses ``service_auth warm`` to do ONE Touch ID + ONE
        # YubiKey unlock that covers every service that needs a
        # bearer token, then spawns each service with its token in
        # the per-subprocess environ. This short-circuit picks
        # that up and skips the (otherwise inevitable) second
        # Touch ID + YubiKey cycle inside the ASR worker. The
        # token format is HKDF-derived from the same master key
        # that the worker would have unlocked itself, so
        # downstream auth checks are bit-identical to the legacy
        # path. SECURITY.md § 'Defense layer 2' covers the
        # threat model around env-var token passing (token-in-env
        # is fine for explicit child-process handoff; we still
        # forbid the parent shell from holding it).
        prewarmed = os.environ.get("LOCAL_SCRIBE_ASR_TOKEN")
        test_mk = os.environ.get("LOCAL_SCRIBE_TEST_MASTER_KEY_HEX")
        if prewarmed:
            _asr_token = service_auth.ServiceToken(
                service="asr", token=prewarmed.strip(),
            )
            logger.info(
                "service_auth: ASR token loaded from "
                "LOCAL_SCRIBE_ASR_TOKEN env (fingerprint=%s) — "
                "warmed by parent via `service_auth warm`",
                service_auth.token_fingerprint(_asr_token.token),
            )
        elif test_mk:
            try:
                mk = bytes.fromhex(test_mk.strip())
            except ValueError as exc:
                raise RuntimeError(
                    f"LOCAL_SCRIBE_TEST_MASTER_KEY_HEX is not valid hex: {exc}"
                ) from exc
            _asr_token = service_auth.ServiceToken.from_master_key(mk, "asr")
            logger.info(
                "service_auth: ASR token derived from test master key "
                "(fingerprint=%s) — production deployments use Touch ID",
                service_auth.token_fingerprint(_asr_token.token),
            )
        else:
            try:
                _asr_token = service_auth.ServiceToken.unlock("asr")
            except Exception as exc:  # noqa: BLE001
                # Surface a clear startup failure rather than serving an
                # un-authenticated API silently.
                logger.error(
                    "service_auth: failed to unlock master key for ASR "
                    "service: %s — set LOCAL_SCRIBE_DISABLE_AUTH=1 to "
                    "start without auth (NOT recommended), or run "
                    "`./run.sh key init` to generate the Option C "
                    "split-key (Touch ID + YubiKey).",
                    exc,
                )
                raise
    yield
    # Best-effort: drop the in-memory holder on shutdown so a core dump
    # captured immediately after stop doesn't contain it.
    _asr_token = None


app = FastAPI(
    title="local-scribe-asr",
    version="0.4.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependency bound once; the underlying token is resolved lazily.
require_asr_token = service_auth.make_token_dependency(_resolve_asr_token)


async def _ws_auth(ws: WebSocket) -> bool:
    """WebSocket auth check -- FastAPI's ``Depends()`` doesn't run for
    ``@app.websocket(...)`` handlers, so we replay the same logic
    inline. Called BEFORE ``ws.accept()`` so we can reject the upgrade
    cleanly instead of accepting then dropping (which leaks a 101
    Switching Protocols on every probe).

    Token sources accepted, in order:
      1. ``Authorization: Bearer ...`` upgrade header (preferred)
      2. ``Authorization: Token ...`` (Deepgram-style)
      3. ``Sec-WebSocket-Protocol: <token>`` (some browsers don't let
         you set arbitrary upgrade headers but DO let you set a
         subprotocol — we accept either spelling)
      4. ``?api_key=<token>`` query param
    """
    if service_auth.is_bypass_enabled():
        return True
    if _asr_token is None:
        # 1011 = internal error. Use HTTP-style close-before-accept.
        await ws.close(code=1011, reason="auth not initialised")
        return False

    candidate: str | None = service_auth.extract_candidate_token(ws)
    if not candidate:
        # Inspect subprotocols (Sec-WebSocket-Protocol). Starlette
        # surfaces these as a list on the connection scope.
        try:
            for proto in (ws.scope.get("subprotocols") or []):
                if proto:
                    candidate = proto
                    break
        except Exception:  # noqa: BLE001
            pass

    if not candidate or not _asr_token.matches(candidate):
        await ws.close(code=1008, reason="unauthorized")
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_word(w) -> dict[str, Any]:
    return {
        "word": w.word.strip(),
        "start": round(float(w.start), 3),
        "end": round(float(w.end), 3),
        "confidence": round(float(getattr(w, "probability", 0.0) or 0.0), 4),
        "punctuated_word": w.word.strip(),
    }


def _avg_confidence(words: list[dict[str, Any]]) -> float:
    if not words:
        return 0.99
    return round(sum(w["confidence"] for w in words) / len(words), 4)


def _write_temp_wav(audio_f32: np.ndarray, sample_rate: int = 16000) -> str:
    """parakeet-mlx wants a file path (it routes through librosa internally),
    so when we have an in-memory numpy buffer (the WS path) we have to
    materialize a temp wav. Returns the path; caller is responsible for
    deleting it."""
    pcm = np.clip(audio_f32, -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype(np.int16)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ws_pcm_")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_i16.tobytes())
    return path


def _run_whisper_backend(
    audio: Any,
    on_segment=None,
    on_start=None,
) -> tuple[str, list[dict[str, Any]], str | None, float]:
    """faster-whisper transcription. `audio` is a file path or a 1-D float32
    numpy array sampled at 16 kHz. Callbacks are invoked from this thread:

        on_start(info)   - SimpleNamespace(duration, language, language_probability)
        on_segment(seg)  - SimpleNamespace(id, start, end, text)
    """
    segments, info = _whisper_model.transcribe(
        audio,
        language=LANGUAGE,
        word_timestamps=True,
        vad_filter=True,
    )
    if on_start is not None:
        on_start(SimpleNamespace(
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            language=info.language,
            language_probability=float(info.language_probability),
        ))
    text_parts: list[str] = []
    words: list[dict[str, Any]] = []
    for seg in segments:
        text_parts.append(seg.text)
        if seg.words:
            for w in seg.words:
                words.append(_build_word(w))
        if on_segment is not None:
            on_segment(SimpleNamespace(
                id=int(getattr(seg, "id", 0) or 0),
                start=float(seg.start),
                end=float(seg.end),
                text=seg.text.strip(),
            ))
    transcript = " ".join(p.strip() for p in text_parts).strip()
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    return transcript, words, info.language, duration


def _run_parakeet_backend(
    audio: Any,
    on_segment=None,
    on_start=None,
) -> tuple[str, list[dict[str, Any]], str | None, float]:
    """parakeet-mlx transcription with the same callback contract as the
    whisper backend, so the FastAPI handlers don't have to branch.

    Parakeet only consumes file paths, so when given a numpy buffer (the
    WS path) we serialize a temp wav and clean it up after."""
    cleanup_path: str | None = None
    if isinstance(audio, np.ndarray):
        path = _write_temp_wav(audio)
        cleanup_path = path
    else:
        path = str(audio)

    duration_holder = {"duration": 0.0}

    def parakeet_on_start(duration_s: float, language: str) -> None:
        duration_holder["duration"] = float(duration_s)
        if on_start is not None:
            on_start(SimpleNamespace(
                duration=float(duration_s),
                language=language,
                language_probability=1.0,
            ))

    last_chunk_end = {"v": 0.0}

    def parakeet_on_progress(progress: float, current_s: float, total_s: float) -> None:
        # parakeet-mlx fires on_progress per processed chunk; we synthesize a
        # whisper-shaped Segment so the streaming endpoint sees a uniform
        # event stream regardless of backend. start = previous chunk end,
        # end = current position.
        if on_segment is None:
            return
        seg_start = last_chunk_end["v"]
        seg_end = float(current_s)
        last_chunk_end["v"] = seg_end
        on_segment(SimpleNamespace(
            id=0,
            start=seg_start,
            end=seg_end,
            text="",
        ))

    try:
        payload = _parakeet_transcribe_to_deepgram(
            Path(path),
            model_id=PARAKEET_MODEL_ID,
            on_start=parakeet_on_start,
            on_progress=parakeet_on_progress,
        )
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass

    alt = payload["results"]["channels"][0]["alternatives"][0]
    transcript = (alt.get("transcript") or "").strip()
    words = alt.get("words") or []
    language = (alt.get("languages") or ["en"])[0]
    duration = float(payload.get("metadata", {}).get("duration") or duration_holder["duration"])
    return transcript, words, language, duration


def _run_asr(
    audio: Any,
    on_segment=None,
    on_start=None,
) -> tuple[str, list[dict[str, Any]], str | None, float]:
    """Dispatch to whichever ASR backend the server was started with."""
    if ASR_BACKEND == "whisper":
        return _run_whisper_backend(audio, on_segment=on_segment, on_start=on_start)
    return _run_parakeet_backend(audio, on_segment=on_segment, on_start=on_start)


async def _run_asr_async(audio: Any, on_segment=None, on_start=None):
    """Schedule ASR off the event loop. Parakeet is pinned to the dedicated
    MLX-stream-initialized worker; Whisper uses the default executor."""
    loop = asyncio.get_running_loop()
    if ASR_BACKEND == "parakeet":
        return await loop.run_in_executor(
            _mlx_executor, _run_asr, audio, on_segment, on_start,
        )
    return await asyncio.to_thread(
        _run_asr, audio, on_segment, on_start,
    )


def _model_info_block() -> dict[str, Any]:
    """Backend-aware Deepgram model_info block."""
    return {
        MODEL_NAME: {
            "name": (
                f"faster-whisper-{MODEL_NAME}"
                if ASR_BACKEND == "whisper"
                else MODEL_NAME
            ),
            "version": "local",
            "arch": _arch,
        }
    }


def _deepgram_batch_response(
    *,
    audio_bytes: bytes,
    transcript: str,
    words: list[dict[str, Any]],
    language: str | None,
    duration_s: float,
    request_id: str,
) -> dict[str, Any]:
    return {
        "metadata": {
            "transaction_key": "deprecated",
            "request_id": request_id,
            "sha256": hashlib.sha256(audio_bytes).hexdigest() if audio_bytes else "",
            "created": _now_iso(),
            "duration": round(duration_s, 3),
            "channels": 1,
            "models": [MODEL_NAME],
            "model_info": _model_info_block(),
        },
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": transcript,
                            "confidence": _avg_confidence(words),
                            "words": words,
                            "languages": [language] if language else [],
                        }
                    ],
                    "detected_language": language or "",
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# OpenAI Whisper-API compatibility (POST /v1/audio/transcriptions)
#
# Char's "OpenAI (Batch Only)" provider posts here when its Advanced -> Base URL
# is set to http://127.0.0.1:8000/v1. Char's open-source client lives at
# fastrepl/anarlog and shows the wire contract we need to honour:
# multipart form `file` + `model` + `response_format` + optional `language`.
# Default model is gpt-4o-transcribe-diarize -> response_format=diarized_json.
# ---------------------------------------------------------------------------

# Subset of ISO-639-1 -> English name. OpenAI's verbose_json returns the full
# English name, not the code, and Char parses the field as an opaque string,
# so we cover the common cases and fall through to the raw code otherwise.
_LANG_CODE_TO_NAME = {
    "en": "english", "es": "spanish", "fr": "french", "de": "german",
    "it": "italian", "pt": "portuguese", "nl": "dutch", "ru": "russian",
    "ja": "japanese", "zh": "chinese", "ko": "korean", "ar": "arabic",
    "hi": "hindi", "tr": "turkish", "pl": "polish", "sv": "swedish",
    "no": "norwegian", "da": "danish", "fi": "finnish", "el": "greek",
    "he": "hebrew", "id": "indonesian", "th": "thai", "vi": "vietnamese",
    "uk": "ukrainian", "cs": "czech", "ro": "romanian", "hu": "hungarian",
}

_OPENAI_VALID_FORMATS = {"json", "text", "srt", "verbose_json", "vtt", "diarized_json"}


# When response_format=diarized_json, run real sherpa-onnx diarization (defaults
# on) and label each segment with its speaker. Set asr.diarization.enabled=false
# in config.json (or DIARIZE=0 / OPENAI_BATCH_DIARIZE=0 env) for ~8x lower latency.
_OPENAI_BATCH_DIARIZE = _CFG.diarize_enabled
_NUM_SPEAKERS = _CFG.num_speakers
# The user-set CLUSTER_THRESHOLD (or None when unset). We hold the explicit
# user override separately so the long-audio auto-bump only kicks in when
# nothing was explicitly configured.
_CLUSTER_THRESHOLD_OVERRIDE: float | None = _CFG.cluster_threshold
_CLUSTER_THRESHOLD_DEFAULT = 0.5
# Audio longer than this auto-skips diarization entirely (returns a single
# `speaker_0` placeholder). sherpa-onnx clustering is O(N^2) and the wall
# time on multi-hour audio gets long, but real meetings do run long, so
# the default is a generous 4 hours. Set asr.diarization.max_seconds=0
# to remove the cap entirely.
_MAX_DIARIZE_SECONDS = _CFG.max_diarize_seconds
# When sherpa-onnx returns more than this many distinct speakers we treat it
# as a clustering blow-up (long mono recordings often produce 100+ phantom
# speakers). The endpoint then collapses to a single speaker rather than
# emitting unusable JSON. Set asr.diarization.max_speakers=0 to disable the cap.
_MAX_SPEAKERS = _CFG.max_speakers
# Audio at or above this duration auto-bumps CLUSTER_THRESHOLD to the looser
# default of 0.7 (long meetings have few speakers; tighter thresholds
# over-shard). Skipped if the user explicitly set CLUSTER_THRESHOLD.
_LONG_AUDIO_BUMP_AFTER_SECONDS = 600
_LONG_AUDIO_CLUSTER_THRESHOLD = 0.7


def _pick_cluster_threshold(duration: float | None) -> float:
    """Decide which CLUSTER_THRESHOLD to use for this clip. Honours an
    explicit env override; otherwise loosens for long files."""
    if _CLUSTER_THRESHOLD_OVERRIDE is not None:
        return _CLUSTER_THRESHOLD_OVERRIDE
    if duration is not None and duration >= _LONG_AUDIO_BUMP_AFTER_SECONDS:
        return _LONG_AUDIO_CLUSTER_THRESHOLD
    return _CLUSTER_THRESHOLD_DEFAULT


def _attach_speakers_to_words(
    audio_path: str,
    words: list[dict[str, Any]],
    duration: float | None = None,
    request_id: str | None = None,
    *,
    num_speakers_override: int | None = None,
    cluster_threshold_override: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Run sherpa-onnx diarization on `audio_path` and attach a speaker
    label to each word. Returns (words_with_speaker, num_speakers).

    On any failure (model load issue, audio decode failure, etc.) we
    log and fall back to a single anonymous speaker so Char's parser
    still gets a valid response.

    Also enforces the duration cap (`MAX_DIARIZE_SECONDS`) and speaker-count
    cap (`MAX_SPEAKERS`) -- both fall back to single-speaker output rather
    than emit a response Char's UI can't render.

    Per-request overrides (passed in from the openai endpoint's query
    string by ``redo-session`` / power users) take precedence over the
    server-wide config knobs:

      * ``num_speakers_override`` -> forces sherpa-onnx to that exact
        cluster count, side-stepping the over-clustering we see on
        long mono recordings.
      * ``cluster_threshold_override`` -> raw threshold (0..1) shipped
        straight into sherpa-onnx; bypasses the long-audio auto-bump.
    """
    log_id = f"[openai {request_id}] " if request_id else ""

    if not _OPENAI_BATCH_DIARIZE or not words:
        return [dict(w, speaker="speaker_0") for w in words], 1

    if (
        _MAX_DIARIZE_SECONDS > 0
        and duration is not None
        and duration > _MAX_DIARIZE_SECONDS
    ):
        logger.warning(
            "%sdiarization auto-skipped: audio is %.0fs > MAX_DIARIZE_SECONDS=%ds. "
            "Returning ASR-only transcript with a single speaker_0 placeholder. "
            "Override with MAX_DIARIZE_SECONDS=0 (no cap) or run "
            "`./run.sh transcribe FILE` for the full local pipeline.",
            log_id, duration, _MAX_DIARIZE_SECONDS,
        )
        return [dict(w, speaker="speaker_0") for w in words], 1

    try:
        from local_scribe.asr.backends import diarization_backend as dz

        if cluster_threshold_override is not None:
            threshold = cluster_threshold_override
        else:
            threshold = _pick_cluster_threshold(duration)
            if (
                threshold != _CLUSTER_THRESHOLD_DEFAULT
                and _CLUSTER_THRESHOLD_OVERRIDE is None
            ):
                logger.info(
                    "%susing CLUSTER_THRESHOLD=%.2f (auto-bumped from %.2f "
                    "for audio >= %ds; pass ?cluster_threshold=N or set "
                    "CLUSTER_THRESHOLD env to override)",
                    log_id, threshold, _CLUSTER_THRESHOLD_DEFAULT,
                    _LONG_AUDIO_BUMP_AFTER_SECONDS,
                )

        num_clusters = (
            num_speakers_override
            if num_speakers_override is not None
            else _NUM_SPEAKERS
        )
        if num_speakers_override is not None:
            logger.info(
                "%sforcing num_speakers=%d (per-request override)",
                log_id, num_speakers_override,
            )

        # Path selection:
        #   * Explicit num_clusters    → sherpa AHC, K forced.
        #   * Explicit threshold ovr   → sherpa AHC, threshold honoured.
        #   * Neither → eigengap auto-K via diarize_auto. This is the
        #     default for Char's Generate flow and prevents the 451-
        #     phantom-speaker blow-up that bit us on the Maus meeting.
        if num_clusters is None and cluster_threshold_override is None:
            logger.info(
                "%seigengap auto-K (no num_speakers / cluster_threshold override)",
                log_id,
            )
            turns = dz.diarize_auto(
                Path(audio_path),
                num_threads=4,
                request_id=request_id,
            )
        else:
            turns = dz.diarize(
                Path(audio_path),
                num_clusters=num_clusters,
                cluster_threshold=threshold,
            )
        if not turns:
            return [dict(w, speaker="speaker_0") for w in words], 1
        diarized = dz.attach_speaker_to_words(words, turns)
        order: list[str] = []
        for w in diarized:
            label = w.get("speaker") or "SPEAKER_00"
            if label not in order:
                order.append(label)

        # Sherpa-onnx blow-up guard: if the clustering returns way more
        # speakers than makes sense for a meeting, bail to single-speaker
        # rather than emit unusable JSON. This is the one that bit us on a
        # 114-min recording where it produced 451 phantom speakers.
        if _MAX_SPEAKERS > 0 and len(order) > _MAX_SPEAKERS:
            logger.warning(
                "%sdiarization returned %d speakers (> MAX_SPEAKERS=%d), "
                "treating as clustering blow-up; collapsing to single speaker_0. "
                "Try a higher CLUSTER_THRESHOLD, or set NUM_SPEAKERS to your "
                "known count, or MAX_SPEAKERS=0 to disable this guard.",
                log_id, len(order), _MAX_SPEAKERS,
            )
            return [dict(w, speaker="speaker_0") for w in words], 1

        remap = {label: f"speaker_{i}" for i, label in enumerate(order)}
        return (
            [dict(w, speaker=remap[w.get("speaker") or "SPEAKER_00"]) for w in diarized],
            len(order),
        )
    except Exception:
        logger.exception("%sdiarization failed; falling back to single speaker", log_id)
        return [dict(w, speaker="speaker_0") for w in words], 1


# When the entire response is going to be a single speaker (either because
# diarization was skipped or because all words landed in the same cluster),
# Char's UI has to mint a UUID per word and per speaker_hint. With 1000+
# segments x ~10 words/segment that's tens of thousands of allocations on the
# main thread, which empirically hangs the transcript view. So when we know
# there's only one speaker we use much chunkier boundaries (~30s / 80 words)
# instead of the 12s / 30 words used when speakers actually change.
_DIAR_SEG_MAX_DURATION = 12.0
_DIAR_SEG_MAX_WORDS = 30
_SINGLE_SPEAKER_SEG_MAX_DURATION = 30.0
_SINGLE_SPEAKER_SEG_MAX_WORDS = 80


def _build_diarized_segments(
    words: list[dict[str, Any]],
    fallback_text: str,
) -> list[dict[str, Any]]:
    """Group consecutive same-speaker words into segments, breaking on
    speaker changes too. Each emitted segment carries the speaker label.

    Boundary policy:
      - When the response is single-speaker (diarization skipped or
        clustering produced one cluster), use ~30s / 80-word chunks. Char's
        UI minted a UUID per word + a speaker_hint per word; 1000+ small
        segments on a 2-hour file synchronously hangs the renderer.
      - When speakers actually differ, keep tighter ~12s / 30-word chunks
        so the transcript reads naturally turn-by-turn.

    Sentence-final punctuation always closes a segment in either mode.
    """
    if not words:
        if fallback_text:
            return [{"start": 0.0, "end": 0.0, "text": fallback_text,
                     "speaker": "speaker_0"}]
        return []

    distinct = {(w.get("speaker") or "speaker_0") for w in words}
    single_speaker = len(distinct) <= 1
    if single_speaker:
        max_duration = _SINGLE_SPEAKER_SEG_MAX_DURATION
        max_words = _SINGLE_SPEAKER_SEG_MAX_WORDS
        # Sentence-final punctuation does NOT close a segment in single-
        # speaker mode -- the reader still sees the period via
        # `punctuated_word`, but multiple sentences flow into one segment.
        # This is what keeps 2-hour single-speaker output to ~200 segments
        # instead of 1400+, which is what Char's UI can actually render.
        break_on_sentence_end = False
    else:
        max_duration = _DIAR_SEG_MAX_DURATION
        max_words = _DIAR_SEG_MAX_WORDS
        break_on_sentence_end = True

    segments: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    sentence_end = (".", "?", "!")

    def flush() -> None:
        if not bucket:
            return
        text = " ".join((w.get("punctuated_word") or w.get("word") or "").strip()
                        for w in bucket).strip()
        if text:
            segments.append({
                "start": round(float(bucket[0]["start"]), 3),
                "end": round(float(bucket[-1]["end"]), 3),
                "text": text,
                "speaker": bucket[0].get("speaker") or "speaker_0",
            })
        bucket.clear()

    for w in words:
        speaker_changed = bool(bucket) and (
            (w.get("speaker") or "speaker_0") != (bucket[0].get("speaker") or "speaker_0")
        )
        if speaker_changed:
            flush()
        bucket.append(w)
        token = (w.get("punctuated_word") or w.get("word") or "").strip()
        last_char = token[-1:] if token else ""
        elapsed = float(bucket[-1]["end"]) - float(bucket[0]["start"])
        sentence_break = break_on_sentence_end and last_char in sentence_end
        if sentence_break or elapsed >= max_duration or len(bucket) >= max_words:
            flush()
    flush()

    if not segments and fallback_text:
        return [{"start": 0.0, "end": 0.0, "text": fallback_text,
                 "speaker": "speaker_0"}]
    return segments


def _build_openai_segments(
    words: list[dict[str, Any]],
    fallback_text: str,
) -> list[dict[str, Any]]:
    """Group ASR words into OpenAI-style transcription segments.

    Char's diarized_json parser expects per-segment {start, end, text}; we
    chunk at sentence-final punctuation and cap segments at ~12s / 30 words
    so very long monologues still get reasonable boundaries.
    """
    if not words:
        if fallback_text:
            return [{"start": 0.0, "end": 0.0, "text": fallback_text}]
        return []

    segments: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    sentence_end = (".", "?", "!")
    max_duration = 12.0
    max_words = 30

    def flush() -> None:
        if not bucket:
            return
        text = " ".join((w.get("punctuated_word") or w.get("word") or "").strip()
                        for w in bucket).strip()
        if text:
            segments.append({
                "start": round(float(bucket[0]["start"]), 3),
                "end": round(float(bucket[-1]["end"]), 3),
                "text": text,
            })
        bucket.clear()

    for w in words:
        bucket.append(w)
        token = (w.get("punctuated_word") or w.get("word") or "").strip()
        last_char = token[-1:] if token else ""
        elapsed = float(bucket[-1]["end"]) - float(bucket[0]["start"])
        if last_char in sentence_end or elapsed >= max_duration or len(bucket) >= max_words:
            flush()
    flush()

    if not segments and fallback_text:
        return [{"start": 0.0, "end": 0.0, "text": fallback_text}]
    return segments


def _srt_timestamp(t: float) -> str:
    t = max(0.0, float(t))
    h, rem = divmod(t, 3600.0)
    m, s = divmod(rem, 60.0)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def _vtt_timestamp(t: float) -> str:
    t = max(0.0, float(t))
    h, rem = divmod(t, 3600.0)
    m, s = divmod(rem, 60.0)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _segments_to_srt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def _segments_to_vtt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_vtt_timestamp(seg['start'])} --> {_vtt_timestamp(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def _openai_transcription_response(
    *,
    transcript: str,
    words: list[dict[str, Any]],
    language: str | None,
    duration: float,
    response_format: str,
):
    """Shape ASR output into one of the OpenAI Whisper-API response formats.

    Char's openai-transcription crate parses three JSON shapes via
    `CreateTranscriptionResponse` (Standard / Verbose / Diarized); the strings
    `text`, `srt` and `vtt` come back as plain bodies.
    """
    fmt = (response_format or "json").strip().lower()

    if fmt == "text":
        return PlainTextResponse(transcript or "")

    if fmt == "srt":
        segments = _build_openai_segments(words, transcript)
        return PlainTextResponse(
            _segments_to_srt(segments),
            media_type="application/x-subrip",
        )

    if fmt == "vtt":
        segments = _build_openai_segments(words, transcript)
        return PlainTextResponse(
            _segments_to_vtt(segments),
            media_type="text/vtt",
        )

    if fmt == "verbose_json":
        lang_code = (language or "").lower()
        return {
            "task": "transcribe",
            "language": _LANG_CODE_TO_NAME.get(lang_code, lang_code or "english"),
            "duration": round(float(duration), 3),
            "text": transcript,
            "words": [
                {
                    "word": (w.get("punctuated_word") or w.get("word") or "").strip(),
                    "start": round(float(w["start"]), 3),
                    "end": round(float(w["end"]), 3),
                }
                for w in words
            ],
            "segments": [
                {
                    "id": i,
                    "seek": 0,
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                    "tokens": [],
                    "temperature": 0.0,
                    "avg_logprob": 0.0,
                    "compression_ratio": 1.0,
                    "no_speech_prob": 0.0,
                }
                for i, seg in enumerate(_build_openai_segments(words, transcript))
            ],
        }

    if fmt == "diarized_json":
        # Char's default OpenAI batch model is gpt-4o-transcribe-diarize, which
        # expects `segments[*].speaker` as a string label. We honour real
        # diarization here when each word has a `speaker` field attached
        # (the endpoint runs sherpa-onnx before calling us); otherwise we
        # fall back to a single anonymous speaker so Char's parser still
        # gets a valid response.
        if any(w.get("speaker") for w in words):
            segments = _build_diarized_segments(words, transcript)
        else:
            segments = [dict(s, speaker="speaker_0")
                        for s in _build_openai_segments(words, transcript)]
        return {
            "task": "transcribe",
            "duration": round(float(duration), 3),
            "text": transcript,
            "segments": [
                {
                    "id": f"seg_{i}",
                    "speaker": seg["speaker"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                    "type": "transcript.text.segment",
                }
                for i, seg in enumerate(segments)
            ],
        }

    return {"text": transcript}


# Char's BATCH_IDLE_TIMEOUT in tauri-plugin-transcription/listener2/ext.rs is
# hardcoded at 60 seconds. The non-streaming `simple` batch path (used for
# `gpt-4o-transcribe-diarize`) only emits its single BatchResponse event at
# the very end, so any audio whose ASR takes >60s aborts on the client side
# (no toast, no log -- the spawned future is just dropped). The progressive
# path (used for `gpt-4o-transcribe`) resets that timer on every SSE delta,
# so we keep it alive with a tiny heartbeat delta every N seconds.
_OPENAI_STREAM_HEARTBEAT_SECONDS = _CFG.stream_heartbeat_seconds


def _parse_diarize_overrides(
    query_params, request_id: str,
) -> tuple[int | None, float | None]:
    """Extract per-request diarization overrides from the query string.

    Recognised keys (loose typing; bad values are logged + ignored
    rather than 400ed, so a user typo can't kill an in-flight Generate):

      * ``num_speakers=N``   -> force exactly N clusters
      * ``cluster_threshold=F`` -> raw 0..1 distance threshold

    Returns ``(num_speakers, cluster_threshold)`` with either set to
    ``None`` when absent / invalid. ``redo-session`` is the primary
    consumer; humans can also pass them via curl when comparing
    different parameterisations on the same audio.
    """
    num_speakers: int | None = None
    threshold: float | None = None

    raw_num = query_params.get("num_speakers")
    if raw_num is not None and raw_num != "":
        try:
            n = int(raw_num)
            if 1 <= n <= 50:
                num_speakers = n
            else:
                logger.warning(
                    "[openai %s] ignoring num_speakers=%s (out of range 1..50)",
                    request_id, raw_num,
                )
        except (TypeError, ValueError):
            logger.warning(
                "[openai %s] ignoring num_speakers=%r (not an int)",
                request_id, raw_num,
            )

    raw_thr = query_params.get("cluster_threshold")
    if raw_thr is not None and raw_thr != "":
        try:
            f = float(raw_thr)
            if 0.0 < f < 1.0:
                threshold = f
            else:
                logger.warning(
                    "[openai %s] ignoring cluster_threshold=%s (must be in (0, 1))",
                    request_id, raw_thr,
                )
        except (TypeError, ValueError):
            logger.warning(
                "[openai %s] ignoring cluster_threshold=%r (not a float)",
                request_id, raw_thr,
            )

    return num_speakers, threshold


def _sse(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _compose_speaker_prefixed_text(
    words: list[dict[str, Any]], fallback_text: str,
) -> str:
    """Inline `Speaker N: ...` prefixes for the streaming text response.

    The progressive batch shape Char's UI accepts only carries plain text --
    no segment array, no speaker-label objects -- so for short files where
    diarization actually fires we fold the speaker label into the prose.
    Lines from the same speaker stay contiguous; speaker turns get a blank
    line between them so the transcript is readable in Char's note view.
    """
    if not words:
        return fallback_text or ""

    lines: list[str] = []
    bucket_speaker: str | None = None
    bucket_words: list[str] = []

    def flush() -> None:
        if not bucket_words:
            return
        text = " ".join(bucket_words).strip()
        if text:
            label = (bucket_speaker or "speaker_0").replace("_", " ").title()
            lines.append(f"{label}: {text}")
        bucket_words.clear()

    for w in words:
        spk = w.get("speaker") or "speaker_0"
        if bucket_speaker is None:
            bucket_speaker = spk
        if spk != bucket_speaker:
            flush()
            bucket_speaker = spk
        token = (w.get("punctuated_word") or w.get("word") or "").strip()
        if token:
            bucket_words.append(token)
    flush()

    if not lines:
        return fallback_text or ""
    return "\n\n".join(lines)


async def _stream_openai_transcription(
    request_id: str,
    audio_path: str,
    started: float,
    do_diarize: bool,
    *,
    num_speakers_override: int | None = None,
    cluster_threshold_override: float | None = None,
):
    """SSE generator for Char's progressive (gpt-4o-transcribe) batch path.

    Emits a `transcript.text.delta` heartbeat every
    STREAM_HEARTBEAT_SECONDS while ASR runs, then a `transcript.text.done`
    with the full transcript (with inlined `Speaker N:` prefixes when
    diarization actually produced multiple speakers). Char's parser
    accumulates deltas into `partial_text` but the non-empty `done.text`
    replaces it, so the heartbeat spaces never reach the user.
    """
    try:
        asr_task = asyncio.create_task(_run_asr_async(audio_path))

        while True:
            try:
                transcript, words, lang, duration = await asyncio.wait_for(
                    asyncio.shield(asr_task),
                    timeout=_OPENAI_STREAM_HEARTBEAT_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                logger.info(
                    "[openai %s] streaming heartbeat (asr in flight, %.1fs elapsed)",
                    request_id, time.time() - started,
                )
                yield _sse({
                    "type": "transcript.text.delta",
                    "delta": " ",
                    "logprobs": [],
                })

        asr_done = time.time() - started
        num_speakers = 0
        skipped_reason: str | None = None
        if do_diarize and words:
            logger.info(
                "[openai %s] running diarization (sherpa-onnx) ...", request_id,
            )
            words, num_speakers = await asyncio.to_thread(
                _attach_speakers_to_words, audio_path, words, duration, request_id,
                num_speakers_override=num_speakers_override,
                cluster_threshold_override=cluster_threshold_override,
            )
            if num_speakers > 1:
                transcript = _compose_speaker_prefixed_text(words, transcript)
        elif not do_diarize:
            skipped_reason = "diarize_disabled"
        elif not words:
            skipped_reason = "empty_words"

        speakers_agg = _compute_speaker_airtime(words)
        diar_meta = _diarization_metadata(
            diarize_enabled=do_diarize,
            duration=duration,
            num_speakers=num_speakers or (1 if words else 0),
            num_speakers_override=num_speakers_override,
            cluster_threshold_override=cluster_threshold_override,
            skipped_reason=skipped_reason,
            speakers=speakers_agg,
        )

        # Sidecar-write transcript.json into Char's session dir BEFORE
        # we send `done`. Char's progressive parser will drop our words
        # but the sidecar lands them on disk for the persister to load
        # on next session-open. Run on a worker thread so the SSE
        # heartbeat loop doesn't have to wait for SHA256 hashing.
        await asyncio.to_thread(
            _maybe_write_char_transcript,
            audio_path, words, lang, request_id,
            metadata=diar_meta,
        )

        elapsed = time.time() - started
        logger.info(
            "[openai %s] stream done in %.2fs (asr=%.2fs, diar=%.2fs, "
            "speakers=%d), %d chars, lang=%s%s",
            request_id, elapsed, asr_done, max(0.0, elapsed - asr_done),
            num_speakers, len(transcript or ""), lang,
            _format_airtime_log(speakers_agg),
        )

        yield _sse({
            "type": "transcript.text.done",
            "text": transcript or "",
            "logprobs": [],
            "usage": {
                "type": "duration",
                "seconds": int(round(float(duration or 0))),
            },
        })
        yield "data: [DONE]\n\n"
    except Exception as exc:
        logger.exception("[openai %s] streaming transcription failed", request_id)
        yield _sse({
            "type": "transcript.text.done",
            "text": f"[Local transcription error: {exc}]",
            "logprobs": [],
            "usage": {"type": "duration", "seconds": 0},
        })
        yield "data: [DONE]\n\n"
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass


@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_asr_token)])
async def openai_audio_transcriptions(request: Request):
    """OpenAI Whisper-API-compatible batch transcription.

    Multipart form fields we honour:
      * file (required)         - audio bytes (any format librosa decodes)
      * model (optional)        - ignored; we always use ASR_BACKEND
      * language (optional)     - ISO-639-1 hint passed to faster-whisper
      * response_format         - json | text | srt | verbose_json | vtt | diarized_json
                                  (default: json; Char defaults to diarized_json)
      * stream                  - "true" enables OpenAI-style SSE response
                                  (transcript.text.delta + transcript.text.done).
                                  Char's progressive batch path uses this for
                                  `gpt-4o-transcribe` and we heartbeat the
                                  delta channel so long files don't trip the
                                  client-side 60-second idle abort.
      * temperature, prompt,
        timestamp_granularities - silently ignored (we don't sample, prompt,
                                  or emit per-token granularity).

    Auth: Authorization: Bearer <token> required, where <token> is the
    HKDF-derived ASR token (see service_auth.py). Char's OpenAI
    api_key field is wired to this token by ``./run.sh configure-char``.
    """
    request_id = str(uuid.uuid4())
    started = time.time()
    content_type = (request.headers.get("content-type") or "").lower()

    if not content_type.startswith("multipart/form-data"):
        return JSONResponse(
            {"error": {
                "message": "Content-Type must be multipart/form-data",
                "type": "invalid_request_error",
                "code": "invalid_content_type",
            }},
            status_code=400,
        )

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse(
            {"error": {
                "message": "Missing required form field 'file'",
                "type": "invalid_request_error",
                "code": "missing_file",
            }},
            status_code=400,
        )

    audio_bytes = await upload.read()
    if not audio_bytes:
        return JSONResponse(
            {"error": {
                "message": "Empty 'file' upload",
                "type": "invalid_request_error",
                "code": "empty_file",
            }},
            status_code=400,
        )

    suffix = os.path.splitext(upload.filename or "audio")[1] or ".bin"
    requested_model = (form.get("model") or "").strip()
    response_format = (form.get("response_format") or "json").strip().lower()
    stream_flag = (form.get("stream") or "").strip().lower() in ("true", "1", "yes")
    if response_format not in _OPENAI_VALID_FORMATS:
        return JSONResponse(
            {"error": {
                "message": (
                    f"Unknown response_format '{response_format}'. "
                    f"Expected one of: {sorted(_OPENAI_VALID_FORMATS)}"
                ),
                "type": "invalid_request_error",
                "code": "invalid_response_format",
            }},
            status_code=400,
        )

    logger.info(
        "[openai %s] received %d bytes (model=%r, response_format=%s, "
        "stream=%s, filename=%r)",
        request_id, len(audio_bytes), requested_model or "<unset>",
        response_format, stream_flag, upload.filename or "<unset>",
    )

    if stream_flag:
        # Persist the audio to a tempfile that outlives this handler; the
        # streaming generator unlinks it in its finally block.
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, audio_bytes)
        finally:
            os.close(fd)
        # Streaming-path diarization gate: the original gate required
        # response_format=diarized_json or model=*-diarize, which made
        # sense back when Char used `gpt-4o-transcribe-diarize` for the
        # non-streaming batch flow. After we moved Char to the
        # progressive (SSE) path to bypass BATCH_IDLE_TIMEOUT, Char
        # sends `model=gpt-4o-transcribe` + `response_format=json` and
        # those gates collapsed -- diarization silently no-op'd, every
        # streamed transcript came back with a single speaker_0.
        # Now: diarize whenever the user has it enabled in config.
        # `?diarize=0` query string is honoured for one-off opt-out.
        do_diarize = _OPENAI_BATCH_DIARIZE and (
            request.query_params.get("diarize", "1").lower()
            not in ("0", "false", "no", "off")
        )
        num_speakers_override, cluster_threshold_override = _parse_diarize_overrides(
            request.query_params, request_id,
        )
        return StreamingResponse(
            _stream_openai_transcription(
                request_id, tmp_path, started, do_diarize,
                num_speakers_override=num_speakers_override,
                cluster_threshold_override=cluster_threshold_override,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        try:
            transcript, words, lang, duration = await _run_asr_async(tmp.name)
        except Exception as exc:
            logger.exception("[openai %s] transcription failed", request_id)
            return JSONResponse(
                {"error": {
                    "message": str(exc),
                    "type": "internal_server_error",
                    "code": "transcription_failed",
                }},
                status_code=500,
            )

        asr_done = time.time() - started
        num_speakers = 0
        diar_enabled = (
            response_format == "diarized_json"
            and _OPENAI_BATCH_DIARIZE
            and bool(words)
        )
        num_speakers_override = None
        cluster_threshold_override = None
        skipped_reason: str | None = None
        if diar_enabled:
            logger.info(
                "[openai %s] running diarization (sherpa-onnx) ...", request_id,
            )
            num_speakers_override, cluster_threshold_override = (
                _parse_diarize_overrides(request.query_params, request_id)
            )
            words, num_speakers = await asyncio.to_thread(
                _attach_speakers_to_words, tmp.name, words, duration, request_id,
                num_speakers_override=num_speakers_override,
                cluster_threshold_override=cluster_threshold_override,
            )
        else:
            if response_format != "diarized_json":
                skipped_reason = f"response_format={response_format}"
            elif not _OPENAI_BATCH_DIARIZE:
                skipped_reason = "diarize_disabled"
            elif not words:
                skipped_reason = "empty_words"

        speakers_agg = _compute_speaker_airtime(words)
        diar_meta = _diarization_metadata(
            diarize_enabled=diar_enabled,
            duration=duration,
            num_speakers=num_speakers or (1 if words else 0),
            num_speakers_override=num_speakers_override,
            cluster_threshold_override=cluster_threshold_override,
            skipped_reason=skipped_reason,
            speakers=speakers_agg,
        )

        # Mirror the streaming path's sidecar write: ensures the
        # non-streaming `gpt-4o-transcribe-diarize` path also lands a
        # transcript.json on disk even when Char's IPC layer flakes out
        # (and gives us a deterministic record of what we returned).
        # Done while tmp still exists so SHA256 is meaningful.
        await asyncio.to_thread(
            _maybe_write_char_transcript,
            tmp.name, words, lang, request_id,
            metadata=diar_meta,
        )

    elapsed = time.time() - started
    if num_speakers:
        logger.info(
            "[openai %s] done in %.2fs (asr=%.2fs, diar=%.2fs, "
            "speakers=%d), %d chars, lang=%s, format=%s%s",
            request_id, elapsed, asr_done, elapsed - asr_done,
            num_speakers, len(transcript or ""), lang, response_format,
            _format_airtime_log(speakers_agg),
        )
    else:
        logger.info(
            "[openai %s] done in %.2fs, %d chars, lang=%s, format=%s",
            request_id, elapsed, len(transcript or ""), lang, response_format,
        )

    return _openai_transcription_response(
        transcript=transcript or "",
        words=words or [],
        language=lang,
        duration=duration,
        response_format=response_format,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "asr_backend": ASR_BACKEND,
        "model": MODEL_NAME,
        "arch": _arch,
        "compute_type": COMPUTE_TYPE if ASR_BACKEND == "whisper" else "mlx-bfloat16",
        "device": DEVICE if ASR_BACKEND == "whisper" else "mlx",
        "language": LANGUAGE or ("en" if ASR_BACKEND == "parakeet" else "auto"),
        "endpoints": {
            "deepgram_batch": "POST /v1/listen",
            "deepgram_stream": "POST /v1/listen/stream",
            "deepgram_ws": "WS /v1/listen",
            "openai_batch": "POST /v1/audio/transcriptions",
        },
    }


@app.post("/v1/listen", dependencies=[Depends(require_asr_token)])
async def listen(request: Request):
    """Deepgram-compatible batch transcription.

    Accepts raw audio in the request body (Deepgram's documented contract) or,
    as a convenience, a multipart upload with a `file` part. Deepgram query
    params (model, language, smart_format, diarize, ...) are accepted and
    quietly ignored - we always use the locally-configured Whisper model.
    """
    request_id = request.headers.get("dg-request-id") or str(uuid.uuid4())
    started = time.time()
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if not isinstance(upload, UploadFile):
            return JSONResponse(
                {"err_code": "Bad Request", "err_msg": "missing 'file' part in multipart body"},
                status_code=400,
            )
        audio_bytes = await upload.read()
        suffix = os.path.splitext(upload.filename or "audio")[1] or ".bin"
    else:
        audio_bytes = await request.body()
        suffix = ".bin"

    if not audio_bytes:
        return JSONResponse(
            {"err_code": "Bad Request", "err_msg": "empty request body"},
            status_code=400,
        )

    logger.info(
        "[batch %s] received %d bytes (%s)",
        request_id, len(audio_bytes), content_type or "raw",
    )

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        try:
            transcript, words, lang, duration = await _run_asr_async(tmp.name)
        except Exception as exc:
            logger.exception("[batch %s] transcription failed", request_id)
            return JSONResponse(
                {
                    "err_code": "Internal Server Error",
                    "err_msg": str(exc),
                    "request_id": request_id,
                },
                status_code=500,
            )

    elapsed = time.time() - started
    logger.info(
        "[batch %s] done in %.2fs, %d chars, lang=%s",
        request_id, elapsed, len(transcript), lang,
    )
    return _deepgram_batch_response(
        audio_bytes=audio_bytes,
        transcript=transcript,
        words=words,
        language=lang,
        duration_s=duration,
        request_id=request_id,
    )


@app.post("/v1/listen/stream", dependencies=[Depends(require_asr_token)])
async def listen_stream(request: Request):
    """Same input as /v1/listen, but streams NDJSON progress events as Whisper
    processes each segment. The final line is the same Deepgram-shaped JSON
    the batch endpoint returns. Useful for UIs (or transcribe_file.py
    --progress) that want a live progress bar.

    This endpoint is intentionally separate from /v1/listen so the Deepgram
    contract Char relies on stays a single plain-JSON response.

    Stream format (one JSON object per line, application/x-ndjson):
        {"type": "start",   "request_id": "...", "duration": 12.34, "language": "en"}
        {"type": "segment", "progress": 0.42, "elapsed": 1.5,
                            "segment": {"start": 5.0, "end": 8.4, "text": "..."}}
        ...
        {"type": "done",    "elapsed": 6.7, "result": <Deepgram JSON>}
        {"type": "error",   "message": "..."}   (instead of "done" on failure)
    """
    request_id = request.headers.get("dg-request-id") or str(uuid.uuid4())
    started = time.time()
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if not isinstance(upload, UploadFile):
            return JSONResponse(
                {"err_code": "Bad Request", "err_msg": "missing 'file' part in multipart body"},
                status_code=400,
            )
        audio_bytes = await upload.read()
        suffix = os.path.splitext(upload.filename or "audio")[1] or ".bin"
    else:
        audio_bytes = await request.body()
        suffix = ".bin"

    if not audio_bytes:
        return JSONResponse(
            {"err_code": "Bad Request", "err_msg": "empty request body"},
            status_code=400,
        )

    logger.info(
        "[stream %s] received %d bytes (%s)",
        request_id, len(audio_bytes), content_type or "raw",
    )

    async def event_stream():
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            duration_holder: dict[str, float] = {"duration": 0.0}

            def push(event: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, event)

            def on_start(info) -> None:
                d = float(getattr(info, "duration", 0.0) or 0.0)
                duration_holder["duration"] = d
                push({
                    "type": "start",
                    "request_id": request_id,
                    "duration": round(d, 3),
                    "language": info.language,
                    "language_probability": round(float(info.language_probability), 4),
                })

            def on_segment(seg) -> None:
                d = duration_holder["duration"]
                progress = min(1.0, float(seg.end) / d) if d > 0 else 0.0
                push({
                    "type": "segment",
                    "progress": round(progress, 4),
                    "elapsed": round(time.time() - started, 3),
                    "segment": {
                        "id": int(getattr(seg, "id", 0) or 0),
                        "start": round(float(seg.start), 3),
                        "end": round(float(seg.end), 3),
                        "text": seg.text.strip(),
                    },
                })

            def producer() -> None:
                try:
                    transcript, words, lang, duration = _run_asr(
                        tmp.name, on_segment=on_segment, on_start=on_start,
                    )
                    push({
                        "type": "done",
                        "elapsed": round(time.time() - started, 3),
                        "result": _deepgram_batch_response(
                            audio_bytes=audio_bytes,
                            transcript=transcript,
                            words=words,
                            language=lang,
                            duration_s=duration,
                            request_id=request_id,
                        ),
                    })
                except Exception as exc:
                    logger.exception("[stream %s] transcription failed", request_id)
                    push({"type": "error", "message": str(exc), "request_id": request_id})
                finally:
                    push(None)

            # Reuse _mlx_executor so the parakeet/MLX stream initializer
            # runs and the GPU isn't shared across concurrent threads. For
            # the whisper backend this is just a regular worker thread; the
            # initializer is a no-op when ASR_BACKEND != "parakeet".
            _mlx_executor.submit(producer)

            while True:
                event = await queue.get()
                if event is None:
                    break
                yield (json.dumps(event) + "\n").encode("utf-8")

        logger.info(
            "[stream %s] closed in %.2fs",
            request_id, time.time() - started,
        )

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _query_int(ws: WebSocket, key: str, default: int) -> int:
    try:
        return int(ws.query_params.get(key, default))
    except (TypeError, ValueError):
        return default


@app.websocket("/v1/listen")
async def listen_ws(ws: WebSocket):
    """Deepgram-compatible WebSocket streaming transcription.

    The client sends raw PCM frames (default 16 kHz, 16-bit signed, mono) and
    optional control text frames: {"type":"KeepAlive"}, {"type":"CloseStream"},
    {"type":"Finalize"}. When the stream ends (or Finalize is received) we run
    faster-whisper on the buffered audio and emit a Deepgram-shaped Results
    message followed by a Metadata message, then close.

    Note: this is "batch over WebSocket" - we don't emit interim partials.
    faster-whisper isn't built for true streaming inference, and Char's
    downstream LLM step only consumes the final transcript anyway. If you need
    live partial captions during a call later, switch to a streaming-capable
    model (e.g., whisper-streaming or a Riva backend) without changing the
    wire protocol here.

    Auth: same token as the HTTP endpoints. Token can be passed via the
    Authorization upgrade header, the Sec-WebSocket-Protocol subprotocol
    (some browsers don't allow custom upgrade headers), or ?api_key=.
    """
    # Validate auth *before* accept() so a wrong-token request never sees
    # the 101 Switching Protocols response.
    if not await _ws_auth(ws):
        return
    await ws.accept()
    request_id = str(uuid.uuid4())
    sample_rate = _query_int(ws, "sample_rate", 16000)
    channels = max(1, _query_int(ws, "channels", 1))
    encoding = (ws.query_params.get("encoding") or "linear16").lower()

    if encoding not in {"linear16", "pcm_s16le"}:
        await ws.send_json({
            "type": "Error",
            "description": f"unsupported encoding '{encoding}' - this server only handles linear16",
            "message": "Send 16-bit signed PCM, or use the POST /v1/listen batch endpoint.",
        })
        await ws.close(code=1003)
        return

    logger.info(
        "[ws %s] open sr=%d ch=%d enc=%s",
        request_id, sample_rate, channels, encoding,
    )

    chunks: list[bytes] = []
    started = time.time()
    finalize_requested = False

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            data = msg.get("bytes")
            if data:
                chunks.append(data)
                continue

            text = msg.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            ctrl = (payload.get("type") or "").lower()
            if ctrl == "keepalive":
                continue
            if ctrl in {"closestream", "finalize"}:
                finalize_requested = True
                break
    except WebSocketDisconnect:
        pass

    audio_bytes = b"".join(chunks)
    if not audio_bytes:
        logger.info("[ws %s] no audio received", request_id)
        try:
            await ws.send_json({
                "type": "Metadata",
                "transaction_key": "deprecated",
                "request_id": request_id,
                "sha256": "",
                "created": _now_iso(),
                "duration": 0.0,
                "channels": channels,
                "models": [MODEL_NAME],
            })
            await ws.close()
        except Exception:
            pass
        return

    pcm = np.frombuffer(audio_bytes, dtype=np.int16)
    if channels > 1 and pcm.size % channels == 0:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    audio_f32 = pcm.astype(np.float32) / 32768.0

    if sample_rate != 16000 and audio_f32.size > 0:
        target_len = int(round(audio_f32.size * 16000 / sample_rate))
        if target_len > 0:
            x_old = np.linspace(0.0, 1.0, num=audio_f32.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            audio_f32 = np.interp(x_new, x_old, audio_f32).astype(np.float32)

    duration_s = float(audio_f32.size) / 16000.0
    logger.info(
        "[ws %s] finalize=%s buffered=%.2fs (%d samples)",
        request_id, finalize_requested, duration_s, audio_f32.size,
    )

    try:
        transcript, words, lang, duration = await _run_asr_async(audio_f32)
    except Exception as exc:
        logger.exception("[ws %s] transcription failed", request_id)
        try:
            await ws.send_json({"type": "Error", "description": str(exc)})
            await ws.close(code=1011)
        except Exception:
            pass
        return

    elapsed = time.time() - started
    avg_conf = _avg_confidence(words)

    results_msg = {
        "type": "Results",
        "channel_index": [0, channels],
        "duration": round(duration, 3),
        "start": 0.0,
        "is_final": True,
        "speech_final": True,
        "from_finalize": finalize_requested,
        "channel": {
            "alternatives": [
                {
                    "transcript": transcript,
                    "confidence": avg_conf,
                    "words": words,
                    "languages": [lang] if lang else [],
                }
            ],
            "search": [],
        },
        "metadata": {
            "request_id": request_id,
            "model_info": _model_info_block()[MODEL_NAME],
            "model_uuid": MODEL_NAME,
        },
    }
    metadata_msg = {
        "type": "Metadata",
        "transaction_key": "deprecated",
        "request_id": request_id,
        "sha256": hashlib.sha256(audio_bytes).hexdigest(),
        "created": _now_iso(),
        "duration": round(duration, 3),
        "channels": channels,
        "models": [MODEL_NAME],
    }

    try:
        await ws.send_json(results_msg)
        await ws.send_json(metadata_msg)
        await ws.close()
    except Exception:
        pass

    logger.info(
        "[ws %s] sent transcript (%d chars, lang=%s) in %.2fs",
        request_id, len(transcript), lang, elapsed,
    )
