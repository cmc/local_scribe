"""
Local Deepgram-compatible Whisper server.

Implements the two pieces of Deepgram's /v1/listen contract that real clients
(including Char with BYOK Deepgram + a custom base URL) actually use:

  * POST  /v1/listen   - batch transcription with raw audio in the request body
  * WS    /v1/listen   - streaming transcription over WebSocket (linear16 PCM)

Char setup
----------
Tell Char to send transcription traffic to this server instead of Deepgram:

    export CHAR_BASE_URL="http://127.0.0.1:8000"
    export CHAR_API_KEY="local"   # any non-empty string; we ignore auth locally

In the desktop app: Settings -> Transcription -> BYOK Deepgram, paste any key,
and (if available) set the base URL to http://127.0.0.1:8000.

Run
---
    uvicorn whisper_server:app --host 0.0.0.0 --port 8000

Configuration env vars
----------------------
    WHISPER_MODEL          (default: large-v3-turbo)
                              tiny | base | small | medium | large-v3 | large-v3-turbo |
                              distil-large-v3 | distil-medium.en | distil-small.en
    WHISPER_COMPUTE_TYPE   (default: int8)    int8 | int16 | float32 (CPU)
                                              int8_float16 | float16 (CUDA only)
    WHISPER_DEVICE         (default: auto)    cpu | cuda | auto
    WHISPER_LANGUAGE       (default: unset)   ISO-639 code; leave unset to autodetect

Model recommendation: on Apple Silicon with >=16GB RAM, `large-v3-turbo` at int8
gives near-`large-v3` accuracy while running ~5x faster than full large-v3.
For absolute best quality on tricky / non-English audio, switch to `large-v3`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from faster_whisper import WhisperModel

logger = logging.getLogger("whisper_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

MODEL_NAME = os.getenv("WHISPER_MODEL", "large-v3-turbo")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
DEVICE = os.getenv("WHISPER_DEVICE", "auto")
LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None

logger.info(
    "loading faster-whisper model=%s compute=%s device=%s",
    MODEL_NAME, COMPUTE_TYPE, DEVICE,
)
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
logger.info("model ready")

app = FastAPI(title="whisper-server", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _run_whisper(
    audio: Any,
    on_segment=None,
    on_start=None,
) -> tuple[str, list[dict[str, Any]], str | None, float]:
    """Synchronous transcription helper. `audio` is anything faster-whisper accepts:
    a file path or a 1-D float32 numpy array sampled at 16 kHz.

    Optional callbacks (invoked from this thread):
        on_start(info)   - fired once after the audio is decoded and total duration is known
        on_segment(seg)  - fired after each segment is produced; `seg` has .start, .end, .text
    """
    segments, info = model.transcribe(
        audio,
        language=LANGUAGE,
        word_timestamps=True,
        vad_filter=True,
    )
    if on_start is not None:
        on_start(info)
    text_parts: list[str] = []
    words: list[dict[str, Any]] = []
    for seg in segments:
        text_parts.append(seg.text)
        if seg.words:
            for w in seg.words:
                words.append(_build_word(w))
        if on_segment is not None:
            on_segment(seg)
    transcript = " ".join(p.strip() for p in text_parts).strip()
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    return transcript, words, info.language, duration


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
            "model_info": {
                MODEL_NAME: {
                    "name": f"faster-whisper-{MODEL_NAME}",
                    "version": "local",
                    "arch": "whisper",
                }
            },
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


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL_NAME,
        "compute_type": COMPUTE_TYPE,
        "device": DEVICE,
        "language": LANGUAGE or "auto",
    }


@app.post("/v1/listen")
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
            transcript, words, lang, duration = await asyncio.to_thread(_run_whisper, tmp.name)
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


@app.post("/v1/listen/stream")
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
                    transcript, words, lang, duration = _run_whisper(
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

            threading.Thread(target=producer, daemon=True).start()

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
    """
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
        transcript, words, lang, duration = await asyncio.to_thread(_run_whisper, audio_f32)
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
            "model_info": {
                "name": f"faster-whisper-{MODEL_NAME}",
                "version": "local",
                "arch": "whisper",
            },
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
