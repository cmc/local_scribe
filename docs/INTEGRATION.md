# How the integration works (a.k.a. "the hack")

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/CHAR_REVIEW.md`](CHAR_REVIEW.md), [`docs/FORK_CONSIDERATIONS.md`](FORK_CONSIDERATIONS.md), [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)

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

### The streaming-batch persistence bug (and our sidecar workaround)

Solving the 60-second abort isn't enough on its own: Char's progressive
batch parser has a second bug downstream that silently drops the
transcript even after a successful 200 OK.

In Char's
[`crates/owhisper-client/src/adapter/openai/batch.rs`](https://github.com/fastrepl/anarlog/blob/main/crates/owhisper-client/src/adapter/openai/batch.rs#L262-L282),
the `transcript.text.done` SSE event is converted to a
`BatchStreamEvent::Result` with a **hardcoded `Vec::new()` for words**:

```rust
ParsedTranscriptionStreamEvent::TextDone { text, usage, .. } => {
    Some(Ok(BatchStreamEvent::Result {
        response: build_batch_response(
            text.trim().to_string(),
            Vec::new(),                    // <-- always empty
            transcription_usage_metadata(usage),
        ),
    }))
}
```

Then in [`apps/desktop/src/stt/useRunBatch.ts`](https://github.com/fastrepl/anarlog/blob/main/apps/desktop/src/stt/useRunBatch.ts#L120-L123)
the persist callback short-circuits on empty words:

```ts
const persist = handlePersist ?? ((words, hints) => {
    if (words.length === 0) {
        return;                            // <-- silent drop, no error, no UI update
    }
    // ...build TranscriptStorage row, write transcript.json...
});
```

So **every** transcript that travels Char's progressive (streaming)
batch path is silently dropped, regardless of how long or short the
audio is. Server-side our log says `200 OK / N chars`, the bytes
make it to Char, then Char throws them away.

We can't fix this from the response shape: the parser only handles
`transcript.text.delta` and `transcript.text.done`, no segment events
carry per-word timing through to the persist callback. The only
hardcoded inputs that decide whether words get stored are the words
*Char itself* synthesises from the response, and we have zero control
over that branch from our side.

**Workaround: write `transcript.json` straight to Char's session
directory ourselves.** When a request hits
`/v1/audio/transcriptions`, [`char_persist.py`](local_scribe/char/char_persist.py)
SHA256-hashes the uploaded audio, walks
`~/Library/Application Support/hyprnote/sessions/<uuid>/audio.mp3`
looking for a match, and if it finds one, atomically writes
`transcript.json` to that session in Char's exact persister schema
(`{"transcripts":[{id, session_id, words[], speaker_hints[], memo_md,
created_at, started_at, user_id}]}`, validated against
`apps/desktop/src/store/tinybase/persister/session/load/transcript.ts`).

Char's TinyBase persister registers
`watchPaths: ['sessions/']`
([`multi-table-dir.ts`](https://github.com/fastrepl/anarlog/blob/main/apps/desktop/src/store/tinybase/persister/factories/multi-table-dir.ts#L80))
so the file is auto-loaded on next session-open without needing to
restart Char. The SSE response still completes normally for backwards
compatibility — Char drops the empty-words result as designed, but the
real transcript is already on disk by the time it does.

Disable the sidecar with `CHAR_PERSIST=0` if you ever need the broken
upstream behaviour for repro / debugging. The audit-log line looks
like:

```
[char_persist <req-id>] wrote transcript.json to /…/sessions/<uuid>/transcript.json (words=14715, speakers=1, sha256=031b967a3d06)
```

A `char_persist: no Char session matches uploaded audio` line at INFO
means the request didn't come from Char (e.g. you `curl`'d the endpoint
directly) or the SHA256 didn't match — both expected, both no-op.

### What you lose vs. real OpenAI Whisper

Compared to actually hitting `api.openai.com/v1/audio/transcriptions`,
the shim:

- Inlines `Speaker N: …` prefixes into the streamed text. The
  progressive shape Char accepts here can't carry the structured
  `segments[*].speaker` array (Char's parser doesn't have a segment
  arm — see § the streaming-batch persistence bug), but the sidecar
  (`char_persist.py`) writes the full word-level + speaker hint data
  to `transcript.json` so Char's UI colours speakers correctly on
  session reload. `MAX_DIARIZE_SECONDS=14400` (4 h) by default —
  set `0` to disable the cap, see § Diarization tuning for the
  per-session redo command.
- Ignores the OpenAI multipart fields `prompt`, `temperature`,
  `timestamp_granularities`, and `language`. Parakeet doesn't take
  sampling-temperature hints or per-token granularity, and language
  detection is automatic (English-only model).

Everything else — sub-second turnaround for short files, the Generate
button, the live-recording UI, summary generation via LM Studio — works
exactly as it would against the real OpenAI / Deepgram backends, except
no audio leaves your laptop.

