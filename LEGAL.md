# Legal & ethics

> **TL;DR.** `local_scribe` is a **proof-of-concept research artefact**
> for end-to-end-private call recording, transcription, and
> summarisation. It is **not** a product, **not** legal advice, and
> the maintainers **do not endorse recording any person without their
> knowledge or consent, under any circumstance, for any reason**. The
> software is provided AS IS under the [MIT License](LICENSE), with
> no warranty and no liability for how it is used. **Complying with
> the recording-consent laws of your jurisdiction is entirely your
> responsibility.** If you are not sure whether you are legally
> permitted to record a particular conversation, **don't**.

---

## 1. What this project is, and is not

`local_scribe` is a **proof-of-concept** that demonstrates what an
end-to-end-private call-recording and transcription stack can look
like on a single laptop: locally hosted ASR, locally hosted LLM,
AES-256 vault for on-disk audio, MFA-gated keys (YubiKey + Touch ID),
per-process outbound firewall, attestation-based design notes for a
future private-cloud extension. See [`README.md`](README.md) §
"Status — proof of concept" and [`SECURITY.md`](SECURITY.md) for the
technical framing.

It is **not**:

- a commercial product, a service, or a hosted offering;
- a substitute for a recording app that has been reviewed by counsel
  in your jurisdiction;
- a substitute for the consent of the people whose voices are being
  recorded;
- a substitute for the privacy notices, data-processing agreements,
  and retention policies that the General Data Protection Regulation
  (EU/UK GDPR), the California Consumer Privacy Act (CCPA/CPRA), and
  comparable laws may require of you;
- in any way endorsed by, affiliated with, or sponsored by Char.app,
  Fastrepl Inc., OpenAI, NVIDIA, Apple, LM Studio, Yubico, AWS, or
  Tailscale. Their names appear here as descriptive references to
  third-party software and hardware this project is designed to
  interoperate with.

