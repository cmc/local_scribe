# local_scribe

Local, private, Apple-Silicon-native transcription + summarization pipeline.
Drops in as a Deepgram-compatible endpoint behind [Char](https://char.so) and
uses your local LM Studio + Qwen3 for note generation. Everything runs offline
once the models are downloaded.

```
                 ┌─────────────────────┐
   live mic ─►   │       Char.app      │   call recording UI + auto-note
                 └──────────┬──────────┘
                            │ POST /v1/listen   (Deepgram contract)
                            ▼
       ┌──────────────────────────────────┐
       │  asr_server.py   :8000           │
       │  • Parakeet-TDT 0.6B v3 (MLX)    │  ← default; English; lowest WER
       │  • faster-whisper large-v3-turbo │  ← optional; multilingual fallback
       └──────────────┬───────────────────┘
                      │ Deepgram JSON
                      ▼
                 Char ──► LM Studio :1234 ──► Qwen3-30B   summary in note UI

       ┌──────────────────────────────────┐
       │  transcribe_file.py              │  manual one-shot CLI
       │  • cache by audio sha256         │   for files Char didn't auto-pick up
       │  • optional speaker diarization  │   (sherpa-onnx + LLM speaker naming)
       │  • streaming LLM summary         │
       └──────────────────────────────────┘
```

## What's in here

| | what | role |
|---|---|---|
| `asr_server.py` | FastAPI service on `:8000`. Implements the bits of Deepgram's `/v1/listen` contract Char uses (POST batch + WebSocket streaming) and routes through Parakeet (default) or faster-whisper. | Char's transcription endpoint |
| `parakeet_backend.py` | parakeet-mlx wrapper. Merges sub-word BPE tokens into clean words, shapes output to Deepgram's word/timing schema. | Default ASR engine |
| `diarization_backend.py` | sherpa-onnx (pyannote 3.0 segmentation + NeMo TitaNet embedding) + an LLM pass to map `SPEAKER_00/01/...` to real names. | Speaker labeling |
| `transcribe_file.py` | CLI for files Char didn't auto-pick up. Streams a structured Markdown summary (TL;DR, Participants, Key points, Decisions, Open questions, Risks, Next steps, Notable quotes), with optional diarization. Caches results by audio sha256. | Manual workflow |
| `run.sh` | Service manager + bootstrap. Single command to install deps, download models, start/stop everything, and produce health reports. | Operator tool |

## Prerequisites — install these manually once

| | what | how | why |
|---|---|---|---|
| 1 | macOS on Apple Silicon | — | Parakeet runs through MLX |
| 2 | Python 3.12 or 3.14 | `brew install python@3.14` | runs the server + CLI |
| 3 | [Char.app](https://char.so) | App Store / DMG | call recording UI |
| 4 | [LM Studio.app](https://lmstudio.ai) | website | local LLM host |
| 5 | LM Studio model: **`qwen3-30b-a3b-instruct-2507`** | LM Studio's model browser (≈18 GB MLX) | summaries + speaker naming |
| 6 | LM Studio CLI: `lms` | `~/.lmstudio/bin/lms bootstrap` | so `run.sh` can auto-load Qwen |

Everything else (Parakeet ≈1.2 GB, sherpa-onnx ONNX bundles ≈45 MB,
faster-whisper ≈1.6 GB if you opt into it) is fetched automatically.

## Quick start

On a freshly cloned repo:

```bash
git clone <this repo>
cd local_scribe
./run.sh bootstrap        # venv + pip deps + ASR + diarization models
# (do the manual installs above if you haven't already)
./run.sh start            # boot ASR server + LM Studio + tail the log
```

`bootstrap` is idempotent — re-runs are a no-op when everything is cached.

`start` runs preflight first (so even if you skipped `bootstrap` it Just Works),
then brings up the services and tails the ASR log. `Ctrl+C` detaches without
stopping anything.

### What `start` will print

You'll see one of three banners:

```
──── pipeline ready ────                                # everything wired
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (Char's transcription endpoint)
  LM Studio API (Qwen3-30B)    : http://127.0.0.1:1234  (summary + speaker naming)
```

```
──── pipeline PARTIALLY ready ────                      # LM Studio not running
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (transcription works)
  LM Studio API                : NOT REACHABLE on :1234
                                 → Char's summary step will fail until you start LM Studio
```

```
──── pipeline PARTIALLY ready ────                      # Qwen not loaded
  ASR server (Parakeet TDT v3) : http://127.0.0.1:8000  (transcription works)
  LM Studio API                : http://127.0.0.1:1234  (reachable)
  qwen3-30b-a3b-instruct-2507  : NOT LOADED
                                 → Char's summary step will fail; load the model in LM Studio.app
```

In the partial cases the message tells you exactly what to fix. Re-run
`./run.sh start` once you've done it.

## Configure Char

Once `./run.sh start` is green, in Char → Settings → Transcription:

| field | value |
|---|---|
| **Model being used** | Custom (the `nova-2` string is decorative — this server ignores it) |
| **Custom provider Base URL** | `http://127.0.0.1:8000` |
| **API Key** | any non-empty string (auth is ignored locally) |
| **Configure Providers** → Char Recommended → LLM | LM Studio @ `http://127.0.0.1:1234`, model `qwen3-30b-a3b-instruct-2507` |

After that, every call you record streams through Parakeet for transcription
and Qwen for the note. Char's WebSocket streaming path also works out of the
box (it's "batch over WebSocket" — final transcript only, no interim
partials, since neither Parakeet nor faster-whisper streams natively).

## Daily usage

### Live calls (Char-driven)

Just record in Char as usual. After a reboot, one `./run.sh start` is enough.

```bash
./run.sh status      # PIDs, ports, which model
./run.sh logs        # tail Char's incoming POST /v1/listen requests
./run.sh stop        # shut down ASR (LM Studio left running)
./run.sh restart     # stop + start
```

### Files Char didn't auto-pick up

```bash
./run.sh transcribe ~/Desktop/old_call.m4a
```

Default behavior:

- Transcribes with Parakeet (`ASR_BACKEND=parakeet`).
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
./run.sh transcribe FILE --no-diarize         # skip speaker labels
./run.sh transcribe FILE --no-cache           # force re-transcribe
./run.sh transcribe FILE --asr-backend whisper      # switch to multilingual whisper
./run.sh transcribe FILE --call-time "2026-05-08T14:30"   # override timestamp
./run.sh transcribe --list-cache              # table of cached transcripts
./run.sh transcribe --clear-cache             # wipe transcript cache
```

## Health & diagnostics

```bash
./run.sh doctor      # full report: python, deps, models, services, Char-config hints (read-only)
./run.sh status      # quick PIDs + ports + which model
./run.sh health      # one-shot HTTP probe of both services (exit non-zero if down)
./run.sh setup       # force reinstall pip deps + redownload models
```

`./run.sh doctor` is the first thing to run if anything misbehaves. It's
read-only and produces a report like:

```
doctor — validating local pipeline

python:
  ● venv at /…/local_scribe/venv (Python 3.14.3)

python packages:
  ● fastapi            0.136.1
  ● uvicorn            0.46.0
  ● parakeet_mlx       ok
  ● faster_whisper     1.2.1
  ● sherpa_onnx        1.13.1
  …

models:
  ● parakeet (parakeet default)   cached at ~/.cache/huggingface/hub/…
  ● pyannote segmentation         ~/.cache/local_scribe/diarization/…/model.onnx
  ● NeMo TitaNet embedding        ~/.cache/local_scribe/diarization/nemo_en_titanet_small.onnx

services:
  ● ASR server   :8000   reachable
  ● LM Studio    :1234   reachable
  ● qwen3-30b-a3b-instruct-2507 loaded

char config (set these in Char's Settings -> Transcription):
  base URL : http://127.0.0.1:8000
  api key  : (any non-empty string - auth is ignored locally)
  intelligence provider : LM Studio @ http://127.0.0.1:1234   model=qwen3-30b-a3b-instruct-2507
```

Yellow/red dots tell you exactly which piece is broken so you don't have to
guess.

## Configuration

All knobs are env vars; defaults are sensible.

| variable | default | what |
|---|---|---|
| `ASR_BACKEND` | `parakeet` | `parakeet` (English, MLX, lowest WER) or `whisper` (multilingual) |
| `ASR_PORT` | `8000` | what port the ASR server listens on |
| `PARAKEET_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | HuggingFace repo for Parakeet weights |
| `WHISPER_MODEL` | `large-v3-turbo` | faster-whisper model id (only used when `ASR_BACKEND=whisper`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` / `int16` / `float32` for the whisper backend |
| `WHISPER_DEVICE` | `auto` | `cpu` / `cuda` / `auto` for the whisper backend |
| `WHISPER_LANGUAGE` | unset | ISO-639 code; force a language (whisper only) |
| `LMSTUDIO_PORT` | `1234` | LM Studio HTTP API port |
| `LLM_MODEL` | `qwen3-30b-a3b-instruct-2507` | the model `lms load` will bring up |
| `LLM_CONTEXT` | `65536` | context length to load Qwen with |
| `LLM_URL` | `http://127.0.0.1:1234/v1/chat/completions` | full chat endpoint URL |
| `LLM_MAX_TOKENS` | `4096` | upper bound for summary completion |
| `ASR_URL` | `http://127.0.0.1:8000/v1/listen` | URL `transcribe_file.py` posts to when `--asr-backend whisper` |
| `DIARIZE` | `1` | set `0` to disable diarization by default |
| `NUM_SPEAKERS` | unset (auto) | hint sherpa-onnx with the exact speaker count if known |
| `CLUSTER_THRESHOLD` | `0.5` | sherpa-onnx fast-clustering threshold |
| `TRANSCRIPT_CACHE_DIR` | `~/.cache/local_scribe/transcripts` | where the transcript cache lives |
| `DIARIZATION_CACHE_DIR` | `~/.cache/local_scribe/diarization` | where sherpa-onnx model files live |
| `PYTHON` | `python3.14` else `python3.12` else `python3` | which interpreter `run.sh` uses to build the venv |

Switch to whisper for, say, a Mandarin call:

```bash
ASR_BACKEND=whisper WHISPER_LANGUAGE=zh ./run.sh restart
```

## Project layout

```
local_scribe/
├── asr_server.py            # FastAPI server (Deepgram-compatible)
├── transcribe_file.py       # CLI for manual files
├── parakeet_backend.py      # parakeet-mlx wrapper, BPE -> Deepgram words
├── diarization_backend.py   # sherpa-onnx + LLM speaker naming
├── run.sh                   # service manager, bootstrap, doctor
├── requirements.txt
├── tests/                   # 84 unit tests, fully hermetic (mock all I/O)
└── .run/                    # PID files, log file, deps stamp (gitignored)
```

Caches (gitignored, safe to delete to free disk):

```
~/.cache/huggingface/hub/                                # Parakeet, faster-whisper
~/.cache/local_scribe/diarization/                  # sherpa-onnx ONNX models
~/.cache/local_scribe/transcripts/<sha256>.json     # cached ASR results
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
venv/bin/python -m unittest discover -s tests   # 84 tests, ~0.05s, no model loads
```

The tests are fully hermetic — they mock all HTTP/MLX/sherpa-onnx so they run
in milliseconds without any models present.

## Troubleshooting

**`./run.sh start` shows "PARTIALLY ready" with `LM Studio NOT REACHABLE`.**
Open LM Studio.app and turn on Developer → Local Server. After that, install
the `lms` CLI with `~/.lmstudio/bin/lms bootstrap` so future `./run.sh start`
calls can keep it up automatically.

**`./run.sh start` shows "PARTIALLY ready" with `<model> NOT LOADED`.**
Open LM Studio.app → Discover → search for `qwen3-30b-a3b-instruct-2507`
and download it. Then `./run.sh restart`.

**Char shows `unauthorized`.**
Char insists on a non-empty API key. Anything works — `local`, `dummy`, `x` —
auth is ignored locally.

**LLM completes immediately with 0 tokens.**
LM Studio silently rejects prompts that exceed the loaded context length. The
pipeline ships with `LLM_CONTEXT=65536`. If you set it lower, large calls will
fail this way. `./run.sh restart` will reload Qwen with the configured
context.

**`./run.sh doctor` says my parakeet model isn't downloaded.**
Run `./run.sh setup` (or just `./run.sh start` — preflight will fetch it).

**`There is no Stream(gpu, 0) in current thread.`**
This MLX threading issue shouldn't surface — all Parakeet work is pinned to a
dedicated worker thread that initializes its own stream and loads the model
on that thread. If it does happen, file an issue with `./run.sh logs`.

**Want to free GPU memory.**
`./run.sh stop` shuts down ASR but leaves LM Studio running so the next
restart is fast. To unload Qwen too: `lms unload qwen3-30b-a3b-instruct-2507`
or `lms server stop`.

**Want to start fresh.**
`./run.sh stop && ./run.sh setup` rebuilds the venv from scratch and
re-downloads the ASR weights. To wipe the transcript cache too:
`./run.sh transcribe --clear-cache`.

## License

MIT for the glue code in this repo. The underlying models have their own
licenses — Parakeet TDT v3 is CC-BY-4.0 (NVIDIA), Whisper is MIT (OpenAI),
sherpa-onnx ONNX models are Apache 2.0 / MIT, Qwen3 is Apache 2.0.
