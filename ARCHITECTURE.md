# Architecture diagrams

This is the single source of truth for **how `local_scribe` is wired
together**, rendered in [Mermaid](https://mermaid.js.org/) so GitHub
displays each diagram inline. Companion to:

- [`README.md`](README.md) — feature overview + operator guide
- [`SECURITY.md`](SECURITY.md) — threat model + defence layers in prose
- [`CHAR_REVIEW.md`](CHAR_REVIEW.md) — Char binary audit + network egress evidence
- [`TODO.md`](TODO.md) — open mitigations

> **Diagram-vs-reality note.** A few of these diagrams describe
> architecture that is implemented in code but **not yet wired into
> `./run.sh bootstrap`** on every install — specifically the
> Keychain master key, the sparse-bundle vault, and the YubiKey
> escrow path. Where this matters, the prose says so. See
> [`TODO.md`](TODO.md) for the remaining wiring work.

## Contents

1. [System overview](#1-system-overview)
2. [Component dependencies](#2-component-dependencies)
3. [Bootstrap flow](#3-bootstrap-flow)
4. [At-rest encryption (designed)](#4-at-rest-encryption-designed)
5. [Service authentication (HKDF tokens)](#5-service-authentication-hkdf-tokens)
6. [Outbound network firewall](#6-outbound-network-firewall)
7. [Char privacy audit](#7-char-privacy-audit)
8. [Live transcription (Deepgram-shape)](#8-live-transcription-deepgram-shape)
9. [Batch transcription (OpenAI-shape)](#9-batch-transcription-openai-shape)
10. [Diarization pipeline](#10-diarization-pipeline)
11. [Transcript history lifecycle](#11-transcript-history-lifecycle)
12. [Inspector UI flow](#12-inspector-ui-flow)
13. [Destructive-action confirmation (typed-DELETE)](#13-destructive-action-confirmation-typed-delete)
14. [Threat model × defence layers](#14-threat-model--defence-layers)
15. [Vault & key lifecycle](#15-vault--key-lifecycle)

### Legend

| shape | meaning |
|---|---|
| `[ rectangle ]` | code module / running service |
| `( rounded )` | external CLI command or human action |
| `(( circle ))` | actor (user / external) |
| `{ diamond }` | decision branch |
| `{{ hexagon }}` | OS-level control (Keychain, `/etc/hosts`, Touch ID, etc.) |
| `[( cylinder )]` | persistent storage |
| solid `-->` | active data / control flow |
| dashed `-.->` | optional / blocked / external attempt |
| crossed `-.X.->` | request that is *intentionally refused* by a defence layer |

---

## 1. System overview

The whole pipeline on one screen: how user audio reaches a finished
note, and which boundary the firewall enforces against any external
provider attempting to receive that audio.

```mermaid
flowchart LR
    User((User))

    subgraph Mac["macOS laptop — single trust boundary"]
      Char["Char.app<br/>recorder + notes UI"]
      ASR["asr_server.py :8000<br/>Parakeet / Whisper"]
      LMS["LM Studio :1234<br/>Qwen3-30B"]
      Insp["inspector_server.py :8001<br/>web UI"]
      Vault[("vault.sparsebundle<br/>AES-256 (designed)")]
      Fw{{"/etc/hosts firewall<br/>(firewall.py)"}}
    end

    Cloud[/"External providers<br/>Sentry · PostHog · OpenAI · Anthropic · …"/]:::blocked

    User <-->|"record / play / view notes"| Char
    User -.->|"browser"| Insp
    Char -->|"POST /v1/audio/transcriptions<br/>(Bearer ls_asr_…)"| ASR
    Char -->|"WS /v1/listen/stream<br/>(Token ls_asr_…)"| ASR
    ASR -->|"transcript JSON<br/>+ diarization meta"| Char
    Char -->|"chat completions"| LMS
    LMS -->|"summary"| Char
    Char -->|"audio + transcript + notes"| Vault
    Insp -->|"sessions / config / audit"| Vault
    Char -.->|"telemetry, updater, providers"| Fw
    LMS -.->|"analytics"| Fw
    Fw -.X.-> Cloud

    classDef blocked stroke-dasharray:5 5,stroke:#c33,color:#c33
```

---

## 2. Component dependencies

File-level dependency graph. Anything in the **crypto / auth** column
roots at the Keychain master key; anything in **pipeline** is pure
data processing that doesn't see secrets.

```mermaid
flowchart LR
    subgraph entry["entry points"]
      runsh["run.sh"]
      tf["transcribe_file.py"]
      rs["redo_session.py"]
    end
    subgraph servers["HTTP servers"]
      ASR["asr_server.py"]
      INS["inspector_server.py"]
    end
    subgraph crypto["crypto / auth"]
      SS["secret_store.py"]
      SA["service_auth.py"]
      VT["vault.py"]
      YK["yubikey_backup.py"]
    end
    subgraph pipe["pipeline / config"]
      DB["diarization_backend.py"]
      TH["transcript_history.py"]
      CA["char_audit.py"]
      FW["firewall.py"]
      CFG["config.py"]
    end
    swift["bin/touchid_keychain.swift<br/>(compiled to bin/touchid-keychain)"]

    runsh --> SS
    runsh --> SA
    runsh --> VT
    runsh --> YK
    runsh --> FW
    runsh --> CA
    runsh --> CFG
    runsh --> ASR
    runsh --> INS
    SA --> SS
    VT --> SS
    YK --> SS
    SS --> swift
    ASR --> SA
    ASR --> DB
    ASR --> TH
    ASR --> CFG
    INS --> SA
    INS --> TH
    INS --> CA
    INS --> FW
    INS --> CFG
    tf --> SA
    tf --> CFG
    rs --> SA
    rs --> CFG
    CA --> SA
    CA --> FW
    CA --> CFG
```

---

## 3. Bootstrap flow

What `./run.sh bootstrap` actually does on a fresh clone. The numbers
match the section headers printed at runtime.

```mermaid
flowchart TD
    Start(["./run.sh bootstrap"]) --> S1
    S1["1/7 python venv + pip deps<br/>requirements.txt"] --> S2
    S2["2/7 Parakeet ASR weights<br/>(HuggingFace snapshot)"] --> S3
    S3["3/7 sherpa-onnx diarization<br/>(pyannote-segmentation + TitaNet)"] --> S4
    S4["4/7 ~/.config/local_scribe/config.json<br/>(seeded with defaults)"] --> S5
    S5["5/7 LM Studio + Qwen3-30B<br/>(brew cask + lms get + lms load)"] --> S6
    S6{"Char.app installed?"}
    S6 -->|"no"| S6a["fetch pinned DMG<br/>(CHAR_KNOWN_GOOD_VERSION)<br/>verify SHA-256<br/>install /Applications/Char.app"]
    S6 -->|"yes"| S6b["compare with<br/>CHAR_KNOWN_GOOD_VERSION"]
    S6a --> S6cfg["./run.sh configure-char<br/>rewrite settings.json<br/>(stt provider + base_url +<br/>HKDF-derived api_key)"]
    S6b --> S6cfg
    S6cfg --> S7{"7/7 install<br/>/etc/hosts firewall?"}
    S7 -->|"yes"| S7y["firewall.upsert_block()<br/>(asks for sudo via osascript)"]
    S7 -->|"no"| S7n["skipped<br/>(re-run ./run.sh firewall enable later)"]
    S7y --> Done([ready: ./run.sh start])
    S7n --> Done
```

---

## 4. At-rest encryption (designed)

The full key + data graph as it lives in code. Hot (in-memory only)
items are red; cold (on-disk ciphertext) items are blue. The bytes
inside `secret_store.MasterKey` are the only ones that must never
touch argv, env vars, or any file other than the Keychain item or
the YubiKey-encrypted backup.

```mermaid
flowchart TD
    subgraph SEP["Secure Enclave Processor (Apple Silicon)"]
        TouchID["Touch ID sensor"]
        KCh["macOS Keychain item<br/>service=local_scribe<br/>account=master_key<br/>ACL: .userPresence<br/>(WhenUnlockedThisDeviceOnly)"]
    end
    Swift["bin/touchid-keychain<br/>(Swift helper)"]
    SS["secret_store.MasterKey<br/>32-byte bytearray<br/>forget() zeros on exit"]
    SA["service_auth<br/>HKDF-SHA256"]
    AsrTok["ls_asr_… token<br/>(in server RAM only)"]
    InsTok["ls_inspector_… token<br/>(in server RAM only)"]
    Vault["vault.sparsebundle<br/>AES-256-XTS via hdiutil<br/>password from stdin (-stdinpass)"]
    Mount["~/Library/Application Support/<br/>local_scribe-vault/hyprnote/<br/>(decrypted under our mount)"]
    Symlink["~/Library/Application Support/hyprnote<br/>→ symlink to mount"]
    Data[("audio.mp3, transcript.json,<br/>note .md, app.db<br/>(plaintext only while mounted)")]
    YK["YubiKey PIV slot 9a<br/>(touch-policy=always)"]
    Backup[("~/.config/local_scribe/<br/>key_backup.age")]

    TouchID -.->|"biometric attest"| KCh
    KCh -->|"hex on stdout<br/>(NEVER argv)"| Swift
    Swift -->|"32 bytes via stdin"| SS
    SS -->|"HKDF info=service:asr"| SA
    SA --> AsrTok
    SS -->|"HKDF info=service:inspector"| SA2["service_auth<br/>HKDF-SHA256"]
    SA2 --> InsTok
    SS -->|"hdiutil attach -stdinpass<br/>(password via pipe)"| Vault
    Vault -->|"transparent decrypt"| Mount
    Mount --> Data
    Symlink --> Mount
    SS -->|"age -r recipient<br/>(stdin master key)"| Backup
    Backup -.->|"age -d -i identity<br/>(YubiKey tap required)"| YK
    YK -.->|"recovered 32 bytes"| SS

    classDef hot fill:#fee,stroke:#c33,color:#900
    classDef cold fill:#eef,stroke:#33c
    class SS,AsrTok,InsTok hot
    class Vault,Backup,Data cold
```

---

## 5. Service authentication (HKDF tokens)

How a Char "Generate" click winds up authenticated against the ASR
server, end-to-end. Read this top to bottom and you have the whole
auth contract.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Char as Char.app
    participant RunSh as ./run.sh
    participant TouchID as Touch ID
    participant Keychain
    participant SA as service_auth
    participant ASR as asr_server :8000

    User->>RunSh: ./run.sh configure-char
    RunSh->>SA: python -m service_auth token asr
    SA->>TouchID: prompt "Derive ASR token"
    TouchID->>User: biometric request
    User->>TouchID: tap
    TouchID->>Keychain: unlock master_key
    Keychain-->>SA: 32 bytes
    SA->>SA: HKDF(salt=app+ver,<br/>ikm=mk, info="service:asr")
    SA-->>RunSh: ls_asr_<32-hex>
    RunSh->>Char: write settings.json::stt.openai.api_key
    Note over RunSh,Char: Token MUST NOT be passed on<br/>the inline-Python argv (open audit item).
    User->>Char: click "Generate" on a session
    Char->>ASR: POST /v1/audio/transcriptions<br/>Authorization: Bearer ls_asr_…
    ASR->>SA: extract_candidate_token(request)
    SA->>SA: hmac.compare_digest(stored, candidate)
    alt token matches
        SA-->>ASR: ok
        ASR-->>Char: 200 transcript JSON
    else token missing / wrong
        SA-->>ASR: 401
        ASR-->>Char: 401 + WWW-Authenticate hint
    end
```

---

## 6. Outbound network firewall

`firewall.py` writes a marker-delimited block into `/etc/hosts` that
blackholes every host in `BLOCK_CATALOG` to `0.0.0.0` (IPv4) and `::`
(IPv6). Categories are independently togglable. The block is
idempotent; reapply with `./run.sh firewall enable`.

```mermaid
flowchart LR
    subgraph CharBin["Char.app processes"]
      Sentry["sentry-sdk<br/>(panic + tracing)"]
      PostHog["posthog<br/>(analytics)"]
      Updater["tauri-updater<br/>(auto-update)"]
      Providers["openai · deepgram ·<br/>anthropic · mistral ·<br/>elevenlabs · gladia · …"]
      Calendar["calendar OAuth<br/>(api.char.com)"]
    end

    subgraph Hosts["/etc/hosts managed block (firewall.py)"]
      Cat1["**telemetry** (default ON)<br/>o4506…sentry.io<br/>us.i.posthog.com<br/>eu.i.posthog.com<br/>desktop2.hyprnote.com"]
      Cat2["**providers** (default ON)<br/>api.openai.com<br/>api.deepgram.com<br/>api.anthropic.com<br/>+ 10 more SDK hosts"]
      Cat3["**char_cloud** (opt-in: --strict)<br/>api.char.com<br/>cloudsync.sqlite.ai"]
    end

    Sink{{"0.0.0.0 / ::<br/>connection refused"}}

    Sentry -.->|"getaddrinfo"| Cat1
    PostHog -.->|"getaddrinfo"| Cat1
    Updater -.->|"getaddrinfo"| Cat1
    Providers -.->|"getaddrinfo"| Cat2
    Calendar -.->|"getaddrinfo"| Cat3
    Cat1 -.-> Sink
    Cat2 -.-> Sink
    Cat3 -.-> Sink

    classDef refused fill:#fee,stroke:#c33,color:#c33
    class Sink refused
```

---

## 7. Char privacy audit

What `./run.sh doctor` and `python -m char_audit` actually check.
Every check returns OK / INFO / WARN / FAIL, with a hint for how to
remediate. The endpoint `/api/char/audit` returns the same data as
JSON to the inspector.

```mermaid
flowchart TD
    Run(["./run.sh doctor<br/>or python -m char_audit"]) --> Load["load Char settings.json<br/>+ store.json"]
    Load --> C1["check 1: current_stt_provider == openai"]
    Load --> C2["check 2: stt.openai.base_url ==<br/>http://127.0.0.1:8000/v1"]
    Load --> C3["check 3: stt.openai.api_key<br/>== HKDF-derived ASR token<br/>(fingerprint match)"]
    Load --> C4["check 4: store.analytics.Disabled<br/>== true (PostHog kill-switch)"]
    Load --> C5["check 5: firewall.status()<br/>blocks every catalog host"]
    C1 --> Score
    C2 --> Score
    C3 --> Score
    C4 --> Score
    C5 --> Score
    Score{"all OK?"}
    Score -->|"yes"| Green(["exit 0 — all green"])
    Score -->|"some WARN"| Yellow(["exit 0 with WARN lines"])
    Score -->|"any FAIL"| Red(["exit 1"])
    Red -.->|"remediation hint"| Fix["./run.sh configure-char<br/>./run.sh firewall enable<br/>./run.sh status"]
```

---

## 8. Live transcription (Deepgram-shape)

The path Char takes when it's recording in real time. The ASR server
emits Deepgram-shaped JSON deltas over a WebSocket because that's
what Char's `Custom` provider expects.

```mermaid
sequenceDiagram
    autonumber
    participant Char
    participant ASR as asr_server :8000
    participant P as Parakeet (MLX)
    participant D as diarization_backend

    Note over Char: User clicks "Record"
    Char->>ASR: WS /v1/listen/stream<br/>?model=…&punctuate=true<br/>Authorization: Token ls_asr_…
    ASR->>ASR: validate token<br/>(close 1008 on miss)
    loop every audio chunk while recording
        Char->>ASR: binary frame (PCM / Opus)
        ASR->>P: incremental decode
        P-->>ASR: partial words + timestamps
        ASR-->>Char: Deepgram delta JSON<br/>{is_final, channel, alternatives}
    end
    Char->>ASR: WS close (user stops)
    ASR->>D: final pass on full buffer
    D-->>ASR: speaker turns + airtime + confidences
    ASR-->>Char: final Deepgram JSON<br/>(speaker_<n>: prefixes)
```

---

## 9. Batch transcription (OpenAI-shape)

The path Char takes when the user clicks **Generate** on a session
that already has an `audio.mp3` on disk. ASR + diarization run in
parallel; SSE heartbeats every 5 seconds keep Char's 60-second batch
timeout from firing.

```mermaid
sequenceDiagram
    autonumber
    participant Char
    participant ASR as asr_server :8000
    participant P as Parakeet (MLX)
    participant D as diarization_backend

    Char->>ASR: POST /v1/audio/transcriptions<br/>multipart audio + Bearer ls_asr_…
    ASR->>ASR: auth check (401 on miss)
    ASR-->>Char: SSE event=heartbeat (every 5s)
    par ASR transcribe
      ASR->>P: full decode
      P-->>ASR: words + word-level timestamps
    and Diarization
      ASR->>D: VAD + segmentation + embeddings + clustering
      D-->>ASR: speaker turns + per-cluster confidences
    end
    ASR->>ASR: align words ↔ speaker turns,<br/>compute airtime
    ASR-->>Char: SSE event=delta (rolling text)
    ASR-->>Char: SSE event=done<br/>OpenAI transcripts payload +<br/>local_scribe.diarization meta
    Char->>Char: persist transcript.json,<br/>flatten paragraphs in UI
```

---

## 10. Diarization pipeline

What `diarization_backend.py` does internally on a finished audio
buffer to attach `speaker_<n>` labels to ASR words.

```mermaid
flowchart LR
    A["audio.mp3"] --> R["resample → 16 kHz mono"]
    R --> V["sherpa-onnx VAD<br/>(speech regions)"]
    V --> SEG["pyannote-segmentation 3.0<br/>(turn boundaries)"]
    SEG --> EMB["NeMo TitaNet<br/>192-d embedding per turn"]
    EMB --> CLU["silhouette-validated<br/>spectral clustering<br/>(auto-K, no fixed N)"]
    CLU --> LBL["speaker_0, speaker_1, …<br/>+ per-turn confidence"]
    LBL --> ALN["align to ASR word timeline<br/>(word-id ↔ speaker)"]
    ALN --> OUT["local_scribe.diarization {<br/>  algorithm: auto_silhouette,<br/>  num_speakers, speakers[],<br/>  word_confidences[]<br/>}"]
```

---

## 11. Transcript history lifecycle

Every retranscription archives the previous result with a
timestamped + SHA-prefixed filename so nothing is ever silently
overwritten. The inspector surfaces both the live transcript and the
archive list, with one-click `.txt` download from any of them.

```mermaid
stateDiagram-v2
    [*] --> NoTranscript : session created

    NoTranscript --> Live : first ASR call writes transcript.json

    Live --> Archiving : user clicks Regenerate /<br/>./run.sh redo-session
    Archiving --> Archived : mv transcript.json →<br/>.local_scribe_history/<br/>YYYYMMDDTHHMMSSZ_<sha7>.json
    Archived --> Live : new ASR result written
    Live --> [*] : session deleted

    state Archived {
        [*] --> Listed
        Listed --> DownloadedTxt : GET /history/<f>/transcript.txt<br/>(flattened paragraphs + airtime,<br/>Content-Disposition attachment)
        Listed --> DownloadedJson : GET /history/<f><br/>(raw archive JSON)
        Listed --> Deleted : DELETE /history/<f><br/>(typed-DELETE confirm in UI)
    }
```

---

## 12. Inspector UI flow

Browser-side flow for an authenticated session. The cookie set by
`/auth?token=…` is `HttpOnly; SameSite=Strict` and survives
inspector restarts. Today the underlying disk is plaintext; once the
vault wiring lands, the same endpoint paths serve decrypted bytes
from the mounted sparse bundle.

```mermaid
sequenceDiagram
    autonumber
    participant Browser
    participant Insp as inspector_server :8001
    participant SA as service_auth
    participant Disk as on-disk session data<br/>(plaintext today / vault later)

    Note over Browser: User cmd-clicks the URL printed by<br/>./run.sh status (one-time per device)
    Browser->>Insp: GET /auth?token=ls_inspector_…
    Insp->>SA: validate against ServiceToken
    SA-->>Insp: ok
    Insp-->>Browser: Set-Cookie: ls_inspector=…<br/>HttpOnly; SameSite=Strict<br/>302 → /
    Browser->>Insp: GET /api/sessions  (cookie)
    Insp->>Disk: scan hyprnote/sessions
    Disk-->>Insp: meta + history_count
    Insp-->>Browser: [{id, title, has_audio, history_count, …}]
    Browser->>Insp: GET /api/sessions/{id}
    Browser->>Insp: GET /api/sessions/{id}/history
    Browser->>Insp: GET /api/sessions/{id}/transcript.txt
    Note right of Insp: Content-Disposition: attachment<br/>filename=transcript-<id>.txt
    Browser->>Insp: GET /api/sessions/{id}/history/<f>/transcript.txt
    Browser->>Insp: DELETE /api/sessions/{id}/audio
    Note right of Insp: only fires after the user typed<br/>"DELETE" in the modal (see § 13)
```

---

## 13. Destructive-action confirmation (typed-DELETE)

Both archive deletion and audio deletion go through the same
two-step modal. The danger button stays disabled until the input
matches `"DELETE"` exactly (case-sensitive); Enter submits when
valid, Escape / Cancel / backdrop click all resolve `false`.

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant UI as Browser (inspector_server SPA)
    participant API as inspector_server

    U->>UI: click "Delete audio" or<br/>"Delete archived transcript"
    UI->>UI: confirmTypedDelete(<br/>  title, description)
    UI-->>U: modal opens, focuses input
    U->>UI: types "delete" (lowercase)
    UI->>UI: confirm button stays disabled
    U->>UI: types "DELETE" exactly
    UI->>UI: confirm button enables
    U->>UI: click Confirm / press Enter
    UI->>API: DELETE /api/sessions/{id}/audio<br/>or /history/<f>
    API->>API: validate filename, unlink
    alt success
      API-->>UI: 200 {deleted, bytes_removed}
    else missing
      API-->>UI: 404
    else traversal / malformed
      API-->>UI: 400 / 500
    end
    UI->>UI: refresh session list + detail panel
```

---

## 14. Threat model × defence layers

Which adversary tier is mitigated by which control. The same
information lives in
[`SECURITY.md § Threat model`](SECURITY.md#threat-model) as a table;
the diagram is the navigable view. Dashed edges are partial
mitigations called out in prose.

```mermaid
flowchart LR
    subgraph adv["Adversary tiers"]
      A1["1. Remote network peer"]
      A2["2. Browser content / extension"]
      A3["3. Co-tenant on the same Mac"]
      A4["4. Shell-as-user, no Touch ID"]
      A5["5. Stolen laptop / forensic imager"]
      A6["6. Phished Touch ID"]
      A7["7. Root / TCC bypass / kernel"]
    end

    subgraph defence["Defence layers"]
      D1["loopback bind (127.0.0.1)"]
      D2["per-service bearer auth<br/>(service_auth)"]
      D3["at-rest vault<br/>AES-256 sparse bundle"]
      D4["outbound /etc/hosts firewall"]
      D5["char_audit + configure-char"]
      D6["pinned Char DMG + LM Studio cask"]
      D7["fingerprint logging<br/>(token reuse detection)"]
    end

    Out[(Out of scope:<br/>see SECURITY.md § Out of scope)]

    A1 --> D1
    A2 --> D2
    A3 --> D2
    A3 --> D3
    A4 --> D2
    A4 --> D3
    A4 --> D4
    A5 --> D3
    A5 -.->|"laptop must<br/>be unmounted"| D3
    A6 -.->|"partial"| D2
    A6 -.->|"partial"| D7
    A1 -.->|"settings drift"| D5
    A4 -.->|"settings drift"| D5
    A1 -.->|"supply chain"| D6
    A7 -.-> Out
```

---

## 15. Vault & key lifecycle

The complete state machine for the master key from
"freshly cloned repo" through compromise / loss / recovery.
`./run.sh vault init`, `./run.sh vault lock`, `./run.sh yubikey
enroll`, and `./run.sh vault rotate` are the operator-facing
transitions. The "Compromised → Rotated" path is the one we exercise
when a Touch ID phish is suspected — re-generating the master key
invalidates every derived bearer token in one step.

```mermaid
stateDiagram-v2
    [*] --> NoKey : fresh clone

    NoKey --> Generating : ./run.sh vault init<br/>(future: ./run.sh key init)
    Generating --> Stored : 32 random bytes →<br/>Keychain (.userPresence)

    Stored --> Mounted : hdiutil attach -stdinpass<br/>(Touch ID prompt)
    Mounted --> Working : services running, vault mounted,<br/>Char data symlinked

    Working --> Unmounted : ./run.sh stop / vault lock
    Unmounted --> Mounted : ./run.sh start (Touch ID)

    Stored --> BackedUp : ./run.sh yubikey enroll<br/>+ backup_key()
    BackedUp --> Stored : (no flow back — escrow is<br/>a one-way write of the key bytes)

    Working --> Compromised : Keychain ACL bypass /<br/>leaked token suspected
    Compromised --> Rotated : ./run.sh vault rotate<br/>(re-encrypt keybag, refresh tokens)
    Rotated --> Stored

    Stored --> Lost : Keychain wiped / Mac dead
    Lost --> Restored : age -d -i yubikey_identity backup.age<br/>(YubiKey tap required)
    Restored --> Stored

    Lost --> [*] : no YubiKey backup →<br/>data unrecoverable
```

---

### Updating these diagrams

If you change behaviour that one of these diagrams describes, update
both the diagram **and** the prose intro for the section. The
ARCHITECTURE.md is part of the doctor-checked surface: drift between
diagram and code is a documentation bug.

The companion text-based ASCII diagram at the top of `README.md` is
intentionally kept in sync with diagram **§ 1 (System overview)** —
they describe the same thing, the Mermaid version is just clickable.
