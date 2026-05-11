# Hardware exploration — split-host deployment options

> **Status: design exploration, not shipping documentation.** This
> document captures the trade-offs of splitting the local_scribe
> pipeline across two physical hosts — a roaming laptop running
> Char.app + the local_scribe wrapper, and a stationary "compute
> box" running the ASR + LLM services, ideally with stronger
> key-protection hardware than the laptop alone can offer. Nothing
> here is wired into the codebase yet; `./run.sh start` still
> assumes a single host. The point of writing this down is to make
> the trade-offs explicit *before* we start picking SKUs, so the
> hardware decision is led by the threat model rather than by
> which device the operator happens to have.

## TL;DR — what would I buy today

| Goal | Recommendation | Roughly |
|---|---|---|
| "Works tomorrow, MLX-accelerated, real TEE for keys" | **Mac Studio M4 Max 64–128 GB + YubiHSM 2** | $2,800–$4,500 |
| "Smaller / cheaper Apple Silicon, same software stack" | **Mac mini M4 Pro 64 GB + YubiHSM 2** | $1,900–$2,500 |
| "Compact, powerful LLM on Linux, lighter TEE story" | **Framework Desktop (Ryzen AI Max+ 395) 128 GB + YubiHSM 2** | $2,200–$2,800 |
| "Strongest TEE / attestation story; willing to pay rack-class" | **Bare-metal AMD SEV-SNP or Intel TDX box + dedicated GPU + YubiHSM 2** | $5,000–$15,000+ |
| "Tiny + portable + cheap" | **NUC-class mini-PC + YubiHSM 2** — *but accept slow LLM inference; SGX is not realistically available on modern NUC parts* | $1,200–$1,800 |

The single thread running through every recommendation is **add a
YubiHSM 2.** It costs ~$650, fits in a USB-A port, and gives every
host above a hardware-rooted key custody story that Apple's Secure
Enclave alone cannot (Apple SEP can't be a general-purpose key
custodian for arbitrary user code — see § "TEE primitives, named"
below). YubiHSM 2 is the missing piece regardless of which compute
box you pick.

The reason a stand-alone "great LLM box, no HSM" answer doesn't
exist is that the **TEE story** and the **LLM-throughput story**
are answered by different parts of the system: TEE / HSM by a
peripheral, LLM throughput by the host CPU + GPU + unified memory.
Once you accept that decomposition, the host choice becomes "what
runs the LLM well in a form factor I want" and the YubiHSM 2 is
the constant.

---

## Why split the hardware at all?

A single-host deployment (laptop runs Char + ASR + LM Studio +
LLM) is the simplest thing to ship and it's what `./run.sh start`
assumes today. It also has real costs:

1. **Thermals and battery.** Qwen3-30B at Q4–Q5 quantisation
   plus Parakeet ASR plus Char.app plus a browser plus a meeting
   = laptop fans on full, battery at half its rated life, palm
   rest at 45 °C. On any machine smaller than an M4 Max
   MacBook Pro, the LLM is the bottleneck.
2. **Memory pressure.** Qwen3-30B Q5 is ~22 GB resident, with KV
   cache for a long context another 6–12 GB. ASR adds 1–2 GB.
   Char + browser + OS easily take another 10–15 GB. **40+ GB
   of working set is realistic during an active meeting.** A
   16 GB or 24 GB laptop swaps; a 32 GB laptop is tight; 48 GB+
   is comfortable. Pushing the LLM off-host frees the laptop to
   be a 16 GB MacBook Air.
3. **Key custody.** Apple Silicon Secure Enclave is a real TEE,
   but its accessibility from third-party code is narrow:
   Keychain ACLs (which gate *access*, not *computation*) and a
   handful of high-level CryptoKit primitives. SEP cannot host
   arbitrary HKDF, cannot prove to a remote verifier "I'm
   running unmodified local_scribe", and cannot be a custodian
   for AES-key-derivation work that doesn't fit one of Apple's
   blessed primitives. A separate compute box with a YubiHSM 2
   (or a SEV-SNP enclave, or an SGX enclave on the few platforms
   that still ship it) gives you a real hardware-rooted key
   custody primitive that runs the operations *inside* hardware
   the OS cannot read.
4. **Separation of concerns / blast radius.** Char captures
   audio; the LLM box never sees raw audio (it sees ASR
   transcripts only) and never sees the network (its only
   client is the laptop over a private link). A compromise of
   Char.app does not by itself yield the LLM box's master key,
   and a compromise of the LLM box does not by itself yield
   live microphone access.
5. **Travel.** "Laptop at the office, LLM at home" works as
   long as the laptop can reach the LLM over WireGuard/Tailscale
   and falls back to on-device ASR (faster-whisper-small on the
   laptop) when offline. Today's single-host design makes that
   impossible without a code change.

If none of (1)–(5) bites, a single host on an M-series MacBook Pro
with 48 GB+ is the right answer and the rest of this document is
moot. If two or more bite, the rest of this document is the menu.

