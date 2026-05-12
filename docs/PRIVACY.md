# Privacy and data locality

> Moved from the top-level README on 2026-05-12 as part of the
> condense-and-link pass. The content below is the canonical
> privacy + data-locality reference for `local_scribe`. The README
> now keeps a one-paragraph teaser plus a link back here.
>
> **Related docs:**
>
> - [`SECURITY.md`](../SECURITY.md) — cross-layer threat model + the
>   continuous-audit checklist. The single document covering the
>   firewall, per-service auth, at-rest vault, YubiKey escrow,
>   Char-settings enforcement, and the third-party audit
>   methodology.
> - [`docs/SECURITY_AUDIT.md`](SECURITY_AUDIT.md) — claim → code →
>   test traceability matrix.
> - [`docs/KEY_SAFETY.md`](KEY_SAFETY.md) — full enumeration of
>   data-loss scenarios + recovery flowcharts.
> - [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — diagrams for the
>   data plane + every defense layer.
> - [`docs/CHAR_REVIEW.md`](CHAR_REVIEW.md) — the third-party
>   threat model for Char.app specifically.

The whole reason this stack exists: every recording, transcript, and
summary lives only on your laptop's disk, processed by models that run
locally on Apple Silicon. There is no "send-to-cloud" toggle hiding
somewhere that could flip on. Once `bootstrap` finishes pulling code
and models, you can disable Wi-Fi and the pipeline keeps working.

> **Hard prerequisite: System Integrity Protection must be fully
> enabled.** `local_scribe` refuses to start (no operator override)
> on any macOS host where `csrutil status` reports anything other
> than `enabled.`. Without SIP, the kernel can't enforce the
> user-space process boundaries every other defense in the project
> relies on — `task_for_pid()`, `DYLD_INSERT_LIBRARIES`, `dtrace
> -p`, replacing `/usr/bin/codesign`, NVRAM-set `boot-args` — and
> the master key in our process memory becomes trivially
> exfiltrable. Verify with `csrutil status` (or `./venv/bin/python
> -m sip_check status`); fix by booting to Recovery, running
> `csrutil enable`, and rebooting. Full rationale:
> [SECURITY.md § Defense layer 0](../SECURITY.md#defense-layer-0--system-integrity-protection-mandatory).

## What stays local

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
[`.gitignore`](../.gitignore) explicitly excludes every audio extension we
know about (`.mp3`, `.m4a`, `.wav`, `.ogg`, `.flac`, `.aac`, `.opus`,
`.aiff`, `.webm`, `.mp4`, `.mov`, `.mkv`) and every transcript /
summary / diarized output (`*.transcript.{json,txt,md}`,
`*.summary.{md,txt}`, `*.diarized.{txt,md}`, `out/`, `outputs/`), so
accidentally `git add`'ing audio or notes from this repo just doesn't
work.

## What crosses the network — and when

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
[`docs/INTEGRATION.md`](INTEGRATION.md) for why Char ends up on
`127.0.0.1` despite thinking it's calling OpenAI's Whisper API and
Deepgram.

## What you still have to trust

Being honest about the parts of the stack that aren't ours:

- **LM Studio.app is closed-source.** It collects basic usage analytics
  by default (app-level metadata; you can opt out under Settings →
  Telemetry). The Qwen3 model itself runs entirely locally; LM Studio
  doesn't transmit your chat content. Disabling LM Studio's telemetry
  by default at bootstrap is on the [TODO](../TODO.md).
- **Char.app is open source** ([source](https://github.com/fastrepl/anarlog),
  MIT-licensed). The full audit lives in [CHAR_REVIEW.md](CHAR_REVIEW.md);
  the short version: the data plane (audio / transcripts / notes) stays
  local, but Char ships with **Sentry crash reporting and PostHog product
  analytics enabled by default**, plus a Tauri auto-updater that polls
  `desktop2.hyprnote.com`. `./run.sh configure-char` writes the in-app
  PostHog kill-switch (`store.json::analytics.Disabled = true`); Sentry
  and the auto-updater have **no in-app toggle** and are blocked at the
  network layer by the outbound-firewall feature
  ([§ Outbound firewall](#outbound-firewall-per-char-by-default) below).
  Calendar / event sync (if you connect a calendar) does talk to your
  calendar provider — orthogonal to recordings, but worth knowing.
- **`asr_server.py` currently binds to `0.0.0.0:8000`**, not
  `127.0.0.1:8000`. macOS's firewall blocks incoming connections by
  default, but if you've allowed Python through the firewall and you're
  on a public Wi-Fi, in principle a peer on the same network could hit
  the endpoint. Even so, **every endpoint other than `/health` now
  requires a per-service bearer token** (see
  [§ Service authentication](#service-authentication) below) so a
  non-local probe gets a 401 rather than access to the API. Tightening
  the default bind to loopback (with an explicit `BIND_ALL=1` opt-in
  for "I want this reachable from another machine on my LAN") is a
  [TODO.md](../TODO.md) item — meanwhile, run on trusted networks or
  behind a firewall.
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

## Verified end-to-end

The full reproducible check-list lives in
[SECURITY.md § Verified end-to-end](../SECURITY.md#verified-end-to-end-last-full-check).
Headline numbers from the most recent run:

- `pytest -q` → **498 passed, 13 subtests** (auth gate, typed-DELETE
  confirm body, argv-leak invariant, Option C lifecycle, firewall
  round-trip, char_settings_writer, transcript-history…).
- ASR `/v1/audio/transcriptions` — 401 without bearer, 401 with wrong
  bearer, 200 with correct bearer; `/health` stays open for liveness.
- Inspector — 401 on `/api/*` without auth, 302 + `HttpOnly;
  SameSite=strict` cookie from `/auth?token=…`, attachment headers on
  the audio + `.txt` transcript download endpoints.
- `DELETE /api/sessions/{id}/audio` and `DELETE /history/{name}` —
  both require `{"confirm":"DELETE"}` in the JSON body in addition to
  the bearer cookie/header. Empty body → 400 (file untouched), wrong
  word → 400 (file untouched). A stolen bearer alone is not enough to
  destroy data.
- `ps auxww` over the live ASR + inspector + `run.sh configure-char`
  processes — no ASR token, no inspector token, no master-key hex on
  any argv.

## Service authentication

Loopback-bind alone isn't enough: anyone who lands a shell on the Mac
(malicious browser extension making CORS requests, a different user on
the same machine, a Tauri app that isn't Char) can `curl
http://127.0.0.1:8000/v1/audio/transcriptions` and start submitting
audio or scraping the inspector's session list. Since v0.5 every
exposed endpoint requires a **per-service bearer token** derived from
the Keychain master key.

### The model

There is one root secret: a 256-bit AES master key, but it is **never
stored whole**. It is split into two halves (Option C — see
[§ Master key management](#master-key-management-option-c-touch-id-and-yubikey)
below for the full lifecycle):

- `kc_half` — 32 random bytes in the macOS Keychain under
  `service=local_scribe / account=master_key_kc_half_v2` with
  `.userPresence` ACL (Touch ID, passcode fallback).
- `yk_half` — 32 random bytes encrypted with `age` to one or more
  enrolled YubiKeys (`touch-policy=always`), stored at
  `~/.config/local_scribe/yk_half.age`.

Either half on its own is uniform random and reveals nothing about
the master. Unlocking requires **both** factors in the same shell
session; the reconstituted master sits in process memory only for
the duration of the unlock + token derivation.

From that single root, each service derives its own bearer token via
HKDF-SHA256:

```
kc_half (Keychain, Touch ID)   yk_half (age + YubiKey tap)
        │                              │
        └────────── XOR ───────────────┘
                    │
            master_key (32 bytes, in process memory only)
                    │
    ├─ HKDF(info=b"service:asr") ───────► ls_asr_<32hex>        ◄── ASR :8000
    ├─ HKDF(info=b"service:inspector") ─► ls_inspector_<32hex>  ◄── Inspector :8001
    └─ HKDF(info=b"service:future") ───►  ls_future_<32hex>     ◄── future services
```

Why HKDF instead of separately-stored tokens:

- **One root to manage.** Rotate it (`./run.sh key rotate`) and every
  per-service token rolls in lockstep with no extra ceremony.
- **Deterministic.** Same master key → same tokens. Char's saved
  OpenAI `api_key` stays valid across server restarts / vault
  remounts.
- **No extra ciphertext on disk.** There's nothing for an attacker to
  scrape — the tokens only exist in the running server's memory and
  in Char's `settings.json` (the latter is written via stdin to
  `python -m char_settings_writer`, never via argv).

### How the gate enforces it

Every gated endpoint demands a token via any of these headers
(checked constant-time):

```
Authorization: Bearer ls_asr_<token>     # OpenAI clients, Char's batch
Authorization: Token  ls_asr_<token>     # Deepgram clients, Char's live
X-API-Key:            ls_asr_<token>     # curl-friendly
?api_key=<token>                         # query-string fallback
Cookie: ls_inspector=<token>             # inspector browser (set by /auth)
```

Endpoints that **stay open** by design:

- `GET /health` on the ASR server — liveness probe used by
  `./run.sh status` and `./run.sh doctor`.
- `GET /api/health` on the inspector — same role.
- `GET /` on the inspector — the SPA HTML loads without auth so the
  browser can render the "click here to authenticate" prompt before
  the cookie has been set. The page exposes no session data.
- `GET /auth?token=…` on the inspector — the cookie-setting handshake.

Everything else returns **HTTP 401** with a `WWW-Authenticate`
header if the token is missing or wrong.

### Where each client gets its token

| Client | How it sends the token |
|---|---|
| **Char** (file Generate + live recording) | `./run.sh configure-char` writes the ASR token into `ai.stt.openai.api_key`. Char sends it as `Authorization: Bearer …`. |
| **transcribe_file.py** (CLI) | Prompts Touch ID on first run, derives the ASR token in-process, sends as `Authorization: Token …`. |
| **redo_session.py** (CLI) | Same. |
| **Browser** (inspector UI) | Visits `http://127.0.0.1:8001/auth?token=…` once — `./run.sh status` prints the full URL. Cookie persists 30 days (HttpOnly + SameSite=Strict). |
| **`curl` (you)** | Same headers as any other client. Run `./venv/bin/python -m service_auth token asr` to print the current token. |

### One-shot operations

```bash
./run.sh status                     # prints token fingerprints + inspector auth URL
./run.sh doctor                     # full health + drift report
./run.sh configure-char             # rewrite Char's settings.json with current ASR token
./venv/bin/python -m service_auth token asr           # print ASR token (prompts Touch ID)
./venv/bin/python -m service_auth fingerprint asr     # safe-to-log first 6 hex chars
./venv/bin/python -m service_auth url inspector       # clickable browser auth URL
```

### Token rotation

Rotating a per-service token = rotating the master key
(`./run.sh key rotate` re-randomises both halves; tokens are
re-derived from the new master). After a rotation:

1. Restart the services so they pick up the new derivations:
   `./run.sh restart`.
2. Re-run `./run.sh configure-char` so Char's saved `api_key` matches
   the new ASR token. (`./run.sh doctor` flags drift loudly if you
   forget.)
3. Reopen the inspector with the new auth URL printed by
   `./run.sh status` — the old cookie is silently ignored.

### What this defends — and what it doesn't

It defends against:

- A malicious browser extension making CORS requests to
  `http://127.0.0.1:8000`. Without the token it gets 401.
- A second user on the same Mac (different UID) probing your
  loopback. Same 401.
- An attacker who's gained code execution as your user but does
  **not** have a Touch ID session yet — they can read tokens out of
  `ps -E` env vars (we don't pass tokens through env) or process
  memory, **but only after physically tapping the sensor**. The
  Keychain item itself is unreadable without `.userPresence`.

It does **not** defend against:

- An attacker who has compromised your user account *and* successfully
  prompts Touch ID by impersonating one of our binaries. macOS doesn't
  pin the prompt to a specific process. This is the soft underbelly
  of any "TouchID-gated Keychain item" — defense-in-depth needs full
  app sandboxing, which we don't have on a non-Mac-App-Store install.
- An attacker with root. Root reads everything.

### Bypass for CI / scripted tests

`LOCAL_SCRIBE_DISABLE_AUTH=1` short-circuits every check. The servers
log a loud warning on startup when this is active. Never set this in
production; only used for the pre-auth-era test suite and for
unattended bootstrap automation.

### Dev mode — explicit, loud SIP-gate bypass for development

`LOCAL_SCRIBE_DEV_MODE=1` (or `./run.sh start --dev`) is the one
documented operator override of the System Integrity Protection
gate. Production operators must never set it. Concretely it lets
the pipeline start on a host where SIP is off, partially off, or
unverifiable — at the cost of the kernel boundary that normally
keeps the reconstituted master key out of cohabiting processes'
heaps.

The bypass surfaces on every UI:

- `./run.sh sip_gate` prints a coloured `[DEV MODE] sip_gate
  bypassed` line on every gated verb.
- `./run.sh doctor` shows a four-line red block at the top of
  its output.
- `./run.sh status` and `python -m local_scribe status` print a
  `[DEV MODE]` marker above the service table.
- The ASR + Inspector services emit the full red banner once per
  process to their log + a `WARNING`-level log line.
- The inspector web UI renders a **sticky, non-dismissible red
  banner across the top of every page**, pulsing slowly,
  driven by the unauthenticated `GET /api/dev_mode/status`
  endpoint (so the banner shows even on the `/auth`
  cold-landing view before any token is typed in).

Dev mode bypasses *only* the SIP gates. Every other layer
(script integrity, Char integrity, pinned-config HMAC,
service-auth bearer tokens, master-key unlock via Touch ID +
YubiKey) still applies in full. The full threat-model walkthrough
(what dev mode actually costs you, what the strict-no-matter-what
caller is, and how to exit dev mode) is in [SECURITY.md § 'Dev
mode'](../SECURITY.md#dev-mode--explicit-sip-bypass-for-development).

### Threat-model summary

The full table — assets, adversaries, capabilities — lives in
[CHAR_REVIEW.md § Threat model](CHAR_REVIEW.md#threat-model), and
the cross-layer security posture document is
[SECURITY.md](../SECURITY.md).

## Outbound firewall (per-Char, by default)

Loopback + bearer-token auth defends our *own* services. The
**outbound** problem is different: Char has three always-on channels
with no in-app toggle — its Sentry DSN (panic + 100%-rate tracing),
the Tauri auto-updater (`desktop2.hyprnote.com` proxied through
Scarf), and the Sentry browser CDN — plus a long catalog of external
STT/LLM provider plugins (`api.openai.com`, `api.deepgram.com`,
`api.anthropic.com`, …) that a settings drift could silently
re-enable.

### Why a custom proxy?

macOS doesn't ship a CLI-installable per-app outbound firewall.

- The **macOS Application Firewall** (System Settings → Network →
  Firewall) is **inbound-only**.
- **`pf`** supports per-user rules but not per-app — Apple's port
  stripped FreeBSD's `pid` / `binary` keywords.
- **Network Extension** (`NEContentFilterProvider`, what Little
  Snitch / LuLu use) is the right answer, but it requires an
  Apple-granted entitlement that an open-source repo can't ship.
- **`sandbox-exec`** can deny network egress but only by IP, not
  hostname (DNS rotation defeats hostname pins).

So we compose the two primitives Apple **does** give us into a
per-Char egress filter:

1. **Containment** — `sandbox-exec` restricts Char's network reach to
   loopback only.
2. **Policy** — Char's `HTTPS_PROXY` env var points at a local
   asyncio CONNECT proxy that consults `firewall.BLOCK_CATALOG` and
   refuses blocked hostnames with `403`.

Together, Char's only network path is the local proxy, and the
proxy enforces our hostname-level allow/deny rules. **Other apps on
the same Mac are completely unaffected.**

This is the **default**. The legacy machine-wide `/etc/hosts` mode
is still available as `--mode system` for operators who explicitly
want it.

### Block catalog

| category | default | example hosts | rationale |
|---|---|---|---|
| `telemetry` | **on** | `o4506190168522752.ingest.us.sentry.io`, `us.i.posthog.com`, `desktop2.hyprnote.com`, `gateway.scarf.sh` | no in-app toggle exists — block at the network layer or accept the leak |
| `providers` | **on** | `api.openai.com`, `api.deepgram.com`, `api.anthropic.com`, `api.mistral.ai`, … | fail-safe — if a settings change ever re-points STT/LLM off-loopback, the connection fails fast instead of silently exfiltrating |
| `char_cloud` | off | `api.char.com`, `cloudsync.sqlite.ai` | Char's hosted backend for calendar OAuth + integrations. Off by default so calendar sync keeps working; opt in with `--strict` |

### Operator commands

```bash
# Default per-Char mode (no sudo)
./run.sh start                    # also starts the egress proxy on :8889
./run.sh char launch              # launches Char under sandbox-exec + HTTPS_PROXY
./run.sh char firewall-status     # is the proxy up? is Char going through us?
./run.sh proxy verify             # send CONNECT api.openai.com:443; assert 403
./run.sh proxy recent             # last 20 DENY/ALLOW/ERROR decisions

# Inspect / configure
./run.sh firewall mode            # show effective mode + proxy port
./run.sh firewall list            # print the host catalog (no sudo)
./run.sh firewall verify          # DNS-probe every catalog host

# Opt-in machine-wide mode
./run.sh firewall enable --mode system    # asks for admin password
./run.sh firewall disable --mode system   # asks for admin password
```

`./run.sh bootstrap` (step 10/10) writes + validates the SBPL profile
and prints how to launch Char. It does **not** ask for sudo — the
system-hosts mode is left as an explicit opt-in. The egress proxy
auto-starts alongside the ASR + Inspector services on every
`./run.sh start`. `./run.sh doctor` reports both layers (proxy
running? sandbox profile valid?) plus the system-hosts state if it
is also installed.

### Caveat: Dock / Spotlight launches bypass the firewall

This is the trade-off of the wrapper-based approach. A Char
launched from the Dock inherits neither `sandbox-exec` nor the
`HTTPS_PROXY` env, so its traffic is **not** filtered.
`./run.sh char firewall-status` and `./run.sh doctor` both detect
and flag this; the only mitigation is to kill the bypassed process
and relaunch via `./run.sh char launch`. A future Network Extension
build signed under our own Developer ID would close this gap (see
[TODO.md](../TODO.md)).

### Removal

- **Process mode**: `./run.sh stop` (stops the proxy). The sandbox
  profile is harmless on disk; delete it with
  `rm ~/.config/local_scribe/char.sb` if you want.
- **System mode**: `./run.sh firewall disable --mode system`
  (clean, asks for admin password) or `sudo $EDITOR /etc/hosts`
  (delete the lines between the `>>> local_scribe firewall` and
  `<<< local_scribe firewall` markers). Either way the backup at
  `/etc/hosts.local_scribe.bak.<latest>` is the pre-change
  reference for diffs.

Catalog source of truth: [`firewall.py`](../local_scribe/egress/firewall.py) →
`BLOCK_CATALOG`. Full rationale + per-host reasons + threat model
in [SECURITY.md § Defense layer 1](../SECURITY.md#defense-layer-1--network-egress-firewall).

## Air-gap mode

Once `./run.sh bootstrap` reports success and `./run.sh doctor` is all
green, you can disable Wi-Fi + Bluetooth and the pipeline keeps working
indefinitely: live recording, batch Generate, summaries, all of it.
Bootstrap downloads are the only network dependency.

## Full security policy

The cross-layer threat model — what we're defending, against whom,
which module enforces what, and how to verify it all on your own
machine — lives in [SECURITY.md](../SECURITY.md). Read that for the
single document covering the firewall, per-service auth, at-rest
vault, YubiKey escrow, Char-settings enforcement, the third-party
audit methodology, and the continuous-audit checklist.

## Master key management (Option C: Touch ID **and** YubiKey)

The master key that every other secret in the system is derived from
lives behind **two factors**: a Keychain item (Touch ID-gated) and a
YubiKey-encrypted age file. **Both are required** to unlock; either
factor on its own yields uniform-random bytes via the XOR
construction. See [SECURITY.md § Defense layer 4](../SECURITY.md#defense-layer-4--option-c-split-key-touch-id-and-yubikey)
for the construction details and threat-model invariants, and
[ARCHITECTURE.md §4](ARCHITECTURE.md#4-at-rest-encryption--option-c-split-key-implemented)
for the diagram.

Operator commands:

```bash
./run.sh key init                 # first-time setup; enroll YubiKey + DR backup
./run.sh key status               # JSON snapshot; no Touch ID / no YubiKey
./run.sh key unlock               # smoke test; prints token fingerprints
./run.sh key rotate               # fresh master + halves; typed ROTATE + YK tap + auto-snapshot
./run.sh key add-yubikey RECIP    # enroll a second YubiKey (paste its age recipient)
./run.sh key dr-restore           # recover via passphrase; auto-detects live v2 + typed gate
./run.sh key migrate              # walk a legacy v1 install over to v2 (idempotent)
./run.sh key destroy              # typed DESTROY + YK tap + auto-snapshot (reversible)
./run.sh key destroy --purge-everything  # typed DESTROY *and* PURGE-EVERYTHING — irreversible
./run.sh key backups list         # list pre-flight snapshots written before destructive ops
./run.sh key backups prune <id>   # delete one snapshot (typed DELETE)
./run.sh key backups restore-kc-half <account>   # roll back kc_half from a backup account
```

**The pipeline refuses to start without a master key.** `./run.sh
start` checks the Keychain for a `kc_half` (Option C) or a legacy v1
whole-key item before any service comes up; absent both, it prints a
red banner pointing at `./run.sh bootstrap` (first install) or
`./run.sh key dr-restore` (recovery) and exits non-zero. This is the
master-key start-guard — there is no override, because starting with
no master key would mean services come up with no bearer-token auth
and on-disk artefacts would be unencrypted.

**Every destructive op is two-factor + reversible by default.** A
YubiKey tap is required to prove physical possession before any
state changes, and a pre-flight snapshot of the about-to-be-replaced
material is written to `~/.config/local_scribe/key-backups/<ts>-<op>/`
so the operation can be rolled back. Snapshots are NEVER auto-pruned
— see [`KEY_SAFETY.md`](KEY_SAFETY.md) for the full enumeration of
data-loss scenarios and recovery flowcharts.

All passphrases are read from `/dev/tty` (no echo, never on argv).
All master-key bytes flow via Keychain ACL → stdin → in-process
buffers — never argv, never env, never logs.

## Encrypted vault (AES-256 sparse bundle, master-key-derived)

The canonical at-rest container for Char's session data and our
transcripts is an `hdiutil` AES-256 sparse bundle whose passphrase is
**HKDF-SHA256-derived from the master key** (label
`local_scribe.vault.passphrase.v1`). That means:

- The passphrase is never written to disk and never shown to the
  operator. It lives in process memory between
  `key_lifecycle.unlock_master_key()` and the `hdiutil -stdinpass`
  call.
- Unlocking the vault is the same operation as unlocking the master:
  Touch ID **and** a YubiKey tap.
- Rotating the master key (`./run.sh key rotate`) re-keys the vault
  envelope automatically via `vault.rotate_password(old, new)`.

`./run.sh bootstrap` creates the vault and relocates Char's data dir
into it as part of the default-install flow. The vault subcommands:

```bash
./run.sh vault init                # create the AES-256 sparse bundle (one-time)
./run.sh vault unlock              # mount + relocate Char's data into the vault
./run.sh vault unlock --no-relocate  # mount only (don't move Char's data)
./run.sh vault lock                # detach the mounted volume
./run.sh vault status              # JSON snapshot (no prompts)
```

## YubiKey operator surface

The full lifecycle (`init`, `rotate`, `add-yubikey`, etc.) lives under
`./run.sh key …`. The `./run.sh yubikey` subcommand is the smaller
convenience surface for tap-test and backup-restore work:

```bash
./run.sh yubikey status            # JSON: tools, key inserted, enrollment, recipient count
./run.sh yubikey list              # one line per enrolled age recipient
./run.sh yubikey enroll            # generate identity on inserted YubiKey
                                   #   - if no master yet: chains into `key init`
                                   #   - if master exists: prints recipient + tells you
                                   #     to run `key add-yubikey <recipient>` (backup key)
./run.sh yubikey verify            # round-trip tap test: decrypts yk_half.age
./run.sh yubikey restore <snap>    # re-instate yk_half.age from a key-safety snapshot
                                   #   (typed RESTORE confirmation required)
```

## Future privacy work

See [TODO.md](../TODO.md) for planned hardening — vault auto-purge
policies, a `./run.sh wipe` command for one-shot panic-mode rotation,
and the planned multi-tenant / org confidential-compute deployments
backed by AWS Nitro Enclaves + CloudHSM-managed attestation keys.