The maintainers publish this work for **research, education, and as
a reference architecture** that downstream projects (including
[Char](https://github.com/fastrepl/anarlog) itself) are explicitly
welcome to adopt and improve upon.

---

## 2. Our position on recording without consent

The maintainers of `local_scribe` **categorically do not endorse**
recording any person without their knowledge or consent. Not for
journalism, not for "gotcha" content, not for evidence-gathering, not
for personal note-taking, not for sales training, not for any
"productivity" reason, not ever. There is no use case for covert
recording that we consider legitimate in the design of this project,
and **building this software does not constitute permission to use
it that way**.

We understand that in **two-party-consent jurisdictions** (also
called "all-party-consent" — see § 3 below), the surreptitious
recording of a private conversation is a **criminal offence** and
can also expose the recorder to civil liability, even when one party
(the recorder) has self-consented. We understand that this is true
in California, Florida, Illinois, Massachusetts, Pennsylvania,
Washington, and a number of other US states; in most of the EU and
the UK under GDPR and national equivalents; and in much of Canada,
Australia, and elsewhere. We have intentionally **not** built any
feature that would help someone evade these laws (no hidden-mode UI,
no transcript scrubbing of consent disclosures, no automatic
deletion that would frustrate a discovery request, no shipping
elsewhere of the recording without the user explicitly authorising
it).

What this project *does* support, and what we recommend you actually
do:

- **Tell every participant** the conversation is being recorded,
  *before* recording starts, in a way they can object to. A verbal
  disclosure on the call — "I'd like to record this call for note-
  taking purposes; is that OK with you?" — followed by a verbal
  acknowledgement is the operational minimum. In writing where
  feasible (calendar invites, meeting agendas).
- **Use the visible recording indicator** that Char provides. Do not
  disable it. Do not fork the project to remove it.
- **Set, document, and honour a retention policy**. The encrypted
  vault makes deletion easy; the typed-DELETE confirm body in the
  inspector ([`SECURITY.md`](SECURITY.md) § "Defense-in-depth:
  typed-DELETE confirm body") makes accidental deletion hard. Use
  both. Respect data-subject erasure requests under GDPR Art. 17,
  CCPA § 1798.105, and equivalents.
- **Treat recordings of people who didn't consent as material that
  shouldn't exist**. If consent was unclear, ambiguous, or absent,
  delete the recording and the transcript. The "secure" part of
  "secure local recording stack" only protects data we should be
  keeping in the first place.
- **Don't share transcripts outside the people who were on the call**
  without their permission. Local-first is a starting point, not an
  excuse.

---

## 3. Recording-consent laws — your responsibility, not ours

Recording-consent law varies enormously by jurisdiction, by the type
of communication (in-person, telephone, VoIP, video), by whether the
recording is for personal use or for processing under a commercial
basis, and by the medium of the transcript downstream. The summary
below is **not legal advice** — it is a pointer to the categories of
law you need to be aware of, presented so a careful reader can
identify which questions to take to a lawyer in their jurisdiction.

### United States

- **Federal:** 18 U.S.C. § 2511 (the Wiretap Act) prohibits the
  interception of wire, oral, and electronic communications without
  the consent of at least one party. This is the floor; states may
  (and many do) impose stricter requirements on top.
- **One-party-consent states** (majority): recording is permitted if
  at least one party — usually the recorder — consents.
- **All-party / two-party-consent states** (minority but populous):
  every party to the conversation must consent. The exact list
  changes over time as case law evolves, but California (Cal. Penal
  Code § 632), Florida (Fla. Stat. § 934.03), Illinois (720 ILCS
  5/14-2), Maryland (Md. Code, Cts. & Jud. Proc. § 10-402),
  Massachusetts (Mass. Gen. Laws ch. 272, § 99), Pennsylvania (18
  Pa. C.S. § 5704), and Washington (RCW 9.73.030) are the most
  commonly cited. **The legality of the recording is generally
  determined by the law of the state where the recorded party is
  located**, not where you are — meaning a call from a one-party
  state into a two-party state is typically held to the two-party
  standard.
- **Federal Communications Commission rules** (47 C.F.R. § 64.501)
  add a separate beep-tone / consent / participant-notification
  requirement for some classes of telephone recordings made by
  carriers.

### European Union & United Kingdom

- **GDPR (EU 2016/679) and UK GDPR**: the audio of an identifiable
  person is *personal data*, and a *recording* of them is
  *processing* of that personal data. The recorder is a
  **controller** and must satisfy a lawful basis under Art. 6
  (almost always *consent*, Art. 6(1)(a), for recordings of this
  kind), provide a transparency notice under Art. 13, honour
  data-subject rights under Arts. 15–22, and apply integrity-and-
  confidentiality controls under Art. 32. Special-category data
  (Art. 9) — e.g. health, biometric voice patterns at scale,
  religious views — triggers additional requirements.
- **National implementations**: most EU member states and the UK
  layer additional rules on top (e.g. the UK's Investigatory Powers
  Act 2016 and the Regulation of Investigatory Powers Act 2000 govern
  interception by public authorities; the Data Protection Act 2018
  is the UK GDPR implementing statute).
- **ePrivacy Directive (2002/58/EC)** continues to govern
  confidentiality of communications in the EU and is being updated
  to the ePrivacy Regulation.

### Other jurisdictions

- **Canada**: the Criminal Code § 184 prohibits interception of
  private communications without the consent of at least one party
  (one-party consent at the federal level); provincial privacy and
  consumer-protection statutes layer on top.
- **Australia**: federal Telecommunications (Interception and
  Access) Act 1979 plus state-level Listening Devices / Surveillance
  Devices Acts (varies by state — Victoria, NSW, Queensland, WA, SA,
  Tasmania, ACT, NT each have their own).
- **Anywhere else**: assume the local regime is at least as strict
  as the strictest of the above and verify before recording.

### Three rules of thumb that travel well

1. **When in doubt, get explicit verbal consent on the record from
   every participant before recording starts.** "Is everyone OK with
   me recording this?" — followed by everyone saying "yes" — is the
   single most defensible practice and works in essentially every
   jurisdiction.
2. **If a participant says no, don't record.** Not even just your
   side. Not even just for "notes". Not even if the call is
   "important". Take notes by hand.
3. **Treat any recording of someone who hasn't consented as material
   that shouldn't exist** (see § 2). Delete it.

### This is not legal advice

Nothing in this document, this repository, or any communication from
the maintainers is legal advice. The maintainers are not your lawyer
and have no attorney–client relationship with you. The laws cited
above change frequently and are interpreted differently by different
courts. **If you are using this software for anything other than
recording yourself talking to yourself, consult a qualified attorney
in your jurisdiction first.**

---

## 4. License

`local_scribe` is released under the **MIT License**. The canonical
text lives in [`LICENSE`](LICENSE) at the root of this repository
and is reproduced in summary below:

> Copyright (c) 2026 local_scribe contributors.
>
> Permission is hereby granted, free of charge, to any person
> obtaining a copy of this software and associated documentation
> files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy,
> modify, merge, publish, distribute, sublicense, and/or sell copies
> of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be
> included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
> EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
> MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
> HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
> WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.

### Why MIT?

- **Compatibility with Char.** Char is MIT-licensed (`fastrepl/anarlog`).
  Picking the same licence means anything we publish here can be
  trivially adopted upstream by the Char project, which is one of the
  stated goals of this scaffold ([`README.md`](README.md) § "Why
  scaffold around Char rather than fork it?").
- **Compatibility with the model ecosystem.** The downstream models
  ship under MIT / Apache 2.0 / CC-BY-4.0 (see
  [`README.md`](README.md) § "License" for the per-model breakdown).
  MIT for the glue keeps the combined-work licence story trivially
  composable.
- **Minimal friction for researchers, security-curious users, and the
  Char team.** No NOTICE file overhead, no field-of-use restrictions,
  no patent-grant subtleties to navigate.

### What MIT does *not* license

The MIT licence covers only the source files in this repository.
It does **not** grant any right to:

- the Char trademark, the Char logo, or any rights in Char.app itself
  (those belong to Fastrepl Inc. and are governed by Char's own
  licence at `fastrepl/anarlog`);
- the trademarks "Tailscale", "AWS", "Nitro", "CloudHSM", "YubiKey",
  "YubiHSM", "LM Studio", "Apple", "Touch ID", "OpenAI", "Deepgram",
  "NVIDIA", "Parakeet", "Qwen", "Whisper", "WireGuard", "Signal", or
  any other third-party mark mentioned in the documentation. All
  such marks belong to their respective owners; references are
  descriptive use only and do not imply endorsement;
- the **model weights** downloaded during `./run.sh bootstrap` —
  those are licensed by their respective publishers under their own
  terms (Parakeet TDT v3: CC-BY-4.0 / NVIDIA; Whisper: MIT / OpenAI;
  sherpa-onnx ONNX models: Apache 2.0 / MIT; Qwen3: Apache 2.0).
  Check the publisher's model card before redistributing.

---

## 5. Limitation of liability — restated in plain English

The MIT licence's "AS IS" clause is the legally operative wording.
For clarity, here is what it means in operational terms:

- This software is provided **without warranty of any kind**, express
  or implied, including without limitation any implied warranty of
  merchantability, fitness for a particular purpose, accuracy,
  completeness, security, non-infringement, or quiet enjoyment.
- The maintainers **make no representation** that the software is
  fit for any particular jurisdiction, regulatory regime, business
  purpose, or use case.
- The maintainers **make no representation** that the security
  primitives in this project (the encrypted vault, the YubiKey
  split-key, the firewall, the typed-DELETE gate, the attestation
  design notes) are correctly implemented, sufficient for any
  threat model you care about, or free of bugs. The security model
  documented in [`SECURITY.md`](SECURITY.md) is the design we have
  tried to implement; **it has not been independently audited by a
  third-party security firm**, and we have neither claimed nor
  warranted that it has.
- The maintainers **make no representation** that the third-party
  software, models, or hardware this project is designed to
  interoperate with (Char, LM Studio, YubiKey, Touch ID, Apple
  Keychain, hdiutil, age, sherpa-onnx, Parakeet, Whisper, Qwen,
  Tailscale, AWS Nitro Enclaves, CloudHSM, YubiHSM) behave correctly,
  remain available, or remain compatible with this project.
- **The maintainers will not be liable** for any direct, indirect,
  incidental, special, consequential, exemplary, or punitive damages,
  including but not limited to: lost recordings, recorded
  conversations that should not have been recorded, prosecution under
  recording-consent laws, civil liability to participants whose
  consent was not obtained, fines and penalties under GDPR / CCPA /
  similar laws, data breaches arising from misuse or misconfiguration
  of the security primitives, loss of revenue, loss of goodwill, loss
  of business, loss of data, business interruption, costs of
  procurement of substitute software, or any other commercial damages
  or losses — even if the maintainers have been advised of the
  possibility of such damages.
- **Using `local_scribe` constitutes acceptance of these terms** and
  of the [MIT License](LICENSE). If you do not accept them, do not
  use the software.

In jurisdictions that do not allow the exclusion of certain
warranties or the limitation of liability for consequential or
incidental damages, the exclusions and limitations above apply to the
maximum extent permitted by applicable law.

---

## 6. Indemnification by the user

By using `local_scribe`, you agree to **indemnify, defend, and hold
harmless** the maintainers, contributors, and copyright holders from
and against any and all claims, damages, losses, liabilities, costs,
and expenses (including reasonable attorneys' fees) arising out of or
related to:

- your use of the software, including any failure to obtain consent
  from participants in a recording;
- your violation of any applicable recording-consent, wiretap,
  privacy, data-protection, employment, professional-conduct, or
  other law;
- your storage, sharing, transmission, or destruction of recordings
  or transcripts produced with the software;
- your breach of any of the terms in this document or in
  [`LICENSE`](LICENSE).

This obligation survives any termination of your use of the software.

---

## 7. Compliance with export-control and cryptography regulations

This project uses standard, publicly available cryptographic
primitives (AES-256, X25519, Ed25519, HKDF-SHA256, ChaCha20-Poly1305
via age, RSA / ECDSA via YubiKey PIV). To the maintainers'
understanding, the source code as published is **publicly available
encryption source code** within the meaning of the US Export
Administration Regulations (EAR) § 742.15(b) and is therefore not
subject to export controls under the EAR. The same status under EU
Council Regulation (EU) 2021/821 (the EU dual-use regulation) is
the maintainers' good-faith understanding but **has not been
formally verified**.

If you are operating in a jurisdiction that restricts the import,
use, or distribution of strong cryptography (some jurisdictions still
do — verify locally), it is your responsibility to comply. The
maintainers will not be a party to circumventing such restrictions.

If you intend to distribute a binary build or hosted service derived
from this project — particularly into or out of US-sanctioned
jurisdictions (Cuba, Iran, North Korea, Syria, the so-called DNR/LNR
regions of Ukraine, Crimea), or to entities on the OFAC Specially
Designated Nationals list — you are responsible for your own export-
control determination. The maintainers do not perform that
determination on your behalf.

---

## 8. Third-party software & trademarks

`local_scribe` interoperates with, but is not affiliated with:

- **Char.app** — © Fastrepl Inc., MIT-licensed at
  [`fastrepl/anarlog`](https://github.com/fastrepl/anarlog).
- **LM Studio** — © Element Labs, Inc., closed-source. References
  here are descriptive of its REST API contract.
- **NVIDIA Parakeet TDT 0.6B v3** — © NVIDIA, CC-BY-4.0.
- **OpenAI Whisper** — © OpenAI, MIT-licensed (model weights);
  references to the OpenAI HTTP API contract are descriptive.
- **Qwen3** — © Alibaba Cloud / Qwen Team, Apache 2.0.
- **sherpa-onnx** — © Xiaomi / k2-fsa contributors, Apache 2.0.
- **age**, **age-plugin-yubikey** — © FiloSottile and contributors,
  BSD-3-Clause / MIT.
- **YubiKey, YubiHSM** — trademarks of Yubico AB.
- **Apple, Touch ID, macOS, Keychain, Secure Enclave, Apple Private
  Cloud Compute, hdiutil, sandbox-exec, SIP** — trademarks of Apple
  Inc.
- **AWS, Amazon Web Services, Nitro Enclaves, CloudHSM** —
  trademarks of Amazon.com, Inc. or its affiliates.
- **Tailscale** — trademark of Tailscale Inc.
- **WireGuard** — registered trademark of Jason A. Donenfeld.
- **Signal**, **whispersystems/signal-protocol**, **X3DH**, **Double
  Ratchet** — trademarks and protocols of Signal Messenger LLC /
  Signal Foundation. The technical comparison in
  [`SECURITY.md`](SECURITY.md) § "How this compares to Signal's wire
  crypto" is descriptive scholarship; nothing here is published with
  Signal Foundation's review or endorsement.

All trademarks remain the property of their respective owners.
References in this project are descriptive (nominative-fair-use) and
do not imply endorsement, sponsorship, or affiliation.

---

## 9. Reporting concerns

- **Security vulnerabilities** in this codebase: see
  [`SECURITY.md`](SECURITY.md) § "Reporting a vulnerability".
- **Licensing or trademark issues**: open a GitHub issue with
  `[legal]` in the title, or contact the maintainer at the email
  address in the repository's `git log`.
- **Concern that a specific deployment of `local_scribe` is being
  used to record people without their consent**: the maintainers
  have no operational control over downstream deployments and
  cannot intervene in any individual instance. The appropriate
  remedy is local law-enforcement and/or civil counsel in the
  jurisdiction where the recording occurred.
- **Allegations that the software itself is unlawful in your
  jurisdiction**: open a GitHub issue with `[legal]` in the title;
  the maintainers will engage in good faith with the specific legal
  basis cited and may publish a jurisdiction note in this document
  if appropriate. The maintainers cannot, however, indemnify you or
  any third party against the consequences of using the software.

---

## 10. Changes to this document

We may update this document as the project, the third-party
ecosystem, or the legal landscape evolves. Material changes will be
called out in the commit message and in any release notes; users are
encouraged to read [`LEGAL.md`](LEGAL.md) at the version of the
software they are actually running.

| date | change |
|---|---|
| 2026-05-10 | Initial publication. Establishes the project's PoC framing, the maintainers' explicit non-endorsement of covert recording, the user's responsibility for consent-law compliance (US federal/state, EU/UK GDPR, other jurisdictions), the MIT licence choice and rationale, the plain-English limitation-of-liability restatement, the user indemnification clause, the export-control good-faith determination, the trademark / third-party-software acknowledgements, and the reporting channels. |