---

## What "split" actually looks like

```text
   ┌─────────────────────────┐                 ┌──────────────────────────┐
   │  laptop (Mac, with you) │                 │  compute box (at home)   │
   │                         │                 │                          │
   │  Char.app               │                 │  ASR server (Parakeet)   │
   │  local_scribe wrapper:  │  HTTPS/mTLS or  │  LM Studio + Qwen3-30B   │
   │   - audio capture       │ ←── WireGuard ──→  inspector (read-only?)  │
   │   - integrity gates     │   on private    │  egress proxy (?)        │
   │     (script/char/sip)   │     LAN          │  master key custody:     │
   │   - vault (transcripts) │                 │    YubiHSM 2 (USB)       │
   │   - egress proxy        │                 │    or SEV-SNP enclave    │
   │   - inspector UI        │                 │                          │
   │   - Touch ID + YubiKey  │                 │                          │
   │     unlock              │                 │                          │
   └─────────────────────────┘                 └──────────────────────────┘
```

The boundary that matters: **the laptop never holds the master
key for longer than it needs to derive its own ASR/inspector
bearer tokens.** The compute box holds the long-lived key custody
(YubiHSM 2 / SEV-SNP enclave). The HKDF service-auth tokens are
derived once at boot and shipped to the requesting service over
an mTLS-pinned tunnel.

### What stays on the laptop

* **Audio capture.** The mic is on the laptop. Char.app captures
  the audio. There is no way to push that to a compute box without
  also pushing the user's physical microphone, which is the wrong
  direction.
* **Char.app + its sandbox + its egress proxy.** Char talks to the
  ASR endpoint over the WireGuard tunnel, but the integrity gates
  that pin Char's CDHash + Team ID + linked-library prefix
  ([`char_integrity.py`](../local_scribe/char/char_integrity.py))
  run *next to Char*, not on the remote box. The compute box never
  sees Char's binary.
* **The encrypted transcript vault** (option, see open question
  below). Char writes its session data under
  `~/Library/Application Support/hyprnote/` on the laptop. We
  already wrap that in an APFS-encrypted DMG mounted only while
  Char is running. Keeping the vault laptop-local means
  transcripts are usable offline; keeping it on the compute box
  means losing the laptop doesn't lose the data. The right answer
  is probably "both, with rsync over the WireGuard tunnel" — but
  the vault key is what's at stake and that's a deeper question.
* **Touch ID + YubiKey unlock UX.** The laptop is what's in front
  of the operator; YubiKey is in the laptop's USB port; Touch ID
  is on the laptop's keyboard or magic-keyboard.
* **All seven defense layers from SECURITY.md, on the laptop
  side.** SIP, egress proxy, Option C split-key, script integrity,
  Char integrity, signed pinned config, secret-scan pre-commit.

### What moves to the compute box

* **ASR server.** Parakeet-MLX on Apple Silicon, or
  faster-whisper-large on Linux with CUDA. Audio bytes flow over
  the tunnel; transcripts flow back.
* **LM Studio + LLM.** Qwen3-30B-Instruct or a successor. This is
  the heaviest workload; this is what the compute box exists for.
* **Master key custody.** The YubiHSM 2 lives in the compute box's
  USB port. The master key never leaves the HSM in plaintext; all
  HKDF derivations for per-service bearer tokens happen HSM-side
  via PKCS#11.
* **Optionally: inspector instance** dedicated to the compute box,
  visible only over the tunnel, surfaces "is the LLM up? is the
  HSM healthy? when was the last attestation?".

### The network in between

Three workable options, ordered by trust-chain narrowness:

1. **WireGuard with PSK + mTLS on top.** WireGuard gives you a
   transport-encrypted point-to-point tunnel keyed by a
   pre-shared key the operator generates on each side and pastes
   into both `wg0.conf` files. Layered HTTPS + mTLS on top means
   even if WireGuard were ever broken, the bearer tokens are
   still wrapped in another TLS session pinned to the compute
   box's identity certificate. **Recommended.** Zero third-party
   trust.
2. **Tailscale with ACL pinning.** Easier UX (no PSK management),
   but it adds Tailscale's control plane to your trust chain.
   Tailscale's control plane never sees your data (each peer
   negotiates its own WireGuard keys via the control plane), but
   it does see your nodes' metadata and can in principle
   authorise a rogue peer onto your tailnet. If you trust
   Tailscale not to be coerced, this is fine. If your threat
   model includes a state-level adversary, it's not.
3. **Plain LAN with mTLS, no tunnel.** Compute box and laptop on
   the same physical network at home, no tunnel, just mTLS over
   HTTPS. Smallest trust chain but no travel story — the laptop
   can't reach the LLM box from a coffee shop.

A practical deployment is (1) on the road and (3) at home — i.e.
WireGuard is always available but the LAN path is preferred when
both ends are on the same network, for latency.

---

## TEE primitives, named

The phrase "TEE" gets used loosely. Before comparing options it's
worth pinning what local_scribe actually needs from one:

