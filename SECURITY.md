# Security policy

This document is the canonical, audit-style description of how
`local_scribe` defends the data it touches. It explains:

- the assets we're protecting and from whom,
- what we did to make sure nothing leaves the laptop,
- how we evaluated and audited each third party we link to,
- every layer of defense currently shipped, in build order,
- what we deliberately do **not** defend against,
- how to verify the posture on your own machine,
- how to report something we got wrong.

Companion documents:

- [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) — every flow described here
  as a clickable Mermaid diagram. Especially relevant:
  [§ 4 At-rest encryption](docs/ARCHITECTURE.md#4-at-rest-encryption-designed),
  [§ 5 Service authentication](docs/ARCHITECTURE.md#5-service-authentication-hkdf-tokens),
  [§ 6 Outbound firewall](docs/ARCHITECTURE.md#6-outbound-network-firewall),
  [§ 14 Threat model × defense layers](docs/ARCHITECTURE.md#14-threat-model--defense-layers),
  [§ 15 Vault & key lifecycle](docs/ARCHITECTURE.md#15-vault--key-lifecycle).
- [`CHAR_REVIEW.md`](docs/CHAR_REVIEW.md) — the bottom-up audit of the
  Char.app binary: every URL the binary references, who it actually
  talks to at runtime, and what we've done about each one. Read that
  if you want the raw evidence behind the claims here.
- [`KEY_SAFETY.md`](docs/KEY_SAFETY.md) — every data-loss scenario from
  key mismanagement (S1–S18), the mitigation tied to each one, and
  a recovery flowchart. Read this BEFORE running any `./run.sh key`
  command for the first time.
- [`README.md`](README.md) — feature overview and operator guide.
- [`TODO.md`](TODO.md) — the open-mitigations backlog.
- [`firewall.py`](local_scribe/egress/firewall.py), [`service_auth.py`](local_scribe/security/service_auth.py),
  [`vault.py`](local_scribe/security/vault.py), [`yubikey_backup.py`](local_scribe/security/yubikey_backup.py),
  [`secret_store.py`](local_scribe/security/secret_store.py), [`char_audit.py`](local_scribe/char/char_audit.py),
  [`key_safety.py`](local_scribe/security/key_safety.py) — the modules implementing the
  controls below.

---

## TL;DR

`local_scribe` is **local-first by design**: every byte of audio,
every transcript, and every LLM-generated summary is produced and
stored on your laptop. There is no SaaS component, no telemetry, no
analytics, no "send to cloud for processing" toggle. After
`./run.sh bootstrap` has finished pulling models, you can disable
Wi-Fi and the pipeline keeps working indefinitely.

We back that promise with **defense in depth**:

| layer | mechanism | module | enforces |
|---|---|---|---|
| **kernel boundaries** | refuse to run when System Integrity Protection is not fully enabled (parses `csrutil status`; rejects fully-disabled and partially-disabled custom configurations) | [`sip_check.py`](local_scribe/security/sip_check.py), gated from [`run.sh`](run.sh) and every FastAPI lifespan | every other defense below assumes `task_for_pid` / DYLD-injection / dtrace restrictions are enforced; the gate makes that a precondition rather than an assumption |
| network-egress | `/etc/hosts` block list with marker-delimited region; `0.0.0.0` + `::` sinks for IPv4 and IPv6 | [`firewall.py`](local_scribe/egress/firewall.py) | Char's Sentry / PostHog / auto-updater + all external STT/LLM APIs are unreachable |
| inter-service auth | HKDF-SHA256-derived per-service bearer tokens; the master key they're derived from lives behind Option C two-factor (Touch ID + YubiKey tap) | [`service_auth.py`](local_scribe/security/service_auth.py), [`key_lifecycle.py`](local_scribe/security/key_lifecycle.py), [`secret_store.py`](local_scribe/security/secret_store.py) | a process with shell-as-user access cannot `curl` any local API even after a Touch ID phish |
| at-rest encryption | AES-256 sparse-bundle vault for Char's session directory | [`vault.py`](local_scribe/security/vault.py) | a stolen / imaged disk yields ciphertext, not recordings |
| split-key construction | `master_key = kc_half XOR yk_half`; halves live in the Keychain (Touch ID) and an `age` file encrypted to a YubiKey | [`key_split.py`](local_scribe/security/key_split.py), [`key_lifecycle.py`](local_scribe/security/key_lifecycle.py) | Keychain pwn alone yields uniform random bytes; YubiKey theft alone yields opaque ciphertext |
| key escrow + disaster recovery | multi-recipient `age-plugin-yubikey` enrollment (2nd YubiKey) + optional passphrase-encrypted `disaster_recovery.age` for the loss-of-both-factors case | [`yubikey_backup.py`](local_scribe/security/yubikey_backup.py), [`disaster_recovery.py`](local_scribe/security/disaster_recovery.py) | losing one factor is recoverable from the other; losing both is recoverable from the DR passphrase |
| Char-settings enforcement | `char_audit.py` flags drift; `configure-char` rewrites the four keys atomically and disables Char's PostHog toggle | [`char_audit.py`](local_scribe/char/char_audit.py) | a settings change that would re-route audio outside the loopback shim is loud, visible, and reversible |
| build-time supply chain | pinned Char DMG SHA256s; pinned `CHAR_KNOWN_GOOD_VERSION`; explicit cask install for LM Studio | [`run.sh`](run.sh) | tampered binaries never reach `/Applications` |

The full threat model and per-layer rationale are below.

**See also.** [`CRYPTO.md`](CRYPTO.md) is the companion document to
this one. Where this file speaks to *which adversary each defense
layer addresses*, `CRYPTO.md` speaks to *which primitive was chosen
for each cryptographic operation and why* (random-number source,
KDF, AEAD, key-wrapping, secret-sharing, integrity, transport — each
section contrasts the choice with the alternatives we considered and
calls out the residual risk). It also tracks the 11 concrete
cryptographic improvements on the roadmap, mirrored into
[`TODO.md`](TODO.md#crypto-improvements-design-tracked-in-cryptomd).

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
- side-channel timing oracles against `constant_time_compare`,
- **an operator who deliberately disables System Integrity Protection.**
  SIP-disabled hosts cannot enforce the user-space process
  boundaries every other defense in this document relies on
  (`task_for_pid()` reading our heap, `DYLD_INSERT_LIBRARIES`
  loading code into Char, `dtrace -p` against the Keychain
  daemon, etc.). We refuse to run on such hosts. See
  [§ Defense layer 0](#defense-layer-0--system-integrity-protection-mandatory).

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
[`CHAR_REVIEW.md` § Threat model](docs/CHAR_REVIEW.md#threat-model);
this section is the compressed summary tied to which defense
mitigates which row.

| # | adversary | capability | mitigation |
|---|---|---|---|
| 1 | **Remote network peer** | TCP probe `127.0.0.1:{8000,8001}` from the LAN | macOS firewall blocks inbound; per-service bearer auth makes 0.0.0.0 binds opaque even when reachable |
| 2 | **Browser content** (XSS, extension) | `fetch()` to our loopback ports from any origin | per-service bearer token in `Authorization`/cookie — browser can't read the Keychain |
| 3 | **Co-tenant** (other macOS user on same Mac) | read files / sockets owned by other users | Keychain is per-user; `.userPresence` requires Touch ID even for the owner; loopback sockets bind to the owning UID only |
| 4 | **Shell-as-user** (malware running as you, no Touch ID session yet) | `curl` localhost, read `$HOME`, read process env | bearer tokens are *not* in env vars / argv / disk — only in server RAM after a successful unlock; the unlock requires BOTH Touch ID AND a fresh YubiKey tap (Option C); vault keeps audio/transcripts as ciphertext at rest; firewall closes outbound exfiltration. SIP (Defense layer 0) is what stops this adversary from reading the in-memory master key out of our heap via `task_for_pid()` after a legitimate unlock — without SIP, every other control in this row collapses |
| 5 | **Backup / forensic imager** (stolen laptop) | read every file on disk; no live process | AES-256 sparse-bundle vault for Char's data; the master key is split across the Keychain (encrypted at rest by macOS) and a YubiKey-encrypted age file — neither alone reconstitutes the key |
| 6 | **Phished Touch ID** (attacker tricks user into tapping for a fake prompt) | one biometric tap | **Option C upgraded mitigation**: a successful Touch ID phish yields `kc_half` (32 random bytes) only. Without the YubiKey it does not derive the master key. Per-request fingerprint logging in `./run.sh status` still makes silent reuse visible; rotation via `./run.sh key rotate` invalidates everything in one step |
| 7 | **Root / TCC-bypass / kernel** | anything | out of scope. **SIP-disabled hosts trivially escalate to this tier**, which is why local_scribe refuses to run on them — see [§ Defense layer 0](#defense-layer-0--system-integrity-protection-mandatory) |

Per-tier rationale, including why we chose `.userPresence` over plain
`kSecAttrAccessibleWhenUnlocked`, lives in
[`CHAR_REVIEW.md § Threat model`](docs/CHAR_REVIEW.md#threat-model).

---

## Defense layer 0 — System Integrity Protection (mandatory)

> **TL;DR:** local_scribe refuses to run on a macOS host where SIP
> isn't fully enabled. The one documented operator override is
> [dev mode](#dev-mode--explicit-sip-bypass-for-development), which
> is loud-on-every-surface and never the default.
> Implementation: [`sip_check.py`](local_scribe/security/sip_check.py), gated from
> [`run.sh`](run.sh) and from every FastAPI service lifespan.

Every other control in this document — the Option C split-key, the
service-auth bearer tokens, the outbound firewall, the Char-binary
integrity check, the [script-integrity gate](#relationship-with-the-script-integrity-gate)
(itself a userspace stand-in for hardware-rooted remote attestation
— see [Defense layer 6's TEE note](#future-direction--trusted-execution-environment)),
the YubiKey tap on key operations — is **predicated on the kernel
enforcing the user-space process boundaries macOS normally
enforces**. SIP is what makes those boundaries real. Without it,
every higher-layer gate is trivially bypassable.

### Concretely, with SIP disabled an attacker can…

| Capability | Why it kills our model |
|---|---|
| `task_for_pid()` against any codesigned binary | After a legitimate Touch ID + YubiKey unlock, the reconstituted master key is in our Python process's heap. Another user-space process can `mach_vm_read` it out and walk away with everything every other defense is built around. Even the Option C split-key collapses: the *combined* master is in memory for the duration of a request. |
| `DYLD_INSERT_LIBRARIES` / `DYLD_FALLBACK_LIBRARY_PATH` not stripped from hardened-runtime binaries | An attacker injects an arbitrary `.dylib` into Char or our Python services *before our code runs*. [`char_integrity.py`](local_scribe/char/char_integrity.py) detects `DYLD_*` set in the calling environment, but that check itself runs inside the injected process. With SIP off, the injected dylib executes before the check; with SIP on, dyld strips the variables. |
| `dtrace -p <any pid>` works | Attaches to the `secd` Keychain daemon and intercepts every `SecItemCopyMatching` call — the Touch ID prompt becomes a pure UX gesture, the bytes flow into the attacker's probe. |
| Replace `/usr/bin/codesign`, `/usr/bin/security`, `/usr/bin/csrutil` | All of our integrity checks shell out to these. With filesystem protections off, an attacker swaps in shims that always print "valid." |
| Set `nvram boot-args="amfi_get_out_of_my_way=1"` | On the next boot, the kernel's AMFI (Apple Mobile File Integrity) stops enforcing code-signing entirely. Every signed-binary check we have becomes meaningless. |
| Load unsigned kernel extensions | A kext rootkit can hook every relevant syscall (`open`, `read`, `task_for_pid`, `csr_check` itself) and lie to us. |

Each of those means a motivated adversary with shell-as-user can
exfiltrate the master key (or substitute their own) without
triggering a single Touch ID prompt, YubiKey tap, or integrity
banner. The split-key buys you nothing if `mach_vm_read` is open.

### Where the gate fires

| Surface | Behaviour |
|---|---|
| `./run.sh start` | Refuses to start any service. Prints the red banner with the rationale + reboot-to-Recovery instructions. |
| `./run.sh bootstrap` | Refuses. Bootstrap creates the master key; we will not generate that secret in an environment where it can be exfiltrated. |
| `./run.sh key {init,unlock,rotate,add-yubikey,dr-restore,migrate,destroy}` | Refuses every key-touching subcommand. `./run.sh key status` and `./run.sh key backups …` (read-only) are not gated so the operator can introspect their install before fixing things. |
| `./run.sh configure-char` | Refuses. The ASR token is derived from the unlocked master key. |
| `./run.sh redo-session` | Refuses. Same reason — the redo CLI is a client that derives a token in-process. |
| `asr_server` / `inspector_server` FastAPI lifespans | Refuse to start, even if launched outside of `./run.sh start` (e.g. an attacker running `uvicorn asr_server:app` directly). Defense in depth. |
| `./run.sh stop / status / logs / doctor / firewall` | **Not gated.** These don't touch keys, and the operator may need precisely these commands to clean up running services before rebooting to Recovery. `doctor` surfaces the SIP failure at the very top of its report. |

### How we detect SIP

We shell out to `/usr/bin/csrutil status`. Three acceptance rules:

1. **Top line is `System Integrity Protection status: enabled.`** —
   the only state we accept without further checks.
2. **Top line is `enabled (Custom Configuration).`** — we parse the
   `Configuration:` block and require every protection from
   {`Filesystem Protections`, `Debugging Restrictions`,
   `DTrace Restrictions`, `NVRAM Protections`, `Kext Signing`,
   `BaseSystem Verification`} to read `enabled`. `Apple Internal`
   is informational only.
3. **Anything else** (top line says `disabled`, csrutil is missing,
   subprocess returns nonzero, output is unparseable) → we **fail
   closed** and refuse to proceed. The one documented operator
   bypass is [dev mode](#dev-mode--explicit-sip-bypass-for-development);
   see below.

The detector has one test-only seam: when the env var
`LOCAL_SCRIBE_TEST_CSRUTIL_OUTPUT` is set, its value is substituted
for the live `csrutil` output. This is used by our pytest suite
(see `tests/test_sip_check.py`) — never by `./run.sh`.

### Dev mode — explicit SIP bypass for development

> **Status: shipped, audited, loud.** Implementation:
> [`local_scribe/common/dev_mode.py`](local_scribe/common/dev_mode.py)
> for the truth + banner; the SIP gates in
> [`sip_check.py`](local_scribe/security/sip_check.py),
> [`run.sh::sip_gate`](run.sh), the ASR + Inspector lifespans, and
> the inspector front-end all consult it. **There is exactly one
> operator-facing override of Defense layer 0 and this is it.**
> Production operators should never set it.

**Why this exists at all.** The original Defense layer 0 had no
operator override on purpose. In practice, three groups of users
need to iterate on the pipeline itself on a SIP-disabled host:

* Developers debugging the FastAPI handlers with `lldb`, `dtrace`,
  or kernel-attaching profilers that require SIP off.
* CI hosts that don't have SIP configured at all (Apple Silicon
  cloud runners, GitHub Actions macOS images, etc.).
* Operators triaging a Char-update regression on a sandbox machine
  where rebooting to Recovery to flip SIP would be more disruptive
  than the test itself.

A binary "refuse to run on any non-SIP host, no exceptions" policy
forces those users to either (a) fork the project and rip the gate
out — losing the rest of the security review along with it — or
(b) inject a worse override (commenting out the check). Both are
strictly worse for the threat model than a single documented,
loud-on-every-surface bypass. The whole design constraint is that
when the bypass is active the operator *cannot* mistake the run
for production: every CLI surface, the inspector UI, and the
process logs scream about it.

**How to enable.**

```bash
# One-shot: scoped to this start invocation. The flag exports
# LOCAL_SCRIBE_DEV_MODE=1 into every subprocess run.sh spawns.
./run.sh start --dev

# Equivalent — set the env var for any shell-level
# Python or run.sh invocation.
LOCAL_SCRIBE_DEV_MODE=1 ./run.sh start
LOCAL_SCRIBE_DEV_MODE=1 ./venv/bin/python -m local_scribe.security.sip_check check
LOCAL_SCRIBE_DEV_MODE=1 ./venv/bin/python -m local_scribe doctor
```

The flag parser recognises the standard set of off-values so that
operators who explicitly set `LOCAL_SCRIBE_DEV_MODE=0` (or `false`,
`no`, `off`, empty string) do NOT get the bypass — those values
mean "I'm being explicit that dev mode is off" and the gate
behaves as if the var were unset.

**What dev mode bypasses.** *Only* the SIP-related gates:

| Gate | Without dev mode | With dev mode |
|---|---|---|
| `./run.sh sip_gate` (every key-touching verb) | refuse, print the red Defense-layer-0 banner | print the dev-mode banner + a single bypass marker, continue |
| `sip_check.enforce_or_die()` from the ASR + Inspector lifespans | raise `SIPDisabledError`, service refuses to start | log a warning + emit the dev-mode banner once, return the report, continue starting |
| `python -m local_scribe.security.sip_check check` (invoked by `run.sh sip_gate`) | exit 1 with the layer-0 banner | exit 0 with the dev-mode banner on stderr |

**What dev mode does NOT bypass.** Every other security control
in this document still applies. Specifically:

* [`script_integrity_gate`](#defense-layer-5--script-integrity)
  — our own scripts must still hash to the blessed baseline.
* [`char_integrity_gate`](#layer-4--char-binary-integrity-+-side-load-detection)
  — Char.app CDHash, Team ID, Bundle ID, and linked-library
  prefixes must still match the pinned baseline.
* [`pinned_config_gate`](#defense-layer-6--signed-pinned-config)
  — the operator HMAC over `pinned.json` + `char_baseline.json`
  must still verify.
* `service_auth` — the HKDF-derived bearer token on every
  `/api/*` endpoint is unchanged.
* `secret_store.unlock_master_key` — Touch ID + YubiKey are
  still required to unlock the master key. (The kernel-boundary
  protection on the unlocked-key window is what's gone; the
  unlock ritual itself is identical.)

An operator who wants to bypass *several* gates can combine env
vars (`LOCAL_SCRIBE_DEV_MODE=1 LOCAL_SCRIBE_DISABLE_AUTH=1`), but
every bypass surfaces its own warning so the cumulative "how
degraded is this run?" picture is auditable from `./run.sh doctor`
alone.

**How the bypass surfaces to the operator.** Loud, on every
surface, on every invocation — there is no quiet path through dev
mode:

1. **`./run.sh sip_gate`** prints a coloured one-line bypass
   marker plus a SECURITY.md anchor on every gated verb.
2. **`./run.sh doctor`** renders a four-line red block at the
   *top* of its output (above the SIP section it would
   otherwise lead with).
3. **`./run.sh status`** prints a `[DEV MODE]` line above the
   service table whenever the current shell has the env var set.
4. **`python -m local_scribe status` / `python -m local_scribe doctor`**
   surface the same banner — the CLI is unified across `run.sh`
   and `python -m`.
5. **ASR + Inspector lifespans** emit the full banner once per
   process to their log (stderr), plus a `WARNING`-level log line
   identifying which service is running with the bypass.
6. **Inspector UI** renders a **sticky red banner across the very
   top of every page**, pulsing slowly (respecting
   `prefers-reduced-motion`), driven by the unauthenticated
   `GET /api/dev_mode/status` endpoint. The banner is
   non-dismissible by design — operator-controlled CSS overrides
   cannot suppress it (the colours live outside the dark/light
   theme variables) and there is no `dismiss` button.
7. **`/api/dev_mode/status` JSON** exposes a `severity: "critical"`
   field while dev mode is on, machine-readable for any future
   monitoring integration.

**The threat-model cost, named.** With dev mode on, every entry
in the [SIP-disabled capability table above](#concretely-with-sip-disabled-an-attacker-can)
applies in full. Specifically: the reconstituted master key sitting
in our Python heap after a Touch ID unlock is readable by any
cohabiting user-space process via `mach_vm_read`. The HMAC over the
pinned config still verifies, the script-integrity gate still
passes, the bearer token still gates `/api/*` — **and the master key
those layers protect is exfiltrable through a window the kernel is
no longer being asked to close.** This is why dev mode is not a
production mode. It exists so that a developer can run the pipeline
*at all* on a SIP-off host; the moment they're done iterating,
unsetting the env var (and restarting) re-engages every gate at
full strictness.

**Exiting dev mode.** The bypass is a per-process env-var read;
there is no on-disk state to clean up:

```bash
./run.sh stop
unset LOCAL_SCRIBE_DEV_MODE
./run.sh start
```

`./run.sh start` will now hard-fail until SIP is fully on, just
like before. If the host's SIP state hasn't changed, that's a
correct outcome — see "How to fix" above for the
reboot-to-Recovery + `csrutil enable` runbook.

**The one strict caller.** `sip_check.enforce_or_die_strict()`
ignores the env var. The production master-key rotation CLI
(`./run.sh key rotate`) uses this variant so that an operator who
forgot to `unset LOCAL_SCRIBE_DEV_MODE` doesn't accidentally
rotate the master key on a SIP-off host — that would expose both
the old and new key material through the same `mach_vm_read`
window. Rotation refuses to proceed without strict SIP, period.

### What this layer does *not* defend against

- **A bootkit installed before macOS comes up.** If the firmware
  itself is compromised, SIP is unreliable. Defense-in-depth here
  is Secure Boot + Activation Lock; both are out of our scope.
- **An exploit chain that escalates to kernel during a live
  session.** SIP is enforced by the kernel; a kernel-mode attacker
  can disable parts of it on the fly. The session-level master
  key in process memory becomes readable at that point. Our only
  defense is the limited window during which the key is alive in
  memory (Touch ID + tap → derive tokens → zero the master ≈
  hundreds of ms).
- **Operator coercion.** Someone forces the user to re-enable SIP
  is not a problem we can solve. Someone forces the user to
  *disable* SIP… is what this gate makes harder than installing
  malware after the fact.

### How to verify

```bash
csrutil status         # must report: System Integrity Protection status: enabled.
./run.sh doctor        # surfaces the same check + every other layer
./venv/bin/python -m sip_check status   # JSON for scripting
```

If `csrutil status` says anything else, follow the banner: boot
into Recovery (hold the power button on Apple Silicon, or `⌘-R`
during boot on Intel), open Utilities → Terminal, run
`csrutil enable`, and reboot.

---

## Defense layer 1 — Network egress firewall

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

### Why a custom proxy instead of `pf` or the macOS Application Firewall

macOS has no native CLI-installable per-app outbound block. Here's
the actual landscape:

| Mechanism | Per-app outbound? | Why we can / can't use it |
|---|---|---|
| **macOS Application Firewall** (`socketfilterfw`) | per-app, **inbound only** | Apple's docs are explicit: ALF doesn't filter outbound. |
| **`pf`** (BSD packet filter) | **no, per-user only** | Apple's pf port stripped FreeBSD's per-PID / per-binary keywords. The only per-process knob remaining is `user <uid>` / `group <gid>`. To use it we'd have to run Char as a dedicated unprivileged user, which fights GUI launch, TCC microphone permissions, Keychain ACLs, and Char's data dir. Not impossible; not a one-line change. |
| **Network Extension** (`NEContentFilterProvider`) | **yes — this is the native answer** | Requires an Apple-granted entitlement (`com.apple.developer.networking.networkextension`) gated behind Developer ID + Apple application review. It's what Little Snitch and LuLu use. **Infeasible for an open-source project to ship from a repo.** |
| **`sandbox-exec`** (kext-backed Apple Sandbox) | per-launched-process tree | Hostname rules are resolved once at profile load, so DNS rotation defeats them — useless on its own. But its IP-level rules are unbreakable: this is the **containment** half of our solution. |
| **HTTPS_PROXY env var** | per-process | Apple's own CI uses this for offline testing. Tauri/reqwest (Char's HTTP stack), the Sentry Rust SDK, PostHog Rust SDK, and the Tauri auto-updater all honour it. This is the **policy** half. |

So we **compose the two primitives Apple actually gives us**: a
`sandbox-exec` policy that allows everything except network egress
to anywhere except loopback, plus an `HTTPS_PROXY` env var that
points Char at a local CONNECT proxy. The proxy applies the
hostname-level policy.

The two layers reinforce each other: even a hypothetical Char build
that ignored `HTTPS_PROXY` couldn't bypass the proxy because its
only network path is loopback. Other apps on the same Mac are
completely unaffected — they don't inherit either the sandbox or
the env var.

### The control

Two modes ship, selected by `./run.sh firewall enable --mode {process|system}`:

**Mode `process`** (default, recommended)
- [`egress_proxy.py`](local_scribe/egress/egress_proxy.py): asyncio CONNECT proxy on
  `127.0.0.1:8889`. Every CONNECT (HTTPS) or plain-HTTP request
  passes through `firewall.is_blocked(hostname)`; blocked hosts get
  a `403` with a JSON deny body, allowed hosts are bridged
  transparently. No TLS interception — the proxy never sees plaintext.
- [`char_sandbox.py`](local_scribe/egress/char_sandbox.py): renders an SBPL profile to
  `~/.config/local_scribe/char.sb`. `(allow default)` + `(deny
  network-outbound)` + re-allow loopback is the full extent of the
  containment.
- [`./run.sh char launch`](run.sh): wraps Char in `sandbox-exec`
  with `HTTPS_PROXY=http://127.0.0.1:8889` injected. This is the
  **only** supported way to start Char if you want the firewall to
  apply. Dock / Spotlight launches inherit neither the env vars nor
  the sandbox; `./run.sh char firewall-status` flags Char processes
  that aren't going through us.
- No sudo. Only affects Char. Survives Char binary updates.

**Mode `system`** (opt-in)
- [`firewall.py`](local_scribe/egress/firewall.py): maintains a marker-delimited block
  in `/etc/hosts` that blackholes every host in the catalog to
  `0.0.0.0` (IPv4) and `::` (IPv6). Resolution fails fast
  (connection refused in ~2 ms).
- Affects every app on the machine, not just Char. Requires admin
  password to install / remove. Backs up `/etc/hosts` to
  `/etc/hosts.local_scribe.bak.<timestamp>` before every change.

Both modes consume the same `firewall.BLOCK_CATALOG`:

| category | default | what's in it |
|---|---|---|
| `telemetry` | **on** | Char's Sentry, PostHog (us+eu), Tauri auto-updater (`desktop2.hyprnote.com` + the Scarf proxy), Sentry browser CDN |
| `providers` | **on** | OpenAI, Deepgram, AssemblyAI, Gladia, Granola, Soniox, Aquavoice, ElevenLabs, Fireworks, Mistral, Pyannote, Anthropic, Google Gemini |
| `char_cloud` | off | `api.char.com` (calendar OAuth + integrations), `cloudsync.sqlite.ai` — opt-in via `--strict` because blocking these breaks calendar sync |

### Operator surface

```bash
# Default per-Char path (mode = process)
./run.sh start                    # also starts the egress proxy
./run.sh char launch              # launches Char under sandbox-exec + HTTPS_PROXY
./run.sh char firewall-status     # is the proxy up? is the running Char going through us?
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

`./run.sh bootstrap` step (10/10) writes + validates the SBPL profile
and tells the operator how to launch Char. It does **not** ask for
sudo — the system-hosts mode is left as an explicit opt-in. The
egress proxy starts automatically alongside the ASR + Inspector
services on every `./run.sh start`. `./run.sh doctor` reports both:
whether the proxy is running and whether the sandbox profile is
valid, plus the system-hosts state if it's also installed.

### What the firewall does **not** do

- Stop a Dock / Spotlight launch of Char. Those launches inherit
  neither the sandbox nor the HTTPS_PROXY env var, so Char talks
  directly to the network. `./run.sh char firewall-status` and
  `./run.sh doctor` both flag this; the only mitigation is to kill
  the bypassed Char process and relaunch via `./run.sh char launch`.
  (Future-work in [`TODO.md`](TODO.md): a Network Extension build
  signed with our own Developer ID would close this gap for users
  who install the signed bundle.)
- Block IP literals — the proxy filters on the CONNECT hostname (or
  the `Host:` header for plain HTTP). An app that hard-codes an IP
  and skips proxying would bypass the policy half. Sandbox
  containment catches this case (loopback-only egress), but only
  for the launched Char tree.
- Intercept TLS plaintext — the proxy is **not** a MITM. Char never
  has to trust a local CA, and we never see request bodies.
- Restrict loopback — Char talking to `127.0.0.1:8000` (our ASR),
  `:8001` (Inspector), `:1234` (LM Studio) is the intended channel
  and stays unaffected. Loopback is in the proxy's pass-through
  list (`LOCAL_PASS_THROUGH`).

### Privileged-prompt UX (every password request explains itself)

When `--mode system` install or uninstall needs admin rights, we do
**not** hand the user a context-free password dialog. Both elevation
paths surface the same information:

- **TTY path (`sudo`)** — `firewall.py:_explain_intent()` prints a
  multi-line banner to stderr *immediately before* `sudo`'s own
  `Password:` line. The banner names the file being edited
  (`/etc/hosts`), the action (install / remove), the blast radius
  ("affects N hostnames across M category(ies)"), the backup path,
  and the inverse command to undo the change.
- **GUI path (`osascript`)** — we pass the same intent as a
  single-line `with prompt "..."` clause to
  `do shell script ... with administrator privileges`. The system
  password dialog then shows our explanation above the password
  field instead of the default `"osascript" wants to make changes`,
  which conditions users to type their password without
  understanding the request.

The construction is pinned by
`tests/test_firewall.py::ElevationExplanationTests` — every dialog
must identify the app (`local_scribe`), the file
(`/etc/hosts`), and the verb (`install` / `remove`), and stay under
the AppleScript prompt length cap. The Touch ID prompts that
unlock the master key apply the same standard: every
`load_kc_half` / `load_master_key` / `ServiceToken.unlock` /
`unlock_master_key` call passes a `prompt` string that names what
the unlock is for (e.g. `"Unlock local_scribe to start the asr
server"`, `"Migrate to split-key (Touch ID)"`,
`"Snapshot kc_half before <op> (Touch ID)"`).

### Audit trail

The proxy logs every decision (`ALLOW` / `DENY` / `ERROR`) to
`.run/egress_proxy.log` at INFO level. `./run.sh char firewall-status`
tails the last 5 decisions. The in-memory `AuditRing` keeps the most
recent 512 decisions for cross-process queries (future inspector
endpoint).

For `--mode system`, every install / uninstall writes a timestamped
backup to `/etc/hosts.local_scribe.bak.<YYYYMMDD-HHMMSS>`. Backups
are owned by `root:wheel 644`, so a future SOC reviewer can
reconstruct the exact sequence of changes and timestamps without
elevated access.

---

## Defense layer 2 — Inter-service authentication

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

[`service_auth.py`](local_scribe/security/service_auth.py) derives a unique 32-hex bearer
token per service from a single in-memory master key via HKDF-SHA256.
The master key is **reconstituted from two on-disk halves** at unlock
time (see Defense layer 4 for the split-key construction):

```
              kc_half (Keychain, Touch ID)
                       │
                       │   XOR
                       │
              yk_half (age-encrypted, YubiKey tap)
                       │
                       ▼
master_key  (32 bytes, in-process bytearray, forget()ed on shutdown)
    │
    ├─ HKDF(info=b"service:asr")        ─►  ls_asr_<32hex>
    ├─ HKDF(info=b"service:inspector")  ─►  ls_inspector_<32hex>
    └─ HKDF(info=b"service:future")     ─►  ls_future_<32hex>
```

- The master key is **never** stored whole on disk. The two halves
  individually carry zero information about the master (XOR of
  uniform-random inputs).
- Reads of `kc_half` require a fresh `.userPresence` session
  (Touch ID, passcode fallback). Reads of `yk_half` require a
  physical tap on the enrolled YubiKey (`touch-policy=always`).
- Tokens are HKDF-deterministic: same master key → same tokens
  across restarts and remounts. Rotating the master key
  (`./run.sh key rotate`) rotates every token in lockstep, no
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
| `DELETE /api/sessions/{id}/audio` | DELETE | bearer **+** typed-confirm body (see below) |
| `DELETE /api/sessions/{id}/history/{name}` | DELETE | bearer **+** typed-confirm body (see below) |

#### Defense-in-depth: typed-DELETE confirm body

A stolen inspector bearer token must not be enough to destroy data
with one `curl`. Both delete endpoints therefore require a JSON body
of exactly `{"confirm": "DELETE"}` (case-sensitive, no padding) in
addition to the bearer cookie/header. Empty body, wrong key, wrong
spelling, lowercase, trailing whitespace, or non-JSON garbage all
return 400 and the file is **not** touched.

This is enforced server-side (`_require_typed_delete_confirm` in
[`inspector_server.py`](local_scribe/inspector/inspector_server.py)) so it cannot be
bypassed by skipping the SPA — the SPA's typed-DELETE modal is the
*usability* gate, the server check is the *security* gate. The
combined property is: deleting a session's audio or a historical
transcript requires **(stolen bearer) AND (knowledge of the literal
string `DELETE`)**, and rotation (`./run.sh key rotate`) invalidates
the first half instantly.

The test suite ([`AudioDeleteEndpointTests` /
`HistoryDeleteEndpointTests` in `tests/test_inspector_server.py`])
asserts the invariant for nine malformed payloads — empty body,
non-JSON, `"yes"`, `"delete"` (lowercase), `"DELETE "` (trailing
space), `True`, `None`, wrong key name, bare string. All bounce
with 400 and the file remains on disk.

### How clients get the token

| client | mechanism |
|---|---|
| Char | `./run.sh configure-char` writes the ASR token into `ai.stt.openai.api_key` (via stdin to `python -m char_settings_writer` — never argv); Char sends `Authorization: Bearer …` on every Generate |
| `transcribe_file.py` / `redo_session.py` | call `service_auth.client_auth_header_for("asr", …)` which prompts Touch ID **and** asks for a YubiKey tap on first use |
| Browser inspector | one click on `http://127.0.0.1:8001/auth?token=…` (printed by `./run.sh status`) sets an HttpOnly, SameSite=Strict cookie for 30 days |
| `curl` / scripting | `./venv/bin/python -m service_auth token asr` (Touch ID + YubiKey tap) → pipe into `-H "Authorization: Bearer …"` |

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

- **Phished Touch ID + Phished YubiKey tap** simultaneously: a rogue
  helper that pops a Touch ID prompt and *also* socially engineers
  the user into tapping their YubiKey in the same window can derive
  tokens. The Option C split-key (Defense layer 4) raises the bar to
  *two* phishes in one user session — a much narrower attack surface
  than the single Touch ID phish that used to suffice. Fingerprint
  logging in `./run.sh status` still makes silent token theft
  visible, and rotation (`./run.sh key rotate`) is one command.
- **Root**: root reads everything in process memory, including the
  reconstituted master key during a legitimate unlock. We don't
  claim TPM-isolated execution.

For the live verification recipe see
[§ Verifying it works on your machine](#verifying-it-works-on-your-machine).

---

## Defense layer 3 — At-rest encryption

[`vault.py`](local_scribe/security/vault.py) creates an AES-256 sparse-bundle disk image
that holds Char's session directory. The master key (reconstituted
from the two factors in layer 4 below) is fed to `hdiutil` to mount
the image; the volume appears at the same path Char already writes
to (`~/Library/Application Support/hyprnote`), so the app sees no
difference. On unmount, the bytes on disk are ciphertext.

This layer addresses adversary #5 (backup / forensic-imager) — a
stolen laptop or copied Time Machine drive yields encrypted bands,
not audio. Without the master key, the AES-256 envelope cannot be
broken in any operationally meaningful time.

**Status**: module implemented + unit-tested. Bootstrap-time
vault-mount wiring (`./run.sh start` mounts before launching services;
`./run.sh stop` detaches afterwards) is tracked in
[TODO.md → "Encrypt audio at rest"](TODO.md). The master key the
vault will consume is already in place (Option C, see Defense layer
4 below); only the `hdiutil`-on-startup glue is pending.

**Known gap: data is plaintext while the vault is mounted.** This is
inherent in the design — the whole point of a mounted volume is that
the operating system serves cleartext reads to the apps that hold
file descriptors on it. The two threat shapes this gap exposes:

1. **Physical-access-while-unlocked.** Someone walks up to an
   unattended-but-logged-in Mac with the vault mounted, plugs in a
   USB stick, drags the session directory across, walks away.
2. **Local-code-access-while-unlocked.** Any user-space process
   running as the operator's UID — a Spotlight indexer plugin, a
   menu-bar app, a cloud-sync agent, a Homebrew post-install hook,
   a curious `npm install` script — can `open(2)` a session's
   `transcript.json` / `audio.mp3` / summary markdown and siphon
   the cleartext.

No layer currently in this document sees either threat; the
operator's UID owns both ends of the copy. Two planned defenses,
both gated on the vault being unlocked, address them in turn:

- A `DiskArbitration` mount-approval daemon that refuses to mount
  *any* external mass-storage device while the vault is unlocked,
  surfaces a clear modal explaining the risk, and requires a fresh
  Touch ID tap to override.
  Tracked in [TODO.md → "Removable-media mount guard while the vault is unlocked"](TODO.md#privacy--security-p0)
  with the full mechanics.
- An `EndpointSecurity` per-process file-access notifier
  (BlockBlock / OverSight aesthetic) that intercepts every `open(2)`
  on the vault path, allow-lists Char + our own code + a small set
  of trusted platform binaries, and prompts the user with full
  process attribution for everything else.
  Tracked in [TODO.md → "Per-process file-access notifier for the Char data bundle"](TODO.md#privacy--security-p0)
  with the full mechanics, the interim FSEvents+`lsof` detective
  mode that does ship from the open-source repo, and the
  Apple-entitlement gate that prevents shipping the *real-time
  blocking* version without a fork.

---

## Defense layer 4 — Option C split-key (Touch ID **and** YubiKey)

The master key is **never** stored whole on disk or in any single
secure-storage item. Instead it is XOR-split into two independent
32-byte halves and persisted across two factors:

```
    master_key = kc_half  XOR  yk_half
```

| half | where it lives | factor to unwrap |
|---|---|---|
| `kc_half` | macOS Keychain (`account=master_key_kc_half_v2`, `.userPresence` ACL, `WhenUnlockedThisDeviceOnly`) | Touch ID (or device passcode fallback) |
| `yk_half` | `~/.config/local_scribe/yk_half.age` — `age` ciphertext to one or more enrolled YubiKey PIV recipients | physical tap on the YubiKey (`touch-policy=always`) |

The XOR construction is information-theoretic: knowing one half
yields *literally no bits* of the master key. Either factor alone
forces a `2^256` brute force, not the `2^128` you would get with
key-bit truncation.

**Modules**:

- [`key_split.py`](local_scribe/security/key_split.py) — pure crypto (XOR + length checks)
- [`secret_store.py`](local_scribe/security/secret_store.py) — Keychain-side `kc_half` API
- [`yubikey_backup.py`](local_scribe/security/yubikey_backup.py) — `age` wrapping of
  `yk_half` + multi-recipient enrollment
- [`key_lifecycle.py`](local_scribe/security/key_lifecycle.py) — orchestrator
  (`init`, `unlock`, `rotate`, `add_yubikey`, `dr_restore`,
  `migrate_v1_to_v2`) plus a thin `python -m key_lifecycle` CLI
- [`bin/touchid_keychain.swift`](bin/touchid_keychain.swift) — Swift
  bridge that gates Keychain access by Touch ID; now accepts
  `--account NAME` so we can address `kc_half` separately from the
  legacy whole-key item during migration

**Operator surface**:

| command | what it does |
|---|---|
| `./run.sh key init [--no-dr] [--force]` | enroll YubiKey + generate master key + split + persist both halves + (optional) passphrase-encrypted disaster-recovery backup. `--force` requires typed `REPLACE` + YubiKey tap + auto-snapshot |
| `./run.sh key unlock` | smoke-test: Touch ID + YubiKey tap → prints per-service token *fingerprints* (never the tokens themselves) |
| `./run.sh key status` | JSON snapshot, no prompts (safe for cron) |
| `./run.sh key rotate` | generate a fresh master + replace both halves; requires typed `ROTATE` + YubiKey tap + auto-snapshot of the prior halves |
| `./run.sh key add-yubikey RECIPIENT` | enroll a second YubiKey; re-wraps `yk_half`. Requires YubiKey tap + auto-snapshot of the prior `yk_half.age` |
| `./run.sh key dr-restore` | recover from passphrase-protected age backup. When a live v2 install exists, requires typed `RESTORE-AND-OVERWRITE` + YubiKey tap + auto-snapshot |
| `./run.sh key migrate` | walk a legacy v1 whole-key install over to v2 split-key; idempotent. Auto-snapshots the v1 Keychain item |
| `./run.sh key destroy` | delete every key artefact. Requires typed `DESTROY` + YubiKey tap + auto-snapshot. **Reversible by default** via the snapshot |
| `./run.sh key destroy --purge-everything` | THE only irreversible operation. Requires `DESTROY` *and* `PURGE-EVERYTHING` typed confirmations + YubiKey tap. Wipes every snapshot too |
| `./run.sh key backups list` | enumerate pre-flight snapshots (newest first) |
| `./run.sh key backups prune <id>` | delete one snapshot directory + its Keychain backup account (typed `DELETE`) |
| `./run.sh key backups restore-kc-half <account>` | roll back the live `kc_half` from a snapshot's Keychain backup account (typed `RESTORE` + Touch ID) |

**Safety-by-default for every destructive op** — physical presence
(YubiKey tap) is required *before* state changes, and a pre-flight
snapshot of the soon-to-be-replaced material is written to
`~/.config/local_scribe/key-backups/<ts>-<op>/` so the operation is
reversible until the operator explicitly prunes the snapshot.
See [`KEY_SAFETY.md`](docs/KEY_SAFETY.md) for the full enumeration of
18 data-loss scenarios (S1–S18), the mitigation tied to each one,
and a recovery flowchart.

**Disaster recovery**: split-key means losing *either* factor is
fatal on its own. `init` therefore offers (and strongly recommends)
a passphrase-encrypted `age` copy of the **whole** master key at
`~/.config/local_scribe/disaster_recovery.age`. The passphrase is
read from `/dev/tty` (no echo, never on argv) and never logged.
Owners are expected to write it down on paper and store it offline.

**What this layer defends against**:

- **Adversary #4 (shell-as-user, no Touch ID)** — Keychain ACL
  refuses to read `kc_half` without Touch ID. Even if the shell
  steals every other byte on disk, it does not get the master key.
- **Adversary #6 (phished Touch ID)** — phishing a biometric still
  doesn't give you the YubiKey; without `yk_half` there is no
  master key. The Touch ID phish goes from "data loss" to "annoying".
- **Adversary #7 (root / TCC bypass / kernel)** — a Keychain
  exfiltration alone yields 32 uniform-random bytes, not the master.
  The attacker still needs to either (a) steal the YubiKey or (b)
  social-engineer the DR passphrase.

**What this layer does *not* defend against**:

- A full kernel implant during a live session can read the
  reconstituted master key out of process memory after a legitimate
  Touch ID + tap. We don't claim TPM-isolated execution.
- An attacker who has compromised both the Mac *and* obtained
  physical possession of an enrolled YubiKey (with Touch ID
  credentials they can present) can unlock as the legitimate user.
  That's "lost laptop + lost YubiKey" — at that point the DR
  passphrase is the only remaining secret.

**Threat-model invariants enforced by tests** (in
[`tests/test_key_lifecycle.py`](tests/test_key_lifecycle.py)):

- The master key never appears on the `argv` of any subprocess
  spawned during `init` / `unlock` / `rotate`. Verified by spying
  on `subprocess.run`.
- No file under `~/.config/local_scribe/` contains the master key
  bytes after `init` (walks every file, byte-search). The only
  ciphertext containing the master is `disaster_recovery.age`,
  which is passphrase-locked.
- All key material flows over Keychain ACL → stdin → in-process
  buffers. `LOCAL_SCRIBE_MASTER_KEY_HEX` (test/CI bypass) is the
  only intentional exception and is loud about itself.

**Status**: modules + CLI + tests all implemented and committed.
The `./run.sh key …` UX is the recommended path for all new installs.
Existing v1 installs migrate automatically on the first `unlock`.

---

## Self-attestation — why we layer integrity checks on top of macOS's built-ins

> **Status: shipped + audited.** Defense layers 5, 6, and 7 below
> all do their own integrity work even though macOS already runs a
> stack of platform-level checks. This section frames *why*: what
> Apple covers, what Apple doesn't, what our layer adds, and the
> honest cost of being belt-and-braces about it. Read this once
> before layers 5–7 so the rest of the chapter doesn't read as
> reinventing-the-wheel.

### What macOS already gives us

Apple's platform stack runs five overlapping integrity / safety
mechanisms before any local_scribe code executes:

| Apple-side check | Trigger | What it verifies | What it doesn't |
|---|---|---|---|
| **SIP** (System Integrity Protection) | kernel boot | `/System`, `/usr` (minus `/usr/local`), `/sbin`, `/bin` are read-only even for root, root can't attach to OS processes via `task_for_pid` | Doesn't protect `/Users/...`, `/usr/local`, `/opt/homebrew`, `/Applications`, or anything under `$HOME`. **All** of our scripts, the Char.app bundle, the operator's master key, the encrypted vault, and the pinned config live in those regions and SIP is silent on them. Covered in [§ Defense layer 0](#defense-layer-0--system-integrity-protection-mandatory). |
| **Gatekeeper** | first launch of a downloaded `.app` carrying the `com.apple.quarantine` xattr | Checks that the bundle is code-signed by a known Apple Developer ID and that Apple's notarization service issued a ticket for that exact build | Runs **once** per app per quarantine xattr. After the operator clicks through, every subsequent launch (and every modification to the app's resources) skips Gatekeeper entirely. Doesn't pin to a *specific* developer or build — any validly notarized app from any developer passes. |
| **Notarization** | server-side, when the developer uploads the build to Apple | Apple scans the upload for known malware signatures and issues a stapled ticket the bundle can carry | Apple's malware DB only — no app-specific allow-listing. **Notarized != audited**: Apple has notarized known-malicious binaries that slipped past signature scans, and Apple revocation can lag the discovery by days. |
| **XProtect / XProtect Remediator** | every executable load, every download, periodic background scan | Pattern-matches known-malware YARA signatures (XProtect) and remediates resident infections (XProtect Remediator). Updated by Apple via XProtectPlistConfigData + XProtectPayloads | **Signature-based**. Only catches malware Apple has already characterised. Says nothing about "this binary used to be X and is now Y", which is the integrity question we actually care about for our pinned dependencies. |
| **TCC** (Transparency, Consent, Control) | first time an app asks for camera, mic, full-disk, screen recording, etc. | Per-bundle-id permission grants stored under `~/Library/Application Support/com.apple.TCC/TCC.db`, gated by user prompts and FileVault-encrypted at rest | TCC is bound to **bundle ID**, not binary identity. A malicious replacement of Char.app keeps Char's microphone grant. Also: a `tccutil reset Microphone` operator command silently revokes consent without telling local_scribe. |

Plus, where relevant: the **App Sandbox** (if the developer opts
in, which Char does not for the full bundle), the **Hardened
Runtime** (Char does opt into this — it constrains library
injection + arbitrary-page exec), and **codesign verification**
on dynamic library loads (catches some swap attacks but
short-circuits as soon as a `@rpath`-resolved library is on disk
with the right signing).

### The gap those checks leave

The combined Apple stack answers "is this binary cryptographically
signed by *someone* Apple recognises, and does it match malware
signatures Apple has already shipped?" That is not the question
local_scribe needs to answer. The questions we actually need to
answer at every `./run.sh start` are:

1. **Identity, not just legitimacy.** Is this `Char.app` the
   *exact* notarized build the operator originally blessed (CDHash
   + Bundle ID + Team ID + linked-library prefixes), or a
   different-but-still-validly-signed build that Char.dev pushed
   without my review?
2. **Drift, not just initial state.** Did anything modify our own
   scripts (`run.sh`, `local_scribe/security/*.py`, the sandbox
   profile) since the operator last saw them, even if those edits
   came from a process running as the same user (Gatekeeper
   doesn't care about user-owned file edits)?
3. **Operator binding, not platform binding.** Is the
   configuration this start is about to use the configuration the
   operator authenticated with their YubiKey + Touch ID, or
   something a different process on the same machine substituted?
4. **Continuous, not point-in-time.** Has any of the above
   changed between two consecutive starts, given that Gatekeeper
   ran once at first launch and won't run again?

None of those are questions Apple's stack is built to answer for
third-party userspace binaries. They're the questions our
Defense layers 5–7 *are* built to answer.

### What our layer adds

| Our layer | What it verifies | How it complements Apple's stack |
|---|---|---|
| [`script_integrity.py`](local_scribe/security/script_integrity.py) gate at every `./run.sh start` | SHA-256 over every git-tracked working-tree file, compared against a baseline blessed by the operator at install. Detects drift in our own scripts, sandbox profile, signed-config loader, etc. | Apple does nothing here — these files live under the operator's `$HOME` and are owned by their UID, so SIP and Gatekeeper are silent. Without this layer a same-user attacker can silently rewrite `egress_proxy.py` to leak transcripts and `./run.sh start` would never notice. |
| [`char_integrity.py`](local_scribe/char/char_integrity.py) gate at every `./run.sh start` | Char.app's bundle CDHash, Team ID, Bundle ID, and linked-library prefixes all match the pinned baseline (`PINNED_TEAM_ID`, `pinned.json`, `~/.config/local_scribe/char_baseline.json`). DMG SHA-256 verified at install (`./run.sh char install`). | Gatekeeper has already run once and is asleep; Apple's notarization accepts *any* Char.dev build, including one Char.dev shipped *after* I audited a specific version. We pin to *that* version's identity, not the developer's identity. |
| [`signed_config.py`](local_scribe/security/signed_config.py) — operator HMAC over `pinned.json` + `char_baseline.json` | An attacker who can write to `~/.config/local_scribe/char_baseline.json` (or even to the in-repo `pinned.json`) can't quietly relax the pin without also forging an HMAC keyed by the master key. Forging the HMAC needs Touch ID + the operator's YubiKey. | Apple's stack has no concept of "the operator's intent" — it trusts whoever signed the upstream binary. The HMAC layer binds *our* trust to the operator, not to the upstream supply chain. |
| [`tools/secret_scan.sh`](tools/secret_scan.sh) pre-commit hook | Refuses to let a real `sk-…`, age-secret key, PEM block, etc. land in a tracked file in the first place. | Apple doesn't intervene in `git commit` at all — this is purely a contributor-side guardrail that complements GitHub's own server-side push protection. |

### Honest pros and cons of this duplication

Pros (what we get by re-checking even though Apple already
checked something):

* **Identity pinning, not vendor pinning.** Apple says "this is
  a validly notarized app". We say "this is *the* Char.app build
  the operator audited and blessed". Those are very different
  trust statements, and the second one is what actually matters
  for a transcript pipeline.
* **Continuous verification, not point-in-time.** Gatekeeper
  runs once. We run on every `./run.sh start`. If the bundle
  changes — auto-update, malicious swap, accidental dev-mode
  build — the next start fails closed.
* **Drift signal, not signature match.** XProtect catches
  *known* malware. Our integrity gate catches *any* change from
  the blessed state, including a one-byte edit that flips a
  feature flag. That's a much higher-resolution signal for a
  privacy-sensitive pipeline.
* **Operator-bound trust.** The HMAC layer binds verification
  to the operator's hardware token. Apple's stack is platform-bound;
  if Apple's notarization service is ever subverted (or a
  Developer-ID cert leaks), an attacker can still produce a
  legitimate-looking Char update. Our HMAC layer cannot be
  forged without the operator's YubiKey *and* Touch ID,
  independent of Apple's chain.
* **Defense in depth.** Each layer catches different threats. A
  same-user attacker who bypasses one (e.g. patches
  `script_integrity.py` itself) trips the next (the HMAC over
  the baseline file doesn't match anymore).
* **Auditability.** Apple's checks are opaque — you can read
  `man codesign` but you can't read the actual logic
  XProtect Remediator uses. Our checks are 1500 lines of Python
  with named tests; you can prove what they verify and when.

Cons (the honest cost of layering):

* **More code = more attack surface.** Every line of integrity
  code is a line that can have a bug. We mitigate by keeping the
  primitives small (HKDF + HMAC + SHA-256, no clever crypto),
  hard-failing on signature mismatch, and gating the
  integrity-script itself behind the same operator HMAC.
* **Software-only.** Userspace integrity is fundamentally
  software-only on macOS today; a kernel-level attacker
  (Adversary #7 in the threat table) can patch the verifier
  before it runs. This is the explicit boundary in
  [§ Future direction — trusted execution environment](#future-direction--trusted-execution-environment)
  — if Apple Silicon ever exposes a userspace TEE, much of this
  layer collapses into a hardware-rooted attestation check.
* **We don't replicate XProtect.** Apple's malware signature
  database isn't something a small project can mirror, and we
  don't try. We catch *drift* in our specific pinned dependencies;
  Apple still does the heavy lifting against general-purpose
  malware payloads, and a SIP-disabled or XProtect-disabled host
  is out of scope (Defense layer 0 hard-fails the start).
* **Maintenance overhead.** Every legitimate Char.app update
  trips `char_integrity_gate` until the operator re-blesses the
  new baseline (`./run.sh char baseline-update` → Touch ID +
  YubiKey). The same is true of our own script edits
  (`./run.sh integrity bless`). This is intentional friction —
  if updates didn't require a re-bless, the gate would be
  decorative — but it's friction.
* **False positives are real.** Char's auto-updater can ship a
  patch release while local_scribe is running; the next start
  fails until the operator either downgrades or re-blesses. The
  doctor output suggests both recovery paths and points at the
  changed CDHash so it's clear what shifted.
* **Performance.** SHA-256 over the entire git working tree +
  Char's binary on every start. Measured around 60–120 ms on an
  M-series Mac with a warm filesystem cache. Inside the noise
  for an interactive pipeline; not zero.

### What each layer of trust ultimately roots in

```text
   Apple notarization              ← upstream supply chain,
        ↓ (catches: known malware,    trust-on-first-use
          unsigned binaries)
   Gatekeeper (first-launch only)  ← UID owns Char.app after this point
        ↓
   SIP (kernel-enforced)           ← read-only system volumes only
        ↓
   ───── userspace boundary ─────
        ↓
   script_integrity_gate           ← *our* scripts, every start
        ↓
   char_integrity_gate             ← Char.app identity, every start
        ↓
   pinned_config_gate              ← operator HMAC, every start
        ↓                            (root of trust: operator's
                                      YubiKey + Touch ID)
   service runs
```

Apple's chain protects the *floor* — without SIP, Gatekeeper, and
notarization, a privilege-escalation footgun would put everything
below the userspace boundary in scope. Our chain protects the
*specific identity + configuration* we depend on, bound to the
operator's hardware token rather than to Apple's platform-wide
trust. Both chains have failure modes; running both in series
means an attacker needs to compromise both to silently exfiltrate
audio.

---

## Defense layer 5 — Char-settings enforcement

[`char_audit.py`](local_scribe/char/char_audit.py) is the runtime contract check.
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

## Defense layer 6 — Signed pinned config

### The problem

Layers 0–5 above are only as good as the constants that drive them.
"Char must be signed by team `6SLY7V277V`" is a strong statement —
until something flips that `6SLY7V277V` to an attacker's Team ID in
the file that holds it. The same applies to:

* the pinned `CHAR_KNOWN_GOOD_VERSION` (`1.0.24`) and the matching
  DMG SHA-256s the bootstrap install verifies against;
* the pinned `PINNED_BUNDLE_ID` (`com.hyprnote.stable`) that the
  bundle-identity check requires;
* the LM Studio version pin;
* the operator-set `char_baseline.json` (the recorded CDHash + every
  Mach-O sha256 inside the bundle), which is the input every
  subsequent `./run.sh char check` compares against.

Two attack paths:

1. **Local rewrite.** Something running as the operator (a poisoned
   editor extension, a script that slipped through bootstrap, a
   downloaded "helper" the operator ran once) flips a single hex
   character in `local_scribe/common/pinned.json` or
   `~/.config/local_scribe/char_baseline.json`. The next `./run.sh
   start` happily trusts a Char bundle the operator never approved.
2. **Supply-chain rewrite.** A malicious commit lands upstream that
   bumps the pinned hashes to a backdoored Char build. The operator
   runs `git pull` and `./run.sh start` without reading the diff.
   The git-tracked baseline ([script-integrity gate](#relationship-with-the-script-integrity-gate)) marks
   the change as "well, HEAD moved" and doesn't object.

### The control

Both files now carry an **operator HMAC** stored as a `.sig` sidecar:

| protected file | location | sidecar |
|---|---|---|
| `pinned.json` (in-tree, ships with repo) | `local_scribe/common/pinned.json` | `local_scribe/common/pinned.json.sig` |
| `char_baseline.json` (per-machine, operator-set) | `~/.config/local_scribe/char_baseline.json` | `~/.config/local_scribe/char_baseline.json.sig` |

The HMAC is **HMAC-SHA256 over the raw file bytes**, keyed by an
HKDF-domain-separated subkey of the master key:

```
signing_subkey = HKDF-SHA256(
    ikm    = master_key,             # 32 bytes, reconstituted via
                                     # Touch ID kc_half ⊕ YubiKey yk_half
    salt   = b"local_scribe.signed_config.v1",
    info   = b"config-sign:v1",
    length = 32,
)
```

The salt + info are distinct from every other HKDF use in the
codebase (compare [`service_auth.derive_service_token`](local_scribe/security/service_auth.py)
which uses `salt=HKDF_SALT` + `info=f"service:{service}"`), so a leak
of a service bearer token can't be replayed as a config signature
and vice versa.

A 6-hex **key fingerprint** is recorded in the sidecar header
alongside the HMAC. It lets `config verify` distinguish "you rotated
the master key, this signature is stale" from "someone tampered with
the file" so the recovery instructions are the right ones.

Sidecar format (two ASCII lines, deliberately human-readable):

```
local_scribe-sig-v1 fp=832c8a alg=hmac-sha256
1bb2c97e3d4f5a6789…  ← 64 hex chars of HMAC-SHA256 over the file bytes
```

See [`local_scribe/security/signed_config.py`](local_scribe/security/signed_config.py)
for the implementation and [`local_scribe/common/pinned.py`](local_scribe/common/pinned.py)
for the loader.

### How it gates startup

The signature is **hard-checked** at `./run.sh start` time by a new
gate that sits between `script_integrity_gate` and
`char_integrity_gate` (layer 5's runtime cousin):

```
sip_gate                  # SIP enabled (layer 0)
  → master_key_gate         # operator has a master key on this machine
  → script_integrity_gate   # git-tracked working-tree drift
  → pinned_config_gate      # ← THIS LAYER: HMAC over pinned + baseline
  → char_integrity_gate     # Char.app codesign/Gatekeeper/CDHash check
```

`pinned_config_gate` runs `python -m local_scribe config verify`,
which unlocks the master key (Touch ID + YubiKey tap), HMACs every
file in the roster, and refuses to start on any mismatch. Failure
modes get distinct exit codes so the banner can suggest the right
remediation:

| exit | failure | recovery |
|---|---|---|
| 10 | missing sidecar — operator never signed | `./run.sh config sign` |
| 11 | HMAC mismatch — file changed since signing | `git diff` the file, then `./run.sh config sign` to re-bless OR `git checkout` to revert |
| 12 | key-fingerprint mismatch — master key was rotated | `./run.sh config sign` |
| 13 | other signed-config error | read the banner |

The escape hatch is `LOCAL_SCRIBE_ALLOW_UNSIGNED_CONFIG=1`, which
downgrades the gate to a warning line. It mirrors
`LOCAL_SCRIBE_ALLOW_DIRTY` / `LOCAL_SCRIBE_ALLOW_DIRTY_CHAR` and
exists for the pre-bootstrap chicken-and-egg case + emergency
recovery; the banner names it explicitly.

### Operator surface

* `./run.sh config status` — read-only snapshot for every file in
  the roster: file present, sig present, parseable, key fp. Does
  NOT unlock the master key.
* `./run.sh config show [--shell|--json]` — print the pinned values
  (also used by `run.sh` itself to source `CHAR_KNOWN_GOOD_VERSION`
  + friends in place of the hardcoded constants).
* `./run.sh config verify` — full HMAC verify with Touch ID + YubiKey;
  exits 0 / 10–13 per the table above.
* `./run.sh config sign` — bless every file in the roster with a
  single Touch ID + YubiKey tap. Automatically wired into
  `./run.sh char baseline-set` and `baseline-update` so an operator
  bumping the baseline doesn't need to remember the second step.

### Threat-model boundary

This layer assumes the master key itself is safe (Touch ID-protected
Keychain half + YubiKey-encrypted half — see layer 4). If both
factors are compromised, the attacker can sign anything they like.
Layer 4's two-factor design exists precisely so this isn't a single
point of failure.

It does **not** protect arbitrary user-tunable settings in
`~/.config/local_scribe/config.json` (ports, model names): those have
a different threat model — the worst case is a worse user experience,
not a weakened security check. Tampering with them does not let an
attacker run a different Char binary.

### Relationship with the script-integrity gate

[`script_integrity.py`](local_scribe/security/script_integrity.py)
covers every tracked `.py` / `.sh` / `.swift` in the working tree by
re-hashing against git HEAD. That catches a flipped byte in
`char_integrity.py` itself. It does NOT cover:

* data files (which is why `pinned.json` and the baseline get their
  own HMAC sidecars);
* the case where the working tree drift is the result of a `git
  pull` from upstream — `script_integrity` sees HEAD moved and is
  satisfied.

The two gates are complementary: the script-integrity gate is "are
the files I checked in untouched?", Defense layer 6 is "did *I* (the
operator on this specific machine) ever explicitly bless these
particular contents?"

<a id="future-direction--trusted-execution-environment"></a>

### Future direction — trusted execution environment

> **Both this layer and the script-integrity gate may be deprecated
> in the future.** They are software-only stand-ins for what would
> ideally be hardware-rooted remote attestation of the running
> process. Today on Apple Silicon:
>
> * The Secure Enclave handles key operations (Touch ID, hardware
>   keys) but does not execute general-purpose code on the
>   operator's behalf. We use it via the Keychain for `kc_half` —
>   the most we can extract from it given the current platform
>   surface.
> * macOS exposes no equivalent of TPM-style remote attestation to
>   userspace. We can't ask the kernel "prove cryptographically
>   that the process at this PID is running an unmodified
>   `local_scribe`"; the best we can do is re-hash the files on
>   disk before launch and trust SIP (Defense layer 0) to keep
>   anyone else from rewriting our heap once we're running.
> * [Apple Private Cloud Compute](https://security.apple.com/documentation/private-cloud-compute/)
>   proves Apple *can* build attestable, sealed enclaves with
>   public transparency logs. Whether equivalent surface ever lands
>   on the operator's local Mac is an Apple roadmap question, not
>   ours.
>
> If/when a userspace-accessible trusted execution environment with
> remote attestation arrives on Apple Silicon (whether a
> general-purpose TEE, a sealed launch context for codesigned
> binaries, or a local-mode of something PCC-shaped), the integrity
> story simplifies dramatically:
>
> | today | with a userspace TEE |
> |---|---|
> | `script_integrity.py` re-hashes every tracked file every start | hardware attestation of the binary that's actually running |
> | `signed_config` HMAC over `pinned.json` + `char_baseline.json` | bind to the attested measurement; tampered files can't get to a state where the runtime trusts them |
> | Operator does Touch ID + YubiKey to bless changes | TEE-rooted policy enforces who can modify what; signing-with-keys becomes a UX question, not a security one |
>
> The work in this file (and in
> [`script_integrity.py`](local_scribe/security/script_integrity.py))
> isn't wasted: it's the right design for the *userspace-only*
> threat model we actually have. If the platform surface improves,
> the layer above the userspace boundary moves up and we shed code,
> not invariants. Until then, hash-on-disk + operator HMAC is the
> best userspace approximation of "this binary is the one we built
> and you blessed".

---

## Defense layer 7 — secret-scan pre-commit hook

### Problem

Defense layers 0–6 stop an attacker from *running* code that
exfiltrates the master key, derived service tokens, or the
operator's API credentials. They do nothing to stop a well-meaning
contributor from `git add`-ing a file that contains those secrets
on a clean machine. A single `git push` over HTTPS to a public
remote moves the secret out of the threat model entirely — once
it's in a public packfile it's permanently public, even after a
force-push, because mirrors, CI logs, and GitHub's REST `events`
feed retain the blob.

The classes of footgun this layer addresses:

* **Direct paste**. An operator paste-tests a real `sk-proj-…`
  key inside `tests/char/test_char_audit.py` and forgets to swap
  it out for the synthetic `sk-proj-AAAAA…` fixture before
  committing.
* **Tool-dropped artefacts**. `age-plugin-yubikey` emitting an
  `AGE-SECRET-KEY-1…` line into `~/Downloads` and a contributor
  copying it into the repo for "convenience".
* **PEM private-key files** (TLS, SSH, code-signing) ending up
  inside the working tree because a build script wrote them
  there.
* **Operator-state directories** (`.config/local_scribe/`,
  `.cache/local_scribe/`) being recreated inside the repo root
  by a buggy test and ending up tracked.

### Control: client-side git hook + scanner

[`tools/secret_scan.sh`](../tools/secret_scan.sh) is a self-contained
bash scanner with zero non-stdlib dependencies. It runs in two
modes:

* `--staged` — diffs `git diff --cached --name-only --diff-filter=ACMR`,
  scans each staged path against a small, conservative
  high-signal regex catalog (PEM private-key blocks, age-secret
  keys, `sk-…`, `sk-ant-…`, `AKIA…`, GitHub `ghp_/gho_/ghu_/ghs_`
  PATs, Slack `xox[abprs]-…`, JWTs), plus a forbidden-path layer
  for `*.age`, `*.pem`, `*.key`, `*.p12`, `*.env`, `id_rsa`, etc.
  Used as a pre-commit hook.
* default (full repo) — same patterns, plus an optional
  trufflehog regex pass over the full git history if
  `trufflehog` is on `$PATH`. Used as a manual audit.

[`tools/install_git_hooks.sh`](../tools/install_git_hooks.sh)
writes a thin shim into `.git/hooks/pre-commit` that delegates
to the version-controlled scanner, so future edits to the scanner
take effect with no re-install. `cmd_bootstrap` invokes the
installer automatically whenever `.git/` is present, so
contributors get the hook on every fresh clone without remembering
to opt in.

### Conservative by design

The patterns are deliberately narrow. False positives turn the
hook into noise that contributors learn to bypass with
`--no-verify`, which is exactly the failure mode this layer is
meant to prevent. Two specific exclusions are baked in:

* **Sentry public DSN keys** of the form
  `https://<32hex>@…ingest.sentry.io/<projid>` are public by
  Sentry's own design (Sentry calls them "public DSNs"). Char's
  binary embeds one and that reference appears verbatim in
  `docs/CHAR_REVIEW.md`; the scanner allowlists it.
* **Synthetic test fixtures** that use long runs of a single
  character (`AAAAAAAA…`, `00000000…`, `deadbeef…`,
  `cafebabe…`) are allowlisted so the privacy-redaction tests
  in `tests/char/test_char_audit.py` don't trip the hook.

Both allowlists live inline in `tools/secret_scan.sh` so the
audit trail is alongside the rule itself, not buried in a config
file.

### Relationship with `.gitignore`

`.gitignore` is the *path* defense and the scanner is the
*content* defense. They overlap intentionally:

* `.gitignore` rejects entire file extensions (`*.age`, `*.pem`,
  `*.key`, `*.env`, `id_rsa`, `.config/`, `.cache/`, …) at the
  `git add` step, before the hook even runs.
* The scanner catches what `.gitignore` misses: a real secret
  pasted into a normally-tracked file (a `.py`, a `.md`, a
  `.sh`), or an explicit `git add -f` that bypasses
  `.gitignore`.

If you ever need to commit a file that the scanner flags, add
the path to `SKIP_PATH_PATTERNS` in `tools/secret_scan.sh` (or
extend the inline known-public exclusion list). Don't use
`--no-verify`; future commits would silently re-trip and the
first one that *is* a real leak goes through unannounced.

### Threat-model boundary

This layer is **client-side**. It protects against accidental
commits, not against a malicious contributor who has decided to
exfiltrate keys: they can disable the hook, edit the scanner,
or upload the secret over a different channel entirely. The
mitigations for that threat are organisational (code review,
post-merge audit) and infrastructural (server-side push
protection, e.g. GitHub's Secret Scanning + Push Protection),
not in `local_scribe`'s scope.

### Audit history

A full repo + history audit was performed (see
`./tools/secret_scan.sh` invocation log + trufflehog regex layer
over all 42 commits on `main`):

* **0 high-signal regex matches** across the entire git history.
* **12 unique entropy matches**, all verified public: Char DMG
  SHA-256s (pinned in `local_scribe/common/pinned.json`), the
  Char binary CDHash, a Sentry public DSN extracted from Char's
  binary (referenced in `docs/CHAR_REVIEW.md`), GitHub URL
  fragments to `github.com/fastrepl/anarlog`, and git commit
  SHAs from this project's own history.
* **One software smell remediated**: `local_scribe.security.key_lifecycle._cli_rotate`
  previously emitted `master_key[:4].hex()` as a stdout
  "fingerprint" — a 32-bit leak of raw key material to shell
  scrollback / screen captures. Replaced with
  `signed_config.fingerprint()` (HKDF-derived, leak-safe).

---

## Beyond the local machine — why TLS alone isn't enough

> **Status: design only.** `local_scribe` does **no off-machine
> traffic today** — the data plane is loopback and the firewall
> blackholes the rest (see Defense layer 1). This section is the
> crypto design constraint that any future Tailscale-VPN /
> private-cloud-LLM path inherits, and the reason a future
> `local_scribe-cloud` build cannot simply be "stick HTTPS on it and
> call it done". The corresponding design exploration is in
> [`TODO.md` § "Multi-tenant / org deployments"](TODO.md). The
> README's [§ Future direction — private-cloud transcription over
> Tailscale](README.md#future-direction--private-cloud-transcription-over-tailscale)
> is the user-facing version of this.

### The "harvest now, decrypt later" problem with classical HTTPS

The naive "we'll just put TLS on it" answer is wrong for call audio,
for three independent reasons.

**1 . Long-term-key compromise reveals past sessions if PFS is missing.**
Pre-TLS-1.3 cipher suites that used RSA key transport (e.g. the once-
ubiquitous `TLS_RSA_WITH_AES_*`) reused the server's long-term private
key to wrap the session key. Anyone who recorded the ciphertext and
*later* obtained that private key — through a server breach, a
subpoena, a corrupt employee, a Heartbleed-class memory disclosure —
could decrypt every recorded session retroactively. This is exactly
the "harvest now, decrypt later" / Snowden-era model. TLS 1.3 *requires*
ephemeral Diffie-Hellman (X25519 / P-256/384/521 ECDHE), which gives
per-session forward secrecy — a future cert compromise cannot decrypt
yesterday's recording. **But you only get that property if every
peer in the path negotiates TLS 1.3 (or TLS 1.2 with an `ECDHE_*`
cipher suite) and nobody downgrades.** Defaults are *better* than they
used to be; defaults are not the guarantee.

**2 . Quantum harvest-now-decrypt-later is real and applies to
strategic audio.** ECDH over Curve25519 / NIST P-curves is broken by
Shor's algorithm on a sufficiently large quantum computer. Estimates
for when that machine exists vary from "10 years" to "never", but the
*relevant* timescale for the data we're handling — call audio with a
strategic shelf-life (legal, financial, M&A, customer-research,
medical) — is *decades*. Anyone willing to record TLS 1.3 ciphertext
today and decrypt it in 2040 is the threat model. The mitigation is
post-quantum KEMs (ML-KEM / Kyber, currently shipping as TLS 1.3
hybrid `X25519MLKEM768` in Chrome and Cloudflare); we expect to
inherit them from whatever Tailscale, AWS Nitro, and Apple PCC ship,
but it has to be a *checked* property, not an assumption.

**3 . TLS terminates at the endpoint, after which the data is
plaintext.** TLS is a *transport* protocol. It protects bytes on the
wire. It does not protect bytes:

- in the receiving process's RAM during inference,
- in the host VM's kernel buffers,
- in the load balancer between the public IP and the actual app
  server (TLS terminates at the LB; the LB→app hop is plaintext or
  separately wrapped),
- in any debug log / sampling profiler / APM agent / Sentry envelope
  the receiving process happens to emit,
- in swap, hibernation files, or core dumps on the receiving host.

The classical cloud-API pattern "POST audio over HTTPS to the
provider" gives you wire-level privacy and *zero* of the above.

**4 . The PKI's weakest CA is the security floor.** TLS authenticates
"the holder of a cert chain rooted in one of 100+ CAs trusted by your
OS". An attacker who can convince *any* one of those CAs to issue a
cert for your enclave host (Symantec did this for Google in 2015;
DigiNotar was breached in 2011; the failure modes are not theoretical)
can MITM the connection without breaking any cryptography. Certificate
pinning fixes this; very few applications actually pin.

### What we plan to do instead, by extension of the current local design

For the off-machine path, the wire crypto is necessary but not
sufficient — it has to be composed with three other primitives, all of
which exist in the current local design and extend cleanly:

| primitive | local design today | extension to off-machine |
|---|---|---|
| **per-operation ephemeral keys** | `service_auth` derives a fresh token from the master key via HKDF; the master never leaves process RAM | per-session DH between laptop and enclave's *attested* ephemeral pubkey; rekey on every Char "Generate" so a single key compromise reveals at most one transcript |
| **hardware-anchored identity** | YubiKey (PIV) + Secure Enclave (Touch ID) | same YubiKey signs the laptop's side of the mTLS handshake to the enclave; cloud side's identity key lives in CloudHSM (never extractable) |
| **measure-and-attest before sending** | Char binary's CDHash is pinned + checked at startup ([`CHAR_REVIEW.md`](docs/CHAR_REVIEW.md)) | the enclave's PCR measurements (Nitro NSM doc, Apple PCC verifiable build, AMD SEV-SNP report) are checked *before* the audio key is wrapped to the enclave's ephemeral pubkey |
| **MFA-gated key release** | `master_key = kc_half XOR yk_half`, both factors per op | same plus: cloud-side DEK only released by CloudHSM after the HSM has independently verified the enclave's attestation document |

That stack gets you the same guarantee for off-machine processing
that the current local design gets you on-disk: **no point in the
data flow at which a year-from-now compromise yields a year-old
transcript**.

### How this compares to Signal's wire crypto

[Signal](https://github.com/signalapp) — `whispersystems/signal-protocol`,
now `signalapp/libsignal` — solved a closely related problem (end-to-
end encrypted IM where the server is untrusted) and the design
principles map onto what we want for `local_scribe`'s cloud path
remarkably well. Worth understanding what they do, what they assume,
and where we diverge.

**Signal protocol = X3DH + Double Ratchet.**

- **X3DH (Extended Triple Diffie-Hellman)** establishes an initial
  shared secret between two parties who have never spoken before. It
  combines four DH outputs — identity key (long-term), signed prekey
  (medium-term, rotated weekly), one-time prekey (consumed once),
  ephemeral key (fresh) — and hashes them into the root key. The
  Signal server stores prekeys but cannot derive the session key:
  it never holds a long-term decryption key for any session, *by
  construction*.
- **Double Ratchet** then evolves the session key on every message.
  Two ratchets compose:
  - The **symmetric chain ratchet** derives a new AEAD key for each
    message from the prior chain key via HKDF; the old key is
    immediately wiped. Compromise of message N's key does not reveal
    messages 1..N-1 (**forward secrecy**).
  - The **DH ratchet** attaches a new ephemeral public key to every
    reply; both sides re-derive a new root key. Compromise of the
    current chain key does not reveal messages from the *next*
    ratchet step onward (**post-compromise security**, sometimes
    called *self-healing* or *future secrecy*).
- **Out-of-band identity verification.** Safety numbers /
  fingerprints let two users confirm they're talking to each other,
  not to a CA-signed impostor.
- **Sealed sender + private contact discovery in SGX enclaves** — the
  server doesn't even learn who's messaging whom.

**Comparison table.**

| property | classical HTTPS (TLS 1.3, no app crypto) | Signal (X3DH + Double Ratchet) | `local_scribe` today (all-local) | `local_scribe` planned cloud path |
|---|---|---|---|---|
| forward secrecy | yes, per session | yes, per **message** | n/a (no network) | yes, per Char "Generate" (rekey on each batch) |
| post-compromise security | no — session key persists until renegotiation | yes — restored on next DH ratchet step | n/a | yes — new attestation + ephemeral key on every batch |
| long-term decryption key on server? | yes — the TLS cert's private key | no — server only stores prekeys + ciphertext | n/a | no — enclave's keypair is ephemeral per attestation |
| can the host operator read plaintext at the endpoint? | yes — TLS terminates in their process | no — endpoints are user devices | n/a | no — TLS terminates *inside* the attested enclave; the host VM sees ciphertext only |
| keys protected by tamper-resistant hardware? | usually no | Secure Enclave / StrongBox on the device | YubiKey + Secure Enclave + KEK split | YubiKey (client) + CloudHSM/YubiHSM (server) + TEE (Nitro / Apple PCC) |
| MITM by a compromised CA defeated? | only if the client pins certs | yes (TOFU + safety numbers, no PKI dependency) | n/a | yes — trust anchor is the *attestation root* (AWS Nitro root, Apple PCC verifiable-build root), not the WebPKI |
| metadata minimisation | no (TLS SNI leaks hostnames) | yes (sealed sender) | n/a — single-tenant | inherited from Tailscale + enclave architecture |

**Where we'd intentionally diverge from Signal.**

Signal is designed for *asynchronous IM between two human users on
phones*. We're designing for *one user → one enclave, request/response
audio streaming, finite session lifetime*. That means:

- We don't need the asynchronous-prekey machinery; both ends are
  online during the session. A two-round attested handshake is
  enough — no need to publish prekeys to a server.
- We don't need a chat ratchet that runs for years across thousands
  of messages with skipped-message tolerance. A session ratchet that
  rekeys per batch is enough.
- We *do* need verifiable-build / attestation, which Signal handles
  via reproducible-build practices for the *client* but not for the
  server. For us the server-side TEE is load-bearing because the
  whole point is that the cloud LLM is a single point of trust we
  have to constrain.
- Identity verification is rooted in the YubiKey + Touch ID MFA
  the README threat model already describes — not in safety
  numbers exchanged between humans.

The principle we steal verbatim from Signal: **don't put a long-term
key on the server.** Everything else falls out of that. TLS today
violates this principle for the convenience of letting the server
keep one cert for a year; HSM-backed attestation gates undo the
violation.

### What CloudHSM, YubiHSM, and your personal YubiKey are actually for

A general-purpose CPU running a TLS server keeps the cert's private
key in process memory. Anything with code execution as that
process — a debugger, a kernel implant, a malicious update, `root`,
a memory-disclosure bug like Heartbleed, a crash-handler that
serialises heap to disk, a corporate snapshot tool — can lift the
bytes. A single memory dump turns into:

- ability to MITM all future sessions of that cert,
- ability to decrypt all *past* sessions for any cipher suite without
  PFS,
- ability to forge signatures that downstream systems will trust.

An **HSM** (Hardware Security Module) is the small purpose-built
chip whose job is to hold private keys and *never let them leave*.
The contract is narrow on purpose:

- Keys are generated **on-chip**; there is no API that returns a
  plaintext private key. You can ask the HSM to *use* a key (sign,
  decrypt, wrap, derive), never to *give* it to you.
- Tamper-evident, often tamper-responsive: drilling the case, lowering
  the voltage past a threshold, or exceeding a temperature window
  zeroises the keystore. The chip is **FIPS 140-2 Level 3** or
  **140-3** validated.
- All operations are logged inside the device, with optional **M-of-N
  quorum** on administrative changes ("two of these five smartcards
  must be present in their slots to delete this key").
- PIN / authentication failures are **rate-limited in hardware** —
  guessing a 6-digit PIN takes years, not seconds.

For our threat model, three classes of HSM matter:

| class | example | what it protects, in our design |
|---|---|---|
| **personal-class HSM** | YubiKey 5 (PIV slot 9d) | `yk_half` is `age`-encrypted to the YubiKey's PIV public key. Decrypting requires the *physical* key plugged in *and* a fresh metal-contact tap (`touch-policy=always`). No software, however privileged, can extract the PIV private key from the YubiKey — the chip has no API for it. This is the "something you have" anchor of our MFA. |
| **on-prem appliance HSM** | YubiHSM 2 (USB-A device, ~$650) | For an org running its own Mac Studio summarisation appliance: holds the long-term mTLS identity of the appliance, holds the KEK that wraps tenant DEKs, exposes only sign / wrap / unwrap through PKCS#11. Same chip-level guarantees as a YubiKey, more storage and bandwidth. See [`TODO.md` § "Option A — Self-hosted Mac Studio appliance"](TODO.md). |
| **cloud HSM** | AWS CloudHSM, GCP Cloud HSM, Azure Dedicated HSM | The cloud-resident version: a hosted FIPS 140-2 Level 3 cluster that the cloud provider provisions but does not have admin rights into. For the AWS Nitro Enclave path: CloudHSM holds the KEK; the enclave fetches an attestation document from the Nitro Security Module; CloudHSM verifies that document against an expected PCR set; only then does it release a wrapped DEK to the enclave for that one session. The cloud operator with root on the EC2 host **cannot** extract the KEK or impersonate the enclave. See [`TODO.md` § "Phase 3 deep-dive: how TEE attestation actually enforces 'no code has changed'"](TODO.md). |

**What HSMs do not protect against.** A common over-claim is "we
use an HSM so we're secure". HSMs solve *key extraction*, not the
adjacent problems:

- They do **not** protect plaintext that has already been decrypted
  for processing. The LLM still has to see the prompt as bytes in
  order to summarise it. That gap is what TEEs / confidential compute
  fill — and why the design pairs an HSM (for keys) with a TEE
  (for runtime), not just one or the other.
- They do **not** stop a phished tap. A YubiKey will happily sign
  whatever the host process asks it to sign while the metal contact
  is bridged; if the host is compromised, the signature happens for
  the attacker's chosen message. This is why every YubiKey prompt in
  `local_scribe` explains *what* it is signing (see § "Privileged-
  prompt UX" under Defense layer 1).
- They do **not** give post-compromise security on their own. Without
  a ratchet, an attacker who gets one signed token has it until it
  expires. The ratchet design above is what bounds the blast radius
  in time.
- A cloud HSM is **not** a TEE for the client code that *talks* to
  it. The Lambda / EC2 instance using the CloudHSM API still runs on
  a normal CPU with normal memory; if that process is compromised,
  it can still ask the HSM to sign / decrypt on its behalf during
  the compromise window. The defense is to pair the HSM with an
  attested enclave so only the enclave can talk to the HSM.

**The composed promise.** Layered, what you get is:

- HSM ⇒ "the key bytes never exist on a general-purpose CPU".
- TEE ⇒ "the plaintext bytes never exist outside an attested,
  measured runtime".
- Attestation ⇒ "the client cryptographically verifies *which*
  runtime, before sending anything".
- Per-session ephemeral keys + ratchet ⇒ "no single key compromise
  reveals more than one batch of audio".
- YubiKey + Touch ID at the laptop ⇒ "no batch leaves the laptop
  without a physically-present human authorising it".

Each layer is bypassable individually; together they encode the
guarantee that the README threat model promises — *call audio is
unreadable without simultaneous possession of the YubiKey, knowledge
of the user password, and the cooperation of an attested cloud
runtime that is provably running the version of `local_scribe-cloud`
you signed off on*. That property is what we lose if we let "just
HTTPS" be the answer, and what we recover by composing the
primitives above.

For the concrete plumbing — which API on which HSM gets called with
which attestation document, in which order, with which Terraform
module standing it up — see [`TODO.md` § "Multi-tenant / org
deployments"](TODO.md).

---

## How we audited the third-party surface

Two third parties matter: Char.app and LM Studio.

### Char.app

Full bottom-up audit in [`CHAR_REVIEW.md`](docs/CHAR_REVIEW.md), which is
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

## Verified end-to-end (last full check)

The combinations below were last exercised against the running stack
on the date of the most recent `docs:`-prefixed commit touching this
file. The checks are also run automatically by the test suite (498
passed + 13 subtests) so any regression should land as a red CI long
before it reaches a release.

| Check | Result | Where the invariant lives |
|---|---|---|
| Test suite (`pytest -q`) | 498 passed, 13 subtests | `tests/` |
| Doctor (`./run.sh doctor`) | all green except opted-out items | `cmd_doctor` in `run.sh` |
| ASR `/v1/audio/transcriptions` without bearer | 401 | `require_asr_token` |
| ASR with wrong bearer | 401 | `service_auth.make_token_dependency` |
| ASR with correct bearer + silence | 200, `{"text":""}` | `asr_server.py` |
| Inspector `/api/sessions` without auth | 401 | `auth_mw` |
| Inspector `/auth?token=…` | 302 + `HttpOnly; SameSite=strict` cookie | `inspector_server.py` |
| Audio download attachment header | `content-disposition: attachment` | `session_audio_download` |
| Transcript `.txt` download header | `content-disposition: attachment` | `session_transcript_txt` |
| Diarised transcript renders "Speaker 1/2/…" | yes | `_render_transcript_text` |
| `DELETE /audio` without body | 400, file untouched | `_require_typed_delete_confirm` |
| `DELETE /audio` with `{"confirm":"yes"}` | 400, file untouched | same |
| `DELETE /history/{name}` without body | 400, archive untouched | same |
| No ASR token / master-key hex on any local_scribe argv | clean | `pgrep python` + `pgrep run.sh` |
| `./run.sh configure-char` runs without `unbound variable` | clean | `run.sh` cmd_configure_char |
| Char audit endpoint | 8 checks, only WARN is firewall when disabled | `char_audit.py` |
| `./run.sh key status` JSON shape | `shape`, `kc_half_present`, `yubikey`, `disaster_recovery` keys | `key_lifecycle.status()` |
| Stale `./run.sh vault init` references | none remaining in operator-facing strings | `run.sh`, `asr_server.py`, `inspector_server.py` |

The two checks that *require* physical hardware to exercise (a YubiKey
tap + a fresh Touch ID prompt) are covered by `tests/test_key_lifecycle.py`
using `age` / `ykman` / `age-plugin-yubikey` shims — the shims observe
the same stdin/argv contract as the real binaries, so the shape of the
data flow is the same. Live human-in-the-loop verification of the
hardware path is documented in
[§ 7 + § 8](#7-typed-delete-confirm-body) below.

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

### 7. Typed-DELETE confirm body

With services running and the inspector cookie unset:

```bash
TOK=$(./venv/bin/python -m service_auth token inspector)
SID=$(curl -sS -H "Authorization: Bearer $TOK" \
  http://127.0.0.1:8001/api/sessions | python -c \
  'import json,sys; print(json.load(sys.stdin)["sessions"][0]["id"])')

# 1) No body — must fail without touching the file.
curl -sS -X DELETE -H "Authorization: Bearer $TOK" \
  http://127.0.0.1:8001/api/sessions/$SID/audio
# → {"detail":"missing confirmation body — send …"} (HTTP 400)

# 2) Wrong word — must also fail.
curl -sS -X DELETE -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"confirm":"yes"}' \
  http://127.0.0.1:8001/api/sessions/$SID/audio
# → {"detail":"confirmation mismatch — …"} (HTTP 400)
```

Verify the audio is still present (`ls`, `md5`) — the test fixture in
`AudioDeleteEndpointTests::test_delete_audio_rejects_wrong_confirm_value`
asserts this on nine malformed payloads.

### 8. End-to-end test

```bash
./venv/bin/python -m pytest tests/ -q
```

Should report **498 passed** (with 13 sub-tests). Coverage by area:
firewall round-trip — `tests/test_firewall.py`; auth gates —
`tests/test_asr_server.py::AsrServerAuthIntegrationTests` and
`tests/test_inspector_server.py::AuthTests`; typed-DELETE confirm —
`tests/test_inspector_server.py::AudioDeleteEndpointTests` and the
sister `HistoryDeleteEndpointTests`; Option C key lifecycle —
`tests/test_key_lifecycle.py`; argv-leak invariant —
`tests/test_char_settings_writer.py`.

---

## Forward-looking — operator UX, tamper alerts, and lock-time data hygiene

Three substantial pieces of operator-facing security work are
scoped in [`TODO.md`](TODO.md) but not yet shipped. They share a
common thread: a privacy-conscious operator needs to be able to
*see* the state of every defense layer above, and the laptop needs
to *defend itself* even when the operator has stepped away from
it. Tracked there in full detail; summarised here so the threat
model in the rest of this document reads correctly.

* **Web UI as the full operator control surface.** Promotes the
  inspector at `http://127.0.0.1:8001` from a read-only session
  browser to the single user-facing entry point for the entire
  stack — install, configure, operate, observe — with the
  existing CLI kept as the scriptable / headless fallback.
  Includes a real-time integrity status tile that surfaces the
  pass / fail state of `script_integrity_gate`,
  `char_integrity_gate`, `pinned_config_gate`, the signed-config
  HMACs, and the egress-proxy block log. Phased plan in
  [`TODO.md`](TODO.md#privacy--security-p0) covering inventory,
  read-only telemetry, service lifecycle, key + vault lifecycle,
  Char + firewall + sandbox controls, the bootstrap wizard, and
  polish. Settled trade-offs (auth model, process model, the
  decision to *not* tunnel `sudo` through the web UI, the
  confirmation pattern, theming) are documented inline with the
  plan.
* **Tamper-alert dispatch when the operator is away from the
  laptop.** Companion to the web-UI layer. An integrity gate
  failure or egress-proxy block today only surfaces in the
  doctor banner the next time the operator opens a terminal —
  useless if they're asleep, in a meeting, or out of the country
  with the laptop still on. Goal: deliver a signed,
  time-stamped alert to a *different* device the operator owns.
  Channel options + the honest trade-offs (Twilio SMS, SMTP,
  APNs push, Signal-CLI, operator-hosted relay) plus the
  credential-safety problem (any provider key on the laptop is a
  target for the same adversary that's tampering) are walked
  through in
  [`TODO.md`](TODO.md#privacy--security-p0). Realistic position:
  a tampering attacker can suppress alerts but cannot forge them
  as long as the receiver validates a signature with a key never
  on the laptop.
* **Auto-dismount the encrypted vault on screen lock; Touch
  ID-gated remount on unlock and on Char restart.** Today the
  vault stays mounted for the entire `./run.sh start` →
  `./run.sh stop` window. Auto-dismount on
  `com.apple.screenIsLocked` cuts the readable-data window down
  to "while the screen is unlocked". Modes (`soft`,
  `cooperative`, `strict`, `paranoid`) trade off Char-stability
  against unmount-aggressiveness; full mode table + UX gotchas
  (recording loss, Touch-ID prompt fatigue, sleep-vs-lock
  semantics, Char crash-on-data-dir-disappearance) are in
  [`TODO.md`](TODO.md#privacy--security-p0). Closes two specific
  threat-model gaps: Adversary #5 with a locked screen and a
  still-mounted vault, and Adversary #4's exfiltration time
  budget.

These items are documented as P0 in `TODO.md` precisely because a
privacy-conscious operator cannot reliably verify "is my pipeline
still untampered, and is my data still encrypted at rest right
now?" without them. They are not currently shipped; the threat
model elsewhere in this document treats their absence as part of
the current baseline, not as a regression.

### Forward-looking — split-host hardware deployment

A separate exploration document,
[`docs/HARDWARE.md`](docs/HARDWARE.md), works through the
threat-model implications of running Char + the local_scribe
wrapper on a roaming laptop while moving the ASR + LM Studio +
LLM services (and the master-key custody) onto a stationary
compute box. The split is interesting *because* it is the only
configuration in which we get hardware-rooted remote attestation
of the LLM-side code (via SEV-SNP / TDX) — which is the one piece
the userspace integrity gates documented in this file cannot
provide. The document also covers the more pragmatic Mac Studio +
YubiHSM 2 configuration as the recommended path for today, the
Framework Desktop (Strix Halo) + YubiHSM 2 alternative for a
Linux-native stack, and the stand-alone YubiHSM 2 option for
operators who want hardware-rooted key custody without splitting
hosts at all. The hardware-side decision feeds back into the
threat model in this file at two specific points: the kernel-mode
Adversary #7 gap (see [§ Threat model](#threat-model)) closes for
the LLM side under SEV-SNP / TDX, and the
[mach_vm_read window from Defense layer 0](#defense-layer-0--system-integrity-protection-mandatory)
closes for the master key as soon as the key moves into a YubiHSM
on either host.

---

## Out of scope

Things we deliberately do **not** defend against, with the rationale:

- **Root / kernel-level malware.** Once root is on the box, every
  defense in user-space is bypassable. The Keychain protects against
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
  defense here is auditing, not cryptography.
- **macOS itself.** We assume the OS kernel, the codesign verifier,
  and Apple's notarisation pipeline are honest. If they aren't,
  nothing in user-space helps.
- **Compromised network infrastructure** between your laptop and
  HuggingFace / GitHub during `bootstrap`. We verify SHA256s where
  publishers expose them (Char DMG), but model weights are downloaded
  over HTTPS with whatever signature scheme HuggingFace ships. A
  state-level MITM with a cooperating CA could substitute weights at
  download time.
- **Off-machine traffic in general — because there is none today.**
  The local pipeline emits zero outbound traffic during normal
  operation (the firewall blackholes everything that tries). The
  "harvest now, decrypt later" / Signal-comparison / HSM-attestation
  story in [§ Beyond the local machine](#beyond-the-local-machine--why-tls-alone-isnt-enough)
  is the design we will be held to if and when a Tailscale-VPN or
  private-cloud-LLM extension lands. Today, the off-machine threat
  model is mitigated by *not having* an off-machine path at all.

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
so we can update [`CHAR_REVIEW.md`](docs/CHAR_REVIEW.md) and the
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
| 2026-05-10 | Added § "Beyond the local machine — why TLS alone isn't enough": the harvest-now-decrypt-later limitation of classical HTTPS, a structured comparison to Signal's X3DH + Double Ratchet design, and the role of personal-class HSMs (YubiKey), on-prem appliance HSMs (YubiHSM 2), and cloud HSMs (CloudHSM) in the planned off-machine path. Forward-looking — gates the design of any future Tailscale / private-cloud build. |
