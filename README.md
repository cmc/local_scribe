# local_scribe

Local, private, Apple-Silicon-native transcription + summarization pipeline.
Drops in as a Deepgram-compatible endpoint behind [Char](https://char.com) and
uses your local LM Studio + Qwen3 for note generation. Everything runs offline
once the models are downloaded.

> **Why Char?** Char is the **open-source**, local-first AI meeting notetaker —
> source at [github.com/fastrepl/anarlog](https://github.com/fastrepl/anarlog)
> (MIT licensed, ~8.4k stars), product page at [char.com](https://char.com).
> Unlike closed-source SaaS notetakers like Granola, the entire client is code
> you can read, fork, and self-host, and your audio/notes stay on disk as plain
> markdown. `local_scribe` is the on-device transcription + summarization
> backend that pairs with it — so the whole stack, app *and* models, is yours.

```
                 ┌──────────────────────────────────────────────────────┐
   live mic ─►   │                       Char.app                       │
   audio file ─► │  (call UI, file imports, note canvas, summary view)  │
                 └────┬───────────────────────────┬─────────────────────┘
                      │ live recording            │ click "Generate"
                      │ POST /v1/listen           │ POST /v1/audio/transcriptions
                      │ (Deepgram contract,       │ (OpenAI Whisper API contract,
                      │  Custom provider)         │  OpenAI Batch Only provider)
                      ▼                           ▼
       ┌────────────────────────────────────────────────────────────┐
       │  asr_server.py   :8000                                     │
       │    Parakeet-TDT 0.6B v3 (MLX)   default; English; lowest WER
       │    faster-whisper large-v3-turbo  optional multilingual    │
       └─────────────────────────────────┬──────────────────────────┘
                                         │  transcript JSON
                                         ▼
                                 Char ──► LM Studio :1234 ──► Qwen3-30B
                                          (summary in note UI)

       ┌──────────────────────────────────┐
       │  transcribe_file.py              │  manual one-shot CLI
       │  • cache by audio sha256         │   for files Char didn't auto-pick up
       │  • real speaker diarization      │   (sherpa-onnx + LLM speaker naming)
       │  • streaming LLM summary         │
       └──────────────────────────────────┘
```

## Privacy and data locality

The whole reason this stack exists: every recording, transcript, and
summary lives only on your laptop's disk, processed by models that run
locally on Apple Silicon. There is no "send-to-cloud" toggle hiding
somewhere that could flip on. Once `bootstrap` finishes pulling code
and models, you can disable Wi-Fi and the pipeline keeps working.

### What stays local

| asset | path | written by |
|---|---|---|
| Audio recording | `~/Library/Application Support/hyprnote/sessions/<uuid>/audio.mp3` | Char.app |
| Transcript JSON (words, speaker hints) | `~/Library/Application Support/hyprnote/sessions/<uuid>/transcript.json` | Char.app, populated from our `/v1/audio/transcriptions` response |
| Generated note / summary (markdown) | `~/Library/Application Support/hyprnote/sessions/<uuid>/<TemplateName>.md` | Char.app, populated from the local LM Studio response |
| Char's session catalog | `~/Library/Application Support/hyprnote/app.db` (SQLite) | Char.app |
| Char settings (auto-config patches go here) | `~/Library/Application Support/hyprnote/settings.json` (+ `.bak.<ts>`) | Char.app + `./run.sh configure-char` |
| Local-scribe transcript cache (sha256→result) | `~/.cache/local_scribe/transcripts/` | `transcribe_file.py` |
| Diarization ONNX models | `~/.cache/local_scribe/diarization/` | `./run.sh bootstrap` |
| Parakeet ASR weights (MLX) | `~/.cache/huggingface/hub/models--mlx-community--parakeet-tdt-0.6b-v3/` | `./run.sh bootstrap` (HuggingFace `snapshot_download`) |
| Qwen LLM weights (MLX) | `~/.cache/lm-studio/models/` | `lms get` (LM Studio.app) |
| Backed-up real OpenAI keys (if you had one in Char) | `~/.config/local_scribe/char-openai-key.<ts>.txt` (chmod 600) | `./run.sh configure-char` |

`local_scribe` never uploads any of this. The repo's
[`.gitignore`](.gitignore) explicitly excludes every audio extension we
know about (`.mp3`, `.m4a`, `.wav`, `.ogg`, `.flac`, `.aac`, `.opus`,
`.aiff`, `.webm`, `.mp4`, `.mov`, `.mkv`) and every transcript /
summary / diarized output (`*.transcript.{json,txt,md}`,
`*.summary.{md,txt}`, `*.diarized.{txt,md}`, `out/`, `outputs/`), so
accidentally `git add`'ing audio or notes from this repo just doesn't
work.

### What crosses the network — and when

Two clearly separated lifecycles. **At install / bootstrap time**, code
and models are downloaded one-shot:

| URL | what's fetched | when |
|---|---|---|
| `pypi.org` (+ wheel mirrors) | Python deps from `requirements.txt` | step (1/5) of `./run.sh bootstrap` |
| `huggingface.co` | Parakeet 0.6B v3 MLX weights (≈1.2 GB) | step (2/5) |
| `github.com/k2-fsa/sherpa-onnx/releases/...` | sherpa-onnx pyannote 3.0 + TitaNet ONNX bundles (≈45 MB) | step (3/5) |
| `formulae.brew.sh` + Homebrew artifact mirrors | LM Studio.app cask | step (4/5), only if `/Applications/LM Studio.app` is missing |
| LM Studio's model hub (HF mirror) | Qwen3 MLX weights (≈32 GB or ≈2.3 GB depending on RAM) | step (4/5), only if missing locally and you confirm the y/N prompt |
| `github.com/fastrepl/anarlog/releases/...` | pinned Char.app DMG | step (5/5), only if Char isn't installed (or you confirm replace) |

After bootstrap is done these URLs are never hit again unless you re-run
bootstrap, `setup`, `install-char`, or `install-llm`.

**At runtime** (every recording, every Generate click, every summary),
the entire data plane is loopback:

| URL | role | what's transmitted |
|---|---|---|
| `http://127.0.0.1:8000/...` | our ASR server (Char's transcription endpoint) | audio in, transcript out |
| `http://127.0.0.1:1234/v1/chat/completions` | LM Studio (running on this Mac) | transcript in, summary out |

That's it. No `api.openai.com`, no `api.deepgram.com`, no
`*.amazonaws.com`, no `*.googleusercontent.com`. You can verify any
time mid-call:

```bash
lsof -nP -i -P | grep -E 'asr_server|LM Studio|Char'
```

…and confirm only `127.0.0.1` connections appear. See
[§ How the integration works](#how-the-integration-works-aka-the-hack)
for why Char ends up on `127.0.0.1` despite thinking it's calling
OpenAI's Whisper API and Deepgram.

### What you still have to trust

Being honest about the parts of the stack that aren't ours:

- **LM Studio.app is closed-source.** It collects basic usage analytics
  by default (app-level metadata; you can opt out under Settings →
  Telemetry). The Qwen3 model itself runs entirely locally; LM Studio
  doesn't transmit your chat content. Disabling LM Studio's telemetry
  by default at bootstrap is on the [TODO](TODO.md).
- **Char.app is open source** ([source](https://github.com/fastrepl/anarlog),
  MIT-licensed). The full audit lives in [CHAR_REVIEW.md](CHAR_REVIEW.md);
  the short version: the data plane (audio / transcripts / notes) stays
  local, but Char ships with **Sentry crash reporting and PostHog product
  analytics enabled by default**, plus a Tauri auto-updater that polls
  `desktop2.hyprnote.com`. `./run.sh configure-char` writes the in-app
  PostHog kill-switch (`store.json::analytics.Disabled = true`); Sentry
  and the auto-updater have **no in-app toggle** and need to be blocked
  at the firewall or via `/etc/hosts` overrides — see
  [§ Mitigations](CHAR_REVIEW.md#mitigations) for the exact rule set.
  Calendar / event sync (if you connect a calendar) does talk to your
  calendar provider — orthogonal to recordings, but worth knowing.
- **`asr_server.py` currently binds to `0.0.0.0:8000`**, not
  `127.0.0.1:8000`. macOS's firewall blocks incoming connections by
  default, but if you've allowed Python through the firewall and you're
  on a public Wi-Fi, in principle a peer on the same network could hit
  the endpoint. Tightening the default bind to loopback (with an
  explicit `BIND_ALL=1` opt-in for "I want this reachable from another
  machine on my LAN") is a [TODO.md](TODO.md) item — meanwhile, run on
  trusted networks or behind a firewall.
- **macOS Spotlight indexes audio files** by default. To exclude Char's
  session directory:
  ```bash
  mdutil -i off "$HOME/Library/Application Support/hyprnote"
  ```
- **iCloud Drive sync** of `~/Library/Application Support/` is off by
  default but if you've enabled Optimize Mac Storage / iCloud Drive →
  Library, your recordings will sync. Check **System Settings → Apple
  ID → iCloud → iCloud Drive → "Library"**.
- **Time Machine snapshots** include the session directory by default.
  Your call recordings end up in your local backups too — usually what
  you want for safety, but it does mean they exist in more than one
  place on disk.

### Air-gap mode

Once `./run.sh bootstrap` reports success and `./run.sh doctor` is all
green, you can disable Wi-Fi + Bluetooth and the pipeline keeps working
indefinitely: live recording, batch Generate, summaries, all of it.
Bootstrap downloads are the only network dependency.

### Future privacy work

See [TODO.md](TODO.md) for planned hardening — encrypting audio +
transcripts at rest with a Keychain-backed key, age-based auto-purge,
a `./run.sh wipe` command, and tightening loopback-only binding.

## What's in here

| | what | role |
|---|---|---|
| `asr_server.py` | FastAPI service on `:8000`. Speaks **two** transcription contracts so both of Char's flows work: Deepgram (`/v1/listen` POST + WebSocket) for live recording, and OpenAI Whisper (`/v1/audio/transcriptions`) for "Generate" on existing audio. Routes both through Parakeet (default) or faster-whisper. | Char's transcription endpoint |
| `parakeet_backend.py` | parakeet-mlx wrapper. Merges sub-word BPE tokens into clean words, shapes output to Deepgram's word/timing schema. | Default ASR engine |
| `diarization_backend.py` | sherpa-onnx (pyannote 3.0 segmentation + NeMo TitaNet embedding) + an LLM pass to map `SPEAKER_00/01/...` to real names. | Speaker labeling |
| `transcribe_file.py` | CLI for files Char didn't auto-pick up. Streams a structured Markdown summary (TL;DR, Participants, Key points, Decisions, Open questions, Risks, Next steps, Notable quotes), with optional diarization. Caches results by audio sha256. | Manual workflow |
| `run.sh` | Service manager + bootstrap. Single command to install deps, download models, start/stop everything, and produce health reports. | Operator tool |

## How the integration works (a.k.a. "the hack")

Char isn't aware of `local_scribe`. From its perspective it's still talking
to OpenAI's Whisper API and an arbitrary Deepgram-compatible "Custom"
endpoint — we just rewrite a handful of `settings.json` values and stand
up our own FastAPI server on `127.0.0.1:8000` that **speaks both contracts
byte-for-byte**, then route every request to Parakeet running locally on
Apple Silicon.

This is purely a config-level shim: no Char binary patching, no MITM
proxy, no DNS tricks, no LaunchAgent. Char ships with provider plugins
for OpenAI and Deepgram; both expose a `base_url` field that accepts any
HTTP origin. We point those at `127.0.0.1` and impersonate the OpenAI/
Deepgram response shapes. Audio never leaves the machine.

### What `configure-char` rewrites in `settings.json`

`~/Library/Application Support/hyprnote/settings.json` is a plain JSON
file Char reads at startup and writes back when you change anything in
the UI. `configure-char` patches **exactly four keys** and leaves
everything else alone (LLM provider, templates, calendars, shortcuts,
theme — all untouched):

| key | typical "before" | after | what it does |
|---|---|---|---|
| `ai.current_stt_provider` | `"openai"` (or `"deepgram"`, etc.) | `"openai"` | picks which provider plugin Char uses for batch (Generate) transcription |
| `ai.current_stt_model` | `"gpt-4o-transcribe-diarize"` (Char default) | `"gpt-4o-transcribe"` | model name string. Char never asks OpenAI what this is — it's a routing key inside Char's own client (see [§ The model-name shadow](#the-model-name-shadow) below). |
| `ai.stt.openai.base_url` | `""` (empty → defaults to `https://api.openai.com/v1`) | `"http://127.0.0.1:8000/v1"` | Char's reqwest client posts to **this URL** for every OpenAI Whisper call. The empty default means "real OpenAI"; we override it to point at the loopback. |
| `ai.stt.openai.api_key` | your real key, if any | `"local"` | non-empty placeholder so Char's auth check passes. We accept any value (or none) on our side and discard the `Authorization: Bearer …` header. |

Before patching, `configure-char` writes a timestamped backup to
`settings.json.bak.<ts>` for trivial rollback. **If the existing
`api_key` looks like a real OpenAI key** (starts with `sk-` and is more
than ~30 chars) you're prompted to save it to
`~/.config/local_scribe/char-openai-key.<ts>.txt` (chmod 600) first; you
should still rotate that key on platform.openai.com because it sat
unencrypted in `settings.json` until now.

The Deepgram "Custom" provider used for live recording isn't touched by
`configure-char` — wire it manually in Char's UI per the
[manual configuration table](#1-live-recording-custom-provider) below.
This is just because Char's settings schema for the Custom provider
isn't as cleanly addressable from outside the app yet.

### The two endpoints we serve

`asr_server.py` is a single FastAPI process on `:8000` that exposes
**two completely different API contracts** so both of Char's flows
work without Char knowing the difference:

| Char flow | Char calls | We expose | Backend |
|---|---|---|---|
| Live recording (mic meeting) | `POST /v1/listen` (raw audio body or multipart) and `WS /v1/listen` (linear16 PCM frames) — Deepgram's contract | `:8000` (Deepgram-compatible) | Parakeet 0.6B v3 (MLX) or faster-whisper |
| "Generate" on existing audio | `POST /v1/audio/transcriptions` (multipart form, optionally `stream=true` SSE) — OpenAI Whisper API contract | `:8000/v1/audio/transcriptions` | Parakeet + sherpa-onnx diarization + inlined speaker prefixes for short files |
| Liveness probe | `GET /health` | `:8000/health` | reports backend, model, advertised endpoints |

Char *never* contacts api.deepgram.com or api.openai.com when this is
running. The Custom-provider Deepgram URL is `127.0.0.1:8000` and the
OpenAI `base_url` is `127.0.0.1:8000/v1`.

### The model-name shadow

`gpt-4o-transcribe` is a real OpenAI model name (visible on
platform.openai.com). We deliberately reuse it because Char's source
hardcodes a routing table that picks the client-side code path based on
the model string. From
[`crates/owhisper-client/src/adapter/openai/mod.rs`](https://github.com/fastrepl/anarlog/blob/main/crates/owhisper-client/src/adapter/openai/mod.rs):

```rust
pub fn supports_progressive_batch_model(model: Option<&str>) -> bool {
    matches!(
        Self::resolve_batch_model(model),
        AudioModel::Gpt4oTranscribe
            | AudioModel::Gpt4oMiniTranscribe
            | AudioModel::Gpt4oMiniTranscribe20251215
    )
}
```

Translation:

- `gpt-4o-transcribe-diarize` → `simple` **non-streaming** batch path (read full body, parse, persist)
- `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` → `progressive` **SSE-streamed** batch path (read deltas, emit progress events, persist on `transcript.text.done`)

We need the progressive path (see § the 60-second hack below), so we
ask Char to send `model=gpt-4o-transcribe`. Our server completely
ignores the `model` field for routing — every request runs through
`_run_asr_async()` on the local Parakeet/Whisper backend regardless. The
model name only shows up in our log lines for traceability.

### The 60-second hack (why we need streaming SSE at all)

Char's transcription plugin enforces a **hardcoded 60-second
client-side idle abort**. From
[`plugins/transcription/src/listener2/ext.rs`](https://github.com/fastrepl/anarlog/blob/main/plugins/transcription/src/listener2/ext.rs):

```rust
const BATCH_IDLE_TIMEOUT: Duration = Duration::from_secs(60);
…
if !mark_terminal_state(&control, BatchTerminalState::TimedOut) {
    return;
}
remove_batch_session(&registry, &session_id, &control);
let _ = TranscriptionEvent::Failed {
    session_id: session_id.clone(),
    code: core::BatchErrorCode::TimedOut,
    error: "Transcription timed out after 60 seconds without progress.".to_string(),
}.emit(&app);
abort_handle.abort();   // silently drops the spawned future
```

The non-streaming `simple` path (which `gpt-4o-transcribe-diarize`
takes) only fires its single `BatchResponse` event at the very end —
when the entire HTTP response has been read and deserialized. So **any
audio whose ASR takes longer than 60 seconds gets killed mid-flight**
before our response arrives. The server returns a perfectly valid 200
OK 26 seconds later, the bytes hit the wire, but Char's spawned tokio
task was already aborted. There's no log entry from Char, no error
toast in the UI, and no `transcript.json` written to disk — the future
is just dropped on the floor.

Our M3 Max runs Parakeet at ~80x realtime, so this trips on any meeting
longer than ~80 minutes. The user-reported "Maus meeting" (114 min
audio, 86 s ASR) was the first reproducible victim and the reason this
section exists.

**The workaround:** the progressive path resets `last_activity_tx`
(the idle-timer source) on every `transcript.text.delta` SSE event.
Our `/v1/audio/transcriptions` detects `stream=true` in the multipart
form and emits SSE that looks like this:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream

data: {"type":"transcript.text.delta","delta":" ","logprobs":[]}     # heartbeat @ t=20s
data: {"type":"transcript.text.delta","delta":" ","logprobs":[]}     # heartbeat @ t=40s
data: {"type":"transcript.text.delta","delta":" ","logprobs":[]}     # heartbeat @ t=60s
data: {"type":"transcript.text.delta","delta":" ","logprobs":[]}     # heartbeat @ t=80s
data: {"type":"transcript.text.done","text":"…full transcript…","logprobs":[],"usage":{"type":"duration","seconds":6848}}
data: [DONE]
```

The heartbeat is a single space — non-empty so Char's parser actually
emits a `Progress` event that ticks the timer (Char ignores zero-length
deltas, hence why this needs to be a literal space and not `""`). The
final `transcript.text.done` carries the real transcript and
**replaces** Char's accumulated `partial_text` buffer (the protocol
allows non-empty `text` on `done` to override any deltas), so the user
never sees the heartbeat spaces in the rendered transcript.

Heartbeat interval is `STREAM_HEARTBEAT_SECONDS` (default `20`, must
stay strictly < 60). That's the entire mechanism: a 32-byte JSON packet
emitted four times to keep an open-source app's idle timer happy long
enough for local ASR to finish.

### What you lose vs. real OpenAI Whisper

Compared to actually hitting `api.openai.com/v1/audio/transcriptions`,
the shim:

- Returns only the **progressive** response shape on the streaming
  path (text + usage). Per-word start/end timestamps are dropped from
  Char's stored `transcript.json`. If you need word-level timing for a
  specific recording, run `./run.sh transcribe FILE` outside Char — it
  preserves them.
- Inlines `Speaker N: …` prefixes into the streamed text on short
  files where diarization fires. The progressive shape Char accepts
  here can't carry the structured `segments[*].speaker` array. Long
  files auto-skip diarization (`MAX_DIARIZE_SECONDS=1800` by default).
- Ignores the OpenAI multipart fields `prompt`, `temperature`,
  `timestamp_granularities`, and `language`. Parakeet doesn't take
  sampling-temperature hints or per-token granularity, and language
  detection is automatic (English-only model).

Everything else — sub-second turnaround for short files, the Generate
button, the live-recording UI, summary generation via LM Studio — works
exactly as it would against the real OpenAI / Deepgram backends, except
no audio leaves your laptop.

## Hardware requirements

This repo was developed and end-to-end-tested against an **Apple M3 Max,
16-core (12P+4E), 128 GB unified memory, macOS 15.0** — the "comfortable"
tier in the table below. Everything in the pipeline is Apple Silicon
native (Parakeet via MLX, sherpa-onnx via CoreML, Qwen via LM Studio's
MLX runtime); there is no Intel or Linux build.

| tier | CPU / RAM | LLM | what works | trade-offs |
|---|---|---|---|---|
| **Comfortable** *(reference)* | M2 Pro / M3 / M4 family, **64 GB+ unified memory**, ≥40 GB free disk | `qwen3-30b-a3b-instruct-2507` (32 GB MLX) | live recording, batch Generate, full-quality summaries, all `transcribe_file.py` features | none |
| **Acceptable** | M1 / M2 / M3, **24–48 GB unified memory**, ≥10 GB free disk | `qwen/qwen3-4b` (2.3 GB MLX) — auto-selected by bootstrap on this tier | everything works; summaries are visibly less detailed and reasoning steps occasionally falter on long calls | swap pressure during 2h+ recordings |
| **Minimum** | M1, **16 GB unified memory** | (none — Parakeet only) | live transcription via Char, manual `./run.sh transcribe` | summary step in Char will fail (no LLM) — use the LM Studio chat UI manually instead, or skip summaries |
| **Won't run** | Intel Mac, Linux, Windows, M1 with <16 GB | n/a | n/a | Parakeet-MLX is Apple Silicon-only; CoreML diarization same |

Always-on disk usage at "comfortable" tier with everything pulled:

| component | size |
|---|---|
| Parakeet TDT 0.6B v3 (MLX) | 1.2 GB |
| sherpa-onnx diarization (pyannote 3.0 + TitaNet) | 45 MB |
| Qwen3-30B-A3B-Instruct-2507 (MLX) | 32 GB |
| LM Studio.app | 600 MB |
| Char.app | 350 MB |
| Python venv + pip deps | 2.5 GB |
| **Total** | **~37 GB** |

Add ≈1.6 GB if you also opt into faster-whisper (`ASR_BACKEND=whisper`)
as a fallback engine. Recordings + transcripts live in Char's directory
and grow with usage (a 1-hour 192 kbps mp3 is ≈85 MB; transcript JSON
≈2 MB; summary markdown <10 KB).

## Prerequisites — install these manually once

Most prerequisites are now installed automatically by `./run.sh
bootstrap` (see [§ Bootstrap automation](#bootstrap-automation) below).
The two things you still need to bring yourself:

| | what | how | why |
|---|---|---|---|
| 1 | macOS on Apple Silicon | — | Parakeet-MLX, MLX-Qwen, and CoreML diarization are all Apple-Silicon-only |
| 2 | Python 3.12 or 3.14 | `brew install python@3.14` | runs the server + CLI; `bootstrap` auto-builds the venv |
| 3 | [Homebrew](https://brew.sh) (recommended) | `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` | how `bootstrap` installs LM Studio.app + Char.app unattended |

Everything else — **LM Studio.app**, the **Qwen3 model**, the **`lms`
CLI**, **Char.app** at the pinned version, **Parakeet ASR weights**,
**sherpa-onnx diarization models**, and the **Python venv** — is
installed by `./run.sh bootstrap` with one or two y/N prompts (one per
multi-GB download you'd want to pre-approve). See the bootstrap section
below for what each step does.

## Quick start

On a freshly cloned repo:

```bash
git clone <this repo>
cd local_scribe
./run.sh bootstrap        # one-shot setup: venv + pip + ASR/diar models +
                          # LM Studio + Qwen LLM + Char.app + auto-config
./run.sh start            # boot ASR server, ensure LM Studio is up + model
                          # loaded, tail the log
```

## Bootstrap automation

`./run.sh bootstrap` is a single command that takes a clean machine
(macOS + Python + Homebrew) all the way to a working pipeline. It runs
**five idempotent steps** — already-done steps short-circuit with a
green checkmark, so re-running on a fully set-up machine prints the
state and exits without changing anything.

```text
(1/5) python venv + pip deps          ─── creates .venv/ if missing,
                                          installs requirements.txt
(2/5) parakeet ASR weights            ─── ~1.2 GB MLX bundle from
                                          mlx-community/parakeet-tdt-0.6b-v3
(3/5) sherpa-onnx diarization models  ─── ~45 MB ONNX (pyannote 3.0
                                          segmentation + TitaNet embedding)
(4/5) LM Studio.app + Qwen LLM        ─── see breakdown below
(5/5) Char.app — install + auto-config
```

### Step (4/5) — LM Studio.app + Qwen LLM, in detail

This is the step that handles your local LLM host end-to-end. It is
**fully unattended past two y/N prompts** (one for the brew cask
install, one for the multi-GB model download — you wouldn't want either
to start without confirmation).

1. **Install LM Studio.app** if `/Applications/LM Studio.app` is missing,
   via `brew install --cask lm-studio` (so it auto-updates and is signed).
   We pin `LMSTUDIO_KNOWN_GOOD_VERSION = 0.4.12` — installed versions
   that match get a "matches pinned" stamp; later versions get a soft
   "usually compatible" note (LM Studio's `lms` CLI surface is stable
   across patch releases). Build suffixes like `0.4.12+1` are normalised
   for the comparison.
2. **Bootstrap the `lms` CLI** by finding the binary inside the app
   bundle (`/Applications/LM Studio.app/Contents/Resources/.../lms`) and
   running `lms bootstrap`. This symlinks it into
   `~/.cache/lm-studio/bin/lms` so it's on your `PATH` for subsequent
   invocations and for `./run.sh start` to use. (Without this step, the
   `lms` symlink only gets created the first time you GUI-launch LM
   Studio.)
3. **Start the LM Studio HTTP server** on `:1234` (`lms server start
   --port 1234`). If it's already running, skipped.
4. **Pick the right model for your hardware.** Reads `sysctl -n
   hw.memsize`:
   - **≥48 GB unified memory** → recommends `qwen/qwen3-30b-a3b-instruct-2507`
     (32 GB MLX, ~36 GB loaded with the default 65 K context).
   - **<48 GB unified memory** → falls back to `qwen/qwen3-4b`
     (2.3 GB MLX, ~3 GB loaded). The threshold is configurable via
     `LLM_MIN_RAM_GB`; the model identifiers are `LLM_MODEL_REPO` and
     `LLM_MODEL_SMALL_REPO`.
5. **Download the chosen model** via `lms get <repo> --mlx -y` if it's
   not already in your local store. Skipped if `/api/v0/models` already
   reports the model id (or a `<owner>/<id>` variant) as known. The
   `--mlx` flag forces the Apple Silicon native variant; `-y`
   auto-accepts the default quantisation.
6. **Load the model** into RAM via `lms load <model> -y --context-length
   65536`, or skip if `/api/v0/models` reports it as `state=loaded`.
   The context length is configurable via `LLM_CONTEXT`.

After step 6, LM Studio is fully ready: server on `:1234`, model loaded,
OpenAI-compatible API at `/v1/chat/completions` waiting for Char to call.

The same orchestrator is exposed standalone as `./run.sh install-llm`,
so you can repair an LM Studio install or pull a different model later
without re-running the full bootstrap.

### Step (5/5) — Char.app, in detail

Same shape as the LM Studio step, with one extra wrinkle (the OpenAI
transcriber config patch):

- If Char isn't installed → offer to download the **pinned version**
  (`v1.0.24`, the build this repo was tested against) from the
  [`fastrepl/anarlog` GitHub Release](https://github.com/fastrepl/anarlog/releases/tag/desktop_v1.0.24),
  verify SHA256, and install it to `/Applications`. See
  [§ Char version pin](#char-version-pin) for what we pin and why.
- If Char *is* installed at a different version → warn that the
  pinned version is the only build the auto-config has been validated
  against, and offer to replace (default *No* — your call).
- Then, regardless of the above, prompt to wire Char's OpenAI
  transcriber at this server (equivalent to `./run.sh configure-char`).
  See [§ How the integration works](#how-the-integration-works-aka-the-hack)
  for the four `settings.json` keys this rewrites.

### What you still have to click manually

After bootstrap finishes there's exactly **one tab in Char** left to
configure that we don't auto-write — Char's *Intelligence* (LLM)
provider. Open Char → **Settings → Intelligence**, set:

- **Provider**: LM Studio
- **Base URL**: `http://127.0.0.1:1234`
- **Model**: `qwen3-30b-a3b-instruct-2507` (or `qwen/qwen3-4b` on smaller
  hardware — whichever bootstrap downloaded for you)

That's it. From there, every recording you take and every audio file
you Generate runs through Parakeet + Qwen on your laptop with no
network egress.

`./run.sh start` runs preflight first (so even if you skipped
`bootstrap` it Just Works), then brings up the services and tails the
ASR log. `Ctrl+C` detaches without stopping anything.

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

### Automated (recommended)

```bash
./run.sh configure-char
```

This is the same hook bootstrap offers, exposed as a standalone command so
you can re-run it any time. It:

- Locates Char's `settings.json` at `~/Library/Application Support/hyprnote/`.
- Quits Char.app if it's running (so the edit doesn't get clobbered on next save).
- **If `stt.openai.api_key` already holds a real-looking key**, prompts whether
  to save it (default Yes) to `~/.config/local_scribe/char-openai-key.<ts>.txt`
  with `chmod 600` before overwriting. If you accidentally pasted a real OpenAI
  project key into Char, this preserves it; you should still rotate that key on
  platform.openai.com because it sat unencrypted in the config file.
- Always backs up the whole `settings.json` to `settings.json.bak.<ts>` for
  trivial rollback.
- Patches exactly four keys (everything else — LLM provider, templates,
  calendars — is left untouched):

  | key | value |
  |---|---|
  | `ai.current_stt_provider` | `openai` |
  | `ai.current_stt_model` | `gpt-4o-transcribe` (progressive/SSE — bypasses Char's 60-second non-streaming idle abort, supports any audio length) |
  | `ai.stt.openai.base_url` | `http://127.0.0.1:8000/v1` |
  | `ai.stt.openai.api_key` | `local` |

- Offers to relaunch Char (default Yes).

Safe to re-run: if `api_key` is already `local`, the backup-key prompt is
skipped; only `settings.json` is re-snapshotted.

### Manual (if you'd rather poke the UI)

Char has **two separate transcription paths** — point both at this server.

#### 1. Live recording (Custom provider)

Used while Char is recording a meeting in real time.

| field | value |
|---|---|
| **Model being used** | Custom (the `nova-2` string is decorative — this server ignores it) |
| **Configure Providers → Custom → Base URL** | `http://127.0.0.1:8000` |
| **Configure Providers → Custom → API Key** | any non-empty string (auth is ignored locally) |

This routes Char's WebSocket streaming and batch live-audio path through our
Deepgram-compatible `/v1/listen` endpoint. (It's "batch over WebSocket" — final
transcript only, no interim partials, since neither Parakeet nor faster-whisper
streams natively.)

#### 2. "Generate transcript" on existing audio (OpenAI Batch Only provider)

Used when you click *Generate* on a note that already has audio. Char's
*Custom* provider is **Deepgram-only** and only used for live recording —
batch file imports go through whichever provider you pick from its "Batch
Only" list. We expose an OpenAI Whisper API-compatible endpoint so you can
point Char's bundled OpenAI provider at us.

| field | value |
|---|---|
| **Model selector** | `gpt-4o-transcribe` *(progressive/SSE — bypasses Char's 60-second non-streaming idle abort that breaks long files; this is the model `configure-char` writes by default)* |
| **Configure Providers → OpenAI → API Key** | any non-empty string |
| **Configure Providers → OpenAI → Advanced → Base URL** | `http://127.0.0.1:8000/v1` |

`gpt-4o-transcribe` triggers Char's progressive batch path, which streams
SSE deltas and resets its idle timer on each one. Our endpoint also
accepts `gpt-4o-transcribe-diarize` for short files where you want the
structured `segments[*].speaker` shape, but anything that takes more
than 60s to transcribe must use `gpt-4o-transcribe`.

For short files, our streaming endpoint still runs sherpa-onnx
diarization and inlines `Speaker N: …` prefixes into the streamed text
(default ON, ~3-4s of extra latency on a 60s clip). `verbose_json`,
`json`, `text`, `srt`, and `vtt` are all supported too on the
non-streaming path.

**Diarization tuning** — sherpa-onnx with the default `CLUSTER_THRESHOLD=0.5`
tends to over-shard on short conversational audio (you may see 6-10 speakers
where there were really 2-3). Either:

  * If you know the speaker count, set `NUM_SPEAKERS=2` (or 3, etc.) before
    `./run.sh start` — gives clean, exact labels.
  * Or set `OPENAI_BATCH_DIARIZE=0` to skip diarization entirely (single
    `speaker_0` placeholder, ~1s instead of ~4s).
  * Or run `./run.sh transcribe FILE` for richer output: same diarization
    plus an LLM pass that maps `speaker_0/1/...` to the actual people's
    names by reading conversational cues.

#### 3. Summary / Intelligence (LM Studio)

| field | value |
|---|---|
| **Configure Providers → Char Recommended → LLM** | LM Studio @ `http://127.0.0.1:1234`, model `qwen3-30b-a3b-instruct-2507` |

After this, every call you record (live) AND every audio file you import
(Generate) routes through Parakeet, with Qwen producing the note.

## Char version pin

Char is open-source and ships frequent updates. Some of those updates rename
keys in `settings.json`, change the multipart contract on
`POST /v1/audio/transcriptions`, or restructure the bundle. Any of those
would silently break our auto-config or pipeline.

To stop you from drifting into an untested combination, this repo pins a
specific Char build it has been end-to-end-validated against:

| field | value |
|---|---|
| Pinned version | `1.0.24` |
| Release tag | [`desktop_v1.0.24`](https://github.com/fastrepl/anarlog/releases/tag/desktop_v1.0.24) (2026-04-16) |
| arm64 DMG sha256 | `7f9c06881b9593b2aec17c8eddd65e5eb67d2c1072bfd008501989eb4181da89` |
| x86_64 DMG sha256 | `e7061d274308b563df724d7da5ede80e0cc68ff7082a3586b41ed8cc2c815503` |

Both SHAs and the version itself are constants (`CHAR_KNOWN_GOOD_VERSION`,
`CHAR_DMG_SHA256_AARCH64`, `CHAR_DMG_SHA256_X86_64`) at the top of `run.sh`.

### Installing the pinned version

```bash
./run.sh install-char
```

What it does:

1. Detects your CPU arch (`arm64` or `x86_64`).
2. Downloads the matching DMG from the GitHub Release shown above
   (≈600 MB on Apple Silicon, ≈125 MB on Intel).
3. Verifies the file's SHA256 against the constant in `run.sh`. **Refuses
   to install on mismatch** — that would mean either the release was
   retagged or the download was tampered with.
4. Mounts the DMG, copies `Char.app` (or `Hyprnote.app` if Char's old
   bundle name is still in there) to `/Applications`, unmounts.
5. Strips the macOS quarantine attribute so Gatekeeper doesn't pop the
   "downloaded from internet" warning the first time you launch (you've
   already opted in by verifying the pinned SHA).

If `Char.app` is already installed at the pinned version, this is a no-op.
If a *different* version is installed, it asks first (default No) before
replacing.

### When `run.sh` warns about drift

`./run.sh doctor`, `./run.sh configure-char`, and the bootstrap flow all
read `CFBundleShortVersionString` from `/Applications/Char.app` and compare
it to `CHAR_KNOWN_GOOD_VERSION`. If they don't match, you'll see:

```text
○ Char 1.0.27 installed; 1.0.24 pinned -- run `./run.sh install-char` to align
```

The warning never blocks — most patches are backwards-compatible — it just
flags that the auto-config flow hasn't been validated against your build.
If you hit weirdness after a Char update, downgrade with
`./run.sh install-char` and check whether the bug reproduces.

### Bumping the pin (for repo maintainers)

When a new Char release ships:

1. Download `hyprnote-macos-aarch64.dmg` and `.sha256`, plus the x86_64
   pair, from the new tag's release page.
2. Smoke-test end-to-end: record a call (live recording → Parakeet),
   import an existing audio file and click *Generate* (`/v1/audio/transcriptions`),
   confirm both work.
3. Update the four constants at the top of `run.sh`
   (`CHAR_KNOWN_GOOD_VERSION`, `CHAR_RELEASE_TAG` is derived,
   `CHAR_DMG_SHA256_AARCH64`, `CHAR_DMG_SHA256_X86_64`).
4. Update this section's table.

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
- Pass `--copy` to also drop the summary on your clipboard (off by default).

Useful flags (full list: `./run.sh transcribe --help`):

```bash
./run.sh transcribe FILE --save call.md       # markdown summary -> file
./run.sh transcribe FILE --save call.json     # full bundle (transcript + diarization + summary)
./run.sh transcribe FILE --save call.txt      # raw transcript only
./run.sh transcribe FILE --save-transcript diarized.txt   # diarized transcript only
./run.sh transcribe FILE --copy               # also copy summary to clipboard
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

char.app:
  ● Char 1.0.24 installed (matches pinned)
  ● Char transcriber configured for this server
```

Yellow/red dots tell you exactly which piece is broken so you don't have to
guess.

## End-to-end smoke test

To verify the whole stack — Char's wire contract, our endpoint, sherpa-onnx
diarization, the local pipeline, and LM Studio — without touching Char's UI:

```bash
# Pick any audio file you have lying around
AUDIO=~/Desktop/short_call.mp3

# 1. Replay Char's exact "Generate" request to our OpenAI-compatible endpoint
curl -sS http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer local" \
  -F "file=@${AUDIO};type=audio/mpeg" \
  -F "model=gpt-4o-transcribe-diarize" \
  -F "response_format=diarized_json" | jq '.task, .duration, (.segments | length)'

# 2. Drive the full local pipeline (ASR + diarization + Qwen summary)
./run.sh transcribe "$AUDIO" --diarize --save /tmp/smoke.md

# 3. Confirm both calls landed
./run.sh logs | tail -3
```

A passing run looks like this (numbers from a 60s clip on M3 Max,
ASR backend = parakeet, diarization on, Qwen3-30B-Instruct loaded):

| stage | latency | notes |
|---|---|---|
| `/v1/audio/transcriptions` first call after `start` | ~30 s | parakeet-mlx loading into the worker thread |
| `/v1/audio/transcriptions` warm | 2.9–3.3 s | asr 0.7–1.1s + diar 2.2s |
| `./run.sh transcribe` cached ASR + diar + Qwen | ~12 s | LLM dominates: 8s @ 42 tok/s, ttft 0.26 s |
| `./run.sh transcribe` first run on a new file | + ~3 s | added ASR cost vs. cached run |

Log line shape on a successful Generate (or smoke run):

```text
[openai 4d06a121-…] received 946368 bytes (model='gpt-4o-transcribe-diarize',
                  response_format=diarized_json, filename='audio.mp3')
[openai 4d06a121-…] running diarization (sherpa-onnx) ...
[openai 4d06a121-…] done in 2.87s (asr=0.67s, diar=2.20s, speakers=3),
                  58 chars, lang=en, format=diarized_json
```

If the wire test passes but Char still produces hallucinated note bodies on
short/empty audio, that's not the pipeline — that's Char's note-template
LLM step. Pick a less prescriptive template (or turn off "Use template")
and re-Generate. See the [Troubleshooting](#troubleshooting) section.

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
| `MAX_DIARIZE_SECONDS` | `1800` | audio longer than this auto-skips diarization on `POST /v1/audio/transcriptions` (returns ASR transcript with single `speaker_0` placeholder). Sherpa-onnx clustering is O(N²) and Char's UI doesn't tolerate multi-minute Generate latencies. Set `0` to disable the cap. |
| `MAX_SPEAKERS` | `12` | if sherpa-onnx returns more than this many distinct speakers, treat it as a clustering blow-up and collapse to single-speaker output rather than emit JSON Char can't render. Set `0` to disable the guard. |
| `STREAM_HEARTBEAT_SECONDS` | `20` | heartbeat interval (in seconds) for the SSE streaming branch of `POST /v1/audio/transcriptions`. Each heartbeat resets Char's hardcoded 60-second `BATCH_IDLE_TIMEOUT`; lower it on slower machines, raise it for less wire chatter. Must stay strictly less than 60. |
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
├── tests/                   # 147 unit tests, fully hermetic (mock all I/O)
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

## Development

```bash
./run.sh setup                                  # one-shot reinstall + redownload
venv/bin/python -m unittest discover -s tests   # 147 tests, ~0.05s, no model loads
```

The tests are fully hermetic — they mock all HTTP/MLX/sherpa-onnx so they run
in milliseconds without any models present.

## Troubleshooting

**`./run.sh start` shows "PARTIALLY ready" with `LM Studio NOT REACHABLE`.**
Run `./run.sh bootstrap` — step (4/5) installs LM Studio.app via Homebrew
cask, bootstraps the `lms` CLI to `~/.cache/lm-studio/bin/lms`, and starts
the local server on `:1234`. If `lms` is already installed but not on
`PATH`, the bootstrap step's "lms CLI present at …" line will tell you
where to find it.

**`./run.sh start` shows "PARTIALLY ready" with `<model> NOT LOADED`.**
Run `./run.sh bootstrap` again — step (4/5) is idempotent and will detect
the missing model, prompt you to download it (`lms get
qwen/qwen3-30b-a3b-instruct-2507 --mlx -y`, ≈32 GB), and `lms load` it.
On Macs with <48 GB unified memory it offers `qwen/qwen3-4b` (≈2.3 GB)
instead.

**Char shows `unauthorized`.**
Char insists on a non-empty API key. Anything works — `local`, `dummy`, `x` —
auth is ignored locally.

**Click "Generate" in Char on an audio note → nothing happens / cloud egress.**
Char's *Custom* provider is Deepgram-only and routes **live** recording only.
File imports use whichever provider you've configured under the "Batch Only"
list. Configure **OpenAI** (Configure Providers → OpenAI → Advanced → Base URL)
to `http://127.0.0.1:8000/v1` with any non-empty API key. After that, every
"Generate" click hits this server. Verify with `./run.sh logs` — you'll see
`[openai <id>] received N bytes ... done in X.XXs (asr=, diar=, speakers=N)`.

**Char shows way too many speakers in Generate output.**
sherpa-onnx with default `CLUSTER_THRESHOLD=0.5` over-shards on short
conversational audio. Set `NUM_SPEAKERS=2` (or your known count) before
`./run.sh start` for exact, clean labels. Or `OPENAI_BATCH_DIARIZE=0` to
skip diarization entirely if you don't need speaker labels.

**Char shows nothing in the Transcript tab after clicking Generate on a long recording.**
Char's tauri-plugin-transcription has a hardcoded 60-second client-side
`BATCH_IDLE_TIMEOUT` that aborts the transcription future if no progress
event arrives for a full minute. The non-streaming `gpt-4o-transcribe-diarize`
batch path only fires a single response event at the end, so any audio
whose ASR exceeds 60s (i.e. anything longer than ~80 minutes against
Parakeet on M3 Max, or anything at all on slower machines) silently
fails with no error toast and no `transcript.json` written to disk.

`./run.sh configure-char` now sets `current_stt_model = gpt-4o-transcribe`
(the non-diarize model name), which routes Char to its **progressive**
SSE-streamed batch path. Our `/v1/audio/transcriptions` endpoint detects
`stream=true` in the request and emits `transcript.text.delta` heartbeat
events every `STREAM_HEARTBEAT_SECONDS` (default 20 s) while ASR runs,
then a final `transcript.text.done` with the full transcript. Each delta
resets Char's idle timer, so any duration of audio is supported.

Trade-offs of the streaming model name:

- The structured `segments[*].speaker` array isn't carried by the
  progressive batch shape Char accepts; for short files where
  diarization actually fires we inline `Speaker N: …` prefixes into the
  streamed text instead. Long files auto-skip diarization anyway
  (`MAX_DIARIZE_SECONDS`).
- Per-word timestamps are dropped from Char's stored `transcript.json`
  on the streaming path. If you want word-level timing for a specific
  recording, run `./run.sh transcribe FILE` outside Char.

If a "Generate" still doesn't render after a server restart:

- Confirm `./run.sh status` shows `current_stt_model = gpt-4o-transcribe`
  in `./run.sh doctor`'s `char.app:` block; if not, re-run `configure-char`.
- Look for `streaming heartbeat (asr in flight, …)` lines in `./run.sh logs`.
  Their absence means Char isn't sending `stream=true` (model misconfigured).
- The very last fallback is to delete `transcript.json` from the session
  folder under `~/Library/Application Support/hyprnote/sessions/` and click
  Generate one more time.

If you'd rather have *some* diarization on long audio:

- Set `NUM_SPEAKERS` to your known count (cheapest fix; gives clean labels)
- Or raise `MAX_DIARIZE_SECONDS` to allow diarization on longer files
  (be aware: sherpa-onnx is O(N²) so a 2-hour meeting takes ~6 minutes
  of clustering)
- Or run `./run.sh transcribe FILE --diarize` outside Char to get the
  full pipeline (ASR + diarization + LLM speaker-name inference) without
  any UI timeout pressure

**Char's note body looks fabricated / corporate-flavored on a short call.**
That's not the transcription pipeline — Char runs a *separate* LLM call to
fill the active note template (1:1 Meeting, Legal meeting, etc.) and small
LLMs confabulate when asked to fill prescriptive sections from a thin
transcript. Run the smoke test above; if the diarized JSON is faithful but
the note body isn't, switch Char to a less prescriptive template (or pick
a more capable LLM in Char → Settings → Intelligence). For the 4B Qwen,
swap to the 30B you've already loaded:
`qwen3-30b-a3b-instruct-2507`.

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