### Confidentiality of key material at rest

> "The master key bytes never appear in normal-world memory."

This is the *minimum* bar. The Apple Secure Enclave does this for
Keychain items. YubiHSM 2 does this for arbitrary PKCS#11 keys.
Intel SGX did this for arbitrary enclave-resident data inside the
EPC region. AMD SME does this trivially (full-RAM encryption, but
not per-process isolation). AMD SEV-SNP does this with per-VM
isolation.

### Confidentiality of key material in use

> "The operations the master key participates in (HKDF, HMAC, AES
> encrypt/decrypt) also run inside hardware the OS cannot
> introspect."

This is stronger and is what actually closes the
`mach_vm_read`-the-Python-heap attack from SECURITY.md § Defense
layer 0. SEP does this for a narrow set of primitives (Keychain
ACL'd items, FileVault crypto). YubiHSM 2 does this for
**everything** you store in it — the HSM signs and derives on
your behalf and only returns the result. SGX did this for
arbitrary code inside an enclave. SEV-SNP does this for arbitrary
code inside a confidential VM.

### Integrity attestation

> "The remote party can cryptographically prove to me that it's
> running the code I think it's running, on hardware I trust."

This is what makes a true split deployment honest. Without it,
the laptop has to *trust* that the compute box is running
unmodified local_scribe. With it, the laptop can verify it.

* **SGX:** Yes — DCAP attestation produces an Intel-signed quote
  over the enclave's MRENCLAVE measurement.
* **SEV-SNP:** Yes — AMD-signed attestation report over the VM's
  initial measurement + launch digest.
* **TDX:** Yes — Intel-signed quote, similar shape to SGX DCAP.
* **YubiHSM 2:** Yes for the HSM's *own* attestation (you can
  prove an audit-blessed YubiHSM is in this USB port); no for the
  *host code* that talks to it.
* **Apple SEP:** Not exposed to third-party code. There is no
  "the laptop's SEP signs that Char.app's CDHash matches the
  expected value" primitive available to userspace.
* **TPM 2.0 (PC platform):** Partial — PCR-based measured boot,
  remote attestation via AIK/EK. Closer to "the OS hasn't been
  tampered with at boot" than "this app right now is what we
  think it is".

local_scribe today addresses the third item — integrity
attestation — in **software**, with the script-integrity gate,
the char-integrity gate, and the signed-pinned-config HMAC. Those
checks run *inside the host they're trying to attest*, which is
fundamentally a software-only solution: a kernel-mode attacker on
the same host can patch the verifier before it runs (Adversary #7
from the threat model). A hardware TEE moves the verifier off
the verified host: the laptop checks the compute box's
attestation report rather than asking the compute box to check
itself.

**This is the single biggest security upgrade a split deployment
offers**, and it's only available on three of the candidate
platforms below: SGX, SEV-SNP, and TDX.

---

## Option-by-option deep dive

### Option 1 — Mac Studio (or Mac mini M4 Pro/Max) + YubiHSM 2

**The pragmatic best for "today, with MLX, with code that works
without porting".**

