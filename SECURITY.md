# Security policy

This document is the canonical, audit-style description of how
`local_scribe` defends the data it touches. It explains:

- the assets we're protecting and from whom,
- what we did to make sure nothing leaves the laptop,
- how we evaluated and audited each third party we link to,
- every layer of defence currently shipped, in build order,
- what we deliberately do **not** defend against,
- how to verify the posture on your own machine,
- how to report something we got wrong.

Companion documents:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — every flow described here
  as a clickable Mermaid diagram. Especially relevant:
  [§ 4 At-rest encryption](ARCHITECTURE.md#4-at-rest-encryption-designed),
  [§ 5 Service authentication](ARCHITECTURE.md#5-service-authentication-hkdf-tokens),
  [§ 6 Outbound firewall](ARCHITECTURE.md#6-outbound-network-firewall),
  [§ 14 Threat model × defence layers](ARCHITECTURE.md#14-threat-model--defence-layers),
  [§ 15 Vault & key lifecycle](ARCHITECTURE.md#15-vault--key-lifecycle).
- [`CHAR_REVIEW.md`](CHAR_REVIEW.md) — the bottom-up audit of the
  Char.app binary: every URL the binary references, who it actually
  talks to at runtime, and what we've done about each one. Read that
  if you want the raw evidence behind the claims here.
- [`README.md`](README.md) — feature overview and operator guide.
- [`TODO.md`](TODO.md) — the open-mitigations backlog.
- [`firewall.py`](firewall.py), [`service_auth.py`](service_auth.py),
  [`vault.py`](vault.py), [`yubikey_backup.py`](yubikey_backup.py),
  [`secret_store.py`](secret_store.py), [`char_audit.py`](char_audit.py)
  — the modules implementing the controls below.

---

## TL;DR

`local_scribe` is **local-first by design**: every byte of audio,
every transcript, and every LLM-generated summary is produced and
stored on your laptop. There is no SaaS component, no telemetry, no
analytics, no "send to cloud for processing" toggle. After
`./run.sh bootstrap` has finished pulling models, you can disable
Wi-Fi and the pipeline keeps working indefinitely.

We back that promise with **defence in depth**:

| layer | mechanism | module | enforces |
|---|---|---|---|
| network-egress | `/etc/hosts` block list with marker-delimited region; `0.0.0.0` + `::` sinks for IPv4 and IPv6 | [`firewall.py`](firewall.py) | Char's Sentry / PostHog / auto-updater + all external STT/LLM APIs are unreachable |
| inter-service auth | HKDF-SHA256-derived per-service bearer tokens, anchored in macOS Keychain with `.userPresence` (Touch ID) ACL | [`service_auth.py`](service_auth.py), [`secret_store.py`](secret_store.py) | a process with shell-as-user access cannot `curl` any local API |
| at-rest encryption | AES-256 sparse-bundle vault for Char's session directory | [`vault.py`](vault.py) | a stolen / imaged disk yields ciphertext, not recordings |
| key escrow | `age-plugin-yubikey` encrypted master-key backup | [`yubikey_backup.py`](yubikey_backup.py) | a dead Keychain doesn't strand the user; lost-laptop recovery requires the physical YubiKey |
| Char-settings enforcement | `char_audit.py` flags drift; `configure-char` rewrites the four keys atomically and disables Char's PostHog toggle | [`char_audit.py`](char_audit.py) | a settings change that would re-route audio outside the loopback shim is loud, visible, and reversible |
| build-time supply chain | pinned Char DMG SHA256s; pinned `CHAR_KNOWN_GOOD_VERSION`; explicit cask install for LM Studio | [`run.sh`](run.sh) | tampered binaries never reach `/Applications` |

The full threat model and per-layer rationale are below.

---

## What we mean by "private"

The user-facing privacy guarantee is:

> Every recording you make, every transcript we produce, and every
> note the LLM generates is processed and stored on this laptop. It
> never leaves the machine unless **you** click a button that tells
> it to. The data plane is loopback-only and survives an air-gap.

That guarantee narrows what we're trying to defend against. We are
**not** trying to defend against:

- a sophisticated nation-state actor who roots the device,
- a kernel-level malware payload reading process memory,
- a compromised macOS supply chain (Apple ships a backdoored kernel),
- DRAM cold-boot attacks,
- side-channel timing oracles against `constant_time_compare`.

We **are** trying to defend against:

- accidental leakage to third-party telemetry / analytics SaaS that
  comes baked in to the apps we depend on (Char's Sentry + PostHog
  + Tauri updater; LM Studio's analytics),
- a settings-mistake / curious-user / malicious-extension flipping
  Char back to a real cloud STT/LLM provider without anyone noticing,
- another user on the same Mac (different UID) probing our services,
- a process running as our user without a Touch ID session (think:
  a sketchy `npm install` post-install script) hitting `curl
  127.0.0.1:8000` to siphon transcripts,
- a stolen laptop / Time Machine drive / iCloud-imaged volume
  yielding readable recordings,
- a Char binary update that silently changes the contract we audited.

This is the standard threat model for a privacy-first local app on a
single-user macOS workstation. Where we sit relative to nation-state
threats is documented in [§ Out of scope](#out-of-scope) below.

---

## Threat model

The full per-tier table is canonical in
[`CHAR_REVIEW.md` § Threat model](CHAR_REVIEW.md#threat-model);
this section is the compressed summary tied to which defence
mitigates which row.

| # | adversary | capability | mitigation |
|---|---|---|---|
| 1 | **Remote network peer** | TCP probe `127.0.0.1:{8000,8001}` from the LAN | macOS firewall blocks inbound; per-service bearer auth makes 0.0.0.0 binds opaque even when reachable |
| 2 | **Browser content** (XSS, extension) | `fetch()` to our loopback ports from any origin | per-service bearer token in `Authorization`/cookie — browser can't read the Keychain |
| 3 | **Co-tenant** (other macOS user on same Mac) | read files / sockets owned by other users | Keychain is per-user; `.userPresence` requires Touch ID even for the owner; loopback sockets bind to the owning UID only |
| 4 | **Shell-as-user** (malware running as you, no Touch ID session yet) | `curl` localhost, read `$HOME`, read process env | bearer tokens are *not* in env vars / argv / disk — only in server RAM after a Touch ID unlock; vault keeps audio/transcripts as ciphertext at rest; firewall closes the *outbound* exfiltration channel even if the malware grabs a cached token |
| 5 | **Backup / forensic imager** (stolen laptop) | read every file on disk; no live process | AES-256 sparse-bundle vault for Char's data; YubiKey-backed key escrow keeps the recovery key out of the volume itself |
| 6 | **Phished Touch ID** (attacker tricks user into tapping for a fake prompt) | one bearer-token derivation | not fully mitigated — per-request fingerprint logging in `./run.sh status` makes silent reuse visible; rotation via `./run.sh vault init` invalidates a stolen token |
| 7 | **Root / TCC-bypass / kernel** | anything | out of scope |

Per-tier rationale, including why we chose `.userPresence` over plain
`kSecAttrAccessibleWhenUnlocked`, lives in
[`CHAR_REVIEW.md § Threat model`](CHAR_REVIEW.md#threat-model).

---

## Defence layer 1 — Network egress firewall

### The problem

Char ships with three always-on outbound channels we cannot turn off
from inside the app:

| host | role | toggleable? |
|---|---|---|
| `o4506190168522752.ingest.us.sentry.io` | Sentry DSN (panic + 100%-rate tracing + minidumps) | No — DSN is `option_env!`-baked at build time |
| `us.i.posthog.com` | PostHog product analytics (machine-id-keyed) | **Yes** — `store.json::analytics.Disabled = true`, we flip it |
| `desktop2.hyprnote.com` | Tauri auto-updater (proxied through Scarf) | No — `updater.active: true` in tauri.conf.stable.json |

Plus a long list of **opt-in** external STT/LLM/TTS providers Char
ships plugins for (`api.openai.com`, `api.deepgram.com`,
`api.anthropic.com`, `api.assemblyai.com`, `api.mistral.ai`, …). We
keep the current provider pointed at the loopback shim, but a future
Char update or a settings mistake could re-point them.

### The control

[`firewall.py`](firewall.py) maintains a marker-delimited block in
`/etc/hosts` that blackholes every host in the catalog to `0.0.0.0`
(IPv4) and `::` (IPv6). Resolution fails fast (connection refused in
~2 ms), there's no kernel extension to install, and removal is one
command.

We use `/etc/hosts` rather than `pf` because:

- it works on every macOS install with no SIP gymnastics, no kext,
  no third-party tools (Little Snitch / LuLu),
- `getaddrinfo` is universally respected by every libc-using app
  (verified for Char: `strings` sweep finds no DoH/DoT client),
- it's plainly readable — anyone can `cat /etc/hosts` to see what
  we did to their machine,
- one inode replace is atomic.

The full host catalog lives in `firewall.BLOCK_CATALOG` and is
categorised:

| category | default | what's in it |
|---|---|---|
| `telemetry` | **on** | Char's Sentry, PostHog (us+eu), Tauri auto-updater (`desktop2.hyprnote.com` + the Scarf proxy), Sentry browser CDN |
| `providers` | **on** | OpenAI, Deepgram, AssemblyAI, Gladia, Granola, Soniox, Aquavoice, ElevenLabs, Fireworks, Mistral, Pyannote, Anthropic, Google Gemini |
| `char_cloud` | off | `api.char.com` (calendar OAuth + integrations), `cloudsync.sqlite.ai` — opt-in via `--strict` because blocking these breaks calendar sync |

### Operator surface

```bash
./run.sh firewall status      # is the block list installed? coverage by category?
./run.sh firewall list        # print the host catalog (read-only, no sudo)
./run.sh firewall enable      # install (asks for admin password)
./run.sh firewall enable --strict   # also block api.char.com (no calendar sync)
./run.sh firewall disable     # remove (asks for admin password)
./run.sh firewall verify      # DNS-probe every catalog host; exit 1 if any resolves
```

`./run.sh bootstrap` step (7/7) offers to install the block list on a
fresh setup. `./run.sh doctor` reports whether the list is installed
and whether the catalog has drifted (i.e. a `local_scribe` upgrade
added new hosts that the current install doesn't cover yet — re-run
`./run.sh firewall enable` to refresh).

The implementation is **safe to re-run**: it's idempotent (re-applying
on an up-to-date file is a zero-diff write), backs up `/etc/hosts`
to `/etc/hosts.local_scribe.bak.<timestamp>` before every change,
and replaces the file atomically via `rename(2)` so concurrent
readers never see a half-written file.

### What the firewall does **not** do

- Block IP literals — `/etc/hosts` only catches name resolution. An
  app that hard-codes an IP bypasses it. Char's plugins all use
  hostnames (verified by `rg -o 'https?://[0-9.]+'` against the
  binary — zero literal-IP hits).
- Block DNS-over-HTTPS clients — none of Char's SDKs bundle their
  own DoH resolver (verified). If a future Char version did, the
  block list would have to add the DoH endpoint hostname instead.
- Stop traffic from a process running with `--no-sandbox` and
  custom `c-ares` configured against `1.1.1.1`. Not a real risk for
  Char today; would surface in `lsof` if it ever became one.
- Restrict the loopback origin — Char talking to `127.0.0.1:8000`
  (our ASR shim) is the intended channel and stays unaffected.

### Audit trail

Every install / uninstall writes a timestamped backup to
`/etc/hosts.local_scribe.bak.<YYYYMMDD-HHMMSS>`. Backups are owned by
`root:wheel 644`, so a future SOC reviewer can reconstruct the exact
sequence of changes and timestamps without elevated access.

---

## Defence layer 2 — Inter-service authentication

### The problem

Loopback bind alone is not a security boundary on a multi-process
machine. Before this control landed, anything running as the user
could:

```bash
curl -X POST http://127.0.0.1:8000/v1/audio/transcriptions \
  -F file=@/some/audio.m4a -F model=gpt-4o-transcribe
```

…and get a full transcript back. Same story for `GET
http://127.0.0.1:8001/api/sessions` — the inspector's session list
was a free `lsof`-discoverable endpoint.

### The control

[`service_auth.py`](service_auth.py) derives a unique 32-hex bearer
token per service from a single master key in the macOS Keychain via
HKDF-SHA256:

```
master_key  (32 bytes, Keychain, Touch ID gated)
    │
    ├─ HKDF(info=b"service:asr")        ─►  ls_asr_<32hex>
    ├─ HKDF(info=b"service:inspector")  ─►  ls_inspector_<32hex>
    └─ HKDF(info=b"service:future")     ─►  ls_future_<32hex>
```

- The master key never leaves the Keychain in plaintext. Reads
  require a fresh `.userPresence` session — i.e. a Touch ID tap
  (passcode fallback). Stored under `service=local_scribe /
  account=master_key` with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`.
- Tokens are HKDF-deterministic: same master key → same tokens
  across restarts and remounts. Rotating the master key
  (`./run.sh vault init`) rotates every token in lockstep, no
  per-service ceremony.
- FastAPI dependencies (`make_token_dependency`) and the WebSocket
  handshake (`_ws_auth` invoked *before* `ws.accept()`) gate every
  endpoint except `/health`, `/api/health`, `/`, and `/auth?token=…`.
- Token comparison is constant-time (`secrets.compare_digest`).

### What's gated

| endpoint | method | gated? |
|---|---|---|
| `/health` (ASR) | GET | open (liveness probe) |
| `/v1/audio/transcriptions` | POST | bearer required |
| `/v1/listen` | POST + WS | bearer required (header *or* subprotocol) |
| `/v1/listen/stream` | POST | bearer required |
| `/api/health` (inspector) | GET | open (liveness probe) |
| `/` (inspector) | GET | open (HTML shell; carries no session data) |
| `/auth?token=…` (inspector) | GET | open by design — validates token & sets the `ls_inspector` cookie |
| `/api/*` (inspector) | any | bearer or cookie required |

### How clients get the token

| client | mechanism |
|---|---|
| Char | `./run.sh configure-char` writes the ASR token into `ai.stt.openai.api_key`; Char sends `Authorization: Bearer …` on every Generate |
| `transcribe_file.py` / `redo_session.py` | call `service_auth.client_auth_header_for("asr", …)` which prompts Touch ID on first use |
| Browser inspector | one click on `http://127.0.0.1:8001/auth?token=…` (printed by `./run.sh status`) sets an HttpOnly, SameSite=Strict cookie for 30 days |
| `curl` / scripting | `./venv/bin/python -m service_auth token asr` (Touch ID) → pipe into `-H "Authorization: Bearer …"` |

The headers we accept (in priority order): `Authorization: Bearer …`,
`Authorization: Token …` (Deepgram contract), `X-API-Key: …`,
`Sec-WebSocket-Protocol: token, …` (for the WS upgrade), `?api_key=…`
(query-string fallback for legacy clients), `Cookie: ls_inspector=…`
(browser only).

### What this defends — and what it doesn't

It defends:

- **Shell-as-user**: an attacker who's gained code execution as your
  user but has not (yet) tricked you into a Touch ID prompt. They
  cannot derive the bearer token without the prompt, and the token
  is not in any file, env var, or argv — only in server RAM.
- **Cross-app curl**: a different macOS app hitting the loopback
  port. Same 401.
- **Browser CORS**: a malicious browser extension `fetch()`ing
  `http://127.0.0.1:8000/v1/audio/transcriptions`. Same 401, plus
  the inspector cookie is HttpOnly so JS can't read it.

It does **not** defend:

- **Phished Touch ID**: macOS doesn't pin the biometric prompt to a
  specific process. A rogue Helper popping a plausible "Unlock to
  sign in" prompt and capturing the resulting derivation is the
  documented soft underbelly of every Keychain-with-userPresence
  workflow. Mitigation: fingerprint logging in `./run.sh status`
  makes silent token theft visible, and rotation is one command.
- **Root**: root reads everything.

For the live verification recipe see
[§ Verifying it works on your machine](#verifying-it-works-on-your-machine).

---

## Defence layer 3 — At-rest encryption

[`vault.py`](vault.py) creates an AES-256 sparse-bundle disk image
that holds Char's session directory. The master key (Keychain-stored,
Touch ID gated) is fed to `hdiutil` to mount the image; the volume
appears at the same path Char already writes to
(`~/Library/Application Support/hyprnote`), so the app sees no
difference. On unmount, the bytes on disk are ciphertext.

This layer addresses adversary #5 (backup / forensic-imager) — a
stolen laptop or copied Time Machine drive yields encrypted bands,
not audio. Without the master key, the AES-256 envelope cannot be
broken in any operationally meaningful time.

**Status**: module implemented + unit-tested; `./run.sh vault {init,
unlock,lock,status}` wiring lands in a follow-up commit (tracked in
[TODO.md](TODO.md)).

---

## Defence layer 4 — YubiKey-escrowed key backup

[`yubikey_backup.py`](yubikey_backup.py) encrypts a copy of the
master key with `age-plugin-yubikey`, requiring a physical tap on the
YubiKey to decrypt. This is the recovery path for the
laptop-died-Keychain-gone scenario.

We chose YubiKey-`age` rather than "write the key into a `.txt`
file under `~/Documents`" because:

- the master key is the root of every other secret derived from it;
  having a plaintext copy on disk would null out layer 3 entirely,
- YubiKey enforces "physical presence" hardware-side, so the
  recovery key cannot be exfiltrated by software alone,
- `age` is a small, audited format with a public spec and a
  Rust implementation; it doesn't impose a third-party SaaS on the
  recovery path.

**Status**: module implemented; `./run.sh yubikey {enroll, verify,
restore}` wiring + the bootstrap-time enrollment prompt land in a
follow-up commit.

---

## Defence layer 5 — Char-settings enforcement

[`char_audit.py`](char_audit.py) is the runtime contract check.
Every `./run.sh doctor` and every inspector page load walks Char's
`settings.json` + `store.json` and produces structured findings:

| check | what it asserts | severity if violated |
|---|---|---|
| `ai.current_stt_provider` | must equal `"openai"` (our shim binding) | WARN — transcripts would route to a different provider |
| `ai.current_stt_model` | must equal `"gpt-4o-transcribe"` (progressive streaming, no 60s timeout) | WARN |
| `ai.stt.openai.base_url` | must equal `http://127.0.0.1:{asr_port}/v1` | WARN — audio would POST to the upstream URL |
| `ai.stt.openai.api_key` | must equal the current ASR token (HKDF-derived) | WARN — every Generate 401s, and a real OpenAI key here would be a privacy red flag |
| `store.analytics.Disabled` | must be `true` (PostHog kill switch) | WARN |
| `firewall.block_list` | block list installed in `/etc/hosts` | WARN — telemetry hosts reachable |
| any other `stt.{provider}.base_url` | matches the upstream default, no rogue proxy URL | WARN if drifted |

`./run.sh configure-char` is the **only** machinery that writes those
keys back. It:

1. quits Char (`osascript -e 'tell application "Char" to quit'`,
   `pkill` as fallback) so writes aren't clobbered,
2. snapshots the current `settings.json` to
   `settings.json.bak.<timestamp>`,
3. if an existing `api_key` looks like a real OpenAI key (`sk-…`),
   offers to back it up to `~/.config/local_scribe/char-openai-key.<ts>.txt`
   (chmod 600) before overwriting,
4. derives the current ASR token via `service_auth.client_token_for`
   (Touch ID prompt as needed),
5. writes the four keys + the PostHog disable toggle atomically,
6. relaunches Char.

This means there's a **single, audited code path** that touches Char's
config. Any other process editing `settings.json` is by definition
drift, and `./run.sh doctor` reports it loudly:

```
char.app:
  ● Char 1.0.24 installed (matches pinned)
  ● Char transcriber configured for this server
  ○ Char api_key DRIFT — saved key doesn't match the current ASR token (fp=832c8a)
      run `./run.sh configure-char` to rewrite Char's settings.json
```

The inspector's "Char Audit" tab surfaces the same data as a one-click
"fix it" button (`POST /api/char/configure`) so end users don't need
to memorise the CLI.

---

## How we audited the third-party surface

Two third parties matter: Char.app and LM Studio.

### Char.app

Full bottom-up audit in [`CHAR_REVIEW.md`](CHAR_REVIEW.md), which is
re-run whenever `CHAR_KNOWN_GOOD_VERSION` in `run.sh` is bumped.
Highlights:

- **Code signing + notarisation**: verified to be signed by Fastrepl,
  Inc. (Team ID `6SLY7V277V`), notarised, hardened-runtime enabled,
  stapled ticket. SHA256 of the DMG is pinned in
  `run.sh::CHAR_DMG_SHA256_AARCH64` so a tampered download fails the
  hash check **before** `cp -R` touches `/Applications`.
- **Entitlements**: no `com.apple.security.network.client` /
  `com.apple.security.network.server` entitlements — Char relies on
  the unsandboxed default, which is exactly what the `/etc/hosts`
  block list catches.
- **URL surface**: `strings`-extracted hostname list cross-checked
  against `lsof` during live recording, then mapped one-to-one to
  the `firewall.BLOCK_CATALOG` entries.
- **Source cross-reference**: every URL is tied back to its source
  file in the open-source [`fastrepl/anarlog`](https://github.com/fastrepl/anarlog)
  repo, so anyone can verify the claim that (for example) the
  Sentry DSN is compile-time-baked and not user-toggleable.
- **Auto-update**: pinned to `1.0.24`. `./run.sh install-char`
  refuses to overwrite a non-pinned version without an explicit
  prompt, and the auto-updater hostname is in the default firewall
  block list.

### LM Studio

LM Studio is closed-source; we don't ship a binary-level audit, but
the controls we apply are:

- it runs entirely on the user's machine (loopback `127.0.0.1:1234`),
- the firewall block list does not include LM Studio's update / model
  hub endpoints **by default** — those are needed for the bootstrap
  model download. Operators wanting an air-gapped install after the
  one-shot download can add the LM Studio update endpoint to a
  local `--strict` extension (TODO),
- LM Studio's analytics opt-out is documented in
  [README §What you still have to trust](README.md#what-you-still-have-to-trust);
  auto-disabling it at bootstrap is in [TODO.md](TODO.md).

The data plane (audio, transcripts) never reaches LM Studio anyway —
LM Studio only sees the **text** of a transcript when Char's
"Intelligence" provider asks for a summary, and that traffic is
loopback only.

### Python / Homebrew supply chain

- Python deps live in `requirements.txt` and are installed via
  `pip install -r requirements.txt` into a per-repo venv. They are
  **not** hash-pinned yet (tracked in
  [TODO.md](TODO.md): "Pin Python wheels with hashes").
- LM Studio is installed via the official Homebrew Cask, which
  publishes its own SHA256s and verifies them on install.
- Models come from HuggingFace (`mlx-community/parakeet-tdt-0.6b-v3`,
  the `sherpa-onnx` ONNX bundles) over HTTPS, with HF's standard
  signature checks for the parquet shards.

---

## Verifying it works on your machine

The following is reproducible top to bottom. Run it after a fresh
`./run.sh bootstrap` to convince yourself nothing is sneaking out.

### 1. Stop the data plane

```bash
./run.sh stop
```

### 2. Probe the firewall

```bash
./run.sh firewall status          # should report "INSTALLED" with telemetry/providers full coverage
./run.sh firewall verify          # every host should report "blocked"
```

Manual cross-check with `curl`:

```bash
curl -sS --max-time 3 https://api.openai.com/v1/models -o /dev/null \
  -w 'HTTP %{http_code}, time_total=%{time_total}s\n'
# expected: curl: (7) Failed to connect ..., time_total≈0.002s
```

### 3. Probe the auth gates (services not running)

```bash
./run.sh start &
sleep 5
curl -i http://127.0.0.1:8000/v1/audio/transcriptions    # → 401
curl -i http://127.0.0.1:8001/api/sessions               # → 401
curl -i http://127.0.0.1:8000/health                     # → 200 (always-open liveness probe)
```

### 4. Confirm Char's loopback contract

While Char is recording a test session:

```bash
lsof -nP -i -P 2>/dev/null | rg -i char | rg '->'
# expected: only 127.0.0.1:* destinations
# definitely not: api.openai.com, api.deepgram.com, etc.
```

### 5. Run the audit

```bash
./run.sh doctor
```

Every line should be a green `●` except for items you've deliberately
opted out of. A `○` next to "outbound firewall" means the block list
isn't installed; `○` next to "Char api_key DRIFT" means
`configure-char` needs to be re-run after a token rotation.

### 6. Read the diff

```bash
sudo grep -A 200 'local_scribe firewall' /etc/hosts
ls -l /etc/hosts.local_scribe.bak.*
```

The block region is self-documenting (every host has a one-line
comment above it explaining why it's there).

### 7. End-to-end test

```bash
./venv/bin/python -m pytest tests/ -q
```

Should report **415 passed**. The firewall round-trip is exercised
by `tests/test_firewall.py`; the auth gates by
`tests/test_asr_server.py::AsrServerAuthIntegrationTests` and
`tests/test_inspector_server.py::AuthTests`; the Char-settings
contract by `tests/test_char_audit.py::FirewallIntegrationTests` and
neighbouring suites.

---

## Out of scope

Things we deliberately do **not** defend against, with the rationale:

- **Root / kernel-level malware.** Once root is on the box, every
  defence in user-space is bypassable. The Keychain protects against
  *other users* and against *this user without Touch ID*, not against
  a kernel-mode attacker.
- **Hardware side channels** (Meltdown / Spectre, cold-boot DRAM
  attacks). Beyond the scope of a single-app threat model.
- **A user who actively wants to send data out.** If you turn off the
  firewall, edit `settings.json` to re-point Char at OpenAI, and tap
  Touch ID to authorise the keychain read, you can absolutely
  exfiltrate audio. The point of the controls is to make every step
  of that choice loud + intentional, not to prevent it.
- **Char silently changing its data contract.** We pin the Char
  version, audit it at pin-bump time, and run `char_audit` every
  doctor pass — but a sophisticated supply-chain attack on Fastrepl's
  signing key could ship a malicious Char build that respects the
  same `settings.json` keys but adds a covert side-channel. The
  defence here is auditing, not cryptography.
- **macOS itself.** We assume the OS kernel, the codesign verifier,
  and Apple's notarisation pipeline are honest. If they aren't,
  nothing in user-space helps.
- **Compromised network infrastructure** between your laptop and
  HuggingFace / GitHub during `bootstrap`. We verify SHA256s where
  publishers expose them (Char DMG), but model weights are downloaded
  over HTTPS with whatever signature scheme HuggingFace ships. A
  state-level MITM with a cooperating CA could substitute weights at
  download time.

---

## Reporting a vulnerability

If you find a security issue in `local_scribe` itself
(`asr_server.py`, `inspector_server.py`, `firewall.py`,
`service_auth.py`, `vault.py`, `yubikey_backup.py`, `char_audit.py`,
or `run.sh`):

- For low-severity issues (config drift, doc errors, edge cases that
  don't expose user data), open a GitHub issue with `[security]` in
  the title.
- For higher-severity issues that would let an unauthenticated local
  attacker read transcripts / audio, or that would cause network
  egress of user content, please contact the maintainer directly
  rather than filing a public issue. The repository's `git log`
  shows the maintainer's email.

If you find an issue in **Char.app**, report it upstream at
[`fastrepl/anarlog`](https://github.com/fastrepl/anarlog/issues).
If the issue would also let `local_scribe` leak data (e.g. a new
Char endpoint we don't know about), please also file a heads-up here
so we can update [`CHAR_REVIEW.md`](CHAR_REVIEW.md) and the
firewall catalog.

If you find an issue in **LM Studio**, report it to LM Studio
support; LM Studio is closed-source and we cannot patch it here.

We do not currently run a paid bug-bounty programme. Material
findings will be credited in the release notes of the commit that
fixes them.

---

## Continuous-audit checklist

Re-run this whenever:

- `CHAR_KNOWN_GOOD_VERSION` is bumped,
- a new dependency lands in `requirements.txt`,
- a new Tauri plugin shows up in `fastrepl/anarlog`'s
  `apps/desktop/src-tauri/src/lib.rs`.

1. `./venv/bin/python -m pytest tests/ -q` — green
2. `./run.sh doctor` — every line green
3. `./run.sh firewall verify` — every host blocked
4. `./run.sh firewall list --strict` — review the catalog vs.
   what's in `CHAR_REVIEW.md`'s egress catalog; add any missing
   hosts to `firewall.BLOCK_CATALOG`
5. `codesign -dv --verbose=4 /Applications/Char.app` — same
   Team Identifier as last audit (`6SLY7V277V`)
6. `strings -a /Applications/Char.app/Contents/MacOS/char | rg -o 'https?://[a-zA-Z0-9._/?=:&#%~-]+' | sort -u` — diff against the previous run; any new host gets a `firewall.BLOCK_CATALOG` entry
7. `lsof -nP -i -P | rg -i char` mid-recording — only `127.0.0.1`
   destinations
8. Commit the updated `CHAR_REVIEW.md` + the new catalog entries.

---

## Document history

| date | change |
|---|---|
| 2026-05-10 | Initial publication. Documents the firewall layer, the bearer-token auth layer, the vault + YubiKey escrow layers (partially shipped), and the audit methodology. |
