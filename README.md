# whisper_server

Local, private, Apple-Silicon-native transcription + summarization pipeline.
Drops in as a Deepgram-compatible endpoint behind [Char](https://char.so) and uses
your local LM Studio + Qwen for note generation. Everything runs offline once
the models are downloaded.

```
                 ┌─────────────────────┐
   live mic ─►   │       Char.app      │   call recording UI + auto-note
                 └──────────┬──────────┘
                            │ POST /v1/listen   (Deepgram contract)
                            ▼
       ┌──────────────────────────────────┐
       │  whisper_server.py   :8000       │
       │  • Parakeet-TDT 0.6B v3 (MLX)    │  ← default; English; lowest WER
       │  • faster-whisper large-v3-turbo │  ← fallback; multilingual
       └──────────────┬───────────────────┘
                      │ Deepgram JSON
                      ▼
                 Char ──► LM Studio :1234 ──► Qwen3-30B   summary in note UI

       ┌──────────────────────────────────┐
       │  transcribe_file.py              │  manual one-shot CLI
       │  • cache by audio sha256         │   for files Char didn't auto-pick up
       │  • optional diarization          │   (sherpa-onnx + LLM speaker naming)
       │  • streaming LLM summary         │
       └──────────────────────────────────┘
```

## What you get

- **`whisper_server.py`** — FastAPI service that mimics the bits of Deepgram's
  `/v1/listen` API that Char actually uses (POST batch + WebSocket streaming).
  Char doesn't know it's not Deepgram.
- **`transcribe_file.py`** — CLI for files Char didn't auto-transcribe.
  Outputs a structured summary (TL;DR, Participants, Key points, Decisions,
  Open questions, Risks, Next steps, Notable quotes), with optional speaker
  diarization, a content-addressed cache so re-runs are instant, and live
  progress + token-streaming.
- **`run.sh`** — single command for the whole pipeline. Auto-installs
  Python deps + ASR/diarization model weights on first run.

## Prerequisites

You need to install these manually once:

| | what | where | why |
|---|---|---|---|
| 1 | macOS on Apple Silicon (M-series) | — | Parakeet uses MLX |
| 2 | Python 3.12 or 3.14 | `brew install python@3.14` | runs the server + CLI |
| 3 | [Char.app](https://char.so) | App Store / DMG | call recording UI |
| 4 | [LM Studio.app](https://lmstudio.ai) | website | local LLM host |
| 5 | LM Studio model: **`qwen3-30b-a3b-instruct-2507`** | LM Studio's model search | summaries (≈18 GB MLX) |
| 6 | LM Studio CLI: `lms` | run `~/.lmstudio/bin/lms bootstrap` once | so `run.sh` can auto-load Qwen |

Everything else (parakeet weights ≈1.2 GB, sherpa-onnx diarization models
≈45 MB, faster-whisper large-v3-turbo ≈1.6 GB if you opt into it) is fetched
automatically the first time you start the pipeline.

## Quick start

```bash
git clone <this repo>
cd whisper_server
./run.sh start
```

That's it. On first run `run.sh` will:

1. Create `./venv` and `pip install -r requirements.txt`
2. Download the Parakeet TDT v3 weights into `~/.cache/huggingface/`
3. Download the sherpa-onnx diarization models into
   `~/.cache/whisper_server/diarization/`
4. Start LM Studio's local server (if `lms` CLI is installed) and load the
   Qwen3 model with a 65k-token context
5. Start `uvicorn whisper_server:app` on `:8000`
6. Tail the ASR log so you can watch traffic. Hit `Ctrl+C` to detach — the
   services keep running in the background.

Subsequent runs skip everything that's already cached.

### Configure Char

In Char → Settings → Transcription:

- **Model being used**: Custom (the right-hand "nova-2" string is decorative —
  this server ignores it and routes through whatever `ASR_BACKEND` is set to).
- **Custom provider Base URL**: `http://127.0.0.1:8000`
- **API Key**: any non-empty string (auth is ignored locally).
- **Configure Providers** → **Char (Recommended)**: point its LLM at
  `http://127.0.0.1:1234` with model `qwen3-30b-a3b-instruct-2507`.

After that, every call you record in Char streams through Parakeet for
transcription and Qwen for the note. No further wiring needed.

## Daily usage

### Live calls (Char-driven)

Just record in Char as usual. `./run.sh start` once after a reboot is enough.

```bash
./run.sh status      # both lights green = ready
./run.sh logs        # tail Char's incoming POST /v1/listen requests
./run.sh stop        # shut down ASR (LM Studio left running)
```

### Files Char didn't auto-pick up

```bash
./run.sh transcribe ~/Desktop/old_call.m4a
```

Default behavior:

- Transcribes with Parakeet (ASR_BACKEND=parakeet).
- Caches the transcript by audio sha256 — second run is instant.
- Diarizes with sherpa-onnx and asks Qwen to map speakers to real names.
- Streams the structured Markdown summary to the terminal token-by-token.
- Copies the summary to your clipboard (use `--no-copy` to skip).

Useful flags (full list: `./run.sh transcribe --help`):

```bash
./run.sh transcribe FILE --save call.md       # markdown summary -> file
./run.sh transcribe FILE --save call.json     # full bundle (transcript + diarization + summary)
./run.sh transcribe FILE --save call.txt      # raw transcript only
./run.sh transcribe FILE --save-transcript diarized.txt   # diarized transcript only
./run.sh transcribe FILE --no-diarize         # ASR only, no speaker labels
./run.sh transcribe FILE --no-cache           # force re-transcribe
./run.sh transcribe FILE --asr-backend whisper   # use multilingual whisper instead
./run.sh transcribe FILE --call-time "2026-05-08T14:30"   # override timestamp in summary
./run.sh transcribe --list-cache              # table of cached transcripts
./run.sh transcribe --clear-cache             # wipe transcript cache
```

## Health & diagnostics

```bash
./run.sh doctor      # full preflight: deps, models, services, char-config hints
./run.sh status      # quick PIDs + ports + which model
./run.sh health      # one-shot HTTP probe of both services
./run.sh setup       # force reinstall pip deps + redownload models
```

`./run.sh doctor` is the first thing to run if anything misbehaves. It
distinguishes between "missing dep" / "missing model" / "service down" so
you don't have to guess.

## Configuration

All knobs are env vars; defaults are sensible.

| variable | default | what |
|---|---|---|
| `ASR_BACKEND` | `parakeet` | `parakeet` (English, MLX) or `whisper` (multilingual) |
| `PARAKEET_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | HF repo for parakeet weights |
| `WHISPER_MODEL` | `large-v3-turbo` | faster-whisper model id (only used when `ASR_BACKEND=whisper`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` / `int16` / `float32` for the whisper backend on CPU |
| `WHISPER_DEVICE` | `auto` | `cpu` / `cuda` / `auto` for the whisper backend |
| `WHISPER_LANGUAGE` | unset | ISO-639 code; force a specific language (whisper only) |
| `WHISPER_PORT` | `8000` | what port the ASR server listens on |
| `LMSTUDIO_PORT` | `1234` | LM Studio HTTP API port |
| `LLM_MODEL` | `qwen3-30b-a3b-instruct-2507` | the model `lms load` will bring up |
| `LLM_CONTEXT` | `65536` | context length to load Qwen with |
| `LLM_URL` | `http://127.0.0.1:1234/v1/chat/completions` | full chat endpoint URL |
| `LLM_MAX_TOKENS` | `4096` | upper bound for summary completion |
| `DIARIZE` | `1` | set `0` to disable diarization by default |
| `NUM_SPEAKERS` | unset (auto) | hint sherpa-onnx with the exact speaker count if you know it |
| `CLUSTER_THRESHOLD` | `0.5` | sherpa-onnx fast-clustering threshold |
| `WHISPER_CACHE_DIR` | `~/.cache/whisper_server/transcripts` | where the transcript cache lives |
| `DIARIZATION_CACHE_DIR` | `~/.cache/whisper_server/diarization` | where sherpa-onnx model files live |
| `PYTHON` | `python3.14` else `python3.12` else `python3` | which interpreter `run.sh` uses to build the venv |

Switch to whisper for, say, a Mandarin call:

```bash
ASR_BACKEND=whisper WHISPER_LANGUAGE=zh ./run.sh restart
```

## Project layout

```
whisper_server/
├── whisper_server.py        # FastAPI server (Deepgram-compatible)
├── transcribe_file.py       # CLI for manual files
├── parakeet_backend.py      # parakeet-mlx wrapper, BPE -> Deepgram words
├── diarization_backend.py   # sherpa-onnx + LLM speaker naming
├── run.sh                   # service manager + preflight
├── requirements.txt
├── tests/                   # 84 unit tests, all hermetic
└── .run/                    # PID files, log file, deps stamp (gitignored)
```

Caches (gitignored, safe to delete to free disk):

```
~/.cache/huggingface/hub/                          # parakeet, faster-whisper
~/.cache/whisper_server/diarization/               # sherpa-onnx ONNX models
~/.cache/whisper_server/transcripts/<sha256>.json  # cached ASR results
```

## API surface (for non-Char clients)

The server is a strict subset of Deepgram, so any Deepgram SDK pointed at
`http://127.0.0.1:8000` will work.

### `POST /v1/listen`

```bash
curl -X POST http://127.0.0.1:8000/v1/listen \
  -H "Content-Type: application/octet-stream" \
  --data-binary @call.m4a
```

Returns a Deepgram-shaped JSON document with `metadata`, `results.channels[0].alternatives[0].{transcript,confidence,words}`,
and `detected_language`.

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

### `GET /health`

```json
{
  "ok": true,
  "asr_backend": "parakeet",
  "model": "mlx-community/parakeet-tdt-0.6b-v3",
  "arch": "Parakeet-TDT",
  "compute_type": "mlx-bfloat16",
  "device": "mlx",
  "language": "en"
}
```

## Development

```bash
./run.sh setup                                  # one-shot reinstall + redownload
venv/bin/python -m unittest discover -s tests   # run the test suite (~0.05s, no model loads)
```

The tests are fully hermetic — they mock all HTTP/MLX/sherpa-onnx so they
run in milliseconds without any models present.

## Troubleshooting

**`./run.sh doctor` says my parakeet model isn't downloaded.**
Run `./run.sh setup` (or just `./run.sh start` — preflight will fetch it).

**LM Studio API isn't running on :1234.**
Open LM Studio.app once and turn on Developer → Local Server. After that
`./run.sh start` can keep it up via the `lms` CLI. If you don't have the
CLI: `~/.lmstudio/bin/lms bootstrap`.

**Char shows `unauthorized`.**
Char insists on a non-empty API key. Anything works — `local`, `dummy`,
`x` — auth is ignored locally.

**LLM completes immediately with 0 tokens.**
LM Studio silently rejects prompts that exceed the loaded context length.
The pipeline ships with `LLM_CONTEXT=65536`. If you set it lower, large
calls will fail this way. Re-run `./run.sh restart` to reload Qwen with the
default context.

**`There is no Stream(gpu, 0) in current thread.`**
This is an MLX threading issue that shouldn't surface anymore — all parakeet
work is pinned to a dedicated worker thread that initializes its own stream
and loads the model on that thread. If it does, file an issue with the
`./run.sh logs` output.

**Want to free GPU memory.**
`./run.sh stop` shuts down ASR but leaves LM Studio running so the next
restart is fast. To unload Qwen too: `lms unload qwen3-30b-a3b-instruct-2507`
or `lms server stop`.

## License

MIT for the glue code in this repo. The underlying models have their own
licenses — Parakeet TDT v3 is CC-BY-4.0 (NVIDIA), Whisper is MIT (OpenAI),
sherpa-onnx ONNX models are Apache 2.0 / MIT.