| Dimension | Detail |
|---|---|
| LLM acceleration | Native MLX / Metal. Qwen3-30B-Instruct runs at ~30–50 tok/s on M4 Max with 64 GB+ unified memory. Same model weights, same backend, same quantisation as your laptop. |
| ASR | Parakeet-MLX is native here, same as the laptop. |
| TEE / key custody | Apple Secure Enclave for Keychain-gated items (good for FileVault, good for the laptop-side YubiKey pairing data, **not sufficient** for arbitrary HKDF on the master key). YubiHSM 2 fills the gap. |
| Attestation | Apple SEP does not expose remote attestation to userspace. YubiHSM 2 attests itself, not the host. **You're paying for confidentiality of keys, not for integrity attestation of the LLM process.** This is honest about the gap. |
| Memory ceiling | M4 Mac mini Pro: 64 GB; M4 Mac Studio Max: 128 GB; M4 Mac Studio Ultra: up to 512 GB (~$8.5k+). |
| Form factor | Mac mini: tiny, fanless under most loads. Mac Studio: small, quiet, real fan. Both rack-mountable. |
| Software burden | macOS. SIP gate applies. Headless operation requires auto-login + a logged-in user session (Touch ID prompts that have a `requireAuthentication`-style ACL can't run on a host with no logged-in user). |
| Cost (2026 prices) | $1,800 (mini Pro 48 GB) → $2,500 (mini Pro 64 GB) → $2,800 (Studio Max 64 GB) → $4,500 (Studio Max 128 GB) |

**What it gets you:**

* Zero porting effort. The exact `local_scribe.asr.asr_server` and
  `local_scribe.cli` that runs on your laptop runs here.
  `./run.sh start` on the compute box brings up ASR + LM Studio;
  the laptop's `./run.sh start --remote <host>` (future flag)
  points Char at the remote endpoint.
* MLX-native performance — there is no faster inference path for
  these models in a desktop form factor at this price point.
* The SIP gate is honoured natively. The script-integrity gate is
  honoured natively. The Char-integrity gate is *not* needed on
  this side (Char doesn't run here).
* Same OS as the laptop = same operational muscle memory.

**What it does not get you:**

* No remote attestation of the LLM process to the laptop. The
  laptop has to trust that the Mac Studio's `script_integrity`
  baseline + `signed_config` HMAC haven't been tampered with by
  a kernel-mode attacker on the Mac Studio. Mitigation: the
  compute box has SIP fully on, lives behind a single trusted
  network interface, and is physically secured.
* Headless macOS sysadmin is a real cost. You'll need to enable
  Auto-login, Remote Login, Remote Management, and configure
  `caffeinate` to keep the user session alive across sleep.
  Workable, but not the rich systemd / Ansible / monitoring story
  Linux gives you.

**Why YubiHSM 2 is mandatory here:** SEP only gates access to
items the operator stored with `kSecAttrAccessControlBiometryAny`.
A Keychain item that you derive an HKDF subkey from inside Python
spends time as plaintext bytes in the Python heap. With SIP on
plus a logged-in user session, that's a tight window, but it's
non-zero. Move the master key into the YubiHSM and the master
**never** appears in Python memory; HKDF subkey derivations are
called out to the HSM via `pkcs11` and the result (the subkey, not
the master) is what Python sees.

### Option 2 — Framework Desktop (Ryzen AI Max+ 395) + YubiHSM 2

**The compact, powerful, Linux-native alternative.**

Announced February 2025, ships Q3 2025. Roughly Mac mini-sized
(4.5 L), Ryzen AI Max+ 395 "Strix Halo" SoC, up to **128 GB
LPDDR5X-8000 unified** between CPU and integrated Radeon 8060S
(40 RDNA 3.5 CUs, roughly RTX 4070 mobile class). $1,999 base;
$2,200–$2,800 in useful configurations.

| Dimension | Detail |
|---|---|
| LLM acceleration | ROCm or Vulkan for llama.cpp / vLLM. Throughput on Qwen3-30B Q5 is ~25–40 tok/s — comparable to M4 Max, possibly faster on long contexts thanks to unified memory bandwidth. |
| ASR | faster-whisper on CUDA / ROCm; Parakeet not Linux-native (it's MLX), so you'd use Whisper here. |
| TEE / key custody | AMD platforms have **SME** (Secure Memory Encryption — full RAM at-rest encryption, no per-process isolation) but **not SEV-SNP** on consumer Ryzen. YubiHSM 2 carries the key-custody story alone. |
| Attestation | None at the host level. (TPM 2.0 measured boot can attest the kernel + initrd, but not arbitrary userspace.) |
| Memory ceiling | 128 GB unified — more than the Mac mini Pro, comparable to a Mac Studio Max. |
| Form factor | 4.5 L. Quiet, fanless under idle, low-fan under load. |
| Software burden | Linux. Full systemd, full Ansible, full Wireshark / strace / eBPF for triage. SIP-equivalent is "secure boot + IMA + dm-verity on the root partition" — workable but more setup. |
| Cost | $2,200 (64 GB) → $2,800 (128 GB) |

**What it gets you over the Mac mini:** more memory at the same
price (128 GB Framework Desktop vs 64 GB Mac mini Pro); a more
manageable headless story; a fully open Linux stack; the
ability to use vLLM (which is faster than LM Studio for batch
inference).

**What it gives up:** MLX. No Parakeet — you're on Whisper. No
SIP-as-Apple-defines-it; you're enforcing your own kernel
integrity via IMA + dm-verity + measured boot via TPM 2.0.

**Why YubiHSM 2 is mandatory here too:** Same reason as the Mac
Studio — Linux has no native equivalent of SEP. The TPM 2.0 chip
on the motherboard is a candidate, but consumer TPM 2.0 chips run
at single-digit ops/sec for ECDSA signing, are unpleasant to
manage at scale, and don't offer the M-of-N quorum YubiHSM 2 does.
A YubiHSM 2 on USB is the right tool.

### Option 3 — Bare-metal AMD SEV-SNP or Intel TDX server

**The strongest TEE story available in 2026, at server-class cost
and complexity.**

This is where actual remote attestation becomes available. An
AMD EPYC 9004/9005 series Genoa/Bergamo box (or any Xeon 5th gen
Scalable for TDX) can run the ASR + LLM services inside a
confidential VM, and the laptop can verify the VM's measurement
against a pinned digest before sending audio.

| Dimension | Detail |
|---|---|
| LLM acceleration | Requires a discrete GPU (an RTX 4090 24 GB or A6000 48 GB makes sense). Note: SEV-SNP **does not protect GPU memory** by default — the LLM weights and KV cache live in GPU VRAM, which the host can introspect via the PCIe BAR. The H100 Confidential Computing mode addresses this on NVIDIA's side, but H100s are $30k. For the threat model "operator's home OS is honest, but I want kernel-level adversary protection on the LLM box", a normal GPU is sufficient: the confidential VM protects the keys + tokens; the GPU sees only inference work. |
| ASR | Whisper on CUDA. Parakeet ports to PyTorch with a small accuracy loss. |
| TEE / key custody | SEV-SNP / TDX hosts the master key + HKDF derivation inside the confidential VM. YubiHSM 2 still useful as the pre-VM bootstrapping anchor (the VM measurement-encrypts its disk image with a key the YubiHSM unwraps). |
| Attestation | **Yes, real remote attestation.** AMD-signed (SEV-SNP) or Intel-signed (TDX) report carries the launch measurement + a fresh nonce; the laptop verifies the report and refuses to send audio if the measurement is unexpected. |
| Memory ceiling | 1–4 TB depending on board. Far beyond what any LLM run-of-the-mill needs. |
| Form factor | Rack 1U/2U typically. Some workstation boards (ASUS Pro WS WRX90E-SAGE-SE with Threadripper Pro) fit a tower case. |
| Software burden | Highest. SEV-SNP / TDX have rough edges in 2026 — kernel patches, firmware versioning, attestation-verifier tooling. Setup is a weekend project, not an afternoon. |
| Cost | $5,000 (Threadripper Pro 7965WX + 256 GB + 4090) → $15,000+ (EPYC 9354 + 512 GB + A6000) |

**What it gets you:** the only configuration in this document
where the laptop can *verify* that the LLM box is running
unmodified code, by hardware-signed attestation. This is the path
that retires our software-only integrity-gate story for the
LLM-side surfaces.

**What it costs:** ~3–5x the price of the Mac Studio path, real
sysadmin investment, and acceptance that the GPU side of the box
is *not* covered by the TEE unless you pay confidential-GPU rates.

### Option 4 — NUC class / mini-PC class (Intel Arrow Lake or AMD Hawk Point)

**Compact and cheap. Not recommended for the LLM workload.**

| Dimension | Detail |
|---|---|
| LLM acceleration | CPU-only on most NUCs (no discrete GPU, integrated GPU is too weak for transformer inference). Qwen3-30B Q5 on CPU runs at 3–6 tok/s — usable for non-interactive batch summarisation but not for "I want the summary by the end of the meeting". |
| ASR | Whisper on CPU is workable for small models; large-v3 is too slow. |
| TEE / key custody | **No SGX on consumer Intel since 11th gen** — Intel removed SGX from desktop and consumer mobile parts. NUCs use consumer parts. AMD desktop Hawk Point has SME but not SEV-SNP. So **no host TEE.** YubiHSM 2 carries the entire story. |
| Attestation | None. |
| Memory ceiling | 64 GB DDR5 typical on modern NUCs. |
| Cost | $800–$1,500 base + $650 YubiHSM 2. |

**My take:** the only reason to pick a NUC over an M4 Mac mini Pro
is shaving off ~$700 *and* committing to a worse LLM experience.
The Mac mini Pro is the right answer at this price tier.

If you specifically want Linux-on-something-tiny, the Framework
Desktop has none of these limitations and is still
mini-PC-shaped. Pick that instead.

### Option 5 — NVIDIA Jetson AGX Orin / NVIDIA DGX Spark

**Strong on LLM inference, weak on the TEE story we care about.**

* **Jetson AGX Orin 64 GB:** $2,000, fanless, ARM TrustZone exists
  but is largely unexposed to userspace. LLM inference is fast on
  the Ampere GPU; up to 275 TOPS sparse INT8.
* **NVIDIA DGX Spark / Project DIGITS:** $3,000, ships May 2025,
  Grace Blackwell GB10, 128 GB unified memory, 1 PFLOPS FP4.
  Designed for AI workloads at home; better than Jetson for the
  LLM size we care about.

| Dimension | Jetson AGX Orin | DGX Spark |
|---|---|---|
| LLM acceleration | Excellent — Ampere GPU with CUDA / TensorRT-LLM. | Best in this list — Blackwell GPU with 4 PFLOPS FP4. |
| ASR | Whisper on CUDA; not Parakeet. | Whisper on CUDA; not Parakeet. |
| TEE | ARM TrustZone present but unexposed; effectively none. | Same — ARM TrustZone present; no exposed remote attestation primitive for userspace yet. |
| Attestation | None. | None. |
| Cost | $2,000 | $3,000 |

**What it gets you:** the best LLM inference per dollar in this
list, by a meaningful margin. If the threat model is "I want fast
local LLM and I will rely on YubiHSM 2 alone for key custody",
Spark is competitive.

**What it gives up:** the entire software stack lives on
NVIDIA's L4T (Linux for Tegra) / NVIDIA AI Workbench, which is
proprietary-blob-heavy. Patching, hardening, and IMA
configuration are noticeably harder than on a stock Ubuntu LTS
box. There is also no MLX path; you're on llama.cpp + CUDA, which
is fine but means weight quantisations need to be re-prepared.

### Option 6 — Stand-alone YubiHSM 2, no compute box change

> "I just want the key custody to be hardware-rooted; I'll keep
> running everything on my laptop for now."

| Dimension | Detail |
|---|---|
| Form factor | USB-A nano (slightly larger than a YubiKey 5C nano). Stays in a USB port. |
| Capabilities | PKCS#11. Stores up to 256 asymmetric keys + 256 symmetric keys + 256 HMAC keys. M-of-N quorum, HMAC-SHA256, ECDSA P-256/P-384, Ed25519, X25519 ECDH, AES-CCM. Hardware RNG. |
| Cost | ~$650 |

**What it gets you:** the master key never leaves the HSM. HKDF
service-token derivations call out to the HSM. The
signed-pinned-config HMAC is HSM-side. Touch ID + YubiKey unlock
becomes "Touch ID unlocks an HSM auth-key; YubiKey is the
out-of-band split-key holder; the master derivation happens
HSM-side". The laptop's Python heap never contains the master
key, full stop. **This single peripheral closes the
mach_vm_read window even without splitting hosts.**

**What it gives up:** none of the LLM-workload benefits of a
separate compute box — your laptop still runs hot during a
meeting.

**Worth knowing:** YubiHSM 2 is **not** Touch ID-gated.
Authorisation is a 16-byte HMAC password, which you can wrap in
a Touch ID Keychain item ourselves. The wrap-with-Touch-ID + tap
then HSM-derive flow is what makes this comparable to the
laptop's current Touch ID + YubiKey 5 unlock.

### Option 7 — TPM 2.0 instead of YubiHSM 2

Modern x86 boxes have a discrete or firmware TPM 2.0 on the
motherboard. Could that be the key custodian instead?

In principle: yes. In practice:

* **Performance.** Consumer TPM 2.0 chips do roughly 2–8 ECDSA
  signs/sec. YubiHSM 2 does ~50–250 depending on key type. For
  our workload (a few token derivations per service per boot)
  performance isn't the killer.
* **API ergonomics.** `tpm2-tools` + `tpm2-tss` is workable but
  the API is heavier than PKCS#11; expect ~2–3 days of
  development to wire it in.
* **Trust.** A motherboard TPM is soldered to the host. If the
  host is compromised at the firmware level, the TPM is
  compromised. A YubiHSM 2 is physically removable — the
  operator can pocket it. This is a real difference for the "I
  was burgled and the LLM box was stolen" scenario.

**Verdict:** if the compute box is rack-mounted in a locked
closet, TPM 2.0 is fine and saves $650. If the compute box is
sitting on a desk where a thief could walk off with the whole
thing, YubiHSM 2 is the better answer because you can pull the
key out of the box.

---

## Memory budget for the LLM workload

So you don't accidentally buy the 32 GB SKU and regret it.

| Component | Resident memory | Notes |
|---|---|---|
| Qwen3-30B-Instruct, Q5 | 22 GB | The active LLM weights. Q4 is ~18 GB if you can tolerate the small quality drop. |
| KV cache @ 8k context | 6–8 GB | Grows roughly linearly with context length. 16k context ≈ 12–16 GB. |
| KV cache @ 32k context | 24–28 GB | Long-context use needs serious headroom. |
| Parakeet-TDT ASR | 1.5 GB | Streaming inference. |
| faster-whisper-large-v3 ASR | 3 GB | Higher than Parakeet but Linux-native. |
| LM Studio host + model loader | 2 GB | Process overhead. |
| local_scribe Python services | 0.5 GB | ASR proxy + inspector + egress proxy combined. |
| OS + scratch (macOS) | 4–6 GB | |
| OS + scratch (Linux server) | 1–2 GB | |
| **Working set, 8k context** | **~38 GB** | Q5 + 8k KV + ASR + OS |
| **Working set, 32k context** | **~58 GB** | Realistic for "summarise a two-hour meeting" |
| **Working set, multi-session** | **+30 GB** | Two simultaneous LLM contexts |

**Practical floors:**

* 48 GB unified memory: tight; OK for 8k context single session.
* 64 GB: comfortable for 16k context, plus ASR, plus headroom
  for inspector and tools.
* 128 GB: comfortable for 32k context, multi-session, plus
  occasional larger model swap-in (e.g. trying Qwen3-72B Q4
  side-by-side).

> **Note on unified memory vs split CPU/GPU memory.** Apple
> Silicon and Strix Halo both use unified memory — the LLM weights
> live in the same DDR pool the CPU uses, no host→GPU copy.
> Discrete-GPU configurations (Option 3, SEV-SNP + RTX 4090) put
> weights in 24 GB of GDDR6X with the CPU sitting on a separate
> 256 GB DDR5 pool. **For LLM throughput the GPU memory ceiling
> is what binds**, not the host RAM. A 24 GB GPU runs Qwen3-30B
> Q4 but not Q8; a 48 GB GPU runs Q8 comfortably; 80 GB+ runs
> 70B-class models. Plan your GPU memory separately from host
> memory in discrete-GPU configurations.

---

## What changes in local_scribe code

A real split deployment is not a config flip; it requires a code
change. The blocks below are sized so each could be its own PR.

### Already split-friendly today

* **`service_auth` HKDF derivation.** Already designed around
  "derive per-service tokens from the master". Cross-host doesn't
  change the algorithm — it changes the bearer-token consumer
  (laptop's Char.app via the egress proxy) from the bearer-token
  producer (compute-box's ASR server). The tokens are
  HKDF-derived deterministically from the master, so both sides
  can derive independently once the master is reconstituted on
  each side.
* **The HMAC-signed pinned config.** Already designed around an
  operator-rooted key. With YubiHSM 2 in the loop the HMAC
  happens HSM-side; no algorithm change.

### Needs new work for split deployment

* **Master key location.** Today the master is reconstituted in
  the Python heap at process start. For split:
  * If both hosts have access to Touch ID + YubiKey 5 in the
    same physical room: each host reconstitutes its own copy
    independently. Simpler.
  * If only the laptop has Touch ID + YubiKey 5: the compute box
    must receive its master from somewhere. Options: store the
    master in the compute box's YubiHSM 2 (the right answer), or
    push it over the WireGuard tunnel at boot (worse — adds a
    bootstrap-time secret on the wire).
  * **Recommended:** the master lives in the compute box's
    YubiHSM 2 *only*, and the laptop reconstitutes a separate
    "laptop-side" master from its own Touch ID + YubiKey. The
    two masters share nothing. The compute box's per-service
    tokens are wrapped by the laptop's master at issuance and
    unwrapped just-in-time. This requires a key-derivation
    protocol revision but has the right "least-privilege"
    shape.
* **`char_integrity` becomes laptop-side only.** Already true
  conceptually; the code today doesn't run on the compute box
  anyway. The hostname-discriminating logic should be made
  explicit (a `IS_CHAR_HOST` config flag).
* **`script_integrity` runs independently on each side.** Each
  host has its own git working-tree baseline.
* **`sip_check` becomes platform-discriminating.** macOS sides
  check SIP. Linux sides check IMA + dm-verity + measured-boot
  PCRs from the TPM. New module: `local_scribe.security.boot_integrity`
  that abstracts over both.
* **`./run.sh start --remote <wireguard-host>`.** New flag.
  Sets `ASR_BASE_URL` to the remote endpoint, sets
  `LMSTUDIO_BASE_URL` similarly, skips local ASR + LM Studio
  startup, leaves Char + egress proxy + inspector running
  locally.
* **Compute-box-side `./run.sh start --service-only`.** New
  flag. Skips the bootstrap-the-Char-bundle paths, brings up
  ASR + LM Studio + an inspector instance scoped to that host.
* **Attestation, if the platform supports it.** If the compute
  box is SEV-SNP / TDX: a new `local_scribe.security.attestation`
  module pulls the attestation report at compute-box boot,
  signs a freshness nonce, and exposes the report at
  `GET /api/attestation`. The laptop's egress proxy verifies
  the report against a pinned launch-digest before forwarding
  audio. This is the change that earns the security upgrade.

### Open: where does the encrypted vault live?

Today the vault is an APFS-encrypted DMG on the laptop, mounted
during a Char session. In a split deployment there are three
plausible designs:

1. **Vault stays on the laptop.** Transcripts are usable offline.
   Backup is the operator's responsibility. Compute-box compromise
   does not expose transcripts.
2. **Vault moves to the compute box.** Transcripts are usable
   only when the laptop can reach the compute box. Backup is
   trivial. Laptop loss does not expose transcripts.
3. **Vault lives on both, mirrored via rsync over WireGuard.**
   Best of both, at the cost of two key holders and a
   reconciliation policy when the mirror diverges.

I lean toward (1) — the operator already cares enough about
privacy to be reading this document; they'll have a backup
strategy. (3) is the right long-term answer but requires more
code than is justified by the threat model upgrade it offers.

---

## Decision tree

A flowchart-as-prose for picking among the options:

```text
Q1. Do you need MLX / Apple Silicon parity with your laptop?
    YES → Option 1 (Mac Studio / Mac mini Pro) + YubiHSM 2.
    NO  → continue.

Q2. Do you need hardware-rooted remote attestation of the
    LLM box's code?
    YES → Option 3 (SEV-SNP / TDX server) + YubiHSM 2.
          Budget ≥ $5k, accept rack-class sysadmin.
    NO  → continue.

Q3. Do you need >64 GB of unified memory at a sub-$3k price?
    YES → Option 2 (Framework Desktop 128 GB) + YubiHSM 2.
    NO  → continue.

Q4. Is "fast LLM at any cost, Linux is fine, no MLX" the goal?
    YES → Option 5 (DGX Spark) + YubiHSM 2.
    NO  → continue.

Q5. Is "as small and cheap as possible, LLM speed is secondary"
    the goal?
    YES → Option 4 (NUC / mini-PC) + YubiHSM 2. Accept slow
          inference.
    NO  → continue.

Q6. Do you actually need a separate compute box at all, or are
    you really just buying TEE-class key custody?
    Latter → Option 6 (YubiHSM 2 on the laptop alone, no
             compute box). Cheapest path to the strongest
             single security upgrade.
```

If I had to pick **one** of these for myself today, with no other
constraint than "the threat model in SECURITY.md and a budget of
$3k", I would pick **Mac Studio M4 Max 64 GB + YubiHSM 2** (Option
1, ~$3.4k stretched). Rationale:

* Same OS as the laptop — operational muscle memory.
* MLX-native — same model weights as on the laptop.
* SIP + Gatekeeper + XProtect still apply.
* Quiet, sits on a shelf, runs cool.
* YubiHSM 2 closes the only real key-custody gap.

If the budget were $5k and remote attestation were a hard
requirement, I'd pick **Option 3 (Threadripper Pro SEV-SNP +
RTX 4090 + YubiHSM 2)** instead. The 3x complexity is the cost of
hardware-rooted integrity on the LLM box.

---

## Open trade-offs I'd want to settle before committing

1. **Does Tailscale belong in the trust chain?** WireGuard with
   PSK has zero third-party dependence but more setup friction.
   Tailscale buys easy NAT-traversal and an excellent ACL story
   at the cost of trusting a third party's control plane. My
   instinct is "WireGuard for the high-assurance configuration,
   Tailscale for everyday convenience, never both at once" — but
   this is worth a real argument before picking.
2. **Should the laptop be able to fall back to local ASR + a
   smaller LLM when offline?** Probably yes — faster-whisper-small
   on the laptop + Qwen3-4B locally would give the user a
   degraded-but-usable experience without the compute box.
   Trade-off: more code paths to test, more storage on the
   laptop.
3. **How do we attest the compute box from the laptop on
   non-SEV-SNP / non-TDX hardware?** The honest answer today is
   "we can't, hardware-attestation-style; we fall back to the
   software integrity gates running on the compute box itself,
   and we accept that a kernel-mode attacker on the compute box
   would defeat them". Worth being explicit about this in
   SECURITY.md if/when this design lands.
4. **What does `./run.sh bootstrap --remote` look like?** The
   bootstrap currently does Char.app install + Touch ID + YubiKey
   key init + initial baseline bless. On a split deployment,
   "bootstrap" is now a coordinated two-host ritual. The UX has
   to be sane — probably "run bootstrap on the laptop, it prints
   a one-time pairing code, paste that into bootstrap on the
   compute box, both sides converge". This is the kind of UX
   that's easy to get wrong; needs a real design pass.
5. **What happens when YubiHSM 2 firmware is updated?** Yubico
   ships firmware updates ~yearly. Each update is signed by
   Yubico and verified by the device, but the device's
   attestation key may need re-anchoring. We should pin the
   YubiHSM 2's attestation certificate at bootstrap time and
   refuse to start if it changes mid-life. Yet another
   blessing/rebless ritual; consistent with how the rest of
   local_scribe handles trust anchors.

---

## What this document doesn't try to answer

* **GPU side-channel attacks on shared inference.** If a future
  threat model includes "the LLM box also runs other tenants",
  the answer is H100/H200 confidential computing + LLM-specific
  side-channel hardening. Out of scope here — local_scribe's LLM
  box is single-tenant by design.
* **Cold-storage backup hardware.** YubiHSM 2 has an M-of-N
  wrap-export ritual for backups; we'd lean on that. Real
  cold-storage policy (off-site safe deposit, two-person
  custody) is operator-policy territory, not architecture.
* **Multi-laptop deployments** (you and a partner both want to
  drive Char against the same compute box). Solvable with
  per-laptop master keys, but the design pass is non-trivial and
  no one has asked for it yet.
* **Quantum-resistant key wrap on the WireGuard PSK.** WireGuard
  already supports a PQ pre-shared key as a defense-in-depth
  measure (`PresharedKey` is mixed into the symmetric key
  derivation). For a >2026 threat model this is worth turning on
  with a Kyber-derived shared secret. Mentioned only so it's not
  forgotten.

---

## Related docs

* [SECURITY.md](../SECURITY.md) — the threat model these
  hardware choices serve.
* [CRYPTO.md](../CRYPTO.md) — the cryptographic primitives the
  hardware needs to be able to run efficiently.
* [docs/KEY_SAFETY.md](KEY_SAFETY.md) — operator-side
  key-handling rituals that interact with whichever hardware
  custodian is chosen.
* [docs/CHAR_REVIEW.md](CHAR_REVIEW.md) — the audit of Char's
  surfaces; relevant because Char remains on the laptop in every
  split scenario.
* [TODO.md § Privacy & security (P0)](../TODO.md#privacy--security-p0)
  — where the "promote a hardware split to actual implementation"
  follow-up would live once this is decided.
