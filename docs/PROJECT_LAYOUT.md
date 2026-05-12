# Project layout

> Moved from the top-level README on 2026-05-12 as part of
> the condense-and-link pass. Combines the README's two
> previously separate sections — "What's in here"
> (per-module table) and "Project layout" (filesystem
> tree) — into one canonical codebase reference.

## Per-module table

| | what | role |
|---|---|---|
| `asr_server.py` | FastAPI service on `:8000`. Speaks **two** transcription contracts so both of Char's flows work: Deepgram (`/v1/listen` POST + WebSocket) for live recording, and OpenAI Whisper (`/v1/audio/transcriptions`) for "Generate" on existing audio. Routes both through Parakeet (default) or faster-whisper. | Char's transcription endpoint |
| `parakeet_backend.py` | parakeet-mlx wrapper. Merges sub-word BPE tokens into clean words, shapes output to Deepgram's word/timing schema. | Default ASR engine |
| `diarization_backend.py` | sherpa-onnx (pyannote 3.0 segmentation + NeMo TitaNet embedding) with **silhouette-validated auto-K** spectral clustering on top (the same approach AWS Transcribe and pyannote.audio v3.1+ use) — picks the speaker count from the data itself, plus an LLM pass that maps `SPEAKER_00/01/...` to real names. | Speaker labeling |
| `transcribe_file.py` | CLI for files Char didn't auto-pick up. Streams a structured Markdown summary (TL;DR, Participants, Key points, Decisions, Open questions, Risks, Next steps, Notable quotes), with optional diarization. Caches results by audio sha256. | Manual workflow |
| `redo_session.py` | Re-runs ASR + diarization on an existing Char session and overwrites its `transcript.json` via `char_persist.py`. Used when the original Generate produced the wrong number of speakers (1:1 came back as one blob, or a long meeting over-clustered). Match by full UUID, UUID prefix, or session-title substring. Invoked via `./run.sh redo-session …`. | Per-session re-do |
| `transcript_history.py` | Auto-archives `transcript.json` before each overwrite into `<session>/.local_scribe_history/<timestamp>_<sha7>.json`. Each archive is the previous file verbatim plus a `local_scribe` metadata block (ASR model, diarization algorithm, K, audio sha256, timestamps). The inspector exposes list/view/download/delete per archive. | Re-transcription history |
| `firewall.py` | Block catalog + dual-mode firewall manager. Default `process` mode is a no-op at the OS layer — enforcement lives in `egress_proxy.py` + `char_sandbox.py`. Opt-in `system` mode rewrites `/etc/hosts` machine-wide. Exposes `is_blocked()` for cross-module use. Driven by `./run.sh firewall …`. | Outbound egress control (policy) |
| `egress_proxy.py` | Pure-stdlib asyncio CONNECT proxy on `127.0.0.1:8889`. Refuses CONNECT requests for any host in `firewall.BLOCK_CATALOG` with a `403` + JSON deny body. Ring-buffer audit log. CLI: `python -m egress_proxy {start,status,verify,recent}`. Auto-starts from `./run.sh start`. | Outbound egress control (enforcement, policy half) |
| `char_sandbox.py` | Renders the SBPL `sandbox-exec` profile that `./run.sh char launch` applies to Char. `(allow default)` + `(deny network-outbound)` + re-allow loopback only. Validates against `sandbox-exec -f profile /usr/bin/true` before launch. Profile lives at `~/.config/local_scribe/char.sb`. | Outbound egress control (enforcement, containment half) |
| `sip_check.py` | Parses `csrutil status` and refuses to let the project start when System Integrity Protection isn't fully on. Gated from `run.sh` (start, bootstrap, every key command, configure-char, redo-session) and from every FastAPI service lifespan. No operator override. | Mandatory SIP gate |
| `service_auth.py` | HKDF-SHA256 per-service bearer tokens derived from the master key. Enforced by every gated FastAPI route. | Inter-service authentication |
| `key_split.py` | Pure XOR construction (`master_key = kc_half XOR yk_half`). Stdlib only. | Split-key crypto primitive |
| `secret_store.py` | macOS Keychain bridge via the Swift Touch ID helper. Holds the `kc_half` item (and the legacy v1 whole-key item during migration). | Keychain factor |
| `yubikey_backup.py` | `age`-based wrapping of `yk_half`, including multi-recipient enrollment so a backup YubiKey can decrypt the same file. | YubiKey factor |
| `disaster_recovery.py` | Passphrase-encrypted age copy of the **whole** master key. Strictly opt-in at `init` time. The recovery path for "lost both factors". | Disaster recovery |
| `key_lifecycle.py` | Orchestrator: `init / unlock / rotate / add_yubikey / dr_restore / migrate_v1_to_v2 / status`. Plus a `python -m key_lifecycle …` CLI that `./run.sh key` delegates to. | Two-factor key lifecycle |
| `key_safety.py` | Pre-flight snapshots + physical-presence proof. Every destructive key op (`rotate`, `init --force`, `dr-restore` over live v2, `add-yubikey`, `migrate`, `destroy`) snapshots the about-to-be-replaced material before mutating. Snapshots are never auto-pruned. CLI: `python -m key_safety {list,prune,restore-kc-half}`. | Data-loss safety net |
| `vault.py` | macOS `hdiutil` wrapper: creates / mounts / unmounts an AES-256 sparse-bundle disk image and relocates Char's data dir into it. The hdiutil passphrase flows in via `stdin` (never argv). | AES-256 vault primitive |
| `vault_unlock.py` | Glue between `key_lifecycle` and `vault`. Derives the hdiutil passphrase from the master key via HKDF-SHA256 (`local_scribe.vault.passphrase.v1`), so unlocking the vault is the same op as unlocking the master. CLI: `python -m vault_unlock {init,unlock,lock,status}` (delegated to from `./run.sh vault`). | Vault ↔ split-key bridge |
| `KEY_SAFETY.md` | Enumeration of every data-loss scenario (S1–S18) and the mitigation tied to each one. Recovery flowchart. Pre-install checklist. | Key-mistake catalogue |
| `CRYPTO.md` | Every cryptographic primitive in use, contrasted with its plausible alternatives, with rationale and residual risk for each. Includes 11 future-improvement items mirrored into `TODO.md`. | Cryptographic-engineering rationale |
| `char_settings_writer.py` | Stdin-driven JSON patcher for Char's `settings.json`. Used by `./run.sh configure-char` so the ASR bearer token never appears in argv. | Argv-leak hardening |
| `char_audit.py` | Reads Char's `settings.json` + `store.json` and asserts the four-key contract + firewall coverage. Surfaces drift in `./run.sh doctor` and the inspector's Char Audit tab. | Char-settings enforcement |
| `bin/touchid_keychain.swift` | Compiled by `./run.sh bootstrap` into `bin/touchid-keychain`. Accepts `--account NAME` so the same binary manages both the legacy whole-key item and the new `kc_half` item. | Touch ID bridge |
| `run.sh` | Service manager + bootstrap. Single command to install deps, download models, start/stop everything, manage the firewall + keys, and produce health reports. | Operator tool |
| `ARCHITECTURE.md` | Every major flow rendered as a Mermaid diagram (system overview, bootstrap, encryption design, auth, firewall, audit, transcription paths, diarization, history, inspector, threat model, key lifecycle). Linked from the top of this README. | Diagrammatic reference |
| `SECURITY.md` | Threat model and per-layer defense rationale. Companion to ARCHITECTURE.md § 14 (threat model diagram). | Security policy |
| `CHAR_REVIEW.md` | Char binary audit + network egress evidence. Companion to ARCHITECTURE.md § 6 (firewall diagram). | Char binary audit |
| `LEGAL.md` | Project-level ethics + legal: the explicit non-endorsement of covert recording, jurisdictional recording-consent pointers (US federal/state wiretap, EU/UK GDPR, etc.), MIT licence rationale, plain-English liability disclaimer, user indemnification, export-control determination, trademark acknowledgements. | Legal & ethics policy |
| `QUESTIONS.md` | Answers to the questions a security or developer reader is most likely to have after skimming the rest: "why didn't you fork Char?", "if my laptop is compromised what does the YubiKey actually buy me?", "isn't `sandbox-exec` deprecated?", "the Dock-launch bypass undermines the firewall, right?", etc. Includes a "Where the criticism is fair" self-assessment of 10 known weaknesses. | FAQ / skeptical-reader response |
| `FORK_CONSIDERATIONS.md` | 654-line analysis of the sidecar-vs-fork trade-off: what forking buys, what it costs (Apple Developer enrollment, signing, notarisation, upstream-merge cadence, brand surface), and the recommended path forward. Linked from `QUESTIONS.md` § Q1. | Fork-decision rationale |
| `tools/seed_demo.py` | Generates 5 synthetic Char sessions (silent MP3 + structured `transcript.json` with diarization metadata + notes + history) under `~/.cache/local_scribe-demo/`. Refuses to write to the real Char data dir. Used by the screenshots in this README and by anyone evaluating the UI without enrolling a YubiKey. | Demo-data seeding |
| `tools/run_demo.sh` | Starts an isolated inspector instance (default port `8765`) against the seeded demo dir, with `LOCAL_SCRIBE_DISABLE_AUTH=1` + a SIP test-stub for screenshot reproducibility. None of those bypasses are honoured by the production `./run.sh start` path. | Demo inspector launcher |
| `tools/capture_screenshots.sh` | Headless-Chrome driver that hits the demo inspector and writes the six PNGs under `docs/screenshots/`. Used by `./run.sh demo` and by anyone refreshing the screenshots in this README. | Screenshot reproducibility |

