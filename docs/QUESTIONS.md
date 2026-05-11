# Questions & skepticism

This document answers the questions a thoughtful security or
developer reader is most likely to have after skimming
[`README.md`](../README.md), [`SECURITY.md`](../SECURITY.md),
[`CHAR_REVIEW.md`](CHAR_REVIEW.md), and
[`LEGAL.md`](../LEGAL.md). It is **not** a marketing FAQ — every
answer below tries to give the trade-off honestly, including
where the criticism in the question is right.

If your question isn't here, open a GitHub issue with `[question]`
in the title; the answer will land here.

## Section index

- [Architectural choices](#architectural-choices)
- [Security-model skepticism](#security-model-skepticism)
- [Practical / does-it-work questions](#practical--does-it-work-questions)
- [Char / upstream relationship](#char--upstream-relationship)
- [Future-direction skepticism](#future-direction-skepticism)
- [Trust & community](#trust--community)
- [Where the criticism is fair](#where-the-criticism-is-fair)

---

## Architectural choices

### Q1. Why didn't you just fork Char and contribute these changes back?

Short answer: we considered it carefully, costed it out, and
the sidecar approach wins on every dimension *except* "remove
capabilities at compile time rather than blocking them at the
firewall." [`FORK_CONSIDERATIONS.md`](FORK_CONSIDERATIONS.md) is
a 654-line analysis of the trade-off — § 10 ("Decision matrix")
and § 11 ("Recommended path forward") are the two-minute version.

The compressed argument:

1. **Forking is a year-long engineering commitment**, not a
   button-click. You inherit Apple Developer enrollment ($99/yr,
   identity verification), code-signing, notarisation, Tauri
   auto-update infrastructure, a release cadence to merge
   upstream changes into, and the legal and brand surface of
   shipping a desktop app. § 4 of `FORK_CONSIDERATIONS.md`
   walks through each one.
2. **Every privacy / security outcome we want is achievable
   without forking.** The firewall blackholes Sentry / PostHog /
   the auto-updater (§ 1 in `FORK_CONSIDERATIONS.md` enumerates
   the levers). The bearer-token gate keeps Char's output
   confined to our loopback shim. The binary integrity check
   ([`CHAR_REVIEW.md`](CHAR_REVIEW.md)) catches a tampered
   build at startup before any key is unlocked.
3. **The fork's actual win is *posture*, not outcome.** "Sentry
   is removed at compile time" reads better than "Sentry is on
   the binary but the firewall blackholes it" — but the data-
   flow result is identical when the firewall is on. That's a
   marketing-fact difference, not a security-fact difference,
   and it's not worth the maintenance cost.
4. **Upstream contributions are still on the table.** If
   Fastrepl is open to it, the threat-model-relevant patches
   (compile-time stripping of telemetry plugins, a settings
   "lockdown" mode, a `base_url` override that survives
   resync) would benefit every Char user, not just ours.

The fork stays "fully costed Option D we can execute on demand"
(§ 11 in `FORK_CONSIDERATIONS.md`). If Fastrepl ever rejects the
upstream PRs *and* ships a release that materially expands
telemetry, we revisit.

### Q2. Why scaffold around someone else's binary at all? Why not build the whole client from scratch?

The same § 4 / § 5 cost analysis applies in reverse. Char already
solves the macOS plumbing problems that take 6-12 months to do
well:

- simultaneous system-audio + microphone capture (CoreAudio +
  ScreenCaptureKit, plus the entitlements rollout),
- a polished session UI + note canvas + calendar integration,
- Apple code signing + notarisation + hardened-runtime + Tauri
  auto-update,
- per-language input methods + accessibility integration,
- 8.4k stars of bug reports already filed and triaged.

Rewriting that to get a privacy story is the wrong order. We
write the privacy story *around* the working client and ship
something useful in weeks instead of quarters. If Char ever
shipped something we couldn't compose around (see Q15),
revisiting this calculus is on the table.

### Q3. Why MIT and not Apache 2.0? Don't you want the patent grant?

Honest answer: the explicit patent grant in Apache 2.0 §3 is
genuinely useful for projects that introduce novel
cryptographic constructions or that ship into a hostile
patent landscape. `local_scribe` does neither — every primitive
in here (AES-256, X25519, Ed25519, HKDF-SHA256, ChaCha20-Poly1305
via `age`, RSA/ECDSA via YubiKey PIV) is decades-old and broadly
practiced. The marginal patent risk we'd carry under MIT is low.

What we gain by picking MIT:

- **Char is MIT.** A scaffold that wants to be upstreamable into
  Char benefits from licence parity — no Apache→MIT relicensing
  conversation needed.
- **The downstream model ecosystem is MIT / Apache-2.0 / CC-BY-4.0.**
  MIT for the glue keeps the combined-work licence story trivially
  composable.
- **No NOTICE-file overhead for contributors.**

If you're a downstream redistributor who specifically needs the
Apache 2.0 patent grant, MIT-licensed code is freely relicensable
under Apache 2.0 in your distribution. The reverse is not true,
which is why we picked the more permissive end.

[`LEGAL.md` § 4](../LEGAL.md#4-license) is the long version with the
explicit "what MIT does *not* license" carve-out for trademarks
and model weights.

### Q4. Why Python? Wouldn't Rust / Go / Swift be safer for this kind of code?

A fair criticism. Python has a memory-safety story (no buffer
overflows) but its operational story is weaker:

- the `cryptography` package's wheels bundle a libssl that has
  to be kept current,
- venv hygiene is its own problem,
- `pip install` is a supply-chain surface,
- the interpreter itself is debuggable from another process (with
  privileges; SIP closes this off for unprivileged peers).

We picked Python for three reasons:

1. **The ASR ecosystem lives in Python.** `parakeet-mlx`,
   `faster-whisper`, `sherpa-onnx`, the diarisation models — all
   ship Python bindings as the first-class API. Wrapping any of
   them from a non-Python host means linking against
   `libpython` anyway, at which point you're paying the
   complexity cost without the readability benefit.
2. **The security-critical surface is small.** The crypto
   primitives are in `key_split.py` (XOR + length checks),
   `service_auth.py` (HKDF-SHA256), `secret_store.py`
   (Keychain shell-out to a Swift helper), and `vault.py`
   (`hdiutil` shell-out). None of those does memory unsafe
   manipulation. The Swift bridge in
   [`bin/touchid_keychain.swift`](bin/touchid_keychain.swift)
   *is* the memory-safe helper for the Keychain ACL path.
3. **The model is iterative.** This is a proof-of-concept
   ([`LEGAL.md` § 1](../LEGAL.md#1-what-this-project-is-and-is-not)).
   Rewriting in Rust is a useful exercise once the design has
   stabilised and the threat model is settled. Doing it now
   means rewriting twice.

If the design proves out, a Rust rewrite of the security-
critical modules (`service_auth`, `key_lifecycle`, `vault`,
`secret_store`'s Python side) is a strictly positive future
direction. [`TODO.md`](../TODO.md) tracks this implicitly under
"hardening".

### Q5. Why Parakeet TDT 0.6B by default? Whisper has 7+ years of community track record

Parakeet TDT 0.6B v3 currently has the **lowest published WER
on Apple Silicon** for English-only transcription, by a margin
that's been verified independently
([NVIDIA model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
ASR leaderboards via Open-ASR-Leaderboard). It runs on MLX
(Apple's array framework), which means it's optimised for the
unified-memory architecture of M-series chips in a way the
GGML / CTranslate2 ports of Whisper aren't.

Whisper is still here:

- `faster-whisper large-v3-turbo` is shipped as the
  multilingual backend (`./run.sh configure-char` picks it for
  any non-English locale),
- a backend flip in `~/.config/local_scribe/config.json` from
  `"backend": "parakeet"` to `"backend": "whisper"` is a one-
  line operator change.

If Parakeet ever regresses on a release, or if a future Whisper
variant takes the WER crown back, the swap is unattended.

### Q6. Why is everything in one repo? Why not split ASR / vault / firewall / inspector into separate projects?

We were going to (and may eventually). For now, monorepo wins on
two grounds:

- **The security primitives have to compose.** The vault key
  feeds the inter-service auth tokens which gate the inspector
  which surfaces the Char audit which proves the firewall is
  active. Cross-cutting changes (e.g. "what happens when the
  master key rotates?") would be N PRs across N repos in a
  poly-repo world. Worth revisiting once interfaces stabilise.
- **There's exactly one maintainer.** Monorepo means one
  release artefact, one test suite, one CI config. Coordination
  cost on a four-person team is different from on a one-person
  team.

The internal module boundaries are tight (every Python module
imports a small surface of `config.py` + its peers; `run.sh` is
the only orchestrator). Splitting along those lines is a
mechanical refactor when the time comes.

---

## Security-model skepticism

### Q7. If my laptop is compromised, the master key is in process memory after Touch ID + YubiKey. So what does the YubiKey actually buy me?

This is the most important honest question in the document.

**It buys you two specific properties, and you should understand
both:**

1. **Pre-unlock confidentiality.** Before someone has tricked you
   into a Touch ID prompt AND a fresh YubiKey tap, the master key
   does not exist in any process's RAM. The two halves on disk
   are individually uniform-random bytes. An attacker who steals
   only `kc_half`, only `yk_half`, only the YubiKey hardware, or
   only the Keychain item gets zero bits of the master. That's a
   real defense against backup theft, forensic disk imaging, and
   "smash-and-grab" attacks where the attacker doesn't get to
   linger.
2. **Per-operation physical-presence proof.** Because YubiKey
   `touch-policy=always` requires a fresh tap for every
   operation that needs `yk_half`, a remote attacker cannot
   silently unlock the master from somewhere else in the world
   even if they've exfiltrated all the on-disk material. They
   need someone physically at the keyboard to tap the metal
   contact.

**What it does *not* buy you:**

- **Post-unlock memory disclosure.** Once you've unlocked, the
  reconstituted master key sits in our Python process's heap
  for the duration of the operation that needs it. A kernel-
  level attacker can read it from there. This is why
  [Defense layer 0](../SECURITY.md#defense-layer-0--system-integrity-protection-mandatory)
  is non-negotiable: SIP is what stops `task_for_pid()` from
  letting an unprivileged peer process do the same thing.
- **The "phished tap" case.** If a rogue helper pops a Touch
  ID prompt *and* socially engineers you into tapping the
  YubiKey, the unlock succeeds for the attacker just as easily
  as for the legitimate operation. The YubiKey signs whatever
  the host asks it to sign while contact is bridged. This is
  why every YubiKey prompt in `local_scribe` is contextual
  ([SECURITY.md § "Privileged-prompt UX"](../SECURITY.md#privileged-prompt-ux-every-password-request-explains-itself))
  — so you have a chance to notice when the *thing being
  signed* doesn't match what you're trying to do.

The net: the YubiKey converts the threat model from "Touch ID
phish ⇒ data loss" to "Touch ID phish ⇒ data loss only if the
attacker is also physically present *or* has phished the tap in
the same session". That's a meaningful narrowing — most real-
world attacks against laptop-resident data are remote.

We do *not* claim TPM-isolated execution. We *do* claim the bar
is higher than "anyone with a shell on this laptop, even your
own laundered-back-from-an-`npm install` shell, can read every
transcript on disk."

### Q8. XOR split-key looks toy. Why not Shamir's secret sharing with M-of-N?

Honest answer: because we don't need M-of-N today, and the XOR
construction is *information-theoretically* optimal for the 2-of-2
case we do need.

The XOR split is exactly the original definition of a one-time
pad: knowing one half of `master = kc_half XOR yk_half` yields
*literally zero bits* of the master, no matter how much
computation you throw at it. Shamir buys you flexibility
(arbitrary thresholds), not stronger confidentiality. For 2-of-2,
they're equivalent in security and XOR is simpler — fewer lines
of code, fewer corner cases, zero polynomial-evaluation surface
to audit.

When (if) we extend to "any N of M YubiKeys can unlock"
(useful for org deployments where 3 sysadmins each hold a
hardware token), the right answer is Shamir or AdaptiveCSS.
[`TODO.md`](../TODO.md) tracks that as future work alongside the
multi-tenant / org deployment design.

### Q9. SIP-enforced startup refusal seems heavy-handed. What if I have a legitimate reason to disable SIP?

Most legitimate reasons to disable SIP (kernel-extension
development, DTrace at scale, deep filesystem instrumentation)
are also incompatible with running a privacy-sensitive
recording stack on the same machine. The two use cases are at
odds: SIP-disabled hosts cannot enforce the user-space process
boundaries every other defense in this project relies on.
[SECURITY.md § Defense layer 0](../SECURITY.md#defense-layer-0--system-integrity-protection-mandatory)
spells out exactly why (`task_for_pid` unrestricted →
heap-readable master key; `DYLD_INSERT_LIBRARIES` un-stripped
→ arbitrary code injection into our Python; `dtrace -p`
unrestricted → Keychain daemon observable).

If your laptop *is* your kernel-development laptop, run
`local_scribe` on a different machine. If that's not an option,
the bar of "I understand the risk and I'm running anyway" is
a fork-and-remove-the-gate operation — not a hidden env-var
override. We won't ship that override because the people who
need it are exactly the people who should *not* be one
typo away from disabling it.

This is intentional friction. It's how we keep accidental
SIP-disabled installs from being indistinguishable from
intentional ones.

### Q10. `sandbox-exec` is Apple-deprecated. You're building on a sand foundation

Documented and acknowledged.
[CHAR_REVIEW.md § "What you should also block at the network layer"](CHAR_REVIEW.md)
and [SECURITY.md § "Defense layer 1"](../SECURITY.md#defense-layer-1--network-egress-firewall)
both note this explicitly. `sandbox-exec`'s SBPL is the legacy
interface to Apple's sandbox engine (the one that actually
enforces every App Store app's entitlements at runtime). It
isn't *removed*, it's *unsupported* for third-party use. Apple
have shipped no public successor with equivalent capabilities;
the official path forward is "use the Endpoint Security
Framework + Network Extension + System Extension", which
requires Apple Developer enrollment, kernel-extension
entitlements, user approval of system extensions, and a much
heavier engineering footprint.

Our position: **we accept the deprecation risk as a transitional
choice for the per-process firewall layer**. The kernel mechanism
still works (verified end-to-end in `test_char_sandbox.py`); the
SBPL syntax has been stable since 10.5. If Apple ever removes
the binary, the in-flight Network Extension path becomes
mandatory rather than optional. [`TODO.md`](../TODO.md) tracks the
NetworkExtension migration as the long-term answer.

The opt-in `system` mode (`/etc/hosts`-based) is a defense in
depth: if `sandbox-exec` disappears tomorrow, system mode still
gives you machine-wide blackholing of the same hostnames.

### Q11. Char launched from the Dock or Spotlight bypasses your firewall completely

Yes. This is the most-prominently-documented trade-off in the
project. Every relevant doc surfaces it:

- [`SECURITY.md` § "What the firewall does *not* do"](../SECURITY.md#what-the-firewall-does-not-do)
- [`CHAR_REVIEW.md` § "Trade-off: Dock / Spotlight launches bypass the firewall"](CHAR_REVIEW.md)
- [`README.md` § "Outbound firewall"](../README.md)

The two-paragraph explanation: macOS does not let an
unprivileged user, without Apple Developer enrollment, install
a system-wide per-app outbound filter that survives a Dock
launch. The kernel-enforced path requires a Network Extension,
a System Extension, user approval of both, and Apple Developer
ID with the relevant entitlements. Until that path is built,
the only enforcement we have is "Char must launch through
`./run.sh char launch` to inherit the sandbox + proxy
environment."

This is documented as a *known* limitation, not a *hidden* one.
For users who are not willing to remember to launch Char through
the wrapper, the opt-in `system` mode rewrites `/etc/hosts`
machine-wide and catches the Dock case (at the cost of breaking
external AI providers for *all* apps on the host).

The clean fix is a Network Extension. It's TODO. Until then,
the project is honest about the gap and gives users two routes
(wrapper-only or system-wide) to choose between.

### Q12. CONNECT proxies don't see TLS contents. Can't Char tunnel data out through your proxy by phoning home over `CONNECT api.openai.com:443` and the proxy not knowing?

This is the question that requires the most care to answer.

**What the proxy sees and what it doesn't:**

- The proxy sees the destination hostname + port in the CONNECT
  request line (`CONNECT api.openai.com:443 HTTP/1.1`).
- The proxy does *not* see the TLS handshake, the SNI, the
  request body, or the response body.
- The proxy decides allow/deny *before* the tunnel is
  established. If the hostname is in `firewall.BLOCK_CATALOG`,
  the tunnel is never opened.

**Why this is enough:**

The threat we're defending against is "Char calls
`api.openai.com` (or Sentry, or PostHog) with our audio /
transcripts in the request body." That requires Char to
*resolve the hostname* and *connect to it* — both of which are
mediated by the CONNECT request. We never need to see inside
the TLS to refuse the tunnel.

**Where the attack actually lives:**

A malicious Char could tunnel data through a hostname that
*isn't* in the block catalog — e.g. by connecting to
`legitimate-cdn.example.com:443` and POSTing the audio there.
This is a real risk class and is exactly why
[`CHAR_REVIEW.md`](CHAR_REVIEW.md) maintains an *audited
allowlist* of where Char is permitted to reach: model-download
hosts during install, the legitimate STT endpoint at
`127.0.0.1`, and a small set of other necessary destinations.
Anything else hitting the proxy lands in the audit ring.

If a future Char release introduced a new outbound destination
we hadn't audited, the proxy would default-allow it (today's
implementation is allowlist-augmented blocklist, not strict
allowlist). The mitigation here is operational, not
cryptographic: re-run `CHAR_REVIEW.md`'s audit on every Char
version bump (the continuous-audit checklist at the bottom of
`SECURITY.md` enforces this).

**The right answer for a high-assurance deployment** is to flip
the proxy from blocklist to strict allowlist mode, accept the
operational cost of curating that list, and pair it with the
Network Extension for kernel enforcement. That's tracked in
`TODO.md`.

### Q13. The vault mounts to plaintext on disk during a session. Cloud SaaS that encrypts at rest is strictly stronger, no?

No, and the framing in the question hides the actual comparison.

**What cloud-SaaS-at-rest gives you:**

- The vendor's database is encrypted with keys the vendor
  manages. The vendor can decrypt on demand.
- A subpoena to the vendor, an SRE with prod access, a
  vulnerable web endpoint, or a corp-side breach all expose
  plaintext.

**What `local_scribe`'s vault gives you:**

- The disk image is AES-256 encrypted with a passphrase
  derived (HKDF-SHA256) from a master key you and only you can
  produce, requiring your YubiKey tap and Touch ID.
- The image is mounted to plaintext under your home directory
  *only while you have unlocked it*, and only on your laptop.
  On unmount (`./run.sh stop`), the bytes on disk are
  ciphertext again.
- Nobody else has the key. No subpoena to a vendor produces
  it. There is no "vendor SRE" attack surface.

The cloud-SaaS comparison is "vendor holds the key, your
plaintext sits on their disk every second of every day" vs.
"you hold the key, your plaintext sits in your machine's RAM
and on your disk only while you're actively using it." The
second model has a *narrower temporal exposure window* and a
*much smaller set of parties who can decrypt*.

The question's framing also confuses *encryption at rest*
with *encryption in use*. Both models leave plaintext in RAM
during processing. Only an attested TEE solves "encrypted in
use", which is exactly why
[SECURITY.md § "Beyond the local machine"](../SECURITY.md#beyond-the-local-machine--why-tls-alone-isnt-enough)
spends a thousand words on it.

A laptop in your physical possession with a YubiKey-gated vault
is a strictly better starting point than a multi-tenant SaaS,
*for the threat models this project actually addresses* (forensic
imaging, theft, subpoena, vendor breach). It is *not* a better
starting point if the threat you actually care about is a
kernel rootkit on your laptop — that's documented as out of
scope.

---

## Practical / does-it-work questions

### Q14. Has this been independently audited?

**No.** This is a single-maintainer proof-of-concept. The
threat-model claims in `SECURITY.md`, the per-layer rationale,
the cryptographic constructions — all of it is published for
review, but none of it has been audited by a third-party
security firm. Treat the implementation as research-grade and
the documentation as the design intent, not a verified result.

[`LEGAL.md` § 5](../LEGAL.md#5-limitation-of-liability--restated-in-plain-english)
is explicit: *"The maintainers make no representation that the
security primitives in this project are correctly implemented,
sufficient for any threat model you care about, or free of bugs.
[…] It has not been independently audited by a third-party
security firm, and we have neither claimed nor warranted that
it has."*

If you have audit capacity to throw at this, it would be
genuinely useful. Open a GitHub issue.

### Q15. Does this work on Intel Macs? Linux? Windows?

- **Intel Macs**: untested. The pipeline is built around MLX
  (Apple Silicon's array framework) for Parakeet; the Whisper
  fallback would work on Intel but at significantly lower
  throughput. Touch ID + Secure Enclave APIs are also weaker on
  Intel Macs (T2-equipped Intel Macs have a Secure Enclave, but
  the integration is different from M-series). Not a target.
- **Linux**: the ASR + diarisation backends work fine; the
  Keychain + Touch ID layer doesn't exist; the vault uses
  `hdiutil` which is macOS-only. A Linux port would replace
  Keychain with `gnome-keyring`/`KWallet`, replace
  `hdiutil`-sparse-bundle with LUKS or `cryfs`/`gocryptfs`, and
  replace Touch ID with `polkit` + PAM. Out of scope for the
  PoC.
- **Windows**: same answer as Linux, plus the Char binary
  itself is currently macOS-only.

This project's threat model is "single-user macOS workstation."
Other platforms need their own threat-model walk-through; the
crypto and key-management designs are generally portable, the
OS-integration glue is not.

### Q16. Is 0.6B parameters really good enough for production transcription?

Empirically yes, with caveats:

- For **clear English audio**, Parakeet TDT 0.6B v3 currently
  produces the lowest WER on the Open-ASR-Leaderboard
  benchmarks of any model that runs locally on Apple Silicon at
  the time of writing. That includes models 10× its size.
- For **noisy / accented / multi-speaker English**, the gap
  between Parakeet and the larger Whisper variants narrows, and
  for some accents Whisper still wins.
- For **non-English**, Parakeet drops out entirely — it's
  English-only. The multilingual fallback is `faster-whisper
  large-v3-turbo`, which is the same model many production
  cloud transcription services use.

The pragmatic check: run the smoke test in
[`README.md` § "End-to-end smoke test"](../README.md) against an
audio file you actually care about, compare the output to your
mental model, and configure the backend (`config.json` →
`asr.backend`) accordingly. There is no single "correct" model
across all use cases.

---

## Char / upstream relationship

### Q17. What happens when Char ships an update that breaks your shim?

Three controls catch this:

1. **`CHAR_KNOWN_GOOD_VERSION` is pinned** in
   [`run.sh`](run.sh). `bootstrap` won't install a Char binary
   outside that pin without an operator override (typed
   confirmation + audit re-run).
2. **`char_integrity.py` baselines the CDHash** of the installed
   Char binary at startup. An unexpected update (silent
   auto-update path you forgot to disable, manual upgrade from
   the website) triggers a startup refusal until the new version
   is audited and the pin is bumped.
3. **`char_audit.py` runs on every `./run.sh doctor`** and every
   inspector page load, checking that Char's `settings.json` +
   `store.json` still match the contract. Drift surfaces
   immediately, before any new audio gets processed.

The continuous-audit checklist at the bottom of
[`SECURITY.md`](../SECURITY.md) lists exactly what to re-run on a
Char version bump: the test suite, doctor, firewall verify,
firewall list (against `CHAR_REVIEW.md`'s egress catalog), the
codesign Team Identifier check, and a `strings`-diff of the
binary against the previous version. All of those are bash one-
liners. Total time ≈ 15 minutes per release.

### Q18. Are you in contact with the Char team? Have they responded?

No formal channel yet. The project is public, the documentation
calls out specific patches that would benefit upstream
adoption ([`FORK_CONSIDERATIONS.md` § 9.1](FORK_CONSIDERATIONS.md)
enumerates them), and we plan to file those as upstream PRs
once the design is settled. The README and `CHAR_REVIEW.md`
both explicitly invite the Char team to take any of these
ideas upstream.

If you're on the Char team and reading this: hi. Drop us a
note via the repo's GitHub Issues with `[char-team]` in the
title and we'll prioritise whatever's most useful to you.

### Q19. What if Char goes closed-source or commercial?

`local_scribe` would survive. The integration depends on three
properties of the released Char binary:

1. The Deepgram + OpenAI custom-provider plumbing accepts a
   `base_url` we can point at `127.0.0.1`.
2. The `settings.json` + `store.json` flags we toggle are
   honoured.
3. The CDHash we baseline can be re-baselined against a new
   release.

A closed-source Char that still honoured 1-3 would still work.
A closed-source Char that broke 1-3 would force one of two
moves: (a) reconsider the fork option in
[`FORK_CONSIDERATIONS.md`](FORK_CONSIDERATIONS.md), now with the
last open-source release as the base; (b) rewrite the client
ourselves on Tauri / Electron / native macOS.

Both moves are feasible. Neither is fun.

---

## Future-direction skepticism

### Q20. You write a lot about AWS Nitro Enclaves, CloudHSM, attestation, Tailscale, Signal-style ratchets. None of that is built. Why is it in the docs at all?

Because the cost of *not* writing it down is much higher than
the embarrassment of writing it down before it's built.

`SECURITY.md` and `TODO.md` are explicit about what's built
versus what's designed. Every forward-looking section opens with
a `Status: design only` callout. The goal of those sections
isn't to oversell — it's to:

1. **Make the threat-model continuation visible.** Any reader
   evaluating the project for adoption needs to know what
   happens *if* it eventually has an off-machine path. Writing
   that down now lets you decide whether the trajectory matches
   your needs.
2. **Force ourselves to pick a coherent design before code.**
   `TODO.md § "Multi-tenant / org deployments"` is 350 lines of
   trade-off analysis (HSM-mediated key release vs. confidential
   compute vs. self-hosted appliance; Phase-3 attestation chain;
   Terraform manifest sketch). That document existing prevents
   the most common failure mode of a privacy project: "let's
   just slap TLS on it and call it secure."
3. **Invite reviewers to poke holes before we ship anything.**
   Reviewers cannot poke holes in code that hasn't been written.
   They *can* poke holes in design documents, and many of the
   improvements to those sections have come from exactly that
   kind of review.

If the design documents bother you, treat them as adversarial
collaboration: read them, find the parts that don't hold up,
file an issue. The whole point is to surface that before there's
a real cluster to migrate.

### Q21. Why even mention Signal? You're not building a messenger

Because Signal solved a closely-related problem (E2EE with an
untrusted server, perfect forward secrecy, post-compromise
security, message-level key derivation) and there is a *lot* to
learn from how they did it. [SECURITY.md §
"How this compares to Signal's wire crypto"](../SECURITY.md#how-this-compares-to-signals-wire-crypto)
is comparative scholarship: where the principle generalises
(don't put a long-term key on the server, per-operation
ephemeral keys, ratcheted forward secrecy), we adopt it; where
it doesn't (async prekey pools for offline IM users), we
intentionally diverge.

We make no claim of being Signal. We do claim that *not*
borrowing from the work Signal has done would be silly.

### Q22. "Private cloud LLM over Tailscale" — Tailscale Inc. sees the coordinator traffic, so that's not actually private

Half-true and worth unpacking, because it matters for the
design.

Tailscale's coordinator (the control plane at `login.tailscale.com`)
sees:

- which devices in your tailnet exist,
- their public keys (for the WireGuard handshake),
- ACL definitions,
- some metadata about which devices have connected when.

The coordinator does **not** see:

- the contents of any traffic between tailnet peers (WireGuard
  is end-to-end between the peers, and Tailscale's controller
  never holds private keys — those are generated on-device
  and never leave),
- audio, transcripts, or LLM prompts flowing peer-to-peer,
- the WireGuard session keys (only the public keys).

For the threat model this project targets, the coordinator's
visibility is metadata, not content. That's a real distinction:
a subpoena to Tailscale Inc. could reveal "this laptop and that
EC2 instance were connected at these times" but not "what they
said to each other".

If even that metadata is unacceptable, Tailscale supports
self-hosted coordinators (Headscale is the open-source one) —
at which point there is no "Tailscale Inc. sees anything" left.
[`TODO.md`](../TODO.md)'s multi-tenant section flags this as the
right answer for orgs with sovereignty requirements.

---

## Trust & community

### Q23. Who maintains this and why should I trust them?

The repository's `git log` shows the maintainer's name and
email. There is exactly one. They:

- have published the entire design (this repo).
- have shipped the entire threat model (`SECURITY.md`).
- have shipped a fully-costed alternative-approach analysis
  (`FORK_CONSIDERATIONS.md`).
- have shipped an honest legal posture (`LEGAL.md`).
- have shipped a *list of things they got wrong* (the section
  below).

None of that is the same as "you should trust them." The same
honest answer holds for every single-maintainer open-source
project: trust is something you build by reading the code, not
something a maintainer can grant themselves by writing
documents.

What we recommend:

1. Read `SECURITY.md` and check it against the source for
   discrepancies.
2. Run the test suite (`./venv/bin/python -m pytest tests/ -q`)
   and confirm what it actually proves.
3. Run `./run.sh doctor` against a fresh install and confirm
   every assertion lines up.
4. Diff the dependencies in `requirements.txt` against your
   own risk tolerance.

If anything is off, that's a `[security]`-tagged GitHub issue.

### Q24. How do I report a bug, file an issue, send a patch?

- **Bug reports**: GitHub issue with a clear repro + the output
  of `./run.sh doctor`.
- **Security vulnerabilities**: see [`SECURITY.md` §
  "Reporting a vulnerability"](../SECURITY.md#reporting-a-vulnerability)
  for the responsible-disclosure flow (low-severity → GitHub
  issue with `[security]`, higher-severity → direct contact).
- **Legal / licensing concerns**: see [`LEGAL.md` § 9
  "Reporting concerns"](../LEGAL.md#9-reporting-concerns).
- **Patches**: PRs welcome, with two notes — (a) please open
  an issue first if the change is non-trivial, so we can agree
  on shape before you do the work; (b) every PR has to leave
  the test suite green and the threat-model claims in
  `SECURITY.md` honest.

There is no bug-bounty programme. Material security findings
will be credited in the release notes.

---

## Where the criticism is fair

A non-exhaustive list of weaknesses the project knows about and
hasn't fixed yet. Calling these out up-front because the
alternative is letting reviewers discover them and assume we
were trying to hide them.

1. **The firewall is bypassable from Dock / Spotlight launches.**
   Network Extension is the right fix. Not built.
2. **There has been no third-party security audit.** Engaging
   one is in `TODO.md` but not scheduled.
3. **The cloud / Tailscale / TEE / HSM extensions are design
   documents, not code.** This is the gap between *what the
   threat model defends if extended* and *what is actually
   defended today*.
4. **The Python services run as the logged-in user, not in a
   privilege-separated subprocess.** A privileged-keyholder /
   unprivileged-frontend split would be safer.
5. **`sandbox-exec` is Apple-deprecated.** It works today, it
   will continue to work as long as Apple keeps shipping the
   binary, and we don't know when (or whether) Apple will
   remove it.
6. **`hdiutil`-backed sparse-bundle vaults are slower than
   APFS-native encrypted volumes.** APFS-encrypted volume
   support is a planned migration.
7. **The Char binary is downloaded over HTTPS from Fastrepl's
   release page; the SHA256 we pin is the SHA256 we pin.** A
   compromise of Fastrepl's signing key (or a state-level MITM
   that re-pins for us before we audit) is out-of-scope of the
   current verification.
8. **There is no Apple Developer enrollment** behind the
   integrity-baselining tooling, so the project itself can't
   produce a Notarised dmg of its own. Operationally fine for a
   PoC; not fine for a 1.0.
9. **The inspector UI ships as inlined HTML/CSS/JS strings**
   inside `inspector_server.py`. Cute for the privacy story
   (no CDN, no XHR to anything but `self`); ugly for
   maintainability. A move to per-file static assets is on
   `TODO.md`.
10. **The test suite is comprehensive on the security
    primitives and the integration glue, but does *not* end-to-
    end test against an actual Char binary.** That gap is
    mitigated by `char_audit.py` running on every startup,
    not eliminated.

If you find one we haven't listed, that's exactly the issue we
want to receive.

---

## Document history

| date | change |
|---|---|
| 2026-05-10 | Initial publication. 24 questions across 6 categories + a "where the criticism is fair" self-assessment with 10 known weaknesses. |
