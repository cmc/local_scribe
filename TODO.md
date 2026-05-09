# TODO / Roadmap

Tracking enhancements that aren't in the current commit but are
worth doing. **Privacy items are P0** because the project's whole value
prop is "audio and transcripts never leave your laptop"; the work below
tightens that guarantee further.

Cross-reference: [§ Privacy and data locality](README.md#privacy-and-data-locality)
in the main README explains the current guarantees this list is
extending.

## Privacy & security (P0)

- [ ] **Encrypt audio at rest.** Char writes raw `audio.mp3` to
      `~/Library/Application Support/hyprnote/sessions/<uuid>/`. If the
      laptop is stolen, image-restored, or its volume mounted from
      another OS, recordings are readable as-is. Plan: a `local_scribe`
      daemon that watches Char's session directory, generates a
      256-bit AES key per session in macOS Keychain (`security
      add-generic-password ...`), encrypts `audio.mp3` to
      `audio.mp3.enc`, zeroes the plaintext. Decrypts on demand by
      either intercepting Char's player path or exposing a transparent
      mountpoint (FUSE). The Keychain ACL would require user
      authentication (Touch ID) on each unlock.
- [ ] **Encrypt transcripts and summaries at rest.** Same approach
      applied to `transcript.json`, `_summary.md`, and the per-template
      note markdown files. Char reads/writes these on Generate; the
      encryption shim has to mediate.
- [ ] **Encrypt the local-scribe transcript cache** at
      `~/.cache/local_scribe/transcripts/`. Currently keyed by audio
      sha256 with the cached output stored as plain JSON.
- [ ] **`./run.sh wipe`** — single command that finds and securely
      overwrites everything under Char's session directory + our
      transcript cache + Time Machine local snapshots that include
      them. Confirms with a typed phrase first ("yes, destroy") because
      there's no undo. Optional `--keep-config` flag to preserve
      `settings.json` so you can re-record without re-bootstrapping.
- [ ] **Auto-purge.** Env var `RETENTION_DAYS` (default unset =
      forever). Background task that deletes session directories older
      than the threshold and writes the deletion to a tamper-evident
      audit log.
- [ ] **Tighten `asr_server.py`'s default bind from `0.0.0.0:8000` to
      `127.0.0.1:8000`.** Add a deliberate `BIND_ALL=1` opt-in for the
      "I want my desktop to ASR for my phone over LAN" use case.
      Verify LM Studio is also localhost-only at start (it is by
      default, but `doctor` should confirm).
- [ ] **Disable LM Studio analytics by default at bootstrap.** Write
      whatever the canonical opt-out file is
      (`~/.lmstudio/preferences.json`?) and report which version it was
      set on, so a future LM Studio upgrade that resets it gets caught
      by `doctor`. Best-effort since LM Studio is closed-source.
- [ ] **Disable Char analytics by default** (if Char ships any). Same
      pattern, written to `settings.json`.
- [ ] **Spotlight exclusion.** `bootstrap` could optionally run
      `mdutil -i off ~/Library/Application\ Support/hyprnote/` so
      Spotlight doesn't index recordings into a separately-readable
      database (`~/Library/Metadata/CoreSpotlight/...`).
- [ ] **iCloud Drive / Time Machine awareness.** `doctor` could detect
      if `~/Library/Application Support/hyprnote/` is being synced
      anywhere off-device and warn (or refuse to proceed without a
      `--i-know-what-im-doing` flag).
- [ ] **Sandbox the ASR server.** A `sandbox-exec` profile that
      restricts `asr_server.py` to its working dirs (no
      `~/Documents`, no `~/Library/Mail`, no Keychain). Cuts the
      blast radius if a model dependency ever gets a remote-code-
      execution vuln through specially-crafted audio.
- [ ] **Tamper-evident audit log.** Every transcription / summary /
      deletion appends a hash-chained line to
      `~/.local_scribe/audit.log` (each line includes a hash of the
      previous line). Lets you verify no entries have been retroactively
      tampered with.
- [ ] **Encrypt or refuse to save the Char OpenAI key backup.** Today
      `configure-char` saves a real OpenAI API key it found to
      plaintext at `~/.config/local_scribe/char-openai-key.<ts>.txt`
      (chmod 600). Better: encrypt it to a Keychain-backed item, or
      just push the user to revoke + rotate at platform.openai.com
      instead of saving a plaintext copy locally.
- [ ] **Refuse to start when screen-sharing / SSH is active.** Detect
      active screen-recording (Apple's TCC database) or SSH sessions
      (`who | grep pts`) and either refuse to launch or warn loudly.
      Nice-to-have, hard to do robustly.
- [ ] **Revoke real OpenAI keys you previously had in Char.** A
      `./run.sh revoke-saved-keys` that walks
      `~/.config/local_scribe/char-openai-key.*.txt`, prints each key
      masked, and offers to open `platform.openai.com/api-keys` so you
      can revoke them — then deletes the local backup.

## UX / features

- [ ] **Web inspector at `:8001`** — small FastAPI service that lists
      Char's sessions, plays audio in-browser, shows transcripts +
      summaries, supports per-session delete + audio download. Reads
      Char's directory directly (no parallel database). Loopback only.
      Earlier proposed; deferred to focus on the LM Studio bootstrap.
- [ ] **`./run.sh retranscribe SESSION_ID`** that re-runs ASR on an
      existing recording and overwrites `transcript.json`. Useful when
      you switch ASR backend, fix a diarization bug, or pull a better
      model. Inspector page would expose this as a button.
- [ ] **Calendar-aware participant prefill.** When a session has a
      linked calendar event, prefill `transcript.speaker_hints` with
      the attendees so the LLM speaker-naming pass starts from real
      names instead of `Speaker N`.
- [ ] **`transcribe_file.py --watch DIR`** — daemon mode that watches a
      folder and auto-transcribes any new audio dropped in.
- [ ] **Faster-whisper as a tested non-English path.** Currently
      second-class. Smoke-test on Mandarin / Japanese / German
      recordings and document accuracy/speed trade-offs.
- [ ] **Smaller LLM presets** (Qwen3-1.7B for 8 GB Macs?) so the
      "Minimum" hardware tier isn't ASR-only.

## Observability / dev

- [ ] **Structured JSON logs** from `asr_server.py` behind a
      `LOG_FORMAT=json` env var. Easier to grep, easier to ship to
      local-only observability.
- [ ] **Per-step latency in the streaming response.** Already logged
      server-side; expose in the SSE `transcript.text.done` payload so
      Char's UI could surface "ASR 86s, diar skipped, total 87s" as a
      subtitle on the transcript.
- [ ] **CI smoke test on `macos-latest`** (GitHub Actions): `./run.sh
      doctor`, run unit tests, exercise `transcribe_file.py` against a
      tiny bundled audio fixture. Won't cover Parakeet / Qwen / Char
      end-to-end, but covers FastAPI plumbing + import correctness.
- [ ] **Pin Python wheels with hashes** in `requirements.txt`.
      Currently uses unpinned versions which are reproducible-ish but
      not byte-reproducible.
- [ ] **`./run.sh logs --json`** — when the structured logging lands,
      add a tail flag that reformats existing JSON lines as a pretty
      colourised table.

## Documentation

- [ ] Mermaid sequence diagram of the data flow when Char calls our
      streaming endpoint (Char → POST `/v1/audio/transcriptions` →
      Parakeet → SSE heartbeats → `transcript.text.done` → Char
      writes `transcript.json` → LM Studio → note).
- [ ] "Why not Granola?" comparison table for prospective users.
- [ ] Short screencast of the bootstrap → first-recording loop.
- [ ] Document the actual disk layout under
      `~/Library/Application Support/hyprnote/` (sessions, app.db,
      humans, chats, search_index) so users know what to back up vs.
      what they can safely delete.
