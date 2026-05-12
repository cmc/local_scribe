# Troubleshooting

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. The content below is the
> canonical reference. The README keeps a short pointer
> paragraph linking back here.
>
> **Related docs:** [`docs/KEY_SAFETY.md`](KEY_SAFETY.md), [`docs/INSPECTOR.md`](INSPECTOR.md)

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

**Char transcripts only contain my own voice — the other speakers are missing.**
Your microphone audio is being captured but the system-audio (other-speaker)
side is silently denied at the macOS Transparency-Consent-Control (TCC)
layer. Diagnosis:

```bash
./run.sh char firewall-status
#  → look for: "TCC attribution responsible=com.googlecode.iterm2
#               — this is a terminal, NOT Char"
```

If the responsible bundle is a terminal (`com.googlecode.iterm2`,
`com.apple.Terminal`, …) and not `com.hyprnote.stable`, you've hit the
pre-2026-05 `sandbox-exec` launch bug or its regression. macOS attributes
Char's permission requests to the launching terminal; terminals don't have
`kTCCServiceAudioCapture` consent, so the system audio path is blackholed
even though Char.app itself is granted that permission.

Fix: quit Char (`Cmd-Q`) and relaunch via the wrapper:

```bash
osascript -e 'quit app "Char"'
./run.sh char launch
```

This uses `/usr/bin/open -a Char.app --env HTTPS_PROXY=…` so Char.app is
its own TCC-responsible bundle. Re-run `./run.sh char firewall-status` —
the TCC line should now read
`responsible=com.hyprnote.stable (system audio capture available)`. If it
still doesn't, see [`docs/CHAR_REVIEW.md` § Layered firewall trade-offs](CHAR_REVIEW.md#layered-firewall-trade-offs-the-may-2026-sandbox-exec-drop)
for the full attribution-chain debugging recipe (`log show --predicate
'process == "tccd"' --last 5m | rg AttributionChain`).

**`./run.sh char launch` refuses with "Char is already running".**
`open -a` would just send an activation Apple Event to the existing Char
process without applying our `HTTPS_PROXY` env, leaving the running Char
unfiltered. Quit Char first:

```bash
osascript -e 'quit app "Char"'    # graceful
# or, if Char is unresponsive:
pkill -f 'Char.app/Contents/MacOS'
./run.sh char launch
```

If you launched Char from the Dock first by mistake,
`./run.sh char firewall-status` will also flag the running process as
"NOT through our wrapper — egress is NOT filtered".

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