## Filesystem tree

```
local_scribe/
├── asr_server.py            # FastAPI server (Deepgram-compatible)
├── transcribe_file.py       # CLI for manual files
├── redo_session.py          # ./run.sh redo-session: re-run ASR + diarization
│                            #   on an existing Char session, overwrite transcript.json
├── parakeet_backend.py      # parakeet-mlx wrapper, BPE -> Deepgram words
├── diarization_backend.py   # sherpa-onnx + LLM speaker naming
├── inspector_server.py      # FastAPI web UI + sessions/config/char-audit API
├── char_audit.py            # Char.app safety check + configure-char logic
├── char_persist.py          # SHA256-match audio -> sidecar-write transcript.json
│                            #   (workaround for Char's progressive parser dropping words)
├── transcript_history.py    # auto-archive previous transcript.json on overwrite
│                            #   <session>/.local_scribe_history/<ts>_<sha7>.json
├── config.py                # config loader (defaults <- file <- env)
├── run.sh                   # service manager, bootstrap, doctor
├── requirements.txt
├── tests/                   # 294 unit tests, fully hermetic (mock all I/O)
└── .run/                    # PID files, log file, deps stamp (gitignored)
```

Caches (gitignored, safe to delete to free disk):

```
~/.cache/huggingface/hub/                                # Parakeet, faster-whisper
~/.cache/local_scribe/diarization/                  # sherpa-onnx ONNX models
~/.cache/local_scribe/transcripts/<sha256>.json     # cached ASR results
```

