# Char.app Security Review

A bottom-up audit of the [Char.app](https://char.com) binary
(`v1.0.24`, the version this repo's auto-config and pipeline are
validated against). Goal: enumerate every channel through which Char
*could* exfiltrate user data, classify each by whether it's on-by-default
or user-triggered, and document concrete mitigations so the
`local_scribe` stack delivers the privacy guarantee promised in the
README.

This is a working audit, not a marketing piece. Findings are based on
**code reading + binary string extraction + live network observation
of the shipping signed build on this machine**, cross-referenced against
the open-source [`fastrepl/anarlog`](https://github.com/fastrepl/anarlog)
repository (commit-current `main` at the time of review). All commands
in [§ Methodology](#methodology) are reproducible against any future
Char build.

> Char is open source (MIT-licensed), notarized, and signed by **Fastrepl,
> Inc. (Team ID `6SLY7V277V`)**. Most of what's documented here is
> verifiable by a single grep against the GitHub repo — the audit's value
> is in centralising the picture of which endpoints fire when, and what
> a privacy-conscious user has to do about it.

---

## TL;DR

| data class | leaves the machine? |
|---|---|
| **Audio recordings** (`audio.mp3`) | **No.** Streamed to whatever STT provider the user configures; with `local_scribe` configured (this repo's default), that's `127.0.0.1:8000`. Verified via `lsof` mid-recording. |
| **Transcripts** (`transcript.json`, words, speaker hints) | **No.** Written to disk after our `/v1/audio/transcriptions` returns. |
| **Generated notes / summaries** (`*.md` per template) | **No.** Char calls the configured LLM (`http://127.0.0.1:1234` here) for the summary; result is written to disk, never re-uploaded. |
| **Calendar events / meeting links** | **Only if connected** through Char's hosted OAuth (`api.char.com`). Off by default. |
| **Stable device fingerprint** (hashed `IOPlatformUUID`) | **Yes — to Sentry**, on every error, panic, or crash. Not user-toggleable; baked in via `option_env!("SENTRY_DSN")` at build time. **Mitigation: firewall block.** |
| **Stable device fingerprint (same hash)** | **Yes — to PostHog**, on every analytics event, *unless* `analytics.Disabled = true` in `store.json`. **Mitigation: write the toggle (we do).** |
| **App version + arch + UA on every update poll** | **Yes — to `desktop2.hyprnote.com`** (proxied through Scarf.sh download analytics) on a periodic timer. The Tauri config has `updater.active: true` for the `stable` channel and there is no in-app toggle. **Mitigation: firewall block.** |

The honest summary: **the data plane (audio, transcripts, notes) stays
local — verified.** What leaks is the **control plane** (telemetry +
update checks): a stable hashed device id, app version, error
backtraces, and product analytics events. None of it includes
recording content. All three channels can be blocked at the network
layer without affecting transcription / summarisation; one of them
(PostHog) has an in-app toggle which we now flip during
`./run.sh configure-char`.

---

## Methodology

Everything below was produced from a clean `/Applications/Char.app`
on macOS 15.0 / Apple M3 Max, against the `1.0.24` build. To
reproduce on any future Char version:

```bash
# 1. Bundle inventory
CHAR=/Applications/Char.app
codesign -dv --verbose=4 "$CHAR" 2>&1            # signing identity, notarisation
codesign -d --entitlements - "$CHAR" 2>&1        # entitlements
defaults read "$CHAR/Contents/Info.plist"        # permission strings, bundle id

# 2. URL / hostname surface (clean by deduping)
strings -a "$CHAR/Contents/MacOS/char" \
  | rg -o 'https?://[a-zA-Z0-9._/?=:&#%~-]+' \
  | sort -u

# 3. Telemetry-keyword sweep
strings -a "$CHAR/Contents/MacOS/char" \
  | rg -i 'sentry|posthog|mixpanel|amplitude|segment\.com|datadog|crashlytics'

# 4. Live connections during use
lsof -nP -i -P 2>/dev/null | rg -i 'char|hyprnote'

# 5. Cross-reference source
curl -sf 'https://raw.githubusercontent.com/fastrepl/anarlog/main/apps/desktop/src-tauri/src/lib.rs'
curl -sf 'https://raw.githubusercontent.com/fastrepl/anarlog/main/plugins/analytics/src/ext.rs'
curl -sf 'https://raw.githubusercontent.com/fastrepl/anarlog/main/apps/desktop/src-tauri/tauri.conf.stable.json'
curl -sf 'https://raw.githubusercontent.com/fastrepl/anarlog/main/crates/host/src/lib.rs'

# 6. Resolve telemetry endpoints to providers
dig +short us.i.posthog.com
dig +short desktop2.hyprnote.com
dig +short -x <ip-from-lsof>
```

Update this file whenever:

- The pinned `CHAR_KNOWN_GOOD_VERSION` in `run.sh` is bumped
  ([§ Char version pin](README.md#char-version-pin)).
- A new Tauri plugin appears in the `tauri::Builder::default()` chain
  in `apps/desktop/src-tauri/src/lib.rs`.
- A new hostname appears in the `strings` sweep that wasn't in the
  previous review.
- An item in [§ TODO](TODO.md)'s privacy section lands and changes the
  expected mitigations here.

---

## Bundle inventory

### Code signing & notarisation

```text
Authority=Developer ID Application: Fastrepl, Inc. (6SLY7V277V)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=Apr 16, 2026 at 07:00:11
Notarization Ticket=stapled
TeamIdentifier=6SLY7V277V
Format=app bundle with Mach-O thin (arm64)
CDHash=a85c74f98c43679764d7fd2a80d4cca5b6618763
Hardened Runtime=enabled (flags=0x10000)
Sandbox=NOT enabled
```

The app is signed by **Fastrepl, Inc.**, notarised, with a stapled
ticket and the hardened runtime turned on. **It is not sandboxed** —
typical for Tauri apps that need `tauri-plugin-fs` arbitrary-path
access to write under `~/Library/Application Support/hyprnote/`.

### Sub-binaries

| binary | size | role | privacy notes |
|---|---|---|---|
| `MacOS/char` | 229 MB (arm64) | Tauri host process; embeds the WebView2/WKWebView frontend, all Rust plugins, Sentry, PostHog, Tauri updater | the main attack surface |
| `MacOS/char-cli` | 9 MB (arm64) | Shell-friendly CLI (`char-cli desktop`, `char-cli completions`) | also instrumented with PostHog — emits `cli_command_invoked` events with subcommand + version |
| `MacOS/char-chrome-native-host` | 576 KB (arm64) | [Chrome native messaging](https://developer.chrome.com/docs/apps/nativeMessaging) host. Allows a Chrome extension to talk to Char locally over stdio. | only invoked by a Chrome extension that's been registered against this binary's path; not a network egress vector |
| `MacOS/check-permissions` | 74 KB (arm64) | Pre-flight permission checker (microphone, calendar, etc.) called before recording | local-only |

### Entitlements

```xml
com.apple.security.cs.allow-jit                       = true
com.apple.security.cs.allow-unsigned-executable-memory = true
com.apple.security.device.audio-input                 = true
com.apple.security.personal-information.addressbook   = true
com.apple.security.personal-information.calendars     = true
```

JIT + unsigned-executable-memory are required for V8/Tauri's WebView.
Audio-input is the microphone gate. Address book + calendars are for
the optional `tauri_plugin_calendar` and contacts integration. **No
network entitlements** — Char relies on the unsandboxed default of
"all outbound TCP allowed".

### Info.plist usage strings

These are the prompts macOS shows when Char first asks for permission:

| key | value |
|---|---|
| `NSMicrophoneUsageDescription` | "record your voice during the meeting" |
| `NSAudioCaptureUsageDescription` | "record voice from other participants" (system audio) |
| `NSCalendarsFullAccessUsageDescription` | "read events" |
| `NSRemindersFullAccessUsageDescription` | "sync and manage tasks" |
| `NSContactsUsageDescription` | "access to function properly" — vague; unclear what specifically reads contacts |
| `NSLocalNetworkUsageDescription` | "access to your local network to function properly" — unspecified, likely for mDNS / Bonjour discovery of LAN STT/LLM servers |

### Tauri plugins enabled in production

From the `tauri::Builder::default()` chain in
`apps/desktop/src-tauri/src/lib.rs` (production build, `com.hyprnote.stable`):

| plugin | what it does | privacy-relevant? |
|---|---|---|
| `tauri_plugin_sentry` | Crash + panic reporting | **Yes** — see [§ Sentry deep dive](#sentry-deep-dive) |
| `tauri_plugin_analytics` | Product-analytics events to PostHog | **Yes** — see [§ PostHog deep dive](#posthog-deep-dive) |
| `tauri_plugin_updater` + `tauri_plugin_updater2` | Tauri auto-updater | **Yes** — see [§ Auto-update channel](#auto-update-channel) |
| `tauri_plugin_calendar` | Google / Outlook / iCloud calendar sync | only if connected |
| `tauri_plugin_todo` | Linear / GitHub / Todoist sync | only if connected |
| `tauri_plugin_auth` | OAuth flow for the above (via `api.char.com`) | only if connected |
| `tauri_plugin_bedrock` | AWS Bedrock LLM provider | only if configured |
| `tauri_plugin_local_stt` | local STT (Cactus runtime + Parakeet/Whisper) | local-only |
| `tauri_plugin_local_llm` | local LLM (Cactus runtime + Llama 3.2 / Cactus VLM) | local-only |
| `tauri_plugin_transcription` | the OpenAI/Deepgram-shaped transcription clients | only fires against the configured `base_url` (we set it to `127.0.0.1:8000`) |
| `tauri_plugin_activity_capture` | activity event capture for analytics enrichment | confined to local store; emits via PostHog |
| `tauri_plugin_autostart` | macOS LaunchAgent that auto-starts Char with `--background` | local-only behaviour, but persists across reboots |
| `tauri_plugin_messenger` | desktop-to-renderer messaging plumbing | local-only |
| `tauri_plugin_mcp` | Model Context Protocol bridge | local-only IPC |
| `tauri_plugin_network` | network plugin (LAN discovery?) | requires `NSLocalNetworkUsageDescription` |
| `tauri_plugin_detect` | feature detection (audio devices, screen sharing?) | local-only |
| ~30 other Tauri stdlib plugins | dialog, fs, http, store, shortcut, tray, ... | mostly local |

### Embedded models

Char ships **541 MB of model weights inside the bundle** under
`Resources/models/cactus/char-vlm/weight/`:

```text
config.txt:
  model_type=lfm2          (Liquid Foundation Model 2 — vision-language)
  vocab_size=65536  hidden_dim=1024  num_layers=16
  vision_num_layers=12     visual_tokens_per_img=0
  context_length=128000    quantization=INT8 (FP16 master)
```

This is a quantised LFM2-VLM small enough to run on-device. The presence
of vision layers + the bundled `vision_*.weights` files means Char can
do screen / image analysis locally when needed (probably for
auto-tagging visual context attached to notes). The runtime is
[Cactus](https://github.com/cactus-compute/cactus), a Rust-native
ML runtime referenced extensively in the binary's strings table.

**Privacy implication:** none direct — this model runs on your laptop;
its weights are static. But it does mean Char has on-device computer
vision available, which is something to know about when granting it
screen-recording permission.

---

## Network egress catalog

Every outbound endpoint discovered, classified by lifecycle. See
[§ Reproduction](#methodology) for how the hostname list was extracted.

### Always-on (default behaviour, no user action required)

| endpoint | what fires it | DNS resolves to | mitigation |
|---|---|---|---|
| `o4506190168522752.ingest.us.sentry.io` | Sentry crash + panic + tracing reports | Sentry SaaS (us-east) | firewall block; **no in-app toggle** |
| `us.i.posthog.com` (also `eu.i.posthog.com` referenced) | PostHog analytics events + feature-flag polling | `posthog-ingress-prod-us-256455477.us-east-1.elb.amazonaws.com` | flip `store.json::analytics.Disabled = true` (our `configure-char` does this) |
| `desktop2.hyprnote.com/update/<target>-<arch>/<version>?channel=stable` | Tauri auto-updater (periodic poll + on-demand check) | **`gateway.scarf.sh`** → ELB in us-west-2 | firewall block; **no in-app toggle**; we already pin a known-good version |
| `browser.sentry-cdn.com` | Sentry browser SDK (the Tauri WebView frontend has its own Sentry init too) | Sentry CDN | firewall block (same as Sentry) |

The first three are documented in detail below. The browser Sentry
matters less because the Rust-side init happens first and covers panics
across the whole process.

### On-demand (firsttime model fetch when "Char Recommended" provider chosen)

| endpoint | what's fetched |
|---|---|
| `hyprnote.s3.us-east-1.amazonaws.com/v0/Cactus-Compute/weights/parakeet-tdt-0.6b-v3-int8.zip` | Parakeet TDT 0.6B v3 (≈300 MB INT8) |
| `hyprnote.s3.us-east-1.amazonaws.com/v0/Cactus-Compute/weights/parakeet-ctc-0.6b-int8.zip` | Parakeet CTC variant |
| `hyprnote.s3.us-east-1.amazonaws.com/v0/Cactus-Compute/weights/whisper-{tiny,small,medium,large-v3-turbo}-{int4,int8}.zip` | various Whisper builds |
| `hyprnote.s3.us-east-1.amazonaws.com/v0/lmstudio-community/Llama-3.2-3B-Instruct-GGUF/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf` | Llama 3.2 3B for the local LLM provider |
| `hyprnote.s3.us-east-1.amazonaws.com/v0/yujonglee/hypr-llm-sm/model_q4_k_m.gguf` | Hyprnote's small fine-tuned LLM |
| `hyprnote.s3.us-east-1.amazonaws.com/v0/nvidia_parakeet-v3_494MB.tar` | NVIDIA Parakeet (tarball form) |

These only fire if you pick the **"Char Recommended"** STT or LLM
provider in Char's settings. With `local_scribe` configured, you don't
need any of them — we ship our own Parakeet + Qwen via LM Studio. After
download, the models live in `~/Library/Application Support/hyprnote/models/`.

### User-triggered (only fires when the user opts into a feature)

| endpoint | feature |
|---|---|
| `api.openai.com` | OpenAI STT (`gpt-4o-transcribe[-diarize]`) **and/or** OpenAI Chat — both rewritten to `127.0.0.1:8000` and `127.0.0.1:1234` by our `configure-char` |
| `api.deepgram.com` | Deepgram STT — only if the *Custom* provider is left at Deepgram's URL; ours rewrites to `127.0.0.1:8000` |
| `api.assemblyai.com`, `api.gladia.io`, `api.granola.ai`, `api.soniox.com`, `api.aquavoice.com`, `api.elevenlabs.io`, `api.fireworks.ai` | other STT/TTS providers — only if user picks them |
| `api.mistral.ai` | Mistral LLM provider — only if user picks it |
| `api.pyannote.ai` | Pyannote.ai diarization — only if user picks it |
| AWS Bedrock | only if user supplies AWS keys |
| `api.char.com` | Char's hosted backend for **integrations**: Linear / GitHub / Google Calendar / Outlook / Todoist OAuth + sync. Sends an `x-device-fingerprint` header. Only contacted after you click "Connect" in Char's integrations panel. |
| `api.github.com` | Char-hosted GitHub OAuth flow + repo / issue listing |
| `cloudsync.sqlite.ai` | referenced in the binary; not observed in live traffic. Likely an opt-in cloud sync for the SQLite catalog (off by default). |
| `cli.char.com`, `char.com`, `char.com/download`, `char.com/discussions/...` | static docs / download pages opened in your default browser by `char-cli desktop`, `char-cli help`, etc. — not background traffic |

### Provider noise (referenced but never called)

The `strings` dump turns up a long list of academic / publishing
hostnames (`apa.org`, `bmj.com`, `acs.org`, `arxiv.org`, `cell.com`,
…). These come from CSL citation-style XML embedded in the bundle for
Char's bibliographic features — purely string constants in citation
templates, **never resolved as URLs at runtime**.

---

## Sentry deep dive

### What's enabled

From `apps/desktop/src-tauri/src/lib.rs`:

```rust
let dsn = option_env!("SENTRY_DSN");
if let Some(dsn) = dsn {
    let release = option_env!("APP_VERSION").map(|v| format!("hyprnote-desktop@{}", v).into());
    let client = sentry::init((dsn, sentry::ClientOptions {
        release,
        traces_sample_rate: 1.0,            // <-- 100% trace sampling
        auto_session_tracking: false,
        ..Default::default()
    }));
    sentry::configure_scope(|scope| {
        scope.set_tag("service.namespace", "hyprnote");
        scope.set_tag("service.name", "desktop");
        scope.set_tag("enduser.pseudo.id", hypr_host::fingerprint());
        scope.set_user(Some(sentry::User {
            id: Some(hypr_host::fingerprint()),
            ..Default::default()
        }));
    });
}
let _guard = sentry_client.as_ref()
    .map(|client| tauri_plugin_sentry::minidump::init(client));
```

### What gets transmitted

- **DSN** (extracted from the binary): `https://00c737b32147fd5e2069718aa9a673ec@o4506190168522752.ingest.us.sentry.io/4510474177609728`
- **`enduser.pseudo.id` tag + Sentry user id**: `hypr_host::fingerprint()` →
  ```rust
  let fingerprint = machine_uid::get().unwrap_or_default();
  let mut hasher = DefaultHasher::new();
  fingerprint.hash(&mut hasher);
  format!("{:x}", hasher.finish())
  ```
  On macOS, `machine_uid::get()` reads **`IOPlatformUUID`** from `IOKit`
  — a stable hardware identifier that survives OS reinstalls. Hashing it
  with `std::collections::hash_map::DefaultHasher` produces a u64 hex
  string. Crucially: **same Mac → same hash, forever**, so Sentry can
  group every error from this device into one identity.
- **Stack traces**, **panic messages**, **`tracing` spans / breadcrumbs**.
  With `traces_sample_rate: 1.0`, Sentry receives 100% of performance
  spans, not just errors — i.e. function-level operation traces during
  normal use, including (potentially) command names invoked, file paths
  in error contexts, and module names.
- **Tauri minidumps** via `tauri_plugin_sentry::minidump::init`. If
  Char crashes (segfault, OOM, etc.), a minidump containing thread
  stacks + process metadata is uploaded.

### What is NOT transmitted

- **Audio bytes** — Sentry is on the panic / tracing path, not the
  recording path. Crash dumps wouldn't contain in-flight audio buffers
  unless a panic happened mid-stream, in which case a tiny window of
  the buffer might end up in a minidump (highly unlikely to be
  intelligible).
- **Transcript text or note content** — same reasoning.

### Opt out

**There is no in-app toggle.** The DSN is pulled from the
compile-time environment via `option_env!("SENTRY_DSN")`; there is no
runtime branch checking a setting. The only ways to disable Sentry on
the shipping binary are:

1. **Block the network endpoint.** See [§ Mitigations](#mitigations).
2. **Build Char yourself from `fastrepl/anarlog`** without setting
   `SENTRY_DSN` — the `if let Some(dsn) = dsn` branch then no-ops.
   This is documented but not what most users will do.

Adding a runtime kill-switch is on this repo's [TODO list](TODO.md).

---

## PostHog deep dive

### What's enabled

From `plugins/analytics/src/ext.rs`:

```rust
pub async fn event(&self, mut payload: hypr_analytics::AnalyticsPayload) -> Result<(), crate::Error> {
    Self::enrich_payload(self.manager, &mut payload);
    if self.is_disabled().unwrap_or(true) { return Ok(()); }   // <-- toggle check
    let machine_id = hypr_host::fingerprint();
    let client = self.manager.state::<crate::ManagedState>();
    client.event(machine_id, payload).await...
}

fn enrich_payload(manager, payload) {
    payload.props.entry("app_version").or_insert(env!("APP_VERSION"));
    payload.props.entry("app_identifier").or_insert(manager.config().identifier);
    payload.props.entry("git_hash").or_insert(manager.misc().get_git_hash());
    payload.props.entry("bundle_id").or_insert(manager.config().identifier);
}
```

PostHog's local-evaluation feature-flag poller is also wired up,
which background-polls the project's flag definitions every 30s when
a personal API key is present.

### What gets transmitted

- **Endpoint**: `https://us.i.posthog.com/batch/` (capture),
  `/flags/?v=2` (feature flag eval), `/api/feature_flag/local_evaluation/?send_cohorts`
- **Distinct id**: same `hypr_host::fingerprint()` (machine_uid hash) as
  Sentry — so PostHog and Sentry share a stable cross-install device id.
- **Event names** (from `char-cli` strings + crates/analytics):
  `cli_command_invoked` (with subcommand prop), `run_import`, plus
  whatever app code calls `analytics().event(...)`. The full list is
  not documented; you can enumerate them by `rg -n 'analytics().event'
  fastrepl/anarlog`.
- **Properties** auto-attached to every event: `app_version`,
  `app_identifier`, `git_hash`, `bundle_id`. The set call also writes
  a `$set` block which is PostHog's user-property update protocol.

### What is NOT transmitted

- **Audio / transcripts / notes** — PostHog's payload is event names +
  small JSON props. Char's analytics calls don't pass note content as
  a property. Verifiable with `rg 'analytics().event' fastrepl/anarlog`
  on the source.

### Opt out — there *is* one

```rust
// in plugins/analytics/src/ext.rs
pub fn set_disabled(&self, disabled: bool) -> Result<(), crate::Error> {
    let store = self.manager.store2().scoped_store(crate::PLUGIN_NAME)?;
    store.set(crate::StoreKey::Disabled, disabled)?;
    Ok(())
}
```

The flag persists to `~/Library/Application Support/hyprnote/store.json`
under the `analytics` key:

```json
{
  "analytics": "{\"Disabled\":true}"
}
```

Char's UI exposes this toggle (in Settings → some "data" / "privacy"
section). On a fresh install the flag is `false` (analytics enabled
until the user clicks the toggle).

**`./run.sh configure-char` now writes this key for you** so analytics
is off from the moment you run our bootstrap. See
[§ Mitigations](#mitigations).

---

## Auto-update channel

### Where it points

From `apps/desktop/src-tauri/tauri.conf.stable.json`:

```json
"updater": {
  "active": true,
  "endpoints": [
    "https://desktop2.hyprnote.com/update/{{target}}-{{arch}}/{{current_version}}?channel=stable"
  ]
}
```

`desktop2.hyprnote.com` resolves to **`gateway.scarf.sh`** — Scarf is
[a download-analytics service](https://about.scarf.sh) that proxies
software downloads and logs the User-Agent, IP geolocation, and
software version of every fetcher. So every update poll Char makes
becomes a row in Hyprnote's Scarf dashboard, indexed by your IP +
version.

### What gets transmitted

Just the URL itself, plus standard HTTP headers:

```
GET /update/darwin-aarch64/1.0.24?channel=stable HTTP/1.1
Host: desktop2.hyprnote.com
User-Agent: tauri-plugin-updater/2.10.1
Accept: application/json
```

No payload, no body. The server returns either `204 No Content`
("you're up to date") or a JSON body with the new version + signature
to download.

### What is NOT transmitted

Nothing besides the URL + standard headers. Sentry/PostHog do not
piggyback on this channel.

### Opt out

**No in-app toggle.** Tauri's stable-channel config has
`updater.active: true` and there's no runtime branch consulting a
setting. Options:

1. **Firewall block** `desktop2.hyprnote.com` (recommended; we pin a
   known-good Char version anyway, so we don't *want* surprise updates
   breaking our auto-config).
2. **Hosts file override**: `127.0.0.1 desktop2.hyprnote.com` in
   `/etc/hosts` — same effect, simpler. (Note: macOS may pick this up
   slowly; flush the DNS cache.)
3. **Build from source** with `updater.active: false`.

Char also has a `tauri_plugin_updater2` (a second updater plugin
loaded after the standard one). Source for it isn't in the public
`fastrepl/anarlog` repo; only the strings sweep tells us it exists.
Worth investigating in a future audit.

---

## Crash reporter subprocess

While Char is running, a second process exists:

```
/Applications/Char.app/Contents/MacOS/char --crash-reporter-server=/var/folders/.../temp-socket-...
```

This is the standard Tauri / Chromium-style crash-reporter subprocess,
listening on a Unix socket for crash reports from the main process. On
crash it serialises to a minidump and feeds it to the Sentry minidump
endpoint via the same DSN as above.

**Privacy implication**: same as Sentry — minidumps include thread
stacks at time of crash. They do not include audio buffers or
transcripts in normal operation.

**Disable** by blocking Sentry's network endpoints (mitigation below).
The subprocess will still spawn and serialise minidumps to a temp dir
on crash, but they'll fail to upload.

---

## Streaming-batch persistence bug

This is a **functional** bug, not a privacy one, but it sits at the
seam between Char and any custom OpenAI-compatible base URL (this
project being one) and worth recording in the same audit because
working around it required us to write into Char's session directory
ourselves. Anyone running an alternative OpenAI shim against Char will
hit this.

**Char's progressive batch parser drops every transcript that comes
back from the streaming `gpt-4o-transcribe` path**, regardless of
length. The mechanism is two layers deep:

1. [`crates/owhisper-client/src/adapter/openai/batch.rs:262-282`](https://github.com/fastrepl/anarlog/blob/main/crates/owhisper-client/src/adapter/openai/batch.rs#L262-L282)
   maps `transcript.text.done` → `BatchStreamEvent::Result` with
   **hardcoded `Vec::new()` for words**. The parsed event reaches the
   accumulator with text but no per-word timing.
2. [`apps/desktop/src/stt/useRunBatch.ts:120-123`](https://github.com/fastrepl/anarlog/blob/main/apps/desktop/src/stt/useRunBatch.ts#L120-L123)
   wraps the persist callback with `if (words.length === 0) return;`,
   which silently no-ops the entire `store.transaction(() => { ... })`
   block that would have written `transcript.json` and updated the
   in-memory store.

So **every** progressive-batch transcription succeeds at the HTTP
layer (`200 OK`, full transcript text in the response body) and is
then dropped client-side with no log entry, no error toast, no UI
change. The non-progressive path (`gpt-4o-transcribe-diarize`) doesn't
have this bug — it preserves words from the diarized JSON via
`convert_diarized_words` — but it's also subject to the
60-second [`BATCH_IDLE_TIMEOUT`](#the-60-second-hack-why-we-need-streaming-sse-at-all)
that breaks any audio whose ASR exceeds 60 seconds. There is **no Char
configuration** that yields both word persistence and arbitrary-length
audio.

We can't fix this from response shape: the SSE parser only handles
`transcript.text.delta` and `transcript.text.done`, and `Vec::new()`
is hardcoded one layer down. There is no segment event type that
threads words into the persist callback.

**Workaround we ship:** [`char_persist.py`](char_persist.py) detects
this scenario in our ASR server and atomically writes
`transcript.json` directly to the matching Char session dir, bypassing
Char's persist callback entirely. Char's TinyBase persister registers
`watchPaths: ['sessions/']`
([`multi-table-dir.ts:80`](https://github.com/fastrepl/anarlog/blob/main/apps/desktop/src/store/tinybase/persister/factories/multi-table-dir.ts#L80))
so the file is auto-loaded. We identify the right session by
SHA256-hashing the uploaded audio and matching against
`<session_uuid>/audio.mp3` — the only stable join key, since Char's
multipart form doesn't include the session ID.

The full call is loopback-only (we touch nothing outside the user's
own `~/Library/Application Support/hyprnote` dir), atomic
(write-tempfile + rename), and idempotent (an existing transcript
ID is preserved). See
[README § The streaming-batch persistence bug](README.md#the-streaming-batch-persistence-bug-and-our-sidecar-workaround)
for the user-facing summary and the audit-log format. Disable with
`CHAR_PERSIST=0` if you ever need the broken upstream behaviour for
repro / debugging.

A non-invasive upstream fix would be a four-line change at the parser
boundary: thread word-level data from the diarized response variant
through the `transcript.text.done` arm — or, even simpler, drop the
`words.length === 0` short-circuit in `useRunBatch.ts` so an
empty-words text-only result still creates a transcript row. Worth
filing on `fastrepl/anarlog` as it affects every custom-base-URL
user, not just us.

---

## What our local-stack avoids — and how

When you run `./run.sh configure-char`, this repo rewrites the four
keys documented in [§ How the integration works](README.md#how-the-integration-works-aka-the-hack):

```
ai.current_stt_provider = "openai"
ai.current_stt_model    = "gpt-4o-transcribe"
ai.stt.openai.base_url  = "http://127.0.0.1:8000/v1"     ← key shim
ai.stt.openai.api_key   = "local"
```

That redirects **the entire batch transcription path** away from
`api.openai.com` to our local Parakeet server. With Live recording
configured at the *Custom (Deepgram)* provider pointing to the same
loopback origin, **no audio bytes ever leave the machine.**

Verified by:

```bash
# Mid-recording, Char's only outbound TCP connections that contain audio:
$ lsof -nP -i -P | rg 'char.*->'
char  ...  TCP  10.x.x.x:NNNN -> 127.0.0.1:8000  (ESTABLISHED)
```

…and nothing to `api.openai.com`, `api.deepgram.com`, etc. The
control-plane endpoints (`desktop2.hyprnote.com`, `us.i.posthog.com`,
`o4506190168522752.ingest.us.sentry.io`) may still appear, but they
carry no audio.

The summary path (LM Studio → Qwen) goes through the
`ai.intelligence` settings which Char's UI controls (one tab in
Char's Settings → Intelligence; we don't auto-write it because it
isn't cleanly addressable from outside the app yet). Once you point
that at `http://127.0.0.1:1234`, transcripts and notes also stay
local.

---

## Mitigations

### What `configure-char` already does for you

After you run `./run.sh configure-char`, your `store.json` will
contain:

```json
{
  "analytics": "{\"Disabled\":true}"
}
```

…meaning Char's PostHog analytics path is short-circuited at the
`if self.is_disabled()` check inside `tauri_plugin_analytics`. No
events sent. (If your install predates this script change, run
`./run.sh configure-char` once to write the flag — it's idempotent.)

### What you should also block at the network layer

Sentry and the auto-updater have **no in-app toggle**. Block them at
your firewall — most macOS users have one of:

- **[Little Snitch](https://www.obdev.at/products/littlesnitch/index.html)**: add deny rules for these hosts:
  ```
  o4506190168522752.ingest.us.sentry.io
  *.ingest.us.sentry.io
  *.ingest.de.sentry.io
  browser.sentry-cdn.com
  desktop2.hyprnote.com
  gateway.scarf.sh
  ```
- **[LuLu](https://objective-see.org/products/lulu.html)** (free): same hostnames; LuLu blocks at the process layer so block all of these for `char` and `char-cli`.
- **[Stock pf firewall](https://www.openbsd.org/faq/pf/)** in `/etc/pf.conf`: a process-level deny isn't natively supported, so use the `/etc/hosts` trick instead.

### Hosts-file shortcut (no firewall app needed)

```bash
sudo tee -a /etc/hosts > /dev/null <<'EOF'
# local_scribe: block Char's telemetry / auto-update endpoints
0.0.0.0 desktop2.hyprnote.com
0.0.0.0 gateway.scarf.sh
0.0.0.0 o4506190168522752.ingest.us.sentry.io
0.0.0.0 us.i.posthog.com
0.0.0.0 eu.i.posthog.com
0.0.0.0 browser.sentry-cdn.com
EOF
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

After this, `dig us.i.posthog.com` should return `0.0.0.0` and Char's
telemetry attempts will fail-fast at TCP connect with no traffic
leaving the machine. Sentry/PostHog client libraries are designed to
fail silently in this case (they retry briefly then drop the queue).

The downside of `0.0.0.0` redirection is per-app granularity is lost —
if some other tool you trust *does* use Sentry / PostHog, those will
also be blocked. Use Little Snitch / LuLu if you need per-process
control.

### Verifying the block works

```bash
# while Char is running:
sudo tcpdump -i any -n 'host desktop2.hyprnote.com or host us.i.posthog.com or host o4506190168522752.ingest.us.sentry.io' -c 20
# Expected: 0 packets captured
```

Or live with `lsof`:

```bash
watch -n 2 'lsof -nP -i -P 2>/dev/null | rg -i char'
```

…and confirm the only TCP destinations are `127.0.0.1` (LocalScribe
ASR + LM Studio) plus whatever calendar/integration provider you
explicitly connected.

---

## Verdict per data class

| data | leaves the machine? | endpoint | mitigation in this repo |
|---|---|---|---|
| Audio (`audio.mp3`) | No | `127.0.0.1:8000` (loopback) | inherent — base_url shim |
| Transcripts | No | written from local response | inherent |
| Generated notes / summaries | No | written from local LM Studio | inherent |
| Calendar events / meeting links | Only if connected | `api.char.com` | document; user controls |
| Stable hashed `IOPlatformUUID` (Sentry) | **Yes by default** | `o4506...sentry.io` | document + recommend firewall |
| Stable hashed `IOPlatformUUID` (PostHog) | **Yes until disabled** | `us.i.posthog.com` | `configure-char` writes the disable flag |
| App version + arch (auto-update) | **Yes by default** | `desktop2.hyprnote.com` → `gateway.scarf.sh` | document + recommend firewall / hosts override |
| Crash minidumps | Only on crash, by default | Sentry | same as Sentry |
| Sentry `tracing` spans (op metadata) | **Yes by default** at `traces_sample_rate=1.0` | Sentry | same as Sentry |

The data plane (audio + transcripts + notes) is **clean** — verified
by code-reading + live `lsof`. The control plane (telemetry +
auto-update) **is not clean by default** but *can be* fully muted with
the in-app toggle (PostHog) plus a firewall / hosts-file rule for the
two non-toggleable channels (Sentry + Tauri updater).

---

## Open questions / re-audit triggers

Items left for a future review (also tracked in [TODO.md](TODO.md)):

- **`tauri_plugin_updater2`**: a second updater plugin loaded after
  the standard one. No source in the public repo; the strings dump
  doesn't reveal a separate endpoint. Worth inspecting whether it
  introduces another network channel.
- **`tauri_plugin_activity_capture`**: name suggests local activity
  monitoring (window focus / keystroke counts?). Source in
  `plugins/activity-capture/`. Verify it doesn't emit events
  containing window titles or keystroke metadata, even with PostHog
  disabled.
- **`tauri_plugin_messenger`** / **`tauri_plugin_detect`**: undocumented
  scope; verify local-only behaviour.
- **`cloudsync.sqlite.ai`**: referenced as a string but not observed
  in live traffic. Confirm it's only invoked by an explicit user
  action (likely a future opt-in cloud sync feature).
- **Cactus VLM**: bundled vision-language model. When does Char
  invoke it? Screen analysis would require screen-recording permission;
  worth checking what triggers it and whether it could leak frame
  contents on a crash (via Sentry minidumps).
- **The `char-chrome-native-host` binary**: which Chrome extension
  registers it, and what protocol does it speak? (Native messaging
  apps can be a lateral-movement vector.)

When `CHAR_KNOWN_GOOD_VERSION` in `run.sh` is bumped, the
[§ Methodology](#methodology) section is the checklist for re-running
this audit.
