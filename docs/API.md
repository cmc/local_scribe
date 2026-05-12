# API surface (for non-Char clients)

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/INTEGRATION.md`](INTEGRATION.md), [`docs/INSPECTOR.md`](INSPECTOR.md)

The server is a strict subset of Deepgram, so any Deepgram SDK pointed at
`http://127.0.0.1:8000` will work.

### `POST /v1/listen`

```bash
curl -X POST http://127.0.0.1:8000/v1/listen \
  -H "Content-Type: application/octet-stream" \
  --data-binary @call.m4a
```

Returns a Deepgram-shaped JSON document with `metadata`, `results.channels[0].alternatives[0].{transcript,confidence,words}`,
and `detected_language`. Query params (`model`, `smart_format`, `punctuate`, …)
are accepted and quietly ignored — we always use the locally-configured ASR.

### `POST /v1/listen/stream` (extension)

Same input as `/v1/listen`. Returns NDJSON progress events:

```json
{"type": "start",   "duration": 600.0, "language": "en", ...}
{"type": "segment", "progress": 0.42, "elapsed": 18.3, "segment": {...}}
{"type": "done",    "elapsed": 41.8, "result": <full Deepgram JSON>}
```

`transcribe_file.py --progress` consumes this for live progress bars.

### `WS /v1/listen`

Char's live recording path. Send raw `linear16` PCM frames; server runs ASR
on close/finalize and emits a single Deepgram `Results` message followed by
`Metadata`. Note: this is "batch over WebSocket" — there are no interim
partials, since neither Parakeet nor faster-whisper streams natively.

### `POST /v1/audio/transcriptions` (OpenAI Whisper API)

What Char hits when you click *Generate* on a note with existing audio
(provider = OpenAI Batch Only, Base URL = `http://127.0.0.1:8000/v1`). Also
works with the official `openai` Python SDK pointed at `base_url="http://127.0.0.1:8000/v1"`.

```bash
curl -X POST http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer any-non-empty-key" \
  -F "file=@call.m4a" \
  -F "model=gpt-4o-transcribe-diarize" \
  -F "response_format=diarized_json"
```

Form fields honoured: `file` (required), `model` (ignored — we always run the
locally-configured ASR), `language` (optional ISO-639-1 hint), `response_format`
(one of `json` (default), `text`, `verbose_json`, `srt`, `vtt`, `diarized_json`).
`temperature`, `prompt`, `timestamp_granularities[]`, and `stream` are accepted
and silently ignored.

`diarized_json` runs real sherpa-onnx speaker diarization by default and
returns segments labelled `speaker_0`, `speaker_1`, ... in encounter order.
Disable with `OPENAI_BATCH_DIARIZE=0` to skip it (~3-4s faster). Tune speaker
count with `NUM_SPEAKERS` / `CLUSTER_THRESHOLD`.

### `GET /health`

```json
{
  "ok": true,
  "asr_backend": "parakeet",
  "model": "mlx-community/parakeet-tdt-0.6b-v3",
  "arch": "Parakeet-TDT",
  "compute_type": "mlx-bfloat16",
  "device": "mlx",
  "language": "en",
  "endpoints": {
    "deepgram_batch":  "POST /v1/listen",
    "deepgram_stream": "POST /v1/listen/stream",
    "deepgram_ws":     "WS /v1/listen",
    "openai_batch":    "POST /v1/audio/transcriptions"
  }
}
```

