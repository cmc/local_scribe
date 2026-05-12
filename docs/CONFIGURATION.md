# Configuration

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/INSPECTOR.md`](INSPECTOR.md)

There are now **two** layered ways to configure the stack:

1.  **`~/.config/local_scribe/config.json`** — the user-editable JSON
    file that the [Inspector UI](INSPECTOR.md) reads/writes.
    `./run.sh bootstrap` step (4/6) seeds this from baked-in defaults,
    and every save through the inspector backs up the previous file as
    `config.json.bak.<ts>`. Ground truth for which port the ASR server
    listens on, which Parakeet/Whisper model to load, the LM Studio
    host/port (handy if you run LM Studio on a different Mac), the
    inspector's bind/port, and Char's expected provider config.
2.  **Environment variables** (table below) — layered on top of
    config.json (env wins). Lets you override one knob for a single
    `./run.sh start` without editing the JSON. Existing scripts that
    set `ASR_PORT=...` keep working unchanged.

The validator at `config.validate()` rejects negative ports, unknown
backends, port collisions between the ASR + Inspector, and binding the
inspector to a non-loopback address without an `auth_token` set.

All knobs are also env vars; defaults are sensible.

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
| `LLM_MODEL` | `qwen3-30b-a3b-instruct-2507` | model id `lms load` brings up (also used as the `model` field in chat completions) |
| `LLM_MODEL_REPO` | `qwen/qwen3-30b-a3b-instruct-2507` | repo path passed to `lms get` during bootstrap |
| `LLM_MIN_RAM_GB` | `48` | bootstrap auto-falls back to the smaller `qwen/qwen3-4b` (≈2.3 GB) below this unified-memory threshold |
| `LLM_CONTEXT` | `65536` | context length to load Qwen with |
| `LLM_URL` | `http://127.0.0.1:1234/v1/chat/completions` | full chat endpoint URL |
| `LLM_MAX_TOKENS` | `4096` | upper bound for summary completion |
| `ASR_URL` | `http://127.0.0.1:8000/v1/listen` | URL `transcribe_file.py` posts to when `--asr-backend whisper` |
| `DIARIZE` | `1` | set `0` to disable diarization by default in `transcribe_file.py` |
| `OPENAI_BATCH_DIARIZE` | `1` | set `0` to skip diarization in `POST /v1/audio/transcriptions` (Char's Generate flow) |
| `NUM_SPEAKERS` | unset (auto) | hint sherpa-onnx with the exact speaker count if known |
| `CLUSTER_THRESHOLD` | `0.5` short / `0.7` long | sherpa-onnx fast-clustering threshold. Auto-bumps to `0.7` for audio ≥ 10 min (long meetings have few speakers; tighter thresholds over-shard). Set this env var to lock a value. |
| `MAX_DIARIZE_SECONDS` | `14400` | audio longer than this (4 h default) auto-skips diarization on `POST /v1/audio/transcriptions` (returns ASR transcript with single `speaker_0` placeholder). Sherpa-onnx clustering is O(N²); the cap exists so an accidentally-long file can't lock up the server. Set `0` to disable the cap. |
| `MAX_SPEAKERS` | `12` | if sherpa-onnx returns more than this many distinct speakers, treat it as a clustering blow-up and collapse to single-speaker output rather than emit JSON Char can't render. Set `0` to disable the guard. |
| `STREAM_HEARTBEAT_SECONDS` | `20` | heartbeat interval (in seconds) for the SSE streaming branch of `POST /v1/audio/transcriptions`. Each heartbeat resets Char's hardcoded 60-second `BATCH_IDLE_TIMEOUT`; lower it on slower machines, raise it for less wire chatter. Must stay strictly less than 60. |
| `INSPECTOR_BIND` | `127.0.0.1` | bind address for the Inspector web UI; loopback by default. Refuse to start non-loopback unless `inspector.auth_token` is also set in `config.json`. |
| `INSPECTOR_PORT` | `8001` | port for the Inspector web UI. |
| `INSPECTOR_AUTH_TOKEN` | unset | optional bearer token. When set, every `/api/*` request must include `Authorization: Bearer <token>`. Required if you ever rebind the inspector to a LAN address. |
| `LOCAL_SCRIBE_CONFIG_DIR` | `~/.config/local_scribe` | where `config.json` (and Char OpenAI-key backups) live. |
| `TRANSCRIPT_CACHE_DIR` | `~/.cache/local_scribe/transcripts` | where the transcript cache lives |
| `DIARIZATION_CACHE_DIR` | `~/.cache/local_scribe/diarization` | where sherpa-onnx model files live |
| `PYTHON` | `python3.14` else `python3.12` else `python3` | which interpreter `run.sh` uses to build the venv |

Switch to whisper for, say, a Mandarin call:

```bash
ASR_BACKEND=whisper WHISPER_LANGUAGE=zh ./run.sh restart
```

